# Welfare Next — API 명세서

> 기준 문서: [TRD.md](../TRD.md) §5·§6. 프론트·백엔드 공통 계약 문서이며, 변경 시 이 문서부터 수정한다.

## 공통 규칙

| 항목 | 내용 |
|---|---|
| Base URL (로컬) | `http://localhost:8000` |
| Base URL (배포) | `https://<백엔드이름>.azurecontainerapps.io` |
| Prefix | 모든 경로 `/api` |
| 인증 | 없음 (로그인 없이 전체 사용) |
| 성공 응답 | `{ "data": ..., "message": "..." }` |
| 에러 응답 | `{ "statusCode": 400, "message": "..." }` |
| 문서 | `/docs` (FastAPI 자동 Swagger) |
| CORS | `FRONT_URL` 환경변수의 주소와 `http://localhost:3000` 허용 |

## API 명세표

| # | Method | Path | 설명 | 요청 | 성공 응답 | 에러 | 관련 화면 | 우선순위 |
|---|---|---|---|---|---|---|---|---|
| 1 | GET | `/api/health` | 서버 상태 확인 | 없음 | `200` `{status:"ok"}` | — | — (배포 검증) | P0 |
| 2 | GET | `/api/policies` | 정부24 정책 검색 | Query: `q`, `field`, `region`, `limit` | `200` 정책 요약 배열 (결과 없으면 `[]`) | `400` 파라미터 오류 | 입력 | P0 |
| 3 | GET | `/api/policies/{id}` | 정책 상세 (지원조건 코드 포함) | Path: `id` (정부24 서비스ID) | `200` 정책 상세 | `404` 정책 없음, `502` 정부24 조회 실패 | 결과(상세 패널) | P0 |
| 4 | POST | `/api/navigations` | 분석 세션 생성 | Body: `applied_policy_id`, `rejection_text`, `profile`, `consent` | `201` `{id, status:"pending"}` | `400` 검증 실패·동의 없음, `404` 정책 없음 | 입력 | P0 |
| 5 | GET | `/api/navigations/{id}/stream` | 에이전트 3개 실행 + 진행 스트리밍 (SSE) | Path: `id` | `200` `text/event-stream` | `404` 세션 없음, `410` 만료 | 분석·결과 | P0 |
| 6 | GET | `/api/navigations/{id}` | 분석 결과 조회 (새로고침·끊김 대비) | Path: `id` | `200` 상태 + 결과 | `404` 세션 없음, `410` 만료 | 분석·결과 | P0 |
| 7 | POST | `/api/navigations/{id}/questions` | 결과에 대한 후속 질문 | Body: `question` | `200` `{answer, ai_generated}` | `400` 빈 질문·500자 초과, `404`, `409` 분석 미완료 | 분석·결과 | P1 (시간 남을 때만) |
| 8 | DELETE | `/api/navigations/{id}` | 분석 결과 삭제 (확인 모달 후 호출) | Path: `id` | `200` `{deleted:true}` | `404` 세션 없음 | 분석·결과 | P0 |

---

## 상세

### 1. GET /api/health

```json
200 { "data": { "status": "ok" }, "message": "ok" }
```

### 2. GET /api/policies

| Query | 타입 | 필수 | 기본값 | 허용 범위 |
|---|---|---|---|---|
| `q` | string | 아니오 | — | 1~50자, 서비스명 키워드 |
| `field` | string | 아니오 | — | 정부24 서비스분야 값 (예: 주거·자립) |
| `region` | string | 아니오 | — | 시·도 또는 시·군·구명 (지역 정책 + 전국 정책 반환) |
| `limit` | int | 아니오 | 20 | 1~50 |

```json
200
{
  "data": [
    { "id": "<서비스ID>", "name": "<서비스명>", "field": "주거·자립", "agency": "<소관기관명>", "region": null }
  ],
  "message": "ok"
}
```
결과 없음: `200 { "data": [], "message": "검색 결과가 없습니다" }`

### 3. GET /api/policies/{id}

```json
200
{
  "data": {
    "id": "<서비스ID>",
    "name": "<서비스명>",
    "summary": "...",
    "target_text": "지원대상 원문",
    "criteria_text": "선정기준 원문",
    "conditions": { "JA0110": 19, "JA0111": 34, "JA0404": "Y" },
    "documents": "구비서류",
    "apply_method": "...",
    "apply_deadline": "...",
    "online_apply_url": "...",
    "detail_url": "<정부24 상세조회URL>",
    "gov24_updated_at": "..."
  },
  "message": "ok"
}
```
에러: `404 { "statusCode": 404, "message": "정책을 찾을 수 없습니다" }`, `502 { "statusCode": 502, "message": "정부24 상세 정보를 불러오지 못했습니다" }` (캐시에 없고 실시간 조회도 실패한 경우)

### 4. POST /api/navigations

| Body 필드 | 타입 | 필수 | 허용 범위 |
|---|---|---|---|
| `applied_policy_id` | string | 예 | 캐시에 있는 정부24 서비스ID |
| `rejection_text` | string | 아니오 | 2,000자 이하, 서버에서 주민번호·전화번호 마스킹 |
| `profile.age` | int | 예 | 0~120 |
| `profile.sido` | string | 예 | 시·도명 |
| `profile.sigungu` | string | 아니오 | 시·군·구명 |
| `profile.household_type` | enum | 예 | `single` · `couple` · `single_parent` · `multi_child` · `other` |
| `profile.household_size` | int | 예 | 1~10 |
| `profile.income_monthly_manwon` | int | 예 | 0 이상 (가구 월 소득, 만원) |
| `profile.housing_type` | enum | 예 | `owned` · `jeonse` · `monthly_rent` · `other` |
| `profile.employment_status` | enum | 예 | `employed` · `job_seeker` · `student` · `none` |
| `profile.has_disability` | boolean | 아니오 | 기본값 `false` |
| `consent` | boolean | 예 | 반드시 `true` (24시간 임시 저장 동의) |

