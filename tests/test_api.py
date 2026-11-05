import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from welfare.agents import AIUnavailable, CopilotNarrator
from welfare.api import create_app
from welfare.eligibility import check_eligibility, mask_personal_information, match_level
from welfare.store import RunCancelled, navigations, policies, steps

PROFILE = {
    "age": 27, "sido": "경상북도", "sigungu": "경산시", "household_type": "single",
    "household_size": 1, "income_monthly_manwon": 230, "housing_type": "monthly_rent",
    "employment_status": "employed", "has_disability": False,
}
SOURCE = [
    {"서비스ID": "applied", "서비스명": "청년 주거지원", "서비스분야": "주거·자립", "소관기관명": "국토교통부", "소관기관유형": "중앙행정기관", "상세조회URL": "https://www.gov.kr/applied"},
    {"서비스ID": "local", "서비스명": "경산 청년 월세", "서비스분야": "주거·자립", "소관기관명": "경상북도 경산시", "소관기관유형": "지방자치단체", "상세조회URL": "https://www.gov.kr/local"},
    {"서비스ID": "other", "서비스명": "서울 청년 월세", "서비스분야": "주거·자립", "소관기관명": "서울특별시", "소관기관유형": "지방자치단체"},
]


class FakeAI:
    async def explain(self, agent, context, on_delta=None):
        return "공개 조건을 확인하고 담당 기관에 상세 선정기준을 문의하세요."


