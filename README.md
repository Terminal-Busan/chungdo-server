# Chungdo API

Python 3.14 기반 FastAPI 서버입니다.

## 로컬 실행

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
python -m uvicorn main:app --reload --port 3001 --env-file .env
```

`.env`에 PostgreSQL `DATABASE_URL`을 설정합니다. Azure PostgreSQL 연결에는 `sslmode=verify-full`을 사용합니다. API 문서는 `/docs`에서 확인할 수 있습니다.

## API

- `GET /`는 기존과 동일하게 `Hello World!` 텍스트를 반환합니다.
- `GET /health/ready`는 PostgreSQL에 연결해 `SELECT 1`을 실행합니다. 성공하면 `200 {"status":"ok"}`, 설정이 없거나 연결이 실패하면 503을 반환합니다. 연결과 쿼리는 각각 5초 제한을 적용합니다.
- `GET /openapi.json`은 API 명세를 반환합니다.

## 검증

```bash
python -m pytest tests -q
python -m pip check
```

DB 성공·실패 테스트는 연결을 대체해 실행합니다. 실제 PostgreSQL 연결은 배포 후 준비 상태 검사에서 검증합니다.

## Azure 배포

GitHub Actions의 `Test and deploy FastAPI app to Azure Web App - chungdo-server-dev`를 수동 실행합니다. 기본 브랜치 또는 배포하려는 브랜치를 선택합니다.

워크플로는 Python 3.14에서 테스트하고 `main.py`, `mcp_server.py`, `requirements.txt`를 ZIP으로 묶습니다. 기존 Azure OIDC 시크릿으로 로그인한 뒤, `chungdo-server-dev`의 Python 버전과 시작 명령을 설정하고 Azure에서 의존성을 설치하도록 구성합니다.

```text
런타임: PYTHON|3.14
시작 명령: python -m uvicorn main:app --host 0.0.0.0 --port 8000
SCM_DO_BUILD_DURING_DEPLOYMENT=true
```

App Service에는 기존 PostgreSQL의 `DATABASE_URL`이 필요하며, DB 네트워크에서 App Service 연결을 허용해야 합니다. 배포 과정에서 기존 환경 변수는 유지합니다. 배포 후 `/openapi.json`으로 FastAPI 실행을 확인하고 `/health/ready`로 DB 연결을 확인합니다. 실패하면 Actions와 App Service 로그 스트림에서 확인합니다.

## Copilot SDK와 MCP

GitHub Copilot Python SDK와 MCP Python SDK는 서버 의존성에 포함됩니다.
Copilot 런타임은 SDK와 별도로 내려받습니다. 인증을 준비한 뒤 로컬 가상 환경에서 실행합니다.

```bash
python -m copilot download-runtime
```

SDK의 기본 런타임 연결 방식은 최초 사용 시 런타임을 자동으로 내려받을 수도 있습니다.
Azure에서 Copilot을 호출하려면 선택한 GitHub 인증 또는 BYOK 설정과 런타임 다운로드가 가능한 환경이 필요합니다.
현재 제공하는 HTTP API는 위의 루트 응답과 DB 준비 상태 검사입니다. AI 질문 API는 이후 구현할 기능입니다.

기존 MCP의 `ping` 도구는 Python으로 옮겼습니다.

```bash
python mcp_server.py
```

이 프로세스는 stdio로 MCP 요청을 받아 `pong`을 반환합니다.
Copilot에 연결할 때 MCP 실행 명령은 현재 Python 인터프리터와 `mcp_server.py`의 절대 경로를 사용합니다.

- [Copilot Python SDK](https://github.com/github/copilot-sdk/blob/main/python/README.md)
- [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/v2/)