```json
요청
{
  "applied_policy_id": "<탈락한 서비스ID>",
  "rejection_text": "소득인정액이 선정기준을 초과하여 부적합 결정되었습니다.",
  "profile": {
    "age": 27, "sido": "경상북도", "sigungu": "경산시",
    "household_type": "single", "household_size": 1,
    "income_monthly_manwon": 230,
    "housing_type": "monthly_rent", "employment_status": "employed",
    "has_disability": false
  },
  "consent": true
}

201
{ "data": { "id": "6f1c...", "status": "pending" }, "message": "created" }
```
에러 예: `400 { "statusCode": 400, "message": "profile.age는 0~120 사이여야 합니다" }`, `400 { "statusCode": 400, "message": "개인정보 임시 저장 동의가 필요합니다" }`

### 5. GET /api/navigations/{id}/stream (SSE)

응답 헤더 `Content-Type: text/event-stream`. 이미 분석이 끝난 세션이면 `result` 이벤트 1회 후 종료.

| event | 언제 | data 예시 |
|---|---|---|
| `step` | 에이전트 시작·완료 | `{"agent":"RejectionInterpreter","status":"started"}` |
| `tool` | 도구 호출 | `{"agent":"PolicyMatcher","tool":"check_eligibility","args":{"policy_id":"<서비스ID>"}}` |
| `token` | 설명 문장 생성 중 | `{"agent":"RejectionInterpreter","delta":"소득 기준이..."}` |
| `result` | 전체 완료 | 6번 API의 `data.result`와 같은 JSON |
| `error` | 단계 실패 | `{"agent":"PathPlanner","message":"timeout","fallback":true}` |

에이전트 이름: `RejectionInterpreter`(사유해석) → `PolicyMatcher`(정책매칭) → `PathPlanner`(경로설계)

### 6. GET /api/navigations/{id}

```json
200
{
  "data": {
    "id": "6f1c...",
    "status": "done",
    "result": {
      "rejection": {
        "failed_criteria": [
          { "criterion": "소득", "user_value": "월 230만원 (중위소득 OO%)", "required_value": "정부24 지원조건 값", "explanation": "..." }
        ],
        "plain_summary": "...",
        "glossary": [ { "term": "소득인정액", "meaning": "..." } ],
        "ai_generated": true
      },
      "alternatives": [
        {
          "policy_id": "<서비스ID>", "name": "<서비스명>", "match": "likely",
          "criteria": [ { "name": "연령", "status": "pass", "detail": "27세 / 19~34세" } ],
          "reason": "...", "ai_generated": true
        }
      ],
      "next_actions": [
        { "type": "apply_new", "title": "...", "description": "...", "documents": ["..."], "policy_id": "<서비스ID>" }
      ],
      "pathway": [ { "order": 1, "policy_id": "<서비스ID>", "why": "..." } ],
      "disclaimer": "최종 자격 판단은 담당 기관에 있습니다."
    },
    "expires_at": "2026-11-06T13:00:00+09:00"
  },
  "message": "ok"
}
```

값 정의

| 필드 | 값 |
|---|---|
| `status` | `pending` · `running` · `done` · `failed` (`done`이 아니면 `result`는 `null`) |
| `alternatives[].match` | `eligible` (전 조건 pass) · `likely` (fail 없음, unknown 있음) · `unlikely` (fail 있음) |
| `criteria[].status` | `pass` · `fail` · `unknown` (규칙 함수 결과, AI가 바꾸지 않음) |
| `next_actions[].type` | `reapply` · `documents` · `appeal` · `apply_new` |

대체 정책 없음: `alternatives: []`, `next_actions`에 정부24 상세조회·복지 상담 안내만 포함

### 7. POST /api/navigations/{id}/questions (P1)

| Body | 타입 | 필수 | 허용 범위 |
|---|---|---|---|
| `question` | string | 예 | 1~500자 |

```json
200 { "data": { "answer": "...", "ai_generated": true }, "message": "ok" }
409 { "statusCode": 409, "message": "분석이 아직 끝나지 않았습니다" }
```

### 8. DELETE /api/navigations/{id}

```json
200 { "data": { "deleted": true }, "message": "deleted" }
404 { "statusCode": 404, "message": "분석 결과를 찾을 수 없습니다" }
```
연결된 `agent_steps`도 함께 삭제된다. 프론트는 반드시 확인 모달을 거친 뒤 호출한다.

---

## 프론트 연동 순서

1. 입력 화면: `GET /api/policies?q=` 로 탈락 정책 검색 → 선택
2. "분석 시작": `POST /api/navigations` → 받은 `id`로 `/result?id=...` 이동
3. 결과 화면: `EventSource(`${API}/api/navigations/${id}/stream`)` 연결, `step`·`tool`·`token`으로 진행 타임라인 표시, `result` 수신 시 결과 렌더링 후 연결 종료
4. 연결이 끊기거나 새로고침하면 `GET /api/navigations/{id}` 로 다시 조회
5. "내 분석 삭제": 확인 모달 → `DELETE /api/navigations/{id}` → 첫 화면 이동