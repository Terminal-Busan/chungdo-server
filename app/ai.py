"""The only Copilot SDK boundary. No shell, filesystem or external-write tools."""

import asyncio
import json
import os
import re
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel, ValidationError

from app.safety import mask_data, mask_pii, sanitize_user_input

INSTRUCTIONS = Path(__file__).parent / "agents/instructions"
AGENTS = {"RejectionInterpreter", "PolicyMatcher", "PathPlanner", "Followup"}
JSON_RETRY = (INSTRUCTIONS / "json_retry.txt").read_text().strip()


def instructions_for(agent: str) -> str:
    if agent not in AGENTS:
        raise ValueError("Unknown agent")
    return (
        (INSTRUCTIONS / "common.txt").read_text()
        + "\n"
        + (INSTRUCTIONS / f"{agent}.txt").read_text()
    )


def prompt_for(context: dict) -> str:
    data = mask_data(
        {
            key: value
            for key, value in context.items()
            if key not in {"rejection_text", "question"}
        }
    )
    prompt = "[분석 대상 및 앞 단계 결과]\n" + json.dumps(
        data, ensure_ascii=False, default=str
    )
    for key in ("rejection_text", "question"):
        if key in context:
            text = escape(sanitize_user_input(context[key]) or "(없음)")
            prompt += f"\n[사용자 입력: {key}]\n<user_input>\n{text}\n</user_input>"
    return prompt


class AIUnavailable(Exception):
    pass


class CopilotService:
    async def generate(
        self, agent: str, context: dict, schema: type[BaseModel], tools=None
    ):
        token = os.getenv("COPILOT_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
        model = os.getenv("COPILOT_MODEL")
        if not token or not model:
            raise AIUnavailable("AI 연결 설정이 필요합니다")
        from copilot import CopilotClient
        from copilot.rpc import PermissionDecisionReject

        def deny_permission(*_):
            return PermissionDecisionReject(
                feedback="읽기 전용으로 등록된 정책 도구만 사용할 수 있습니다"
            )

        instructions = (
            instructions_for(agent)
            + "\nJSON schema:\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        prompt = prompt_for(context)
        try:
            async with asyncio.timeout(45):
                with TemporaryDirectory(prefix="welfare-copilot-") as directory:
                    client = CopilotClient(
                        github_token=token,
                        use_logged_in_user=False,
                        mode="empty",
                        base_directory=directory,
                        working_directory=directory,
                        env={
                            key: os.environ[key]
                            for key in ("PATH", "SSL_CERT_FILE")
                            if key in os.environ
                        },
                    )
                    async with client:
                        session = await client.create_session(
                            model=model,
                            tools=tools or [],
                            available_tools=[tool.name for tool in tools or []],
                            on_permission_request=deny_permission,
                            system_message={"mode": "append", "content": instructions},
                            enable_session_store=False,
                            enable_config_discovery=False,
                            enable_session_telemetry=False,
                            enable_skills=False,
                            enable_file_hooks=False,
                            enable_host_git_operations=False,
                            memory={"enabled": False},
                            infinite_sessions={"enabled": False},
                            mcp_servers={},
                            streaming=True,
                        )
                        try:
                            for attempt in range(2):
                                response = await session.send_and_wait(
                                    prompt if attempt == 0 else JSON_RETRY,
                                    response_schema=schema,
                                    timeout=40,
                                )
                                try:
                                    content = response.data.content if response else ""
                                    return schema.model_validate_json(content)
                                except (ValidationError, AttributeError, TypeError):
                                    if attempt:
                                        raise AIUnavailable(
                                            "AI 응답 형식 오류"
                                        ) from None
                        finally:
                            await session.disconnect()
                            await client.delete_session(session.session_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- Normalize third-party failures without exposing credentials.
            # SDK errors may contain prompts, tokens, or local paths.
            raise AIUnavailable("AI 분석을 완료하지 못했습니다") from None


def grounded_summary(summary: str, context: dict, max_sentences: int = 3) -> str:
    """Reject newly invented numbers and obvious guarantees before streaming."""

    def factual_data(value):
        if isinstance(value, dict):
            return {
                key: factual_data(item)
                for key, item in value.items()
                if key not in {"rejection_text", "question"}
            }
        if isinstance(value, list):
            return [factual_data(item) for item in value]
        return value

    known_numbers = set(
        re.findall(
            r"\d+(?:\.\d+)?",
            json.dumps(factual_data(context), ensure_ascii=False, default=str),
        )
    )
    if any(
        number not in known_numbers for number in re.findall(r"\d+(?:\.\d+)?", summary)
    ):
        raise AIUnavailable("근거 없는 수치")
    if any(
        word in summary
        for word in (
            "무조건",
            "지급 확정",
            "반드시 선정",
            "수급 확정",
            "받을 수 있습니다",
            "선정됩니다",
            "사용자 탓",
            "당신 잘못",
        )
    ):
        raise AIUnavailable("확정적 안내")
    if (
        len([s for s in re.split(r"(?<!\d)\.|\.(?!\d)|[!?。]", summary) if s.strip()])
        > max_sentences
    ):
        raise AIUnavailable("설명 문장 수 초과")
    return mask_pii(summary)
