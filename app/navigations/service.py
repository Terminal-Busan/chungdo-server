"""Analysis sessions, sequential workflow and persisted SSE events."""

import asyncio
import json
import logging
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from app import telemetry
from app.ai import AIUnavailable, CopilotService
from app.db import Database
from app.guidance.service import GuidanceService
from app.navigations.interpreter import (
    GLOSSARY,
    fallback_explanation,
    validate_explanation,
)
from app.policies.eligibility import check_eligibility, criterion_records
from app.policies.service import PoliciesService, PolicySearchSession
from app.safety import mask_data, sanitize_user_input
from app.schemas import (
    AnalysisResult,
    CreateNavigation,
    ExplanationOutput,
    GuidanceOutput,
    MatchOutput,
    Profile,
    Rejection,
)

logger = logging.getLogger(__name__)


class NavigationsService:
    def __init__(self, db: Database, ai=None):
        self.db = db
        self.policies = PoliciesService(db)
        self.ai = ai or CopilotService()
        self.tasks: dict[str, asyncio.Task] = {}
        self.stage_timeout = 45

    async def create(self, body: CreateNavigation):
        if not body.consent:
            raise HTTPException(400, "개인정보 임시 저장 동의가 필요합니다")
        await self.policies.get(body.applied_policy_id)
        navigation_id = str(uuid4())
        await self.db.query(
            """INSERT INTO navigations
            (id, applied_policy_id, rejection_text, profile) VALUES (%s, %s, %s, %s)""",
            (
                navigation_id,
                body.applied_policy_id,
                sanitize_user_input(body.rejection_text),
                mask_data(body.profile.model_dump()),
            ),
        )
        return {"id": navigation_id, "status": "pending"}

    async def get(self, navigation_id: str):
        rows = await self.db.query(
            "SELECT * FROM navigations WHERE id=%s", (navigation_id,)
        )
        if not rows:
            raise HTTPException(404, "분석 결과를 찾을 수 없습니다")
        row = rows[0]
        if row["expires_at"] <= datetime.now(UTC):
            await self.cancel(navigation_id)
            await self.db.purge_expired()
            raise HTTPException(410, "분석 결과가 만료되었습니다")
        return row

    async def delete(self, navigation_id):
        rows = await self.db.query(
            "SELECT id FROM navigations WHERE id=%s", (navigation_id,)
        )
        if not rows:
            raise HTTPException(404, "분석 결과를 찾을 수 없습니다")
        await self.cancel(navigation_id)
        await self.db.query("DELETE FROM navigations WHERE id=%s", (navigation_id,))

    async def cancel(self, navigation_id):
        task = self.tasks.get(navigation_id)
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self):
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    async def start(self, navigation_id):
        row = await self.get(navigation_id)
        if row["status"] != "pending":
            return
        claimed = await self.db.query(
            """UPDATE navigations SET status='running', started_at=now()
            WHERE id=%s AND status='pending' AND expires_at>now() RETURNING id""",
            (navigation_id,),
        )
        if claimed:
            task = asyncio.create_task(
                self.run(navigation_id), name=f"analysis-{navigation_id}"
            )
            self.tasks[navigation_id] = task
            task.add_done_callback(lambda _: self.tasks.pop(navigation_id, None))

    async def emit(self, navigation_id, event, data):
        await self.db.query(
            """INSERT INTO agent_steps (navigation_id, agent_name, step_type, content)
            SELECT id, %s, %s, %s FROM navigations
            WHERE id=%s AND status='running' AND expires_at>now()""",
            (
                data.get("agent", "Workflow"),
                {
                    "step": "started" if data.get("status") == "started" else "output",
                    "tool": "tool_call",
                    "error": "error",
                }.get(event, "output"),
                {"event": event, "data": data},
                navigation_id,
            ),
        )

    async def events(self, navigation_id, after=0):
        return await self.db.query(
            "SELECT id, content FROM agent_steps WHERE navigation_id=%s AND id>%s ORDER BY id",
            (navigation_id, after),
        )

    async def tool_event(self, navigation_id, agent, name, args):
        await self.emit(
            navigation_id, "tool", {"agent": agent, "tool": name, "args": args}
        )

    def tools(self, navigation_id, agent, policies, profile, search=None):
        from copilot.tools import Tool, ToolResult

        async def handler(invocation):
            args = invocation.arguments or {}
            try:
                if invocation.tool_name not in names:
                    raise ValueError("허용된 도구가 아닙니다")
                if (
                    "profile" in args
                    and Profile.model_validate(args["profile"]) != profile
                ):
                    raise ValueError("분석 중인 프로필은 변경할 수 없습니다")
                if invocation.tool_name == "search_policies" and search is not None:
                    data = await search.search(**args)
                elif invocation.tool_name == "check_eligibility" and search is not None:
                    data = await search.check(args.get("policy_id"))
                else:
                    policy_id = args.get("policy_id")
                    if policy_id not in policies:
                        raise ValueError("허용된 정책이 아닙니다")
                    await self.tool_event(
                        navigation_id,
                        agent,
                        invocation.tool_name,
                        {"policy_id": policy_id},
                    )
                    data = (
                        self.policies.detail(policies[policy_id])
                        if invocation.tool_name == "get_policy_detail"
                        else criterion_records(
                            check_eligibility(profile, policies[policy_id])
                        )
                    )
                return ToolResult(
                    text_result_for_llm=json.dumps(
                        mask_data(data), ensure_ascii=False, default=str
                    )
                )
            except (ValueError, TypeError, HTTPException):
                await self.tool_event(
                    navigation_id,
                    agent,
                    invocation.tool_name
                    if invocation.tool_name in names
                    else "unavailable_tool",
                    {"rejected": True},
                )
                return ToolResult(
                    result_type="failure",
                    text_result_for_llm="허용된 검색 범위·후보·프로필만 사용하세요",
                )

        names = {
            "RejectionInterpreter": ["get_policy_detail", "check_eligibility"],
            "PolicyMatcher": ["search_policies", "check_eligibility"],
            "PathPlanner": ["get_policy_detail"],
        }[agent]
        tools = []
        for name in names:
            if name == "search_policies":
                properties = {
                    "keyword": {"type": "string", "maxLength": 50},
                    "field": {"type": "string", "maxLength": 50},
                    "region": {"type": "string", "maxLength": 100},
                    "exclude_ids": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 64},
                        "maxItems": 20,
                    },
                }
                required = []
            else:
                properties = {"policy_id": {"type": "string", "maxLength": 64}}
                if search is None:
                    properties["policy_id"]["enum"] = list(policies)
                if name == "check_eligibility":
                    properties["profile"] = Profile.model_json_schema()
                required = ["policy_id"]
            tools.append(
                Tool(
                    name=name,
                    description="정부24 정책 캐시와 현재 분석의 규칙 판정을 읽는 전용 도구",
                    handler=handler,
                    parameters={
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                    skip_permission=True,
                )
            )
        return tools

    async def stage(self, navigation_id, agent, operation, fallback):
        with telemetry.agent_stage(agent) as observation:
            await self.emit(navigation_id, "step", {"agent": agent, "status": "started"})
            try:
                async with asyncio.timeout(self.stage_timeout):
                    output = await operation()
                if observation["outcome"] == "error":
                    observation["outcome"] = "success"
            except (TimeoutError, AIUnavailable, HTTPException) as error:
                await self.emit(
                    navigation_id,
                    "error",
                    {
                        "agent": agent,
                        "message": "timeout"
                        if isinstance(error, TimeoutError)
                        else "단계 분석을 완료하지 못해 규칙 결과로 안내합니다",
                        "fallback": True,
                    },
                )
                output = fallback()
                observation["outcome"] = "timeout" if isinstance(error, TimeoutError) else "fallback"
            if agent == "RejectionInterpreter":
                summary = output[0].plain_summary
                for start in range(0, len(summary), 30):
                    await self.emit(
                        navigation_id,
                        "token",
                        {"agent": agent, "delta": summary[start : start + 30]},
                    )
            await self.emit(navigation_id, "step", {"agent": agent, "status": "completed"})
            return output

    async def run(self, navigation_id):
        try:
            row = await self.get(navigation_id)
            profile = Profile.model_validate(row["profile"])
            notice = sanitize_user_input(row["rejection_text"])
            state = {
                "applied": {"id": row["applied_policy_id"], "conditions": {}},
                "criteria": [],
            }

            def fallback_a1():
                return fallback_explanation(
                    state["applied"], notice, state["criteria"]
                ), False

            async def interpret():
                agent = "RejectionInterpreter"
                await self.tool_event(
                    navigation_id,
                    agent,
                    "get_policy_detail",
                    {"policy_id": row["applied_policy_id"]},
                )
                applied = await self.policies.get(row["applied_policy_id"])
                state["applied"] = applied
                await self.tool_event(
                    navigation_id,
                    agent,
                    "check_eligibility",
                    {"policy_id": applied["id"]},
                )
                state["criteria"] = check_eligibility(profile, applied)
                context = {
                    "applied_policy_id": applied["id"],
                    "profile": profile.model_dump(),
                    "policy": self.policies.detail(applied),
                    "criteria": criterion_records(state["criteria"]),
                    "rejection_text": notice,
                    "glossary_reference": GLOSSARY,
                }
                answer = await self.ai.generate(
                    agent,
                    context,
                    ExplanationOutput,
                    self.tools(navigation_id, agent, {applied["id"]: applied}, profile),
                )
                return validate_explanation(
                    answer, applied, notice, state["criteria"], context
                ), True

            a1, generated = await self.stage(
                navigation_id, "RejectionInterpreter", interpret, fallback_a1
            )
            applied = state["applied"]

            async def log_search(name, args):
                await self.tool_event(navigation_id, "PolicyMatcher", name, args)

            search = PolicySearchSession(
                self.policies, profile, applied, a1, log_search
            )

            async def match():
                answer = None
                try:
                    answer = await self.ai.generate(
                        "PolicyMatcher",
                        {
                            "profile": profile.model_dump(),
                            "applied_policy_id": applied["id"],
                            "rejection": a1.model_dump(),
                            "search_priorities": search.plan,
                        },
                        MatchOutput,
                        self.tools(navigation_id, "PolicyMatcher", {}, profile, search),
                    )
                except AIUnavailable:
                    telemetry.note_fallback()
                    await self.emit(
                        navigation_id,
                        "error",
                        {
                            "agent": "PolicyMatcher",
                            "message": "규칙 판정 기반 후보를 표시합니다",
                            "fallback": True,
                        },
                    )
                await search.fill()
                return search.select(answer)

            alternatives = await self.stage(
                navigation_id, "PolicyMatcher", match, search.select
            )
            selected_policies = [search.policies[a.policy_id] for a in alternatives]
            policy_map = {
                applied["id"]: applied,
                **{p["id"]: p for p in selected_policies},
            }

            def fallback_plan():
                return GuidanceService().build(
                    applied,
                    selected_policies,
                    criteria=search.criteria,
                    rejection=a1,
                    notice=notice,
                )

            async def plan():
                for policy_id in policy_map:
                    await self.tool_event(
                        navigation_id,
                        "PathPlanner",
                        "get_policy_detail",
                        {"policy_id": policy_id},
                    )
                answer = await self.ai.generate(
                    "PathPlanner",
                    {
                        "profile": profile.model_dump(),
                        "rejection": a1.model_dump(),
                        "alternatives": [a.model_dump() for a in alternatives],
                        "policies": [
                            self.policies.detail(p) for p in policy_map.values()
                        ],
                    },
                    GuidanceOutput,
                    self.tools(navigation_id, "PathPlanner", policy_map, profile),
                )
                return GuidanceService().build(
                    applied,
                    selected_policies,
                    answer,
                    criteria=search.criteria,
                    rejection=a1,
                    notice=notice,
                )

            actions, pathway = await self.stage(
                navigation_id, "PathPlanner", plan, fallback_plan
            )
            disclaimer = "최종 자격 판단은 담당 기관에 있습니다."
            if applied.get("is_demo"):
                disclaimer += " 데모 데이터이며 실제 정책 또는 신청 안내가 아닙니다."
            result = AnalysisResult(
                rejection=Rejection(
                    **a1.model_dump(exclude={"possible_reasons"}),
                    ai_generated=generated,
                ),
                alternatives=alternatives,
                next_actions=actions,
                pathway=pathway,
                disclaimer=disclaimer,
            )
            await self.db.query(
                """UPDATE navigations SET status='done', result=%s
                WHERE id=%s AND status='running' AND expires_at>now()""",
                (result.model_dump(), navigation_id),
            )
        except asyncio.CancelledError:
            await self.db.query(
                "UPDATE navigations SET status='pending' WHERE id=%s AND status='running' AND expires_at>now()",
                (navigation_id,),
            )
            raise
        except Exception:  # noqa: BLE001 -- A background task must persist its failed state.
            logger.error("Analysis workflow failed")
            try:
                await self.emit(
                    navigation_id,
                    "error",
                    {
                        "agent": "Workflow",
                        "message": "분석 중 오류가 발생했습니다",
                        "fallback": False,
                    },
                )
                await self.db.query(
                    "UPDATE navigations SET status='failed', result=NULL WHERE id=%s AND status='running'",
                    (navigation_id,),
                )
            except HTTPException:
                logger.error("Could not persist analysis failure")
