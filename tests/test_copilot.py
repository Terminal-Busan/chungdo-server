"""Exercise the real adapter's SDK calls with a transport substitute, no credentials."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.ai import JSON_RETRY, AIUnavailable, CopilotService
from app.schemas import ExplanationOutput


@pytest.mark.parametrize("invalid_count", [0, 1, 2])
def test_sdk_configuration_json_retry_and_cleanup(monkeypatch, invalid_count):
    captured = {"calls": 0, "deleted": [], "prompts": []}

    class Session:
        session_id = "temporary-test-session"

        async def send_and_wait(self, prompt, timeout, response_schema):
            assert response_schema is ExplanationOutput
            captured["calls"] += 1
            captured["prompt"] = prompt
            captured["prompts"].append(prompt)
            content = (
                "not-json"
                if captured["calls"] <= invalid_count
                else json.dumps(
                    {
                        "plain_summary": "기준을 확인하세요.",
                        "failed_criteria": [],
                        "possible_reasons": [],
                        "glossary": [
                            {"term": "지원대상", "meaning": "지원하는 대상"},
                            {"term": "선정기준", "meaning": "선정 조건"},
                        ],
                    }
                )
            )
            return SimpleNamespace(data=SimpleNamespace(content=content))

        async def disconnect(self):
            captured["disconnected"] = True

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            captured["closed"] = True

        async def create_session(self, **kwargs):
            captured["session"] = kwargs
            return Session()

        async def delete_session(self, session_id):
            captured["deleted"].append(session_id)

    import copilot

    monkeypatch.setattr(copilot, "CopilotClient", Client)
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("COPILOT_MODEL", "test-model")

    def generate():
        return asyncio.run(
            CopilotService().generate(
                "RejectionInterpreter", {"text": "990101-1234567"}, ExplanationOutput
            )
        )

    if invalid_count == 2:
        with pytest.raises(AIUnavailable):
            generate()
    else:
        assert generate().plain_summary == "기준을 확인하세요."
    assert captured["calls"] == (2 if invalid_count else 1)
    if invalid_count:
        assert captured["prompts"][1] == JSON_RETRY
    assert captured["client"]["mode"] == "empty"
    assert captured["client"]["use_logged_in_user"] is False
    assert captured["session"]["available_tools"] == []
    assert captured["session"]["enable_session_store"] is False
    assert captured["session"]["memory"] == {"enabled": False}
    assert captured["session"]["on_permission_request"](None, None).kind == "reject"
    if not invalid_count:
        assert "990101" not in captured["prompt"]
        assert "test-token" not in captured["prompt"]
    assert captured["disconnected"] and captured["closed"]
    assert captured["deleted"] == ["temporary-test-session"]


def test_missing_credentials_fails_safely(monkeypatch):
    for key in ("COPILOT_GITHUB_TOKEN", "GITHUB_TOKEN", "COPILOT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(AIUnavailable):
        asyncio.run(
            CopilotService().generate("RejectionInterpreter", {}, ExplanationOutput)
        )
