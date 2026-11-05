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

워크플로는 Python 3.14와 격리된 PostgreSQL에서 테스트하고 `main.py`, `mcp_server.py`, `requirements.txt`, `app/`, `seed/`를 ZIP으로 묶습니다. 기존 Azure OIDC 시크릿으로 로그인한 뒤, `chungdo-server-dev`의 Python 버전과 시작 명령을 설정하고 Azure에서 의존성을 설치하도록 구성합니다.

```text
런타임: PYTHON|3.14
시작 명령: python -m uvicorn main:app --host 0.0.0.0 --port 8000
SCM_DO_BUILD_DURING_DEPLOYMENT=true
```

기존 `/` 텍스트 응답과 PostgreSQL 상태 확인용 `/health/ready`도 제공합니다. MCP `ping` 도구는 `python mcp_server.py`로 실행합니다.

## Copilot SDK와 MCP

GitHub Copilot Python SDK와 MCP Python SDK는 서버 의존성에 포함됩니다.
Copilot 런타임은 SDK와 별도로 내려받습니다. 인증을 준비한 뒤 로컬 가상 환경에서 실행합니다.

```bash
python -m copilot download-runtime
```

SDK의 기본 런타임 연결 방식은 최초 사용 시 런타임을 자동으로 내려받을 수도 있습니다.
Azure에서 Copilot을 호출하려면 선택한 GitHub 인증 또는 BYOK 설정과 런타임 다운로드가 가능한 환경이 필요합니다.
복지 분석 API는 아래 절을 참고하세요. Copilot 인증이 없거나 모델 호출이 실패하면 분석은 규칙 결과와 공식 정책 정보로 계속되며, SSE `error`에 `fallback: true`를 전달합니다. 후속 질문은 AI가 필요하며 실패 시 500을 반환합니다.

기존 MCP의 `ping` 도구는 Python으로 옮겼습니다.

```bash
python mcp_server.py
```

이 프로세스는 stdio로 MCP 요청을 받아 `pong`을 반환합니다.
Copilot에 연결할 때 MCP 실행 명령은 현재 Python 인터프리터와 `mcp_server.py`의 절대 경로를 사용합니다.

