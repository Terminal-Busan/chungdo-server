"""Real PostgreSQL integration tests; isolated schema, never the team's tables."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from app.ai import AIUnavailable
from app.db import Database
from app.policies.service import PoliciesService
from app.schemas import (
    ExplanationOutput,
    GuidanceOutput,
    MatchOutput,
    Profile,
    SummaryOutput,
)
from main import app
from tests.test_eligibility import PROFILE


class OfflineAI:
    async def generate(self, *args, **kwargs):
        raise AIUnavailable("offline")


class WorkingAI:
    async def generate(self, agent, context, schema, tools=None):
        if schema is ExplanationOutput:
            return schema(
                plain_summary="입력 조건과 지원 기준을 비교했습니다. 세부 기준은 신청처에 확인하세요.",
                failed_criteria=[],
                possible_reasons=[],
                glossary=[
                    {"term": "지원대상", "meaning": "대상"},
                    {"term": "선정기준", "meaning": "기준"},
                ],
            )
        if schema is MatchOutput:
            from copilot.tools import ToolInvocation

            search_tool = next(tool for tool in tools if tool.name == "search_policies")
            for _ in range(2):
                found = await search_tool.handler(
                    ToolInvocation(
                        tool_name="search_policies",
                        arguments={"exclude_ids": [context["applied_policy_id"]]},
                    )
                )
            candidates = json.loads(found.text_result_for_llm)
            return schema(
                alternatives=[
                    {"policy_id": "invented-id", "reason": "근거 없는 후보입니다."},
                    *[
                        {
                            "policy_id": a["id"],
                            "reason": "기존 탈락 조건과 비교해 검토할 후보입니다.",
                        }
                        for a in candidates
                    ],
                ]
            )
        if schema is SummaryOutput:
            return schema(plain_summary="공식 안내와 신청처에서 확인하세요.")
        applied = context["policies"][0]["id"]
        return GuidanceOutput(
            next_actions=[
                {
                    "type": "apply_new",
                    "title": "허위",
                    "description": "허위",
                    "documents": ["허위 서류"],
                    "policy_id": "invented-id",
                },
                {
                    "type": "documents",
                    "title": "상담",
                    "description": "상담",
                    "documents": [],
                    "policy_id": None,
                },
                {
                    "type": "reapply",
                    "title": "재신청",
                    "description": "재신청",
                    "documents": [],
                    "policy_id": applied,
                },
            ],
            pathway=[{"order": 1, "policy_id": "invented-id", "why": "허위 순서"}],
        )


@pytest.fixture
def api(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL is required for real PostgreSQL integration tests"
        )
    schema = "test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    isolated = make_conninfo(url, options=f"-c search_path={schema}")
    monkeypatch.setenv("DATABASE_URL", isolated)
    monkeypatch.setenv("WELFARE_DEMO_MODE", "true")

    async def setup():
        db = Database(isolated)
        assert (await db.query("SELECT current_schema() AS name"))[0]["name"] == schema
        await db.initialize()
        service = PoliciesService(db)
        for row in json.loads(
            (Path(__file__).resolve().parents[1] / "seed/demo.json").read_text()
        ):
            await service.upsert(row)

    asyncio.run(setup())
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.navigations.ai = OfflineAI()
            yield client
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def create(api, **extra):
    return api.post(
        "/api/navigations",
        json={
            "applied_policy_id": "demo-rejected-housing",
            "rejection_text": "소득 초과 990101-1234567 / 010-1234-5678",
            "profile": PROFILE,
            "consent": True,
            **extra,
        },
    )


def execute(api, navigation_id):
    response = api.get(f"/api/navigations/{navigation_id}/stream")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    records = []
    for block in response.text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            records.append((lines["event"], json.loads(lines["data"])))
    return response, records


def test_health_and_search_contract(api):
    assert api.get("/api/health").json() == {"data": {"status": "ok"}, "message": "ok"}
    data = api.get("/api/policies?q=주거&region=경상북도").json()["data"]
    assert len(data) == 2
    assert set(data[0]) == {"id", "name", "field", "agency", "region"}
    assert api.get("/api/policies?q=존재하지않음").json() == {
        "data": [],
        "message": "검색 결과가 없습니다",
    }
    assert api.get("/api/policies?q=%").json()["data"] == []
    assert api.get("/api/policies?limit=51").status_code == 400
    assert api.get("/api/policies?q=").status_code == 400


def test_policy_detail_contract(api):
    detail = api.get("/api/policies/demo-rejected-housing").json()["data"]
    assert isinstance(detail["conditions"], dict)
    assert detail["documents"] is None
    assert api.get("/api/policies/not-found").status_code == 404


@pytest.mark.parametrize(
    "updates",
    [
        {"consent": False},
        {"consent": "true"},
        {"profile": {**PROFILE, "age": 121}},
        {"profile": {**PROFILE, "age": True}},
        {"profile": {**PROFILE, "income_monthly_manwon": -1}},
        {"profile": {**PROFILE, "housing_type": "invalid"}},
        {"rejection_text": "x" * 2001},
    ],
)
def test_invalid_input(api, updates):
    response = create(api, **updates)
    assert response.status_code == 400
    assert set(response.json()) == {"statusCode", "message"}
    assert "990101" not in response.text


def test_missing_policy_and_invalid_id(api):
    assert create(api, applied_policy_id="missing").status_code == 404
    assert api.get("/api/navigations/not-a-uuid").status_code == 400
    assert api.get(f"/api/navigations/{uuid4()}").status_code == 404


def test_full_workflow_fallback_stream_and_deletion(api):
    response = create(api)
    assert response.status_code == 201
    navigation_id = response.json()["data"]["id"]
    pending = api.get(f"/api/navigations/{navigation_id}").json()["data"]
    assert pending["status"] == "pending" and pending["result"] is None
    assert (
        api.post(
            f"/api/navigations/{navigation_id}/questions", json={"question": "방법?"}
        ).status_code
        == 409
    )
    stream, records = execute(api, navigation_id)
    result = next(data for event, data in records if event == "result")
    assert [
        data["agent"]
        for event, data in records
        if event == "step" and data["status"] == "started"
    ] == ["RejectionInterpreter", "PolicyMatcher", "PathPlanner"]
    assert any(event == "tool" for event, _ in records)
    assert any(event == "error" and data["fallback"] for event, data in records)
    assert result["rejection"]["ai_generated"] is False
    assert result["alternatives"]
    assert all(a["match"] != "unlikely" for a in result["alternatives"])
    assert "데모" in result["disclaimer"]
    assert "990101" not in stream.text and "010-1234" not in stream.text
    fetched = api.get(f"/api/navigations/{navigation_id}").json()["data"]
    assert fetched["status"] == "done" and fetched["result"] == result
    _, replay = execute(api, navigation_id)
    assert replay == [("result", result)]
    row = api.portal.call(
        app.state.db.query,
        "SELECT rejection_text FROM navigations WHERE id=%s",
        (navigation_id,),
    )[0]
    assert "990101" not in row["rejection_text"]
    assert api.delete(f"/api/navigations/{navigation_id}").json()["data"] == {
        "deleted": True
    }
    assert api.get(f"/api/navigations/{navigation_id}").status_code == 404
    assert (
        api.portal.call(
            app.state.db.query,
            "SELECT * FROM agent_steps WHERE navigation_id=%s",
            (navigation_id,),
        )
        == []
    )


def test_ai_success_filters_invented_policy_ids(api):
    app.state.navigations.ai = WorkingAI()
    navigation_id = create(api).json()["data"]["id"]
    _, records = execute(api, navigation_id)
    result = next(data for event, data in records if event == "result")
    assert result["rejection"]["ai_generated"] is True
    assert set(result["rejection"]) == {
        "failed_criteria",
        "plain_summary",
        "glossary",
        "ai_generated",
    }
    assert result["rejection"]["failed_criteria"][0]["criterion"] == "소득"
    assert 2 <= len(result["rejection"]["glossary"]) <= 5
    assert 3 <= len(result["next_actions"]) <= 6
    assert all(isinstance(a["policy_id"], str) for a in result["next_actions"])
    assert "invented-id" not in json.dumps(result)
    assert result["pathway"]
    answer = api.post(
        f"/api/navigations/{navigation_id}/questions", json={"question": "どう?"}
    )
    assert answer.status_code == 200 and answer.json()["data"]["ai_generated"] is True


def test_all_agent_contexts_and_search_events_follow_the_document(api):
    contexts = {}

    class RecordingAI(WorkingAI):
        async def generate(self, agent, context, schema, tools=None):
            contexts[agent] = context
            return await super().generate(agent, context, schema, tools)

    app.state.navigations.ai = RecordingAI()
    navigation_id = create(api).json()["data"]["id"]
    _, records = execute(api, navigation_id)
    assert set(contexts) == {"RejectionInterpreter", "PolicyMatcher", "PathPlanner"}
    assert all(
        context["profile"]["household_size"] == 1 for context in contexts.values()
    )
    assert "possible_reasons" in contexts["PolicyMatcher"]["rejection"]
    assert "possible_reasons" in contexts["PathPlanner"]["rejection"]
    assert all(
        "criteria" in a and "match" in a
        for a in contexts["PathPlanner"]["alternatives"]
    )
    calls = [
        data
        for event, data in records
        if event == "tool" and data["tool"] == "search_policies"
    ]
    assert len(calls) == 3
    assert all("demo-rejected-housing" in call["args"]["exclude_ids"] for call in calls)


def test_expiration_purges_private_data_and_logs(api):
    navigation_id = create(api).json()["data"]["id"]
    execute(api, navigation_id)
    api.portal.call(
        app.state.db.query,
        "UPDATE navigations SET expires_at=now()-INTERVAL '1 second' WHERE id=%s",
        (navigation_id,),
    )
    assert api.get(f"/api/navigations/{navigation_id}").status_code == 410
    assert api.get(f"/api/navigations/{navigation_id}/stream").status_code == 410
    row = api.portal.call(
        app.state.db.query, "SELECT * FROM navigations WHERE id=%s", (navigation_id,)
    )[0]
    assert (
        row["profile"] == {} and row["result"] is None and row["rejection_text"] is None
    )
    assert (
        api.portal.call(
            app.state.db.query,
            "SELECT * FROM agent_steps WHERE navigation_id=%s",
            (navigation_id,),
        )
        == []
    )


def test_production_does_not_return_demo_data(api, monkeypatch):
    monkeypatch.setenv("WELFARE_DEMO_MODE", "false")
    assert api.get("/api/policies").json()["data"] == []


def test_no_recommendations_still_has_next_action(api):
    api.portal.call(
        app.state.db.query,
        "DELETE FROM policies WHERE id<>%s",
        ("demo-rejected-housing",),
    )
    navigation_id = create(api).json()["data"]["id"]
    _, records = execute(api, navigation_id)
    result = next(data for event, data in records if event == "result")
    assert result["alternatives"] == []
    assert result["next_actions"] and result["pathway"] == []


def test_cors_and_openapi_contract(api):
    allowed = api.options(
        "/api/navigations",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    blocked = api.get("/api/health", headers={"Origin": "https://untrusted.invalid"})
    assert "access-control-allow-origin" not in blocked.headers
    assert api.get("/docs").status_code == 200
    document = api.get("/openapi.json").json()
    assert "422" not in document["paths"]["/api/navigations"]["post"]["responses"]
    assert document["paths"]["/api/navigations"]["post"]["responses"]["201"]["content"][
        "application/json"
    ]["schema"]
    assert "Profile" in document["components"]["schemas"]


def test_duplicate_start_claims_only_one_workflow(api):
    navigation_id = create(api).json()["data"]["id"]
    calls = []

    async def controlled_run(current_id):
        calls.append(current_id)

    app.state.navigations.run = controlled_run

    async def starts():
        await asyncio.gather(
            *(app.state.navigations.start(navigation_id) for _ in range(5))
        )
        await asyncio.sleep(0)

    api.portal.call(starts)
    assert calls == [navigation_id]


def test_delete_cancels_running_analysis(api):
    navigation_id = create(api).json()["data"]["id"]

    class SlowAI:
        async def generate(self, *args):
            await asyncio.sleep(30)

    app.state.navigations.ai = SlowAI()
    api.portal.call(app.state.navigations.start, navigation_id)
    response = api.delete(f"/api/navigations/{navigation_id}")
    assert response.status_code == 200
    assert api.get(f"/api/navigations/{navigation_id}").status_code == 404
    assert (
        api.portal.call(
            app.state.db.query,
            "SELECT * FROM agent_steps WHERE navigation_id=%s",
            (navigation_id,),
        )
        == []
    )


def test_database_failure_does_not_expose_connection_details(api, monkeypatch):
    from unittest.mock import AsyncMock

    with monkeypatch.context() as patch:
        patch.setattr(
            psycopg.AsyncConnection,
            "connect",
            AsyncMock(
                side_effect=psycopg.OperationalError(
                    "postgresql://private:secret@host/db"
                )
            ),
        )
        response = api.get("/api/policies")
    assert response.status_code == 500
    assert response.json() == {"statusCode": 500, "message": "일시적인 오류입니다"}
    assert "secret" not in response.text and "private" not in response.text


def test_agent_tools_are_scoped_to_known_policy_ids(api):
    from copilot.tools import ToolInvocation

    navigation_id = create(api).json()["data"]["id"]
    policy = api.portal.call(
        app.state.navigations.policies.get, "demo-rejected-housing"
    )
    tools = app.state.navigations.tools(
        navigation_id,
        "RejectionInterpreter",
        {policy["id"]: policy},
        Profile(**PROFILE),
    )
    assert {tool.name for tool in tools} == {"get_policy_detail", "check_eligibility"}
    tool = next(tool for tool in tools if tool.name == "get_policy_detail")
    result = api.portal.call(
        tool.handler,
        ToolInvocation(tool_name=tool.name, arguments={"policy_id": policy["id"]}),
    )
    assert json.loads(result.text_result_for_llm)["id"] == policy["id"]
    blocked = api.portal.call(
        tool.handler,
        ToolInvocation(tool_name=tool.name, arguments={"policy_id": "unknown-policy"}),
    )
    assert blocked.result_type == "failure"
