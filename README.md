# Welfare Next API

FastAPI 서버이며 [API 계약](docs/API.md)의 8개 엔드포인트를 구현합니다. Swagger는 `/docs`에서 확인합니다.

## 로컬 실행

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
python -m uvicorn main:app --reload --port 8000 --env-file .env
```

`.env`에 `GOV24_SERVICE_KEY`를 설정합니다. 정부24 목록·상세·지원조건 API의 실제 응답을 캐시합니다. 인증키가 없고 캐시도 없으면 정부24 조회 API는 502를 반환합니다.

`DATABASE_URL`에는 PostgreSQL 연결 문자열을 사용합니다. Azure PostgreSQL 연결에는 `sslmode=verify-full`을 사용합니다. 인증서 경로가 따로 지정되지 않은 검증 연결에는 certifi CA 묶음을 사용하며, 연결 문자열의 `sslrootcert`와 `PGSSLROOTCERT` 설정을 우선합니다. 로컬에서 이 값이 비어 있으면 `welfare.db` SQLite 파일을 생성합니다. 앱 시작 시 `welfare_` 접두사의 테이블을 생성하며 기존 테이블을 변경하지 않습니다.

`FRONT_URL`과 `http://localhost:3000`에 CORS를 허용합니다.

## 분석 흐름

1. `GET /api/policies`로 정책을 검색하고, 선택한 정책의 `/api/policies/{id}`에서 상세 정보를 확인합니다.
2. `POST /api/navigations`에 프로필과 저장 동의를 보내 세션 ID를 받습니다.
3. `/api/navigations/{id}/stream`을 열면 사유해석, 정책매칭, 경로설계를 순서대로 실행합니다. `step`, `tool`, `token`, `error`, `result` 이벤트를 보냅니다.
4. `GET /api/navigations/{id}`로 저장된 상태·결과를 다시 조회합니다. 완료된 스트림을 다시 열면 `result` 이벤트 한 번만 반환합니다. 진행 중에는 `Last-Event-ID` 이후의 이벤트를 재전송할 수 있습니다.
5. 후속 질문은 `POST /api/navigations/{id}/questions`로 보냅니다. 삭제는 `DELETE /api/navigations/{id}`를 호출합니다.

분석 세션과 진행 이벤트는 DB에 저장합니다. 원자적 실행권 획득과 120초 임대로 여러 연결·여러 서버의 중복 실행을 방지합니다. 비정상 종료된 작업은 스트림 재접속 후 임대 만료 시 다시 실행할 수 있습니다. 정상 종료 중인 작업은 실행권을 반환합니다.

세션은 생성 후 24시간 동안 조회할 수 있습니다. 접근 시와 30초 주기의 정리 작업에서 만료 세션의 프로필·사유·결과·진행 이벤트를 삭제합니다. 만료 ID만 7일간 보관해 410 응답을 구분하며 이후에는 404를 반환합니다. 사용자가 삭제하면 ID와 이벤트도 삭제합니다. 거절 안내문의 주민번호·전화번호는 저장 전에 마스킹합니다.

## Copilot과 자격 판정

`COPILOT_GITHUB_TOKEN`을 설정하면 GitHub Copilot Python SDK를 호출합니다. 로컬 CLI 로그인을 사용하려면 `COPILOT_ENABLED=true`를 설정합니다. 필요하면 `COPILOT_MODEL`을 지정합니다. 런타임을 미리 준비하려면 실행합니다.

```bash
python -m copilot download-runtime
```

각 설명 생성은 도구 접근을 비활성화한 별도 Copilot 세션과 임시 저장 공간에서 실행합니다. SDK에서 생성 중인 설명을 받아 마스킹 후 `token` 이벤트로 전달합니다. 주민번호·전화번호가 여러 SDK 델타에 나뉘어 도착하는 경우도 버퍼링해 마스킹합니다. 인증 미설정·시간 초과·호출 실패 시 `error` 이벤트의 `fallback=true`와 함께 규칙 기반 설명을 사용하며 `ai_generated=false`로 표시합니다.

연령·지역·단일 1인가구 대상 태그는 코드로 검사합니다. 소득인정액, 재산, 다중 대상 태그 등 공개 코드만으로 확정할 수 없는 조건과 선정기준 원문은 `unknown`으로 유지합니다. Copilot은 판정 값을 수정할 수 없습니다. 정부24에 지역 필드가 없으므로 지방·시군구·광역시도·교육청 정책의 소관기관명에서 지역을 가져옵니다. 목록 후보 중 같은 서비스분야·지역에 해당하는 최대 10개의 상세를 비교하고 최대 5개를 반환합니다.

전달받은 API 문서의 TRD는 저장소에 없어, 추가 소득 기준표나 조건 조합 규칙을 가정하지 않았습니다. 정부24와 Copilot을 실제 호출하는 통합 검증에는 각각의 인증이 필요합니다.

## 검증

```bash
python -m pytest tests -q
python -m pip check
```

정부24·Copilot 호출은 테스트 대역으로 검증합니다. HTTP 계약, 스트림 재전송, 입력 검증, 마스킹, 만료, 삭제 및 규칙 판정을 포함합니다. `PG_TEST_URL`이 설정되면 별도의 임시 PostgreSQL DB에서 실제 저장·동시 실행·복구·만료·종속 삭제도 검사합니다. GitHub Actions의 테스트용 PostgreSQL 서비스를 사용합니다.

## Azure 수동 배포

GitHub Actions에서 `Test and deploy FastAPI app to Azure Web App - chungdo-server-dev`의 **Run workflow**를 실행합니다. Python 코드와 `welfare/` 패키지를 묶어 기존 App Service에 배포합니다.

App Service 환경 변수에 `DATABASE_URL`, `FRONT_URL`, `GOV24_SERVICE_KEY`, `COPILOT_GITHUB_TOKEN`을 등록합니다. DB 네트워크에서 App Service 접속을 허용해야 합니다. 워크플로가 Python 3.14, 시작 명령과 소스 빌드를 설정하고 배포 후 `/api/health`, `/openapi.json`, `/health/ready`를 확인합니다.

```text
런타임: PYTHON|3.14
시작 명령: python -m uvicorn main:app --host 0.0.0.0 --port 8000
SCM_DO_BUILD_DURING_DEPLOYMENT=true
```

기존 `/` 텍스트 응답과 PostgreSQL 상태 확인용 `/health/ready`도 제공합니다. MCP `ping` 도구는 `python mcp_server.py`로 실행합니다.

## 외부 계약

- [정부24 v3 공식 OpenAPI](https://infuser.odcloud.kr/api/stages/44436/api-docs)
- [Copilot Python SDK](https://github.com/github/copilot-sdk/blob/main/python/README.md)