def gov_response(request):
    endpoint = request.url.path.rsplit("/", 1)[-1]
    identifier = request.url.params.get("cond[서비스ID::EQ]")
    if endpoint == "serviceList":
        rows = SOURCE
    elif endpoint == "serviceDetail":
        rows = [{**row, "서비스목적": "청년 주거 지원", "지원대상": "청년", "선정기준": "소득인정액 확인 필요", "구비서류": "주민등록등본", "신청방법": "방문 신청", "수정일시": "2026-11-05"} for row in SOURCE if row["서비스ID"] == identifier]
    else:
        rows = [{"서비스ID": identifier, "JA0110": 19, "JA0111": 34, "JA0404": "Y"}] if identifier in {row["서비스ID"] for row in SOURCE} else []
    return httpx.Response(200, json={"data": rows, "totalCount": len(rows), "matchCount": len(rows)})


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("GOV24_SERVICE_KEY", "test-key")
    monkeypatch.setenv("FRONT_URL", "https://front.example/")
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(gov_response))
    application = create_app(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", gov_client=upstream, narrator=FakeAI())
    with TestClient(application) as client:
        yield client


def create_navigation(client, **changes):
    assert client.get("/api/policies").status_code == 200
    body = {"applied_policy_id": "applied", "profile": dict(PROFILE), "consent": True, **changes}
    response = client.post("/api/navigations", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def read_events(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    result = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            result.append((lines["event"], json.loads(lines["data"])))
    return result


def test_health_validation_docs_and_cors(api):
    assert api.get("/api/health").json() == {"data": {"status": "ok"}, "message": "ok"}
    for query in ("q=", "q=%20", "q=" + "a" * 51, "limit=0", "limit=51", "limit=x"):
        response = api.get("/api/policies?" + query)
        assert response.status_code == 400
        assert set(response.json()) == {"statusCode", "message"}
    schema = api.get("/openapi.json").json()
    assert "/api/navigations/{id}/stream" in schema["paths"]
    assert "422" not in schema["paths"]["/api/navigations"]["post"]["responses"]
    for origin in ("http://localhost:3000", "https://front.example"):
        response = api.options("/api/navigations", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
        assert response.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-origin" not in api.get("/api/health", headers={"Origin": "https://untrusted.example"}).headers


def test_policy_search_detail_and_no_results(api):
    found = api.get("/api/policies", params={"region": "경상북도 경산시", "field": "주거·자립", "q": "청년"}).json()
    assert {item["id"] for item in found["data"]} == {"applied", "local"}
    assert len(api.get("/api/policies?limit=1").json()["data"]) == 1
    assert api.get("/api/policies?q=없는정책").json() == {"data": [], "message": "검색 결과가 없습니다"}
    detail = api.get("/api/policies/applied").json()["data"]
    assert detail["conditions"] == {"JA0110": 19, "JA0111": 34, "JA0404": "Y"}
    assert detail["detail_url"] == "https://www.gov.kr/applied"
    assert api.get("/api/policies/missing").status_code == 404


@pytest.mark.parametrize("changes", [
    {"consent": False}, {"consent": 1}, {"profile": {**PROFILE, "age": 121}},
    {"profile": {**PROFILE, "age": "27"}}, {"profile": {**PROFILE, "household_size": 0}},
    {"profile": {**PROFILE, "income_monthly_manwon": -1}},
    {"profile": {**PROFILE, "has_disability": "false"}},
    {"profile": {**PROFILE, "sido": " "}}, {"rejection_text": "가" * 2001},
])
def test_navigation_validation(api, changes):
    body = {"applied_policy_id": "applied", "profile": PROFILE, "consent": True, **changes}
    response = api.post("/api/navigations", json=body)
    assert response.status_code == 400
    assert response.json()["statusCode"] == 400


def test_unknown_policy_and_missing_sessions(api):
    assert api.post("/api/navigations", json={"applied_policy_id": "missing", "profile": PROFILE, "consent": True}).status_code == 404
    for path in ("/api/navigations/missing", "/api/navigations/missing/stream"):
        assert api.get(path).status_code == 404
    assert api.delete("/api/navigations/missing").status_code == 404


def test_full_analysis_replay_follow_up_and_delete(api):
    identifier = create_navigation(api, rejection_text="주민번호 900101-1234567 전화 010-1234-5678")
    before = api.get(f"/api/navigations/{identifier}").json()["data"]
    assert before["status"] == "pending" and before["result"] is None
    assert api.post(f"/api/navigations/{identifier}/questions", json={"question": "어디에 문의하나요?"}).status_code == 409
    events = read_events(api.get(f"/api/navigations/{identifier}/stream"))
    starts = [data["agent"] for name, data in events if name == "step" and data["status"] == "started"]
    assert starts == ["RejectionInterpreter", "PolicyMatcher", "PathPlanner"]
    assert any(name == "tool" for name, _ in events)
    final = api.get(f"/api/navigations/{identifier}")
    assert final.headers["cache-control"] == "no-store"
    result = final.json()["data"]["result"]
    assert result == [data for name, data in events if name == "result"][0]
    assert final.json()["data"]["status"] == "done"
    assert result["rejection"]["ai_generated"] is True
    assert result["alternatives"][0]["policy_id"] == "local"
    assert result["alternatives"][0]["match"] == "likely"
    assert any(check["status"] == "unknown" for check in result["alternatives"][0]["criteria"])
    replay = read_events(api.get(f"/api/navigations/{identifier}/stream"))
    assert replay == [("result", result)]
    assert api.post(f"/api/navigations/{identifier}/questions", json={"question": "문의처는?"}).json()["data"]["ai_generated"] is True
    for question in (" ", "가" * 501):
        assert api.post(f"/api/navigations/{identifier}/questions", json={"question": question}).status_code == 400
    row, _ = api.portal.call(api.app.state.store.get, identifier)
    assert "900101" not in row["rejection_text"] and "010-1234" not in row["rejection_text"]
    assert api.delete(f"/api/navigations/{identifier}").json() == {"data": {"deleted": True}, "message": "deleted"}
    assert api.get(f"/api/navigations/{identifier}").status_code == 404

    async def assert_cascade():
        async with api.app.state.store.engine.connect() as connection:
            assert (await connection.scalar(select(func.count()).select_from(steps).where(steps.c.navigation_id == identifier))) == 0
    api.portal.call(assert_cascade)


def test_expiry_purges_pii_and_returns_410(api):
    identifier = create_navigation(api)

    async def expire():
        async with api.app.state.store.engine.begin() as conn:
            await conn.execute(update(navigations).where(navigations.c.id == identifier).values(expires_at=time.time() - 1))
    api.portal.call(expire)
    for suffix in ("", "/stream"):
        assert api.get(f"/api/navigations/{identifier}{suffix}").status_code == 410
    assert api.post(f"/api/navigations/{identifier}/questions", json={"question": "안녕"}).status_code == 410
    row, was_expired = api.portal.call(api.app.state.store.get, identifier)
    assert row is None and was_expired
    assert api.delete(f"/api/navigations/{identifier}").status_code == 200


def test_copilot_fallback_is_labelled(api):
    api.app.state.runner.narrator.explain = AsyncMock(side_effect=AIUnavailable("timeout"))
    identifier = create_navigation(api)
    events = read_events(api.get(f"/api/navigations/{identifier}/stream"))
    assert len([event for event in events if event[0] == "error" and event[1]["fallback"]]) == 3
    result = api.get(f"/api/navigations/{identifier}").json()["data"]["result"]
    assert result["rejection"]["ai_generated"] is False
    assert result["alternatives"][0]["ai_generated"] is False
    assert api.post(f"/api/navigations/{identifier}/questions", json={"question": "어떻게 하나요?"}).json()["data"]["ai_generated"] is False


def test_no_alternatives_only_offers_source_and_consultation(api):
    api.app.state.policies.candidates = AsyncMock(return_value=[])
    identifier = create_navigation(api)
    read_events(api.get(f"/api/navigations/{identifier}/stream"))
    result = api.get(f"/api/navigations/{identifier}").json()["data"]["result"]
    assert result["alternatives"] == [] and result["pathway"] == []
    assert [action["type"] for action in result["next_actions"]] == ["documents"]


def test_atomic_claim_and_deleted_run_cannot_write(api):
    identifier = create_navigation(api)

    async def check():
        store = api.app.state.store
        tokens = await asyncio.gather(store.claim(identifier), store.claim(identifier))
        assert sum(token is not None for token in tokens) == 1
        token = next(token for token in tokens if token)
        await store.delete(identifier)
        with pytest.raises(RunCancelled):
            await store.emit(identifier, token, "result", {}, result={})
    api.portal.call(check)


def test_last_event_id_validation(api):
    identifier = create_navigation(api)
    for cursor in ("bad", "-1"):
        assert api.get(f"/api/navigations/{identifier}/stream", headers={"Last-Event-ID": cursor}).status_code == 400


def test_age_unknown_tags_and_income_cannot_be_changed_by_ai():
    policy = {"conditions": {"JA0110": 19, "JA0111": 25, "JA0201": "Y", "JA0202": "Y"}}
    checks = check_eligibility(policy, PROFILE)
    assert checks[0].status == "fail"
    assert match_level(checks) == "unlikely"
    assert any(check.status == "unknown" for check in checks)
    assert match_level(check_eligibility({"conditions": {}}, PROFILE)) == "likely"


def test_upstream_failure_and_stale_detail_fallback(api):
    assert api.get("/api/policies").status_code == 200
    assert api.get("/api/policies/applied").status_code == 200

    async def fail(*args, **kwargs):
        from fastapi import HTTPException
        raise HTTPException(502, "provider failed")
    api.app.state.policies.gov.detail = fail

    async def age_cached_detail():
        async with api.app.state.store.engine.begin() as conn:
            await conn.execute(update(policies).where(policies.c.id == "applied").values(detail_at=time.time() - 90000))
    api.portal.call(age_cached_detail)
    assert api.get("/api/policies/local").status_code == 502
    assert api.get("/api/policies/applied").status_code == 200


def test_masking_and_unconfigured_ai(monkeypatch):
    text = mask_personal_information("9001011234567 900101-1234567 01012345678 02-123-4567 +82-10-1234-5678")
    assert "900101" not in text and "12345678" not in text and "1234-5678" not in text
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_ENABLED", raising=False)
    with pytest.raises(AIUnavailable, match="not_configured"):
        asyncio.run(CopilotNarrator().explain("FollowUp", {}))


@pytest.mark.parametrize("explicit,environment", [(False, False), (True, False), (False, True)])
def test_store_uses_verified_tls_ca_without_overriding_custom_settings(monkeypatch, explicit, environment):
    import certifi
    import welfare.store as module

    original = module.create_async_engine
    captured = {}

    def record(*args, **kwargs):
        captured.update(kwargs["connect_args"])
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "create_async_engine", record)
    monkeypatch.delenv("PGSSLROOTCERT", raising=False)
    if environment:
        monkeypatch.setenv("PGSSLROOTCERT", "/custom/root.pem")
    url = "postgresql://test:secret@localhost/test?sslmode=verify-full"
    if explicit:
        url += "&sslrootcert=/custom/root.pem"
    store = module.Store(url)
    if explicit or environment:
        assert "sslrootcert" not in captured
    else:
        assert captured["sslrootcert"] == certifi.where()
    asyncio.run(store.close())


def test_streaming_masks_identifiers_split_across_sdk_deltas():
    from types import SimpleNamespace
    from copilot.session_events import AssistantMessageDeltaData
    from welfare.agents import stream_response

    chunks = []

    class Session:
        def on(self, handler):
            self.handler = handler

        async def send_and_wait(self, prompt, timeout):
            for value in ["안내 " * 20 + "900101-", "1234567 연락 010-", "1234-5678 확인 " + "추가 안내 " * 20]:
                self.handler(SimpleNamespace(
                    data=AssistantMessageDeltaData(delta_content=value, message_id="test", parent_tool_call_id=None),
                    agent_id=None,
                ))
                await asyncio.sleep(0)
            return SimpleNamespace(data=SimpleNamespace(content="done"))

    async def collect(delta):
        chunks.append(delta)

    asyncio.run(stream_response(Session(), "test", collect))
    text = "".join(chunks)
    assert "900101" not in text and "010-" not in text
    assert "[주민번호 마스킹]" in text and "[전화번호 마스킹]" in text
