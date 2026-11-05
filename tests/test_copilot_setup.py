import asyncio
import json
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import copilot_setup
from app.ai import AIUnavailable, CopilotService
from app.schemas import SummaryOutput


@pytest.fixture(autouse=True)
def clean_credentials(monkeypatch):
    for name in ("COPILOT_GITHUB_TOKEN", "GITHUB_TOKEN", "COPILOT_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_secret_reaches_azure_only_through_private_temporary_file(monkeypatch):
    token = "private-test-token"
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", token)
    monkeypatch.setenv("COPILOT_MODEL", "selected-model")
    captured = {}

    def run(command, **options):
        assert token not in repr(command)
        assert options["capture_output"] and options["check"]
        path = Path(command[command.index("--settings") + 1][1:])
        captured["path"] = path
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text()) == {
            "COPILOT_GITHUB_TOKEN": token,
            "COPILOT_MODEL": "selected-model",
            "COPILOT_ENABLED": "false",
        }
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(subprocess, "run", run)
    copilot_setup.configure_azure("test-group", "test-app")
    assert not captured["path"].exists()


@pytest.mark.parametrize("existing_name", ["COPILOT_GITHUB_TOKEN", "GITHUB_TOKEN"])
def test_existing_azure_token_and_model_are_preserved(monkeypatch, existing_name):
    calls = []

    def azure(*arguments):
        calls.append(arguments)
        if "list" in arguments:
            assert arguments[arguments.index("--query") + 1].endswith(".name")
            return json.dumps([existing_name])
        path = Path(arguments[arguments.index("--settings") + 1][1:])
        assert json.loads(path.read_text()) == {"COPILOT_ENABLED": "false"}
        return ""

    monkeypatch.setattr(copilot_setup, "azure", azure)
    copilot_setup.configure_azure("test-group", "test-app")
    assert len(calls) == 2


def test_missing_secret_aborts_before_mutating_azure(monkeypatch):
    calls = []

    def azure(*arguments):
        calls.append(arguments)
        return "[]"

    monkeypatch.setattr(copilot_setup, "azure", azure)
    with pytest.raises(RuntimeError, match="COPILOT_GITHUB_TOKEN"):
        copilot_setup.configure_azure("test-group", "test-app")
    assert len(calls) == 1 and "list" in calls[0]


def test_temporary_secret_is_deleted_even_if_azure_fails(monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "private-test-token")
    paths = []

    def azure(*arguments):
        paths.append(Path(arguments[arguments.index("--settings") + 1][1:]))
        raise RuntimeError("Azure unavailable")

    monkeypatch.setattr(copilot_setup, "azure", azure)
    with pytest.raises(RuntimeError):
        copilot_setup.configure_azure("test-group", "test-app")
    assert paths and not paths[0].exists()


def test_azure_exception_hides_subprocess_output(monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "az", stderr="private-test-token")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError) as error:
        copilot_setup.azure("webapp", "list")
    assert "private-test-token" not in str(error.value)


def test_check_uses_real_backend_adapter_without_user_data(monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "private-test-token")
    generate = AsyncMock(return_value=SummaryOutput(plain_summary="연결됐습니다."))
    monkeypatch.setattr(CopilotService, "generate", generate)
    asyncio.run(copilot_setup.check_connection())
    agent, context, schema = generate.await_args.args
    assert agent == "Followup" and schema is SummaryOutput
    assert context["result"] == {}
    assert "private-test-token" not in repr(context)


def test_check_requires_server_token_even_with_local_login_enabled(monkeypatch):
    monkeypatch.setenv("COPILOT_ENABLED", "true")
    generate = AsyncMock()
    monkeypatch.setattr(CopilotService, "generate", generate)
    with pytest.raises(RuntimeError, match="인증 토큰"):
        asyncio.run(copilot_setup.check_connection())
    generate.assert_not_awaited()


def test_check_does_not_accept_empty_model_response(monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "private-test-token")
    generate = AsyncMock(return_value=SimpleNamespace(plain_summary=" "))
    monkeypatch.setattr(CopilotService, "generate", generate)
    with pytest.raises(RuntimeError, match="빈 응답"):
        asyncio.run(copilot_setup.check_connection())


def test_check_command_fails_without_exposing_sdk_error(monkeypatch, capsys):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "private-test-token")
    monkeypatch.setattr("sys.argv", ["copilot_setup", "check"])
    monkeypatch.setattr(
        CopilotService,
        "generate",
        AsyncMock(side_effect=AIUnavailable("private-test-token")),
    )
    assert copilot_setup.main() == 1
    assert "private-test-token" not in capsys.readouterr().out
