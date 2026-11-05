"""Private deployment commands; never expose credentials or add public API routes."""

import argparse
import asyncio
import json
import os
import subprocess
from tempfile import NamedTemporaryFile


def azure(*arguments):
    try:
        return subprocess.run(
            ["az", *arguments, "--only-show-errors"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        # CLI errors may include app settings; do not forward stdout/stderr.
        raise RuntimeError("Azure Copilot 설정을 적용하지 못했습니다") from None


def configure_azure(resource_group, app_name):
    target = ["--resource-group", resource_group, "--name", app_name]
    token = os.getenv("COPILOT_GITHUB_TOKEN", "").strip()
    if not token:
        # Ask Azure for names only, never return existing secret values to CI logs.
        existing = json.loads(
            azure(
                "webapp",
                "config",
                "appsettings",
                "list",
                *target,
                "--query",
                "[?(name=='COPILOT_GITHUB_TOKEN' || name=='GITHUB_TOKEN') && value!='' && value!=null].name",
                "--output",
                "json",
            )
        )
        if not existing:
            raise RuntimeError(
                "COPILOT_GITHUB_TOKEN을 Repository secret 또는 Azure 환경변수에 먼저 등록하세요"
            )

    settings = {"COPILOT_ENABLED": "false"}
    if token:
        settings["COPILOT_GITHUB_TOKEN"] = token
    model = os.getenv("COPILOT_MODEL", "").strip()
    if model:
        settings["COPILOT_MODEL"] = model
    # Preserve existing Azure values unless an explicit non-empty setting is supplied.
    for name in (
        "DATABASE_URL",
        "GOV24_API_KEY",
        "GOV24_SERVICE_KEY",
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "FRONT_URL",
    ):
        if value := os.getenv(name, "").strip():
            settings[name] = value
    # mode 0600, automatic deletion; no secret in process arguments or source ZIP.
    with NamedTemporaryFile(mode="w+", suffix=".json") as file:
        json.dump(settings, file)
        file.flush()
        azure(
            "webapp",
            "config",
            "appsettings",
            "set",
            *target,
            "--settings",
            f"@{file.name}",
            "--output",
            "none",
        )


async def check_connection():
    from app.ai import CopilotService
    from app.schemas import SummaryOutput

    if not (os.getenv("COPILOT_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")):
        raise RuntimeError("서버 Copilot 인증 토큰이 설정되지 않았습니다")
    # Real model request through the production adapter, with no personal data.
    answer = await CopilotService().generate(
        "Followup",
        {"question": "AI 연결 확인입니다. 짧게 응답해 주세요.", "result": {}},
        SummaryOutput,
    )
    if not answer.plain_summary.strip():
        raise RuntimeError("Copilot이 빈 응답을 반환했습니다")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure-azure")
    configure.add_argument("--resource-group", required=True)
    configure.add_argument("--app-name", required=True)
    commands.add_parser("check")
    args = parser.parse_args()
    try:
        if args.command == "configure-azure":
            configure_azure(args.resource_group, args.app_name)
        else:
            asyncio.run(check_connection())
    except Exception:  # noqa: BLE001 -- Deployment output must not contain SDK/CLI secrets.
        print(
            "Copilot 설정 또는 실제 호출 확인 실패: 토큰·권한·모델·네트워크를 확인하세요."
        )
        return 1
    print(
        "Copilot 서버 설정 적용 완료"
        if args.command == "configure-azure"
        else "Copilot 실제 호출 확인 완료"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
