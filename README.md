# Chungdo API

Python 3.14 기반 FastAPI 서버입니다.

## 로컬 실행

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
python -m uvicorn main:app --reload --port 8000 --env-file .env
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

워크플로는 Python 3.14와 격리된 PostgreSQL에서 테스트하고 `main.py`, `mcp_server.py`, `requirements.txt`, `app/`, `seed/`를 ZIP으로 묶습니다. 기존 Azure OIDC 시크릿으로 로그인한 뒤, `chungdo-server-dev`의 Python 버전과 시작 명령을 설정하고 Azure에서 의존성을 설치하도록 구성합니다.

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
복지 분석 API는 아래 절을 참고하세요. Copilot 인증이 없거나 모델 호출이 실패하면 분석은 규칙 결과와 공식 정책 정보로 계속되며, SSE `error`에 `fallback: true`를 전달합니다. 후속 질문은 AI가 필요하며 실패 시 500을 반환합니다.

기존 MCP의 `ping` 도구는 Python으로 옮겼습니다.

```bash
python mcp_server.py
```

이 프로세스는 stdio로 MCP 요청을 받아 `pong`을 반환합니다.
Copilot에 연결할 때 MCP 실행 명령은 현재 Python 인터프리터와 `mcp_server.py`의 절대 경로를 사용합니다.

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