- [정부24 v3 공식 OpenAPI](https://infuser.odcloud.kr/api/stages/44436/api-docs)
- [Copilot Python SDK](https://github.com/github/copilot-sdk/blob/main/python/README.md)
- [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/v2/)

## Welfare Next API

공통 계약은 [docs/API.md](docs/API.md)를 따릅니다. `/docs`에서 실제 요청/응답 스키마를 확인할 수 있습니다.

- `GET /api/health`
- `GET /api/policies?q=&field=&region=&limit=20`
- `GET /api/policies/{id}`
- `POST /api/navigations`
- `GET /api/navigations/{id}/stream` (SSE)
- `GET /api/navigations/{id}`
- `DELETE /api/navigations/{id}`
- `POST /api/navigations/{id}/questions` (P1)

`app/policies/`는 정책·추천·규칙 판정, `app/guidance/`는 공식 자료에 근거한 다음 행동, `app/navigations/`는 사유해석·순차 실행·세션·SSE를 담당합니다. Router는 HTTP 처리, Service는 업무 로직, `app/schemas.py`는 DTO와 검증을 담당합니다. SDK 연결은 `app/ai.py`, DB 접근은 `app/db.py`에 모았습니다. 기존 `/`, `/health/ready`, MCP ping은 유지합니다.

### DB 준비 및 실제 정책 동기화

`.env`에 `DATABASE_URL`, `GOV24_API_KEY`를 설정하고 실행합니다. 초기화는 명시적으로 실행하며 기존 테이블을 삭제하지 않습니다.

```bash
python -m app.sync_gov24 --init --init-only
python -m app.sync_gov24 --pages 3 --max-policies 100
```

실제 정책이 적재되지 않았다면 검색 결과는 빈 배열입니다. 외부 API 실패를 가짜 정책으로 대체하지 않습니다. 캐시가 있으면 정부24 장애 중에도 조회·분석할 수 있습니다. 지원조건 매핑은 TRD의 JA 코드를 사용하며, 표현되지 않는 기준은 `unknown`입니다. 중위소득 참고값은 [보건복지부 기준](https://www.mohw.go.kr/menu.es?mid=a10708010900)과 연도를 `app/config/median_income.json`에 기록했습니다. 입력 월 소득은 소득인정액과 다르므로 원문에 소득인정액을 요구하면 확인 필요로 안내합니다. 기준 연도가 달라지면 소득 판정도 `unknown`으로 처리합니다.

### 명시적 데모 실행

별도의 로컬 DB를 사용하세요. `.env`에 `WELFARE_DEMO_MODE=true`를 설정한 뒤 실행합니다.

```bash
python -m app.sync_gov24 --init --demo
python -m uvicorn main:app --reload --port 8000 --env-file .env
```

정책 이름은 `[데모]`, ID는 `demo-` 접두사이며 실제 정부 정책이 아닙니다. 결과 고지에도 데모임을 표시합니다. 운영 모드에서는 데모 정책을 반환하지 않습니다. 예시 탈락 정책ID는 `demo-rejected-housing`이며, API 명세의 사용자 조건 예시로 분석할 수 있습니다.

### AI 및 세션 처리

배포용 토큰은 GitHub 저장소 Settings → Secrets and variables → Actions에 `COPILOT_GITHUB_TOKEN` 이름의 Repository secret으로 등록합니다. 개인 계정 소유의 fine-grained 토큰에 `Copilot Requests` 권한과 해당 계정의 Copilot 이용 권한이 필요합니다. 선택 사항인 Repository variable `COPILOT_MODEL`로 사용할 모델을 지정할 수 있습니다. 토큰은 코드·ZIP·로그에 넣지 않습니다.

배포 워크플로가 토큰을 Azure App Service 환경변수로 전달합니다. GitHub secret이 비어 있으면 Azure에 이미 등록된 `COPILOT_GITHUB_TOKEN` 또는 `GITHUB_TOKEN`을 유지하며, 양쪽 모두 없으면 배포 설정 변경 전에 중단합니다. 서버 시작 명령은 SDK 런타임 준비 → `python -m app.copilot_setup check`로 실제 모델 응답 확인 → API 실행 순서입니다. 인증·모델·네트워크 오류가 있으면 API 시작과 배포 검증이 실패하므로 단순 health 성공을 AI 연결 성공으로 오인하지 않습니다. 이 시작 점검은 서버 재시작마다 짧은 모델 요청 1회를 사용합니다. 서버 실행 중 외부 AI 장애에는 기존 규칙 기반 폴백을 유지합니다.

서버에 `COPILOT_GITHUB_TOKEN`(또는 `GITHUB_TOKEN`)과 `COPILOT_MODEL`을 설정합니다. 모델 이름은 계정에서 사용 가능한 모델로 지정하세요. MAF나 별도 AI 서버를 추가하지 않고 설치된 Copilot SDK를 직접 호출합니다. 에이전트 도구는 현재 분석의 정책 캐시 조회·규칙 판정만 허용하고, 기본 파일·셸·외부 쓰기 도구는 노출하지 않습니다. AI는 사유·추천 이유·행동·경로의 구조화된 JSON을 반환하며, 서버가 판정값·출처·후보ID·서류·신청방법·순서를 검증하고 교정합니다.

모델 출력은 Pydantic으로 검증하고 JSON 오류 시 1회 재시도합니다. 에이전트 단계마다 DB 검색·도구 처리·모델 호출을 포함해 45초 제한을 적용하고, 초과 시 SSE `error`와 안전한 폴백을 제공한 뒤 다음 단계로 진행합니다. 검증되지 않은 모델 토큰과 내부 추론은 클라이언트에 내보내지 않으며, 검증된 사유 설명을 `token` 이벤트로 전달합니다. AI 미사용 폴백은 `ai_generated: false`로 구분합니다.

### 에이전트 인스트럭션 명세 반영

기준은 [에이전트 인스트럭션 명세](docs/AGENT_INSTRUCTIONS.md)입니다. 공통·역할별 지시문과 JSON 재시도 문구는 `app/agents/instructions/`에서 읽습니다. 통지 문구는 저장 전에 주민번호·전화번호 및 패턴으로 식별 가능한 이름·상세주소를 마스킹하고, 입력 안의 태그 문자열을 제거한 뒤 이스케이프하여 `<user_input>` 안에만 전달합니다. 임의의 이름·주소를 완벽히 식별하는 기능은 아니므로 실제 입력을 사용한 개인정보 검증도 필요합니다.

- A1: 규칙의 fail만 탈락 조건으로 사용하며, fail이 없으면 통지 문구·선정기준에 실제로 있는 항목으로 가능한 사유를 최대 3개 구성합니다. 소득 설명에 추정임을 밝히고, 용어 풀이 2~5개를 제공합니다. 원문 용어가 부족하면 상세 도구의 필드명인 지원대상·선정기준을 풀이합니다. 정의는 `app/config/glossary.json`을 사용합니다.
- A2: 같은 분야 → 지역 → 사용자 상황에 맞는 다른 분야 순서로 최대 3회 검색하며 원래 정책은 항상 제외합니다. 판정 대상은 최대 8개, 결과는 최대 5개입니다. fail 없는 후보가 3개 이상이면 fail 후보를 제외하고, 3개 미만일 때만 미충족을 명시한 후보를 추가합니다. 사업자·기관 전용 JA 코드만 있는 정책은 제외합니다. 반복 판정은 캐시를 사용합니다.
- A3: 행동 3~6개를 제공하며, 서류·신청방법은 DB 원문만 사용합니다. 정보가 없으면 신청처 확인을 안내합니다. 이의신청은 근거가 있을 때만 검토하고 재신청은 조건 변화 시 다시 확인하도록 안내합니다. 경로는 fail 유무 → unknown 개수 → 명확하게 해석 가능한 공식 신청기한 → 모델 순서 순으로 교정합니다. 대체 정책이 없으면 경로는 비우고 정부24·읍면동 상담을 안내합니다.

외부 API는 [기존 API 계약](docs/API.md)을 유지합니다. 새 `possible_reasons`는 에이전트 간 컨텍스트로 전달하고 첫 가능한 사유를 기존 `plain_summary`에 반영합니다. 내부 A3의 `policy_id: null`은 공개 응답에서 해당 분석의 원래 정책ID에 연결합니다. 실제 AI가 사용된 필드에만 `ai_generated: true`를 표시하며 폴백에 AI 생성을 표시하지 않습니다. 문서의 6절에 구체적인 테스트 입력이 없어, 정상·오류·근거 없음·후보 제한·출력 교정·타임아웃 사례를 `tests/test_agent_instructions.py`에 추가했습니다.

SSE 연결이 끊겨도 분석은 계속되고 GET으로 결과를 확인할 수 있습니다. 완료된 세션의 SSE는 `result`만 한 번 반환합니다. 실행 시작은 DB의 원자적 상태 변경으로 중복 방지합니다. 삭제 시 현재 실행 작업과 로그를 제거합니다. 결과는 생성 24시간 후 접근 불가하며, 만료된 입력·결과·로그는 최대 60초 주기로 정리합니다. 만료 ID에 410을 반환하기 위해 개인정보 없는 세션 식별 정보는 남깁니다. Copilot 세션은 요청별 임시 디렉터리를 사용하고 종료 시 정리합니다. 서버 프로세스 종료 중인 분석은 가능한 경우 pending으로 되돌리고, 비정상 종료로 남은 running은 시작 후 5분이 지나면 failed로 처리합니다. MVP는 단일 Uvicorn worker로 운영하세요.

### 검증

```bash
python -m pytest tests -q
# 별도의 테스트 DB에서 실행: 매 테스트마다 격리 스키마를 생성·삭제합니다.
TEST_DATABASE_URL='postgresql://USER:PASSWORD@localhost/TEST_DB' python -m pytest tests -q
python -m pip check
```

`TEST_DATABASE_URL`이 없으면 실제 PostgreSQL 통합 테스트는 skip합니다. CI는 별도 PostgreSQL 서비스에서 통합 테스트까지 실행하도록 설정했습니다. 모의 AI/정부24 테스트와 실제 외부 인증을 사용하는 검증은 별개입니다. 운영 전 실제 정부24 동기화, Copilot 인증·런타임·모델 응답, 배포 URL과 CORS는 실제 환경에서 확인해야 합니다. 이름·주민번호·전화번호를 입력하지 마세요. 수집된 통지 문구의 주민번호·전화번호는 저장 전 마스킹하며, 개인정보·토큰·DB URL은 오류 응답과 로그에 포함하지 않습니다.

### main 병합 시 실행 기준

기본 `main:app`은 합의한 세 모듈(`app/policies`, `app/guidance`, `app/navigations`)과 에이전트 인스트럭션을 사용합니다. 팀원이 추가한 `welfare/` 구현과 테스트는 변경 없이 보존했으며 별도 앱 팩토리 `welfare.api:create_app`으로 실행할 수 있습니다. 두 앱을 같은 포트에 중복 등록하지 않습니다. 기본 앱은 위의 DB 초기화 절차가 필요합니다. `welfare/`의 SQLite 자동 생성·실행 임대 기능은 해당 팩토리의 기능입니다.

양쪽 의존성과 배포 패키지를 포함하고 CI에서 `TEST_DATABASE_URL`, `PG_TEST_URL`을 모두 설정합니다. 팀원의 PostgreSQL 인증서 검증 설정을 기본 앱에도 반영했고, `GOV24_SERVICE_KEY`도 `GOV24_API_KEY`의 별칭으로 지원합니다. `COPILOT_ENABLED=true`이면 로컬 Copilot 로그인을 허용하고 모델명을 비우면 SDK 기본 모델을 사용합니다. 실제 배포·외부 AI 인증 검증은 별도입니다.
