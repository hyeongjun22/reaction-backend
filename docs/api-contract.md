# re:action API Contract v0.7

> 진실 소스. 모든 endpoint 변경은 이 문서 PR과 동반된다.
> 기준 문서: `Reaction_DB_설계서_v0.7.1` + `Reaction_DevBaseline_v1.0_2026-05-15`
> 응답·에러·Idempotency·시간 규약은 [ADR-0002](decisions/0002-api-contract-freeze.md) 로 동결됨.
> 변경 이력은 [`api-change-log.md`](api-change-log.md). 이전 swagger.yaml v0.2.0은 **폐기**.

---

## 1. 응답 규약

### 1.1 base URL

| 환경 | URL |
| --- | --- |
| local | `http://localhost:8000` |
| compose | `http://reaction-backend:8000` |
| staging | TBD |
| production | TBD |

### 1.2 성공 응답 형태

성공 응답은 **envelope 없이 도메인 객체를 직접** 반환한다 (OpenAPI 친화 + 클라이언트 단순).

```json
{ "goalId": "goal_abc", "title": "캡스톤", "tier": "FOCUS", ... }
```

### 1.3 에러 응답 형태 (4xx / 5xx)

```json
{
  "code": "INTERVIEW_SLOT_LOCKED",
  "message": "이미 종료된 세션의 슬롯은 수정할 수 없어요.",
  "field": null,
  "server_time": "2026-05-21T01:23:45.678+09:00"
}
```

- `code` — 도메인 prefix UPPER_SNAKE_CASE
- 표준 HTTP status code 매핑: 400 / 401 / 403 / 404 / 409 / 422 / 500
- `message` — **화면에 그대로 띄울 수 있는 한국어**(v2.30-auth). 요청 검증 422
  (`COMMON_VALIDATION_ERROR`)도 pydantic 영어 원문 대신 종류별 한국어("꼭 필요한 항목이
  빠졌어요.", "200자까지 입력할 수 있어요. 조금 줄여 주세요." 등)이고, 어느 입력인지는 `field`
  로 알린다. 없는 경로·메서드(404/405)도 한국어. 분기는 `message` 가 아니라 `code` 로 할 것.
  `field` 는 **요청에 실제로 있는 필드 경로**이거나 `null` 이다 — 본문이 JSON 으로 읽히지도
  않으면(`{not json`) 가리킬 필드가 없으므로 `null` 이다(v2.30 — 예전엔 pydantic 의 문자 위치가
  `field: "1"` 로 나갔다). 배열 원소는 `daysOfWeek.0` 처럼 경로 그대로.
  DB 컬럼 길이를 넘는 문자열은 스키마에서 못 막았더라도 500 이 아니라 422
  `COMMON_VALIDATION_ERROR`("입력한 내용이 너무 길어요. 조금 줄여 주세요.")다.
- 500 `COMMON_INTERNAL_ERROR` 에도 CORS 헤더와 `x-request-id` 가 붙는다(v2.30-auth) —
  크로스오리진 네이티브 앱도 네트워크 오류가 아니라 이 envelope 을 받는다.

### 1.4 에러 코드 도메인 prefix

| prefix | 도메인 |
| --- | --- |
| `AUTH_*` | 인증/세션 |
| `USER_*` | 사용자 |
| `ONBOARDING_*` | 온보딩 상태머신 |
| `INTERVIEW_*` | 딥 인터뷰 |
| `POLICY_*` | 시간 정책 / 정책 스냅샷 |
| `GOAL_*` / `HABIT_*` | 목표/습관 |
| `PLAN_*` | 계획 생성/편집 |
| `CALENDAR_*` | Google Calendar |
| `EXEC_*` | 실행/체크인 |
| `REFLECT_*` | 회고 |
| `RECOVERY_*` | 회복 옵션 |
| `REVIEW_*` | 주간 리뷰 |
| `NOTIF_*` | 알림 |
| `INBOX_*` | Life Inbox |
| `FIXED_SCHEDULE_*` | 고정 일정 |
| `LLM_*` | LLM 호출 (timeout, fallback used 등) |
| `RATE_LIMIT_*` | 비싼 엔드포인트 사용량 상한 (§1.10, #325) |
| `AGENT_*` | Agent 동시성 (advisory lock 미획득 등, ADR-0005 §7.6) |
| `IDEMPOTENCY_*` | 멱등 키 충돌 |
| `COMMON_*` | 공통 (검증 실패·미구현·내부 오류) |

### 1.5 시간 / 타임존

- 응답 시간 필드는 **KST(+09:00) ISO 8601 with offset**
- 날짜만은 `YYYY-MM-DD` (`target_date` 등)
- 서버 내부 저장은 UTC

### 1.6 인증

- Google OAuth 후 자체 JWT (`Authorization: Bearer <access_token>`)
- access TTL: 60분, refresh TTL: 14일 (default, 후속 결정 가능)
- `AUTH_INVALID_TOKEN` / `AUTH_TOKEN_EXPIRED` 로 401 분기

### 1.7 Idempotency

다음 endpoint는 **`Idempotency-Key` 헤더 필수** (24h 보장):

- `POST /reflection/batch`
- `POST /recovery/decisions`
- `POST /replan/{execution_id}/approve` (recovery, S20 최종 적용 — §12)
- `POST /plans/replan/{plan_id}/approve` (planning, 주간 재계획 승인 — §11)
- `POST /calendar/events/approve-insert`
- `POST /reviews/habit-penalty/{habit_id}/accept`

같은 key 재호출 → 캐시된 응답 그대로. `IDEMPOTENCY_KEY_MISMATCH` 시 409.

캐시는 **(호출자, endpoint, key)** 로 스코프된다 (DB 설계의 `UNIQUE(user_id, endpoint, key)`
와 정렬). 키 값만으로 캐싱하면 body 없는 endpoint(`/replan/{id}/approve`)는 모든 호출의
body 해시가 같아 mismatch 409 로도 안 걸러지고, 다른 사용자의 응답이 그대로 재생된다.
→ **FE 는 키를 사용자·대상 스코프로 만들 것** (`replan-{executionId}` 형태. `Date.now()`
같은 전역 타임스탬프는 충돌한다).

### 1.8 ID 표기

- 문자열, 도메인 prefix 권장: `user_*`, `goal_*`, `action_*`, `block_*`, `exec_*`, `interview_*`, `recovery_*`, `policy_*`, `inbox_*` …

### 1.9 필드 네이밍

- 응답 도메인 객체 필드는 **camelCase** (`goalId`, `ambiguityScore`, `weekStart` …)
- `ErrorResponse`(§1.3) · `HealthResponse`(§17) 등 공통 메타 응답은 정의된 필드명을 그대로 사용 (`server_time` 등)

### 1.10 LLM 비용/사용량 상한 (#325)

두 축이 서로 다른 문제를 막는다 — 헷갈리지 말 것:

- **전역 일일 토큰 예산**(`LLM_GLOBAL_DAILY_TOKEN_BUDGET`) — 전 사용자 합산 토큰이 한도를
  넘으면, 그 시점부터 신규 LLM 호출은 **조용히 룰 기반 폴백으로 전환**된다(200 OK,
  `aiSource="rule"`). 인터뷰·계획·만다라트·회복 **전부** 이 동작 — 명시적 에러를 내지
  않는다. 이미 모든 Draft 응답에 `isDraft`+`aiSource` 가 실려 있어(Draft Layer) FE 가
  "이번 결과는 AI 가 아니라 룰로 나왔다"를 판별할 수 있으므로 별도 신호가 불필요하다.
  예산 검사는 **이번 호출의 프롬프트 크기(추정 토큰)를 미리 더해서** 본다 — 긴 입력 한 번이
  한도를 통째로 넘기지 못한다. 호출 1회 프롬프트가 `LLM_MAX_PROMPT_CHARS`(기본 60,000자)를
  넘어도 같은 방식(룰 폴백)으로 처리한다(v2.30-llm).
- **엔드포인트별 사용자 일일 요청 횟수 상한**(`LLM_ENDPOINT_DAILY_CALL_LIMIT`, 대상:
  인터뷰/계획·만다라트/회복) — 이건 폴백으로 대신하지 않는다. 한 사용자가 같은 기능을
  하루에 너무 많이 눌러 반복 재생성하는 상황을 막는 것이라, 요청 자체를 **429
  `RATE_LIMIT_DAILY_CALLS_EXCEEDED`** 로 명시 거절한다. 응답에 `Retry-After`
  헤더(초, 다음 KST 자정까지 남은 시간)를 싣는다 — FE 는 이 값으로 "언제 다시 가능한지"
  안내한다. 두 한도 모두 0 이면 무제한.

  세는 단위는 **요청 1건**이다 (#370). 엔드포인트 하나가 내부적으로 LLM 을 몇 번 부르든
  (인터뷰 턴 2~3회, 계획 생성 최대 7회) 상한에는 1 로 잡힌다. **인터뷰는 별도 상한**
  (`LLM_ENDPOINT_DAILY_CALL_LIMIT_INTERVIEW`, 기본 60) — 계획 인터뷰는 필수 슬롯이 18개라
  완주 한 번에 요청이 19건 이상 들기 때문이다.

  계획·만다라트 상한(`planning`)은 **상한을 거는 엔드포인트의 요청만** 센다(v2.30-llm) —
  `/plans/generate`·`/plans/mandala/subgoals`·`/plans/mandala/generate`·
  `/plans/mandala/next-cycle`·`/plans/replan`. 상한이 없는 `/plans/milestones`·
  `/plans/materials/search`(그라운딩 예산이 따로 있다)·`/plans/materials/study-method`·
  `/plans/mandala/{planId}/regenerate-branch` 는 이 계수에 들어가지 않는다. 429 메시지는
  "오늘 준비된 {기능} 횟수를 다 썼어요. 내일 다시 열려요." (코드·헤더 무변경).

  모든 응답에 서버가 생성한 **`X-Request-ID`** 헤더가 실린다. 문의·장애 추적용 식별자이며,
  요청 헤더로 보낸 값은 **무시된다**(이 값이 위 상한의 계수 키라서 클라이언트가 고르면
  상한을 우회할 수 있다).

### 1.11 AI 출력 톤 필터와 사용자 문구 (v2.30-llm)

금지어 치환(DevBaseline §4.2 잠금)과 톤 게이트(사람 귀인·자존감 부양 → 룰 폴백)는 **AI 가 쓴
문장**에 건다. 사용자 문구는 거치지 않는다(§12 `edited` 결정과 같은 원칙). AI 응답이 사용자
문구를 **그대로 옮겨 쓴 자리**도 사용자 문구로 본다 — 인터뷰 답 정규화 값, 목표 트리
root·branch 제목, 룰 폴백의 `"{목표 제목} N회차"`, 만다라 축 제목을 딴 칸 등.

- '사용자 입력'은 그 호출의 프롬프트 변수 중 **사용자가 직접 쓴 것만**이다 — 목표 제목·성공
  이미지·현재 수준·접근 방식, 인터뷰 답, 인박스 원문, 검색어, 궁극목표 슬롯, 사용자가
  편집·잠근 만다라 축·칸 제목. **서버가 만들거나 가져온 값은 아니다**: 특히 참고 자료
  본문(`materials`)은 사용자가 붙여넣은 링크를 서버가 열어 가져온 **제3자 웹페이지 본문**을
  담을 수 있어 제외한다. 거기서 베껴 온 문장은 금지어 치환·톤 게이트를 그대로 통과하지
  못한다. 요약·JSON·숫자 같은 파생 값도 제외한다.
- 판정 기준: 그 AI 호출에 들어간 사용자 입력(답·목표 제목·축 제목 등)과 **연속 두 어절
  이상**이 글자 그대로 같거나(금지어 외 4자 이상 더 겹칠 때), 입력 하나 **전체**를 옮겨 쓴 경우.
- AI 가 스스로 쓴 금지어·톤 마커는 사용자 입력이 옆에 있어도 **전과 똑같이** 치환·거절된다
  (예: 목표가 '포기하지 않고 영어 회화 끝내기' 여도 AI 가 쓴 '포기하지 마세요' 는 치환).
- 응답 스키마 무변경. 예전엔 '포기하지 않는 창업가' 가 '잠깐 쉬어가는하지 않는 창업가' 로
  저장되거나, 'UX 디자이너가…'·'자격증 네가지…'·'똑똑하게 돈 관리하기' 목표의 AI 계획이 매번
  버려지고 `aiSource="rule"` 자리표시자 계획이 나왔다. 이미 깨져 저장된 값은 그대로 남는다.

---

## 2. Auth (`/auth`)

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/auth/google` | Google id_token → 자체 JWT (access+refresh) 발급. **신규 가입만** 가입 게이트(#324) 대상 |
| POST | `/auth/refresh` | refresh → 새 access |
| POST | `/auth/logout` | refresh 무효화 |
| GET | `/auth/me` | 현재 사용자 (`onboarding_state` 포함). `toneMode` 는 `gentle｜strict｜encouraging｜null` — **아직 톤을 고르지 않았으면 null**(v2.30-auth2, `GET /settings` 와 같은 값). 그전에는 이 경로만 빈 문자열이었다. 필드는 그대로 있다 |

**가입 게이트(#324, FE #237 §8)** — `POST /auth/google` 요청에 선택 필드 `inviteCode` 가
추가된다. **기존 사용자 로그인(요청의 email 이 이미 `users` 에 있음)은 이 게이트를 전혀
거치지 않는다** — `inviteCode` 를 생략하거나 무효값을 보내도 영향 없다. 신규 가입(새
email)에만 순서대로 3중 검사가 적용된다:

1. `SIGNUPS_ENABLED=false`(긴급 차단, 재배포 없이 토글) → 403 `AUTH_SIGNUPS_DISABLED`.
2. 누적 가입 인원이 `SIGNUP_CAPACITY` 에 도달 → 403 `AUTH_SIGNUP_CAPACITY_REACHED`.
   **미설정이면 상한 없음(v2.29 기본값)**.
3. `SIGNUP_INVITE_REQUIRED=true` 일 때만: `inviteCode` 미제공/무효 → 422
   `AUTH_INVALID_INVITE_CODE`. 이미 소진된 코드 → 409 `AUTH_INVITE_CODE_ALREADY_USED`.
   **기본은 꺼져 있다(v2.29)** — 이때 `inviteCode` 는 보내도 무시된다(검증·소비 안 함).

즉 v2.29 기본 설정에서는 **Google 계정이면 누구나 가입된다**. 2·3번 에러 코드는 운영자가
설정을 다시 켰을 때만 나온다.

코드는 대소문자·앞뒤 공백 무관하게 정규화해 비교한다. 유효한 코드는 그 가입에서 **1회만**
소비되며(재사용 불가), `scripts/manage_invite_codes.py` 로 운영자가 미리 발급한다(admin
API 없음 — 이 레포의 다른 운영 작업과 같은 CLI 스크립트 관례).

**Google 공개키 조회 실패(v2.30-auth)** — `POST /auth/google` 이 id_token 서명을 확인하려고
Google 공개키를 가져오다 실패·지연(5초 상한)하면 503 `COMMON_INTERNAL_ERROR`("잠시 후 다시
시도해 주세요"). 토큰 자체가 틀린 경우(401 `AUTH_INVALID_ID_TOKEN`)와 구분된다.

**계정 삭제 후 refresh 차단(#321)** — `POST /auth/refresh` 는 이제 `decoded.user_id` 로
사용자 존재를 조회하고, soft-delete(`archived_at` set, §16 `/settings/delete-account`)
된 계정이면 401 `AUTH_INVALID_TOKEN`. 이전에는 jti revoke set 여부만 확인해, 계정을
삭제해도 삭제 전 발급된 refresh token 으로 계속 새 access 를 받을 수 있었다.

**refresh token httpOnly 쿠키(#323)** — 웹은 새로고침하면 메모리에만 두던 refresh
token 이 사라져 60분마다 재로그인해야 했다(XSS 노출을 우려해 localStorage 에 안
뒀기 때문). 이제 `POST /auth/google` 이 `refreshToken` 을 응답 본문(그대로 유지)과
`reaction_refresh` httpOnly 쿠키로 **둘 다** 내려준다:

```
Set-Cookie: reaction_refresh=<token>; HttpOnly; Path=/auth; SameSite=Lax; Max-Age=1209599[; Secure]
Set-Cookie: reaction_refresh=<token>; HttpOnly; Path=/api/auth; SameSite=Lax; Max-Age=1209599[; Secure]
```

- **같은 쿠키를 두 경로에 심는다(v2.28).** 웹은 Vercel rewrite(`/api/:path*` → 백엔드)를 거쳐
  브라우저 주소가 `/api/auth/refresh` 다. 예전엔 `Path=/auth` 하나라 브라우저가 그 요청에 쿠키를
  **싣지 않았다** — 쿠키 폴백이 배포 환경에서 동작한 적이 없다. 직접 접속은 `/auth`, 프록시 경유는
  `/api/auth` 쿠키가 실린다. 경로 목록은 `REFRESH_COOKIE_PATHS`(기본 `["/auth","/api/auth"]`).
  로그아웃은 두 경로 모두 지운다.
- **웹 FE 가 할 일**: 메모리에 refresh token 이 없어도(새로고침 뒤) 401 이면 `POST /auth/refresh`
  를 **빈 본문 `{}`** 으로 호출한다(same-origin 이라 쿠키는 브라우저가 싣는다). 로그아웃도 본문
  없이 `POST /auth/logout` 을 불러야 쿠키가 지워진다.

- `Secure` 는 `APP_ENV≠local` 일 때만 붙는다(로컬 개발은 http 라 Secure 쿠키가 아예
  전송 안 됨).
- `POST /auth/refresh`·`POST /auth/logout` 은 **본문 우선, 없으면 쿠키로 폴백** — 요청
  본문에 `refreshToken` 을 아예 생략해도(빈 객체 `{}`) 쿠키가 있으면 동작한다. 이제
  `refreshToken` 은 두 요청 스키마 모두에서 **선택**(하위호환 — 기존처럼 본문에 실어
  보내는 클라이언트는 무변경 동작).
- 본문·쿠키 **둘 다** 없으면 401 `AUTH_INVALID_TOKEN`.
- `logout` 은 성공/실패 무관하게 항상 쿠키를 지운다(`Max-Age=0`) — 브라우저 쪽 정리가
  목적이라 토큰 유효성과 별개.
- **네이티브 앱(capacitor://localhost)은 쿠키를 쓰지 않는다** — 크로스오리진이라
  `SameSite=None` 이 필요한데, 이미 OS Keystore 로 안전한 저장소가 있어 그 CSRF 노출을
  감수할 이유가 없다(이슈가 명시한 "단순한 쪽"). 계속 본문의 `refreshToken` 을 읽어
  Keystore 에 저장한다 — 이번 변경으로 아무것도 안 바뀐다.
- CORS: `allow_credentials=True` + 명시적 origin 화이트리스트(`CORS_ALLOW_ORIGINS`)는
  이미 설정돼 있었다(§1.1 base URL 별 CORS 설정) — 이번 PR 에서 변경 없음.

---

## 3. Onboarding (`/onboarding`)

상태머신:
```
WELCOME → ONBOARDING_INTERVIEW → ONBOARDING_CONFIRM
       → ONBOARDING_CALENDAR ⇄ ONBOARDING_MANUAL_SCHEDULE
       → ONBOARDING_POLICIES → ONBOARDING_FIRST_PLAN
       → ONBOARDING_NOTIFICATIONS → ACTIVE
```

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/onboarding/status` | `{ currentState, suggestedNextScreen }` |

`suggestedNextScreen` 매핑 (DevBaseline §5 화면 흐름):

| `onboarding_state` | 다음 화면 |
| --- | --- |
| WELCOME · ONBOARDING_INTERVIEW | S02 |
| ONBOARDING_CONFIRM | S03 |
| ONBOARDING_CALENDAR | S04 |
| ONBOARDING_MANUAL_SCHEDULE | S05 |
| ONBOARDING_POLICIES | S07 |
| ONBOARDING_FIRST_PLAN | S06 |
| ONBOARDING_NOTIFICATIONS | S08 |
| ACTIVE | S10 |

진행 자체는 각 도메인 라우터가 자기 단계 완료 시 `users.onboarding_state` 를 전이.

`users.onboarding_state` 자동 전이 트리거 (Issue #17 실구현):

| 트리거 endpoint | from | to |
| --- | --- | --- |
| 계획 인터뷰 종료 — `POST /interview/sessions/{id}/answers`(마지막 답)·`/finish`·`/next-question`·`POST /interview/sessions`(시드로 마감), **목표가 1개 이상 저장됐을 때만** (v2.30-interview) | `WELCOME` / `ONBOARDING_INTERVIEW` | `ONBOARDING_CONFIRM` |
| `POST /fixed-schedules` | `ONBOARDING_CALENDAR` / `ONBOARDING_MANUAL_SCHEDULE` | `ONBOARDING_POLICIES` |
| `POST /time-policies` | `ONBOARDING_POLICIES` | `ONBOARDING_FIRST_PLAN` |
| `POST /plans/{planId}/approve` | 온보딩 단계 전체 (`WELCOME` … `ONBOARDING_NOTIFICATIONS`) | `ACTIVE` |
| `PATCH /notifications/settings` | `ONBOARDING_NOTIFICATIONS` | `ACTIVE` |

각 트리거는 `expected_from` 에 해당할 때만 전이 (멱등). 이미 더 진행된 상태(예: `ACTIVE`)면 no-op — 같은 endpoint 두 번 호출해도 안전. **첫 계획 승인(`POST /plans/{planId}/approve`)은 온보딩 완료 신호로 보고 어느 온보딩 단계에서든 곧바로 `ACTIVE` 로 마감한다** — 실제 FE 흐름에서 상류 단계 전이(`WELCOME`→…)가 항상 트리거되지 않아 `onboarding_state` 가 `WELCOME` 에 고정되면, 완료 후에도 새로고침 시 재-온보딩되고 계획이 중복 누적되던 문제를 막기 위함(원설계는 `ONBOARDING_FIRST_PLAN → ONBOARDING_NOTIFICATIONS`, #32/Issue #17).

---

## 4. Interview (`/interview`) — S02 딥 인터뷰 / S29 궁극목표 인터뷰

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/interview/sessions` | 신규 세션 + FSM 첫 질문. 본문 `{ kind? }` (`"plan"` 생략 시 기본, `"ultimate"` 궁극목표). `sessionId` 는 UUID. 이월 시드만으로 필수 슬롯이 다 차 물을 게 없으면 빈 질문 대신 **곧바로 종료 응답**(`endReason=completed` + `summary`·`outcome`/`ultimateOutcome`)을 201 로 준다 (v2.30-interview) |
| GET | `/interview/sessions/{id}` | 진행 상태 — `ambiguityScore`, `totalTurns`, `currentQuestion`. 종료 세션이면 `kind` 에 따라 `outcome` 또는 `ultimateOutcome` 동봉 |
| POST | `/interview/sessions/{id}/answers` | 슬롯 답 UPSERT — `{ slotKey, value, clientTurn }`. **`clientTurn` 은 직전 응답의 `totalTurns` 를 그대로** 보낸다 — 세션의 현재 턴과 다르면(이미 처리된 답의 재전송·다른 기기·두 탭) 409 `INTERVIEW_STALE_TURN`, 채점·질문 생성(LLM)을 하지 않고 턴도 진행하지 않는다. 세션 kind 의 슬롯 카탈로그에 없는 `slotKey` 는 422 `COMMON_VALIDATION_ERROR`(`field: "slotKey"`) (v2.35). `value` 는 **사용자 답 모양만** — 칩 `{type:"chip",values:[…]}`·텍스트 `{type:"text",raw}`·구간 `{type:"range",start,end}` 와 그 약식(문자열 = 텍스트, 목록 = 칩, `{start,end}` = 구간). 그 밖의 모양(`pending`·`skip`·`spec` 같은 서버 전용 표시, `type` 없는 dict 등)은 422 `COMMON_VALIDATION_ERROR`(`field: "value"`) (v2.36). 종료 시 `summary`+`outcome`/`ultimateOutcome`. 한 답에서 다른 슬롯을 함께 채우는 수확은 **계획 전체를 가두는 값에는 쓰지 않는다** — `time.activity_window`(활동창)·`goals.weekly_time`(유도 슬롯)은 물어서만 채운다 (v2.30-interview). 고정 보기가 있는 칩·select 에 **보기로 맞출 수 없는 답**(직접 입력)이 오면 그 슬롯은 닫히지 않고 같은 `slotKey` 질문이 한 번 더 온다 — 길이 보기 슬롯은 보기와 같은 길이일 때만 받고(표기 차이는 허용) 가까운 보기로 반올림하지 않는다 (v2.30-interview) |
| POST | `/interview/sessions/{id}/next-question` | 현재 슬롯 질문 재생성 (resume용, LLM 호출 — 시작·답 제출과 같은 사용자 톤으로 만든다, v2.35). 답 제출 밖에서 마지막 빈 슬롯이 채워진 세션(예: `POST /plans/materials/spec-confirm` 이 `goals.materials` 를 씀)이면 질문 대신 `answers` 의 마지막 답과 **같은 종료 응답**(`endReason=completed` + `summary`·`outcome`/`ultimateOutcome`)을 준다 — 목표 영속·프로필 영속도 같다. 이미 종료된 세션이면 종료 응답 재조회 (v2.30-interview) |
| POST | `/interview/sessions/{id}/finish` | 조기 종료 `[충분해요]` → `endReason=early_user` + `outcome`/`ultimateOutcome` |
| GET | `/interview/slot-catalog?kind=plan\|ultimate` | 슬롯 카탈로그 — `slotKey·label·answerType·isRequired·category·options·multiple`. `kind` 쿼리 생략 시 `plan`(하위호환). `multiple`(additive, v2.30-interview)은 보기를 여러 개 골라도 **전부 쓰이는** 슬롯만 true(`time.peak_window`·`ultimate.values`) — 나머지 칩·select 는 첫 값만 쓰인다. ⚠️ **`goals.why_now` 는 v2.12 에서 빠졌다** — 묻지도 하베스팅하지도 않아 계획·마일스톤 프롬프트가 영원히 빈 값을 받고 있었다 |

응답 예: `GET /interview/sessions/{id}` (kind="plan")
```json
{
  "sessionId": "interview_01",
  "ambiguityScore": 3,
  "totalTurns": 5,
  "endReason": null,
  "currentQuestion": {
    "slotKey": "goals.deadlines",
    "text": "마감일이 정해진 게 있어요?",
    "answerType": "date_picker",
    "options": [],
    "multiple": false
  },
  "summary": null,
  "outcome": null,
  "ultimateOutcome": null
}
```

- `ambiguityScore`(int) = **남은 미해결 필수 슬롯 수** (진행될수록 감소, **정상 종료 시 항상 0**). `kind` 에 따라 분모가 다르다 — `plan` 최대 18개, `ultimate` 9개. **다른 답에서 유도돼 묻지 않는 슬롯은 세지 않는다**: `goals.weekly_time`(주당 시간)은 `goals.session_length × goals.frequency` 로 계산되므로 그 둘을 답하면 질문도 안 나가고 분모에서도 빠져 `plan` 이 17개가 된다(빈도를 '몰아서 · 상관없음' 으로 답해 계산이 안 될 때만 18개). FSM 이 묻지 않는 슬롯을 세면 사용자가 채울 방법이 없어 진행바가 100%에 닿지 못한다 (v1.70).
- `currentQuestion.options` = chip/select 보기 (카탈로그 기반). `goals.heaviest`(`kind="plan"` 전용) 는 두 출처를 합쳐 동적 생성한다(ADR-0008 §8 "B"): ① 방금 답한 `goals.list` ② 사용자가 만다라 축에서 승격해 둔 목표(`GET /goals` 의 `promotedFromAxis` 카드들의 실제 `title`) — 먼저 두고, `goals.list` 응답과 겹치는 제목은 한 번만 남긴다. 승격만 해 두고 `goals.list` 에 다시 타이핑하지 않아도 이번 학기 목표로 바로 고를 수 있다. `goals.heaviest` 로 고른 제목이 `goals.list` 에 없었더라도 `core_goals`(→ `materialize_goals`)에 자동 포함된다 — 안 그러면 마감·주당시간 같은 heaviest 전용 필드가 title 매칭 실패로 유실된다. text/date/range 는 `[]`.
- `currentQuestion.multiple`(bool, additive, v2.30-interview) = 보기를 여러 개 담아도 전부 쓰이는가(카탈로그의 `multiple` 과 같다). **false 면 단일 선택**으로 받아야 한다 — 서버는 첫 값만 쓴다(역할·가장 무거운 목표·빈도·세션 길이·톤 등). 두 번째 선택이 말없이 버려지지 않게 FE 는 false 인 질문에서 "여러 개 골라도 돼요" 안내를 빼고 단일 선택으로 토글한다.
- 종료 턴(`endReason` 채워지고 `currentQuestion=null`)에는 `summary`(확인 카드) + `outcome`(`kind="plan"`, First Plan 시드) **또는** `ultimateOutcome`(`kind="ultimate"`, 만다라 시드)이 채워진다 — `kind` 별로 **정확히 하나만** 채워지고 나머지는 `null`. `outcome` 을 union 으로 바꾸지 않아 기존 FE 계획 인터뷰 타입은 무변경.
- ⚠️ **자리표시자 목표** — `outcome.coreGoals` 는 최소 1개가 계약이라, 목표를 답하지 않고 끝나면(`[충분해요]` 를 목표 질문 전에 누르거나, 목표 질문에 세 번 '모르겠어요' 로 답해 `unresolvedSlots` 에 `goals.list` 가 남은 경우) `{ "title": "(미입력 목표)", "confidence": 0.0 }` 한 개가 실린다. 이 항목은 **목표가 아니다** — 영속되지 않아 `GET /goals` 에 없고, 계획 승인도 건너뛴다(#88). FE 는 `confidence === 0 && title === "(미입력 목표)"` 인 후보를 목표 카드로 그리지 말고, 남는 게 없으면 인터뷰로 돌아가게 안내한다 (v2.30-interview 문서화).
- `outcome.unresolvedSlots`·`ultimateOutcome.unresolvedSlots` = **사용자가 값을 주지 않은 필수 슬롯** — 아직 안 물은 슬롯 + "없어요·모르겠어요"·빈 답·보기 밖 답(상한까지) 등으로 **스킵된** 슬롯이다. 둘 다 outcome 이 기본값으로 채운 칸이다. 그래서 정상 종료(`endReason=completed`)여도 비지 않을 수 있다. 진행바(`ambiguityScore`)는 **다시 물을** 슬롯만 세므로 스킵된 슬롯을 세지 않는다(100% 에서 되돌아가지 않는다). 이 목록의 슬롯은 프로필(설정의 '인터뷰에서 파악한 값')에 저장되지 않고, 재인터뷰에서 다시 묻는다 (v2.36, #499).
- 단일 활성 세션 + **재시작 승리(restart-wins)**: `POST /interview/sessions` 는 진행 중(`endReason=null`) **같은 kind** 세션이 있으면 그 세션을 `endReason=abandoned` 로 닫고 새 세션을 만든다 — **항상 201**. 다른 kind 의 진행 중 세션은 건드리지 않는다(계획 인터뷰와 궁극목표 인터뷰는 독립적으로 동시에 진행 가능). 이어하기는 저장해 둔 sessionId 로 `next-question` 재개. (v1.12 이전의 409 `INTERVIEW_SESSION_EXISTS` 는 클라이언트가 sessionId 를 잃으면 복구 불가라 폐기 — 코드 자체는 하위호환 위해 enum 에 유지.)
- 동시성 lock(ADR-0005 §7.6): 모든 mutating 진입점(`sessions`·`answers`·`next-question`·`finish`)은 `user_id × interview:{kind}` advisory lock 으로 보호(kind 별 독립 락) — 다른 디바이스가 **같은 kind** 를 점유 중이면 409 `AGENT_CONCURRENT_ACCESS` 즉시 fail.
- `kind` 값이 `"plan"`/`"ultimate"` 밖이면 요청 자체가 422 `COMMON_VALIDATION_ERROR` — 텍스트 슬롯으로 조용히 폴백하지 않는다.
- 궁극목표 인터뷰(S29, 필수 9슬롯: `ultimate.statement`·`domain`·`horizon`·`measure`·`success_image`·`identity`·`current_position`·`pillars_hint`·`constraints`, 선택 3슬롯: `values`·`assets`·`role_model`)는 계획 인터뷰와 **양방향 이월**된다: 계획 인터뷰의 `identity.*`/`recovery.*` 등은 궁극목표 인터뷰 시작 시드로(단 학기 중/방학 `identity.season` 은 학기마다 바뀌어 **이월하지 않고 다시 묻는다**, v2.30-interview), 궁극목표 인터뷰의 `ultimate.*` 전량은 계획 인터뷰 시작 시드로 회수된다 — 단 `goals.list` 같은 **다른 슬롯을 자동으로 채우지는 않는다**(그 목표는 사용자가 직접 고른다). ⚠️ **궁극목표 → 궁극목표로는 이월하지 않는다**(v2.30-interview) — 궁극목표 인터뷰를 다시 열면 `ultimate.statement` 부터 다시 묻는다(`ambiguityScore=9`). 예전엔 지난 답 전부가 이월돼 시작부터 필수 슬롯이 다 찼고, `currentQuestion=null`·`endReason=null` 로 멈춰 다시 세울 길이 없었다.
- 궁극목표 세션 완료는 계획 목표 영속 경로(`materialize_goals`/`supersede_proposed_goals`)를 타지 않는다 — 직전 계획 인터뷰의 잠정 목표가 지워지지 않는다.
- 구현 상태(#6, #6-B): 엔진+영속화 배선 + 단일 활성 세션(restart-wins, kind 별) + 동시성 lock(kind 별) + 궁극목표 인터뷰(kind="ultimate") 완료. **후속**: 재조립 시 transient 상태(stall_count·used_fallback) 영속. `POST /goals/ultimate`(U1, `UltimateGoalOutcome` → `Goal` 영속)는 `goal_nodes.tree_kind` 도입(§6 후속 PR)과 함께 배선된다.
- `answers`/`next-question`(LLM 호출) 은 사용자별 일일 호출 상한 대상 — 초과 시 429
  `RATE_LIMIT_DAILY_CALLS_EXCEEDED`(§1.10, #325). **`POST /interview/sessions`(시작)도 같은
  상한으로 막는다**(v2.30-interview) — 첫 질문 생성도 LLM 을 불러 같은 한도에 잡히는데, 시작만
  열려 있어 한도에 닿은 사용자가 첫 질문을 받고 그 뒤 모든 답이 429 로 실패했다. 거절된
  시작은 세션을 만들지도, 진행 중 세션을 닫지도 않는다. `finish` 는 막지 않는다(끝내기는
  언제나 가능해야 한다).

---

## 5. Time Policies (`/time-policies`) — S07

`policy_type` 별 discriminated payload.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/time-policies` | 내 활성 정책 전체 |
| POST | `/time-policies` | 신규 정책. payload는 type별 다름 |
| POST | `/time-policies/prefill-from-interview` | S07 진입 시 정책 prefill 후보(DB 미저장). ⚠️ **이름과 달리 인터뷰 답을 반영하지 않는다** — 언제나 `sleep` 23:00–07:00 · `break_min` 15 · `late_night_block` 22:00~ 세 개다(v2.30 문서 정정 — 찾던 슬롯 키가 카탈로그에 없어 처음부터 그랬다). 활동 시간대로 개인화하는 건 후속 결정 |
| PATCH | `/time-policies/{id}` | 부분 수정 |
| DELETE | `/time-policies/{id}` | soft delete (`is_active=false`) |

`policy_type`: `sleep` (1개 필수) / `lunch` / `break_min` / `no_touch` / `late_night_block` / `custom`

---

## 6. Goals (`/goals`) — S26, S31 만다라트 상시 뷰, S32 셀 상세

> ⚠️ **인터뷰·계획이 만드는 제목은 저장 한도에서 잘린다**(v2.30-planB) — 목표·단계·마일스톤
> 제목 200자, 카드 제목 300자를 넘으면 낱말 경계에서 자르고 `…` 를 붙여 저장한다. 그 값이
> 그대로 이 절의 응답에 실린다. 한도 이하 제목은 손대지 않으며, 사용자는 `PATCH /goals/{id}`
> 로 언제든 고칠 수 있다. 예전엔 긴 답을 그대로 넣다 인터뷰 마지막 턴·계획 승인이 500 이 됐다.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/goals` | tier별 그룹 (`focus`/`maintain`/`parked`). **잠정 목표(`status="proposed"`)도 포함**해 내려간다 — 인터뷰를 마치면 목표가 보여야 하므로(#96). FE 는 배지 등으로 구분 표시. **PR7**: 각 카드에 `isUltimate`(궁극목표 진입점 배지용)와 `promotedFromAxis`(만다라 축에서 승격된 목표면 그 축 제목, 아니면 `null` — 축 배지용)를 함께 실어, 카드마다 `GET /goals/{id}/mandala` 를 따로 부르는 N+1 을 피한다 |
| POST | `/goals` | 신규(`status="active"` 로 생성). Focus ≤ 3 / Maintain ≤ 5 (초과 시 422 `GOAL_TIER_LIMIT_EXCEEDED`). Parked 한도 X. **한도 계산에서 `proposed` 와 `completed` 는 세지 않는다**(v1.91) — 아직 하기로 한 목표가 아니므로. **한도를 거는 모든 쓰기(추가·tier 변경·완료 되돌리기·축 승격·`/plans/mandala/next-cycle`·인박스 `convert-to-goal`)는 사용자 단위로 직렬화된다**(v2.30-goals) — 두 번 탭한 요청이 같은 개수를 읽고 둘 다 통과하던 경합을 막는다. 뒤 요청은 앞 요청이 끝난 개수로 판정받고(보통 422), 5초 안에 차례가 안 오면 기존 409 `AGENT_CONCURRENT_ACCESS`. 422 문구는 화면 이름으로 나간다 — 예: "집중 목표는 최대 3개까지예요. 하나를 유지·보류로 옮기거나 완료하면 새로 담을 수 있어요." |
| PATCH | `/goals/{id}` | 제목/마감/우선순위/tier/**category**(#326) 변경. tier 변경 시 한도 재검사, category 변경 시 `POST /goals` 와 같은 허용값으로 검증(무효값 422 `COMMON_VALIDATION_ERROR`). 생략한 필드는 기존 값 유지 — 기존 계획/분해 트리·통계는 소급 변경하지 않는다(재인터뷰 제안 여부는 FE 가 저장 성공 응답의 category 를 보고 판단). **`deadline: null`(또는 빈 문자열)을 명시하면 마감 해제**(v2.30-goals — 예전엔 `null` 을 "안 바꿈" 으로 읽어 마감을 지울 수 없었다), 빼면 그대로. 형식이 틀리면 422 `COMMON_VALIDATION_ERROR`(`field="deadline"`, "마감일은 2026-12-31 처럼 연-월-일로 적어 주세요.") |
| GET | `/goals/{id}/nodes` | 이 목표의 **실제 분해 트리** — 계획 승인 시 영속된 `goal_nodes` 를 읽는다(보관된 옛 분해 제외, `depth`→`orderIndex` 정렬). 분해 자체는 First Plan(`planning/goal_decompose` + 마일스톤)이 수행한다. **계획을 아직 승인하지 않은 목표는 `nodes=[]`·`rootNodeId=null`** (404 아님 — 목표는 있고 분해만 없는 정상 상태). ⚠️ 이 자리에 있던 `POST /goals/{id}/decompose` 는 **제거**됐다: 목표와 무관하게 하드코딩된 데모 트리(캡스톤 → 설계/구현/발표)를 돌려주던 mock stub 이었고 FE 가 그걸 화면에 그려, 어떤 목표를 분해해도 같은 캡스톤 단계가 나왔다. **`nodeType: "milestone"`** 인 행이 섞여 나올 수 있다(ADR-0007 PR-2) — `depth=1` 로 `"subgoal"`(이번 4주 분해)과 같은 깊이를 공유하지만 `parentId=null` 이고 매 승인에도 안 바뀐다. FE 는 `nodeType` 으로 걸러 마감까지의 뼈대(마일스톤)와 이번 4주 실행 트리(subgoal/leaf)를 구분해야 한다. 각 노드에 `completedAt`(nullable)이 실린다 — **`nodeType="milestone"` 이 아니면 항상 null** 이다(세션 수행 여부는 `action_items.status` 가 진실 소스라 노드에 복사본을 두지 않는다, ADR-0007 §3). 마일스톤 완료 토글은 아래 `PATCH /goals/{goalId}/nodes/{nodeId}` |
| PATCH | `/goals/{goalId}/nodes/{nodeId}` | **중간 목표(마일스톤) 완료 표시** (v1.90, ADR-0007 §3). body `{ completed: boolean }` → `completedAt` 을 지금(KST)으로 찍거나 `null` 로 되돌린다. 응답은 갱신된 `GoalNode` 한 개. **멱등** — 같은 값을 다시 보내도 200. `completed=false` 로 오조작을 되돌릴 수 있다. ⚠️ **`nodeType="milestone"` 인 계획 트리 노드만 받는다** — core/subgoal/leaf 도, 만다라 칸도 404 `GOAL_NOT_FOUND`(존재 여부를 흘리지 않으려 같은 코드로 묶었다). **leaf 를 열어주지 않는 게 핵심이다**: 세션 수행 여부는 `action_items.status` 가 진실 소스이고, 노드에 두 번째 완료 표시를 두면 그 진실이 갈린다. 마일스톤만 예외인 이유는 롤업으로 표현할 수 없는 판단("세션은 다 했는데 아직 아니다" / "세션은 안 했지만 다른 경로로 달성했다")이 **AI 가 아니라 사용자 몫**이기 때문이다(ADR-0007 §3). 제목·요약은 여기서 못 고친다 — 뼈대 편집은 마일스톤 확인 화면 → `generate` → `approve` 경로 하나로 모여 있다(PR-6a). 만다라 칸 편집은 `PATCH /goals/mandala/nodes/{nodeId}` |
| POST | `/goals/{id}/park` | Focus → Parked |
| POST | `/goals/{goalId}/complete` | **목표 완료 확정** (v1.91, ADR-0007 6b). body `{ completed: boolean }` → `status` 를 `"completed"` 로, `false` 면 `"active"` 로 되돌린다. 응답은 갱신된 `Goal`. **멱등**. ⚠️ **보관(soft delete)이 아니다** — `archivedAt` 을 건드리지 않아 `GET /goals` 목록에 남는다(FE 가 `status` 로 배지를 단다). 끝낸 것과 치운 것은 다른 뜻이다. 완료가 실제로 뜻하는 바 두 가지가 함께 성립한다: **tier 한도(Focus≤3/Maintain≤5)를 더 안 먹고**, **새 계획 후보에서 빠진다**(다음 `generate` 가 이 목표에 트리를 붙이지 않는다). 마일스톤이 다 끝나면 `GET /reviews/weekly` 의 `goalCompletionProposals` 가 제안하지만 **여기에 가드는 없다** — 다른 경로로 달성했거나 방향이 바뀌어 접는 경우가 있고 그 판단은 사용자 몫이다(ADR-0007 §3). ⚠️ **진행 중(`active`)인 목표만 완료할 수 있다** — `proposed`(계획 미승인 잠정 목표)는 422 `COMMON_VALIDATION_ERROR`. 완료→해제 왕복으로 `active` 가 되면 승인 게이트(`/plans/{planId}/approve`)를 우회하고 잠정 목표 만료 cron 대상에서도 빠진다. 이미 완료된 목표에 `completed=true` 를 다시 보내는 건 멱등 no-op. **`completed=false` 는 완료한(`completed`) 목표만 되돌린다** — 그 외(`active`·`proposed`)엔 아무것도 바꾸지 않고 지금 상태를 200 으로 돌려준다(v2.30-goals — 예전엔 `proposed` 가 여기서 `active` 로 나와 계획 승인과 tier 한도를 건너뛰었다). ⚠️ **되돌리기(`completed=false`)는 tier 한도를 다시 검사한다** — 완료하면 한도 집계에서 빠지므로, 안 재면 "완료 → 새 목표 생성 → 완료 해제" 로 Focus≤3 을 넘길 수 있다. 한도가 찼으면 422 `GOAL_TIER_LIMIT_EXCEEDED`. ⚠️ **완료하면 그 목표의 남은 예정 카드와 블록이 정리된다**(v1.92, soft) — `source='goal'` 또는 `source='recovery_*'`·`status='planned'`·미보관 카드를 `archivedAt` 으로, 그 블록을 `block_status='cancelled'` 로. 안 그러면 끝냈다고 확인한 목표가 다음 날 아침 브리프에 그대로 뜬다. **시작·완료·실패한 카드**와 **사용자가 시간을 옮긴(`user_edit`) 블록을 가진 카드**는 보존된다. 직접 만든/인박스에서 온 카드도 대상이 아니다. **만다라 유래 카드와 회복 카드도 함께 멈춘다**(v2.22, #367) — 예전엔 둘 다 빠져서, 카드가 전부 만다라 유래인 **궁극목표는 완료를 눌러도 한 장도 정리되지 않았다**. 정리 함수가 승인 경로의 보호 규칙(계획 재생성이 만다라 73칸과 회복 카드를 쓸어가면 안 된다)을 함께 갖고 있었고, 완료 경로가 그걸 그대로 물려받은 것이다. 지금은 호출부가 축을 고른다 — 승인 경로의 보호는 그대로다. ⚠️ 회복 카드는 **v2.22 이후 생성된 것만** 걸린다: 목표 연결(`goalId`)을 그때부터 부모 카드에서 물려받기 때문이다(그 전 카드는 `goalId` 가 비어 있어 어느 목표 스코프에도 안 걸린다). ⚠️ **되돌려도 카드는 안 돌아온다.** 다시 하려면 **되돌리기 → `/plans/generate` → `approve`** 를 거쳐야 한다(완료 목표는 계획 후보에서 빠지므로 되돌리기가 선행이다). 그 되돌리기가 tier 한도에 걸리면 422 라, Focus 가 꽉 찬 상태에서는 먼저 다른 목표를 정리해야 한다. soft 정리라 데이터는 남는다. 없는/보관된 목표는 404 `GOAL_NOT_FOUND` |
| DELETE | `/goals/{id}` | soft delete (`archivedAt` + `status="archived"`). **그 목표의 남은 예정 카드와 블록도 함께 멈춘다**(v2.30-goals) — `POST /goals/{goalId}/complete` 와 **같은 정리**다: `source='goal'`/`'recovery_*'`·`status='planned'`·미보관 카드는 `archivedAt`, 그 블록은 `block_status='cancelled'`. 시작·완료·실패한 카드와 사용자가 시간을 옮긴(`user_edit`) 블록을 가진 카드는 보존된다. 예전엔 목표 행만 보관해서, 지운 목표의 카드가 오늘 화면·주간 캘린더·아침 브리프·`pre_card` 알림에 계속 떴고 취소도 거절됐다. **궁극목표(`isUltimate=true`)면 만다라도 닫는다** — 살아 있는 만다라 노드를 보관하고, 그 칸에 링크된 반복형 습관(`goalNodeId`)도 보관한다(`GET /habits`·오늘 화면에서 빠지고 새 주 인스턴스도 안 생긴다). 축에서 승격한 목표는 독립 목표라 남는다. 보관된 궁극목표의 칸은 `PATCH`/`promote`/`habit`/`POST /plans/mandala/next-cycle` 모두 404 `GOAL_NOT_FOUND`. 전부 soft 정리이고 되돌리기 endpoint 는 없다 |
| POST | `/goals/ultimate` | **궁극목표 확정**(PR5, S29→S30). 딥 인터뷰(`kind="ultimate"`) 산출물 → `Goal(status="active", goalTier="parked")`. body `{ outcome? }` — 생략하면 서버가 최근 '정상 종료' 궁극목표 인터뷰에서 복구(완료된 인터뷰가 없으면 422 `COMMON_VALIDATION_ERROR`). **사용자당 1개**(`Goal.isUltimate`) — 이미 있으면 같은 행을 갱신(409 없음, 재인터뷰로 다듬는 정상 경로). 응답은 `Goal`(위 스키마 그대로, 201). `category` 는 항상 `"other"`(궁극목표는 여러 카테고리를 가로지르므로 하나로 분류하지 않는다). `GET /goals` 의 parked 그룹에 일반 목표와 섞여 나온다(의도된 동작) — `isUltimate=true` 카드에 FE 가 만다라 진입점 배지를 붙인다(S26, PR7). **`deadline`** 은 인터뷰의 `ultimate.horizon`(3/5/7/10/10년 이상/기한 없음)에서 확정된다(ADR-0008 §2) — 오늘 + N년, "기한 없음"이면 `null`. 재인터뷰로 horizon 을 바꾸면 이 값도 같이 갱신된다. **승격된 학기 목표(U10)는 이 마감을 상속하지 않는다** — `PATCH /goals/{id}` 로 사용자가 따로 정한다 |
| GET | `/goals/{id}/mandala` | **만다라트 상시 뷰**(PR6, S31). `goal.isUltimate=true` 여야(아니면 404). 73노드(≤) + 진척도. **중앙 칸(depth 0) `title` 은 항상 `statement`(= 목표 제목)와 같다**(v2.30-goals) — `PATCH /goals/{id}`·재인터뷰(`POST /goals/ultimate`)로 문장을 고치면 중앙 칸도 함께 바뀌고, 중앙 칸을 `PATCH /goals/mandala/nodes/{id}` 로 고치면 목표 제목도 바뀐다. **아직 승인된 만다라 트리가 없으면 `nodes=[]`·`rootNodeId=null`**(404 아님 — 위 `nodes` endpoint 와 같은 "정상, 그냥 비어 있음" 규약). `progress`/`coverage` 는 컬럼 캐시가 아니라 매 조회 시 파생(leaf 는 `completedAt` 직접체크 우선, 없으면 카드 성공률; 축은 leaf 8개 **고정 분모**로 나눠 "1칸 하고 100%" 착시 방지; 성공 정의는 주간 리포트 adherence 와 동일 상수 재사용) |
| GET | `/goals/{id}/mandala/rebuild-preflight` | **다시 세우기 사전 확인**(v2.05, U13). `goal.isUltimate=true` 여야(아니면 404). 읽기 전용 — LLM 0콜, DB 쓰기 0. "다시 세우기" 버튼이 확인 시트를 띄우기 위한 자료다. 다시 세우기는 새 endpoint 가 아니라 `POST /plans/mandala/subgoals` → `generate` → `approve` 를 한 번 더 타는 것이고, 그 승인이 옛 트리를 보관하면서 **사용자가 손으로 쌓은 것**(완료 표시·축 승격·습관 링크)이 archived 노드에 매달린다. 그래서 승인 **전에** 무엇이 걸려 있는지 보여준다: `totalCells`/`completedCells`, `promotedAxes[]`(축 제목 + 그 축에서 승격된 목표), `linkedHabits[]`(반복형 칸 + 링크된 습관), `liveActionItems`(만다라 칸에 직접 매달린 미완 카드 수 — 승격 목표의 계획 트리 카드는 `tree_kind='plan'` 이라 여기 안 세고 다시 세워도 안 사라진다), `warnings[]`(승계 규칙을 사용자 말로 옮긴 완성 문장 — `/plans/generate` 의 `warnings` 와 같은 규약이라 FE 는 조립하지 않는다). **아직 트리가 없으면 `hasTree=false` + 전부 0/빈 배열**(404 아님 — 처음 세우기도 같은 경로를 타고 확인 시트만 건너뛴다) |
| PATCH | `/goals/mandala/nodes/{nodeId}` | **셀 상세 편집**(PR6, S32). body `{ title?, whyText?, completed? }` — 준 필드만 갱신, 어떤 필드든 건드리면 `source="user"` 로 전환(AI/rule 점선 렌더가 실선으로 바뀜). `completed:true`→`completedAt=now`, `false`→`null`. 제목 길이는 노드 깊이별 상한(축 10자/셀 16자) 초과 시 422 `COMMON_VALIDATION_ERROR`. 응답은 `MandalaNode` — 이 endpoint 는 롤업(`progress`/`coverage`)을 다시 계산하지 않고 `null`(필요하면 `GET /mandala` 재호출) |
| POST | `/goals/mandala/nodes/{nodeId}/promote` | **하위목표(축) 승격**(PR6, S32). body `{ goalTier }` — 그 축을 `Goal(status="proposed")` 로. **중앙(core)·셀(leaf)은 대상이 아니다**(depth≠1 이면 422). Focus≤3/Maintain≤5 초과 시 기존 422 `GOAL_TIER_LIMIT_EXCEEDED` 재사용. **멱등** — 이미 승격된 축을 다시 누르면(그 Goal 이 살아있으면) 새로 만들지 않고 그 행을 그대로 반환(201) |
| POST | `/goals/mandala/nodes/{nodeId}/habit` | **반복형 전환**(U12, ADR-0008 §1). body `{ title?, category, frequencyPerWeek, minutesPerSession, timePreference, priorityLevel }` — 새 `Habit` 을 만들어 이 칸에 링크(`habits.goalNodeId`). "코딩테스트 1일 1문제"·"쓰레기 줍기" 처럼 끝이 없는 칸을 계획(action_item)으로 안 내려보내고 습관 인프라(`habit_instances.doneCount`)로만 주간 횟수를 추적하기 위함. **칸(leaf)만 대상**(depth≠2 면 422 `COMMON_VALIDATION_ERROR`). `title` 생략 시 칸 제목 그대로. **멱등** — 이미 링크된 활성 습관이 있으면 새로 안 만들고 그대로 반환(201). 응답은 `Habit`(§7) |
| DELETE | `/goals/mandala/nodes/{nodeId}/habit` | **반복형 → 프로젝트형 되돌리기**(ADR-0008 §1). 링크된 습관을 soft delete. 칸 자체는 남는다. 링크가 없으면(이미 프로젝트형) 그냥 204(멱등, 에러 아님) |

응답 ID 형식: `goal_<uuid>` (§1.8). category enum 9종 (`study`/`project`/`health`/`routine`/`schedule`/`career`/`relationship`/`self_dev`/`other`).

**`hasPlan`**(v2.30-goals) — 이 목표에 살아 있는 계획 트리가 있는지. `GET /goals` 는 목록 전체를 한 번에,
단건 응답(`PATCH`·`park`·`complete`·`promote`·`ultimate`)은 그 목표를 한 번 물어 채운다. `POST /goals` 로
방금 만든 목표는 정의상 `false`. 예전엔 단건 응답이 늘 `true` 라 만들거나 고친 직후 '미계획' 배지와
'이 목표 계획 세우기' 가 사라졌다.

**입력 상한**(v2.30-goals) — 넘으면 500 대신 422 `COMMON_VALIDATION_ERROR`(envelope·코드 그대로, 문구만 한국어):
- 목표 `title`(`POST /goals`, `PATCH /goals/{id}`): 앞뒤 공백을 떼고 **1~200자**. 공백뿐이거나 비면
  "목표 이름을 적어 주세요.", 길면 "목표 이름은 200자까지 적을 수 있어요."(`field="title"`).
  `PATCH` 에서 `title` 을 빼면 그대로다. `estimatedMinutes` 는 0~1,000,000.
- 궁극목표(`POST /goals/ultimate`) 제목은 인터뷰 문장을 **앞 200자로 줄여**(넘으면 끝에 `…`) 저장한다 —
  긴 문장이 500 을 내고 재시도도 영영 실패하던 경로. 원문은 인터뷰 기록에 그대로 남는다.
- 만다라 축·칸 제목(`subgoals[].title` 1~10자, `cells[].title` 1~16자 — `/plans/mandala/*` 요청 포함)은
  **허용 범위 그대로**, 문구만 한국어: "축 이름은 1~10자로 적어 주세요." / "칸 내용은 1~16자로 적어 주세요."
  `field` 에 `subgoals.3.title` 처럼 위치가 실린다. 칸 편집(`PATCH /goals/mandala/nodes/{id}`)의 빈 제목은
  "칸 내용을 적어 주세요."
- 반복형 전환(`POST /goals/mandala/nodes/{id}/habit`)의 `title`·`minutesPerSession` 은 §7 과 같은 상한.

`status` enum 4종 — `proposed` / `active` / `completed` / `archived`.
**`proposed`(잠정)** 는 딥 인터뷰가 추출했지만 **계획이 아직 승인되지 않은** 목표다. 인터뷰 완료 시
`proposed` 로 저장되고, `POST /plans/{planId}/approve` 가 그 목표를 `active` 로 승격한다.
승격되지 않은 잠정 목표는 두 경로로 정리된다 — ① **다음 인터뷰가 대체(보관)**, ② **14일간
미승격 시 cron 이 보관** (`expire_proposed_goals`, 매일 04:00 KST, #178) — 인터뷰 한 번 하고
돌아오지 않는 사용자에게도 탈출구가 있도록. **만다라 축에서 사용자가 직접 올린(`promote`) 목표는 이
만료에서 빠진다**(v2.30-goals — 예전엔 14일 뒤 말없이 보관됐다). 올린 목표를 지우면(보관)
`GET /goals/{id}/mandala` 의 그 축 `promotedGoalId` 는 `null` 로 나간다(다시 올릴 수 있다). 둘 다 soft 보관(`status='archived'`+`archived_at`)
이며 사용자 알림은 없다(ADR-0005 §7.8).
`GET /goals` 에는 계속 노출되지만 tier 한도(Focus ≤3 / Maintain ≤5)에는 포함되지 않는다.

PR7 구현 메모 (만다라 → 오늘/브리프 연결 — "만들고 나면 죽은 문서가 되는" 걸 막는 마지막 조각):
- `Goal.isUltimate`/`Goal.promotedFromAxis` 는 additive(하위호환) — `GET /goals`/`POST
  /goals/ultimate`/`POST /goals/mandala/nodes/{id}/promote` 응답에서만 실제 값을 채운다.
  `POST /goals`·`PATCH /goals/{id}`·`POST /goals/{id}/park` 응답은 `isUltimate=false`·
  `promotedFromAxis=null` 로 고정(조회 시점에 역조회하지 않음 — 다음 `GET /goals` 새로고침이
  채운다).
- **S31 진입점**(S26 목표 화면 → 만다라트 상시 뷰)은 FE 가 `isUltimate=true` 카드에 버튼을
  다는 것으로 끝난다 — 별도 endpoint 없음.
- **모닝 브리프**(`GET /today/agenda` 의 `brief`, §10)는 승격된 축 중 실제로 `active` 인
  게 있으면 그 축 이름을 headline/reasonWhyNow 어딘가에 자연스럽게 한 번 엮을 수 있다
  (LLM 재량, 프롬프트 규칙 — 없으면 언급 자체를 안 한다). **응답 스키마 변경 없음** — 기존
  자유 텍스트 필드 안의 문구만 달라진다.

응답 예 `GET /goals/{id}/nodes` (계획 승인 후). `orderIndex`/`nodeType`/`isLeaf` 는 PR6 에서
additive 로 추가됐다(만다라 렌더의 전제 — `orderIndex` 없이는 FE 가 8칸 중 몇 번째인지 모른다):
```json
{
  "goalId": "goal_abc",
  "rootNodeId": "node_11111111-...",
  "nodes": [
    { "nodeId": "node_11111111-...", "parentId": null, "title": "알고리즘 문제 풀기", "depth": 0, "orderIndex": 0, "nodeType": "core", "isLeaf": false, "completedAt": null },
    { "nodeId": "node_aaaaaaaa-...", "parentId": null, "title": "기초 문법", "depth": 1, "orderIndex": 0, "nodeType": "milestone", "isLeaf": false, "completedAt": null },
    { "nodeId": "node_22222222-...", "parentId": "node_11111111-...", "title": "1주차: 입문 및 기초 문법", "depth": 1, "orderIndex": 0, "nodeType": "subgoal", "isLeaf": false, "completedAt": null },
    { "nodeId": "node_33333333-...", "parentId": "node_22222222-...", "title": "조건문 기초 문제 3개 풀기", "depth": 2, "orderIndex": 0, "nodeType": "leaf", "isLeaf": true, "completedAt": null }
  ]
}
```

응답 예 `GET /goals/{id}/mandala`(PR6, S31, `MandalaTreeResponse`). 좌표는 서버가 안 내린다 —
`(depth, parent.orderIndex, orderIndex)` 로 FE 가 계산(`SLOT=[0,1,2,3,5,6,7,8]`, 설계서 §7.3):
```json
{
  "goalId": "goal_3f8c…",
  "rootNodeId": "node_11111111-...",
  "statement": "메이저리그 8구단 드래프트 1순위",
  "nodes": [
    { "nodeId": "node_11111111-...", "parentId": null, "title": "메이저리그 8구단 드래프트 1순위",
      "depth": 0, "orderIndex": 0, "nodeType": "core", "isLeaf": false,
      "whyText": null, "source": "llm", "locked": false, "completedAt": null,
      "promotedGoalId": null, "habitId": null, "progress": 0.125, "coverage": 0.5 },
    { "nodeId": "node_22222222-...", "parentId": "node_11111111-...", "title": "구위",
      "depth": 1, "orderIndex": 0, "nodeType": "subgoal", "isLeaf": false,
      "whyText": null, "source": "user", "locked": true, "completedAt": null,
      "promotedGoalId": "goal_9c2d…", "habitId": null, "progress": 1.0, "coverage": 0.125 },
    { "nodeId": "node_33333333-...", "parentId": "node_22222222-...", "title": "주 3회 불펜피칭",
      "depth": 2, "orderIndex": 0, "nodeType": "leaf", "isLeaf": true,
      "whyText": null, "source": "llm", "locked": false, "completedAt": "2026-08-20T15:00:00+09:00",
      "promotedGoalId": null, "habitId": null, "progress": 1.0, "coverage": null },
    { "nodeId": "node_44444444-...", "parentId": "node_22222222-...", "title": "코딩테스트 1일1문제",
      "depth": 2, "orderIndex": 1, "nodeType": "leaf", "isLeaf": true,
      "whyText": null, "source": "user", "locked": false, "completedAt": null,
      "promotedGoalId": null, "habitId": "habit_7a1e…", "progress": null, "coverage": null }
  ],
  "progress": 0.125,
  "coverage": 0.5
}
```
> 아직 승인된 만다라 트리가 없으면 `{"goalId": "...", "rootNodeId": null, "statement": "...", "nodes": [], "progress": 0.0, "coverage": 0.0}`.

응답 예 `GET /goals/{id}/mandala/rebuild-preflight`(v2.05, U13, `MandalaRebuildPreflightResponse`).
`warnings` 는 서버가 완성한 문장이라 FE 는 그대로 확인 시트에 얹는다:

```json
{
  "goalId": "goal_8f...",
  "hasTree": true,
  "rootNodeId": "node_2a...",
  "statement": "대기업 개발자로 입사",
  "totalCells": 61,
  "completedCells": 7,
  "promotedAxes": [
    {
      "orderIndex": 0,
      "axisTitle": "개발 실력",
      "goalId": "goal_c1...",
      "goalTitle": "사이드 프로젝트 배포",
      "goalStatus": "active",
      "goalTier": "focus"
    }
  ],
  "linkedHabits": [
    {
      "subgoalIndex": 0,
      "orderIndex": 2,
      "cellTitle": "1일 1커밋",
      "habitId": "habit_77...",
      "habitTitle": "1일 1커밋",
      "frequencyPerWeek": 5
    }
  ],
  "liveActionItems": 0,
  "warnings": [
    "제목이 그대로인 칸은 완료 표시와 습관 링크가 새 만다라트로 그대로 넘어가요.",
    "제목이 바뀌거나 사라진 칸은 이어지지 않아요 — 그 습관은 링크만 풀려 단독 습관으로 남아요.",
    "지금까지 7칸을 완료로 표시하셨어요.",
    "축에서 승격한 목표 1개(「사이드 프로젝트 배포」)는 그대로 남아요. 같은 이름의 축이 새 만다라트에 없으면 축 배지만 빠져요."
  ]
}
```


승인 전에는 `{"goalId": "...", "rootNodeId": null, "nodes": []}`.

`habitId` 는 additive(ADR-0008 §1) — leaf 가 `POST /goals/mandala/nodes/{id}/habit` 으로
반복형 전환됐으면 링크된 습관 id, 아니면 `null`(=프로젝트형, 기본값).

**반복형 leaf 의 롤업(ADR-0008 §1.2)**: 완료 개념이 없으므로 그 leaf 자신의
`progress`/`coverage` 는 항상 `null` (위 예시 4번째 노드). 대신 그 칸이 속한 축의 롤업에서:
- `progress`(분자) — 반복형 칸은 아예 빠진다(0으로도 안 잡는다). 그 축에 **프로젝트형
  leaf 가 하나도 없으면**(전부 반복형이거나 gaps) 축의 `progress` 는 `0.0` 이 아니라
  `null`("판단 불가" — `_leaf_progress` 가 종결 카드 없을 때 `None` 을 내는 것과 같은 규약).
- `coverage`(분모는 항상 고정 8, §7.8) — 반복형 칸은 **이번 주 그 습관을 1회 이상
  했으면**(`GET /habit-instances` 의 `doneCount > 0`) 착수로 잡힌다. 프로젝트형 leaf 의
  기존 판정(종결 카드 있음)과 합산된다.

즉 8칸이 전부 반복형인 축은 `progress: null`, `coverage`는 이번 주 실제로 건드린 반복 칸
비율로 나온다 — "0%"라는 오해를 주는 숫자가 나오지 않는다.

---

## 7. Habits (`/habits`, `/habit-instances`) — S27

`POST /habits` 시 **이번 주 `habit_instances` 자동 생성** (cron 도입 전 임시; Issue #24 cron 후속). `frequencyPerWeek` 변경 시 `target_count` 동기화. `weekStart` 누락 시 이번 주 KST 월요일.

응답 `Habit` 에 `goalNodeId`(additive, ADR-0008 §1) — 만다라 반복형 칸에서 만들어진 습관이면
그 칸(leaf) id, 일반 습관이면 `null`. 이 습관 자체를 만들거나 지우는 경로는 여전히
`§6 Goals`의 `POST`/`DELETE /goals/mandala/nodes/{nodeId}/habit` 뿐이다 — 여기 `/habits`
CRUD 로 만다라 링크를 직접 걸거나 뗄 수는 없다(만다라 칸 소유권 검증이 그쪽에 있다).

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/habits` | 내 습관 전체 |
| POST | `/habits` | 신규 — `{ title, category, frequencyPerWeek }`. 응답 `Habit` 에 **`currentInstanceId`**(이번 주 인스턴스 id, v2.30-goals — 방금 만든 습관을 곧바로 `check` 할 수 있게; 생성 응답에만 싣고 그 외 응답은 `null`). **등록한 주의 `targetCount` 는 남은 날만큼**: `ceil(빈도 × (7 − 요일) / 7)`, 최소 1(월=0 … 일=6 — 토요일에 만든 '매일'은 2). 다음 주부터는 빈도 그대로. `category` 가 목표 전용 값(`project`/`schedule`/`career`)이면 `other` 로 받는다(v2.30-goals — 오늘 화면 폼이 목표 분류를 띄워 422 로 사라지던 경로), 그 밖의 모르는 값은 422. `title` 은 앞뒤 공백을 떼고 1~200자(비면 "습관 이름을 적어 주세요.", 길면 "습관 이름은 200자까지 적을 수 있어요." — 422 `COMMON_VALIDATION_ERROR`, v2.30-goals), `minutesPerSession` 1~1440 |
| PATCH | `/habits/{id}` | 빈도/제목 — `title` 상한은 `POST` 와 같다(생략하면 그대로). `frequencyPerWeek` 를 바꾸면 **이번 주 인스턴스의 `targetCount` 도** 새 값으로(등록한 주면 남은 날 비율), 이미 한 횟수는 새 목표에서 멈춘다(`doneCount = min(doneCount, target)`). 지난 주 기록은 그대로(v2.30-goals) |
| DELETE | `/habits/{id}` | soft delete |
| GET | `/habit-instances?weekStart=YYYY-MM-DD` | 이번 주 인스턴스 (`doneCount` vs `targetCount`). **이번 주를 읽을 때는 없는 인스턴스를 먼저 채운다**(v2.30-goals, 멱등 — 새 주 인스턴스를 만드는 월요일 00:05 cron 전에 열어도 체크 대상이 있다). 다른 주는 읽기만 |
| POST | `/habit-instances/{id}/check` | 1회 달성(`targetCount` 에서 멈춤, 동시에 두 번 와도 둘 다 센다). **지난 주 인스턴스로 오면 이번 주 인스턴스를 올리고 그것을 돌려준다**(v2.30-goals — 일요일 밤에 열어 둔 화면에서 월요일에 누른 체크가 지난 주로 가던 경로; 지난 주 기록은 그대로). 응답의 `instanceId`·`weekStart` 로 새 주를 알 수 있다 |
| POST | `/habit-instances/{id}/uncheck` | **1회 되돌리기**(v2.30-goals, additive) — 잘못 누른 체크용. `doneCount` 를 1 줄이고 0 아래로는 안 내려간다(다시 불러도 안전). 응답은 `check` 와 같은 `HabitInstance`. 없는/다른 사용자의 인스턴스는 404 `HABIT_NOT_FOUND` |

---

## 8. Planning (`/plans`) — S06, S14, S15, S16, S30 만다라트 초안

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/plans/milestones` | **Stage A** — 목표를 **중간 목표(마일스톤) 3~5개**로 나눈 초안. 입력은 `generate` 와 동일(`interviewSessionId` 또는 `outcome` 인라인, 빈 본문이면 최근 '정상 종료' 인터뷰로 자동 복구 + `density`). LLM 1콜 + 룰 폴백(준비·진행·마무리 3단계)이라 가볍고 **lock 없음 · DB 쓰기 0**. 응답 `MilestoneListResponse` — `{ milestones: [{title, summary}], aiSource }`. 사용자가 이 목록을 **확인·편집(추가/삭제/재배열)** 한 뒤 `generate` 의 `milestones` 로 실어 보내면(v2.30-planA: **최대 10개 · 이름 200자 · 설명 500자** — 넘으면 `generate` 가 LLM 전에 422 `COMMON_VALIDATION_ERROR`(field `milestones`, 한국어 안내). 예전엔 생성은 통과하고 승인에서 일반 500 으로 영영 막혔다), 분해가 그 목록을 branch 로 고정하고 각 안에서만 세션을 만든다(제목·순서 유지). 보내지 않으면 현행대로 자동 전체 분해(하위호환). **이 단계는 DB 에 쓰지 않는다** — 확정한 목록이 실제로 저장되는 시점은 `{planId}/approve` 다(ADR-0007 PR-2). ⚠️ **이 목표에 이미 확정·영속된 뼈대가 있으면 LLM 을 돌리지 않고 그걸 그대로 돌려준다** — `aiSource="saved"`(v1.82, ADR-0007 PR-2.5). 2주기 이후의 정상 경로다: 마일스톤은 매 주기 교체되는 leaf 트리와 달리 **마감까지 살아남는 층**이라, 주기마다 새로 지어내면 사용자가 처음 확정한 뼈대와 다른 목록이 나오고 승인 경로는 이미 마일스톤이 있다는 이유로 저장을 건너뛰어 DB 와 실제 계획이 갈라진다. FE 는 이 응답을 **지난번 확정한 뼈대**로 보여주면 된다(다시 확인·편집하는 화면은 그대로). 이 목록을 **편집해서** `generate`→`approve` 하면 그 편집이 저장된 뼈대에 반영된다(v1.83, ADR-0007 PR-6). 참고 자료가 링크뿐이면 `generate` 와 **같은 방식으로 열어서** 뼈대에 반영한다(#226) — 뼈대를 정하는 게 이 단계라, 여기서 자료가 빠지면 Stage B 는 일반론 위에 묶인다. ⚠️ **목표가 미입력 자리표시자(`(미입력 목표)`)뿐이면 LLM 을 부르지 않고 422 `COMMON_VALIDATION_ERROR`(`field="goals.list"`)** (v2.30-planB) — 첫 질문에서 [충분해요]로 끝난 인터뷰다. FE 는 목표 입력(인터뷰)으로 되돌린다 |
| POST | `/plans/materials/search-query` | **자료 검색 ①** — 목표에서 검색어를 제안한다. **외부 호출 0회 · LLM 0회 · 과금 0** (규칙으로 만든 문자열이라 `isDraft` 대상 아님). 응답 `{ suggestedQuery, goalTitle, notice }` — `notice` 는 "이 검색어가 그대로 웹 검색에 나간다"는 고지(#259 §4.1 ① 프라이버시). 사용자가 고치기 전까지 **아무것도 나가지 않는다** |
| POST | `/plans/materials/search` | **자료 검색 ②** — 사용자가 확인·편집한 `query`(2~200자) **그것만** 외부로 보낸다. 서버가 목표 슬롯을 몰래 덧붙이지 않는다. 그라운딩 1건 과금(일일 예산 `LLM_DAILY_GROUNDING_BUDGET`, 기본 5). 응답 `MaterialsSearchResponse` — **`isDraft=true`, 아무 데도 저장하지 않는다**(③ 확정 전까지). `status` 5종은 **사용자가 다음에 할 행동이 각각 달라서** 나눈다: `found`(본문 + `sources[]` + 모델이 실제 던진 `searchQueries[]`) / `not_found`(검색어를 고쳐 재시도) / `blocked_copyright`(**재시도해도 영영 안 된다** — 직접 붙여넣기로 안내) / `quota_exceeded`(내일 다시) / `unavailable`(일시 장애). `remainingToday` 는 오늘 남은 횟수(무제한이면 `null`) |
| POST | `/plans/materials/confirm` | **자료 검색 ③ (HITL 게이트)** — "이 자료 맞아요". `text`(1~20,000자)를 **사용자가 고친 그대로** 받아 `goals.materials` 슬롯에 기록한다. 명시 승인이므로 Draft 아님. **여기서부터는 붙여넣기와 구분되지 않는다** — 다음 계획 생성이 기존 경로로 집어가고(`build_outcome` → `materialsNote` → `materials_for_prompt` 울타리 → 분해), 검색 전용 저장소도 분해 쪽 분기도 없다(#259 ⑪). 응답 `{ goalTitle, savedChars, notice }` |
| POST | `/plans/materials/study-method` | **자료 검색 파이프라인 1단계(ADR-0010)** — 위 ①②③ 과 **별도**(교체 아님, 병행). 목표에 맞는 학습 방식 + 도서/영상 검색어 2종 + 자료 조합 추천(`materialMix`)을 낸다. LLM 구조화 호출 1회(그라운딩 아님) — RECITATION 리스크가 없는 일반 전략 조언이라 검색 없이 낸다. 응답 `StudyMethodResponse` — `{ approach, focusPoints[], bookQuery, videoQuery, materialMix, goalTitle, notice, isDraft, aiSource }`. `materialMix` 는 `"book"\|"video"\|"both"` — 이 목표엔 책·영상 중 뭘 확정하는 게 좋을지의 **권장값**이다(예: 이론+실전 트레이닝이 둘 다 필요하면 `"both"`) — `spec-confirm` 이 몇 건을 받을지 강제하지 않는다, 최종 선택은 사용자 몫이다. **아직 아무것도 검색하지 않는다** — `notice` 가 그 사실을 고지하고, 사용자가 두 검색어를 확인·편집한 뒤에야 `catalog` 로 나간다(①과 같은 프라이버시 원칙). 인터뷰 없으면 422 |
| POST | `/plans/materials/catalog` | **자료 검색 파이프라인 2단계(ADR-0010)** — 사용자가 확인·편집한 `bookQuery`/`videoQuery`(각 2~100자, **최소 하나 필수**) 로 알라딘/YouTube 후보를 찾는다. LLM 0회 — API 호출만. 저장하지 않는다. 응답 `MaterialsCatalogResponse` — `{ books[]: {..., isbn13}, bookNotice, videos[]: {playlistId, ...}, videoNotice }`. **두 소스는 독립적으로 실패한다** — 한쪽이 안 되도 다른 쪽 후보는 그대로 온다(각 `*Notice` 에 실패 사유). 후보에는 목차·분량이 없다(도서는 알라딘이 API 로 안 준다 — L0 실측 0/10; 영상은 재생목록 상세가 별도 호출이라서다) — `isbn13`/`playlistId` 를 아래 3단계로 넘긴다. ⚠️ YouTube `search.list` 는 1회 100유닛/일일쿼터 10,000유닛이라 **앱 전체 하루 ~100회가 상한**이고, 사용자별 상한은 아직 없다 — 초과하면 `videos=[]` + `videoNotice` 로 알릴 뿐 500 이 아니다 |
| POST | `/plans/materials/book-detail` | **자료 검색 파이프라인 3단계 — 도서(ADR-0010)** — 후보의 `isbn13` 하나로 페이지 수 + 목차(best-effort)를 조회한다. LLM 0회. 저장하지 않는다. 응답 `BookDetailResponse` — `{ detail: {title, author, isbn13, pageCount, chapters: [{title, endPage}], tocSource}, notice }`. 페이지 수는 알라딘에서 안정적으로 온다(L0 10/10) — 조회 자체가 실패해야 `detail=null`. 목차(`chapters[]`)는 국중 seoji best-effort(L0 10권 중 1권, 판본마다 다르다) — **못 가져와도 실패가 아니다**: `detail` 은 채워지고 `tocSource=null`·`chapters=[]` 일 뿐이며, 이때 `notice` 도 비어 있다(정상 경로). seoji 조회 자체가 진짜 실패(타임아웃 등)했을 때만 `notice` 로 알린다. `chapters[].endPage` 는 그 챕터의 마지막 페이지(best-effort, 페이지 번호가 단조증가하지 않으면 이 책 전체가 `null` — 틀린 값을 내느니 없는 편이 낫다). 진도(`pagesPerSession`)는 여기서 계산하지 않는다 — 목표의 시간 예산이 필요해 `spec-confirm` 이 계산한다 |
| POST | `/plans/materials/video-detail` | **자료 검색 파이프라인 3단계 — 영상(ADR-0010)** — 후보의 `playlistId` 하나로 커리큘럼(영상 제목 목록)+분량(재생시간)을 조회한다. LLM 0회. 저장하지 않는다. 응답 `VideoDetailResponse` — `{ detail: {title, channelTitle, playlistId, playlistUrl, videoCount, totalMinutes, curriculum: [{title, minutes}], truncated}, notice }`. 이 소스는 커리큘럼·분량이 핵심이라(L0 4/4) 못 가져오면 `detail=null`(쿼터 초과 포함, 500 아님). `curriculum` 은 200편에서 잘리고 그때 `truncated=true` — `videoCount` 는 잘려도 재생목록 전체 기준 편수. 비공개·삭제돼 볼 수 없는 영상(`Private video`/`Deleted video`, 재생시간 없음)은 `curriculum`·`videoCount`·`totalMinutes` 어디에도 넣지 않는다(v2.30-materials). 볼 수 있는 영상이 하나도 없으면 `detail=null` |
| POST | `/plans/materials/spec-confirm` | **자료 검색 파이프라인 4단계 — 확정(ADR-0010, HITL 게이트)** — book/video-detail 이 돌려준 상세를 **그대로** `details`(1~2건, discriminator `kind`)로 되받아 `goals.materials` 슬롯에 `{"type":"spec", "items":[{"kind":"book"\|"video", ...}, ...]}` 로 쓴다(③ `/confirm` 과 같은 슬롯 — 나중 확정이 이긴다). **책 1개·영상 1개까지, 같은 종류를 두 번 보내면 422.** `study-method` 의 `materialMix` 가 몇 건이 좋을지 권장하지만 강제하지 않는다. 재조회하지 않는다. 응답 `{ goalTitle, kinds[], notice, bookPace }`. **책이 있으면 여기서 "하루에 몇 쪽" 진도(`bookPace: {pagesPerSession, totalSessions, daysUntilDeadline, summary, chapters[]}`)를 계산한다** — `pageCount`(항상 있음, L0 10/10) 하나만으로 계산되므로 목차 유무와 무관하게 나온다. 마감(`goals.deadlines`)이 없거나 지났으면 나눌 기간이 없어 `bookPace=null`(지어내지 않는다). 세션 수(`totalSessions`)는 `POST /plans/generate` 가 쓰는 것과 **같은 함수**(`target_sessions_per_week`, density=`"standard"` 고정)로 정한 시간 예산을 기준으로 삼는다 — 이 값은 어디까지나 **추정치**(실제 생성 요청의 density 는 나중에 사용자가 고른다). **목차 전 챕터에 `endPage` 가 있으면(best-effort) 균등 분할이 아니라 `bookPace.chapters[]`(`{title, endPage, sessions}`)가 챕터마다 정수 개의 세션을 배정한다** — 세션이 챕터 중간에서 끊기지 않도록, 이때 `totalSessions` 는 이 배정의 합이다(균등 분할 값이 아니다). 목차가 커버하는 마지막 페이지가 `pageCount` 보다 작으면(부록·연습문제 등, 흔하다) 그 차이도 "(목차 이후 나머지 분량)" 이름 없는 항목으로 마저 배정한다 — 존재하는 페이지를 없는 셈 치지 않는다. 챕터 중 하나라도 `endPage` 를 모르면(부분 목차) 챕터 배정 자체를 포기하고 균등 분할로 폴백한다(`chapters=[]`). **클라이언트가 보낸 진도 값은 신뢰하지 않는다** — `details` 에 진도 필드가 있어도 서버가 지금 계산한 값으로 항상 덮어써 슬롯에 저장한다. 응답의 `bookPace` 와 저장되는 슬롯의 `items[].pagesPerSession`/`totalSessions`/`chapters[].sessions` 는 같은 값이다. **다음 계획 생성부터 반영된다** — `interview_adapter._materials_note` 가 각 항목(진도 문구 + 챕터별 페이지·세션 체크포인트 포함)을 텍스트로 풀어 이어붙여 `materialsNote` 에 싣고, 그 뒤로는 붙여넣기 텍스트와 **완전히 같은 경로**(`materials_for_prompt` 울타리 → `goal_decompose`)를 탄다(새 프롬프트 변수·새 인젝션 방어 없음). 인터뷰 없으면 422 |
| POST | `/plans/generate` | First Plan orchestrator(LangGraph) 실행. 입력: `outcome`(InterviewOutcome 인라인) 또는 `interviewSessionId`(+`targetDate` 선택). **빈 본문이면 최근 '정상 종료' 인터뷰(abandoned 제외)로 자동 복구** — FE 가 sessionId 를 잃어도 생성 가능(완료 인터뷰가 없으면 422). **`goalId`(선택, additive, v2.23 #398): 이 계획이 다룰 목표를 명시한다** — `GET /reviews/weekly` 의 `nextCycleProposals[].goalId` 를 그대로 넣는다. 주면 가용 시간·선호·정체성은 인터뷰 답 그대로 두고 heaviest 목표만 그 목표로 갈아끼운다(그 목표에 대해 이미 답한 주당 시간·세션 길이 슬롯은 보존). 안 주면 종전대로 최근 인터뷰의 heaviest 를 재투영하는데, 목표를 여러 개 굴리면 그게 **다른 목표**일 수 있으니 제안 카드에서 왔다면 반드시 넘길 것. 없는/남의/보관된 목표는 404 `GOAL_NOT_FOUND`(존재 여부를 흘리지 않으려 한 갈래로 묶었다), **완료한 목표는 422** `COMMON_VALIDATION_ERROR`(목표는 실제로 있고 화면에도 보이므로 '없다'가 거짓말이 된다 — 되돌린 뒤 다시 세우라고 안내). `proposed`(계획 미승인 잠정 목표)는 막지 않는다 — 첫 계획을 세우는 정상 경로가 그 상태다(#96). 만다라 승격 목표의 2주 상한도 그대로 걸린다(제목으로 판정하므로 자동). `scope`(선택, 기본 `"horizon"`): `"horizon"`=마감까지 전 구간, **단 한 번에 세우는 계획은 최대 4주(≈한 달)** — 마감이 그보다 멀면 4주까지만 배치하고 그 사실을 `warnings` 로 알린다(먼 미래를 자리표시자로 채우는 대신 주간 재계획이 이어받는다) / `"week"`=`targetDate` 가 속한 **달력 주(월~일)** 만. **heaviest 목표가 만다라 축에서 승격된 목표(`POST /goals/mandala/nodes/{id}/promote`)면 이 상한이 4주가 아니라 2주다**(ADR-0008 §3) — `warnings`의 "N주까지만" 문구도 2주로 나온다. 매번 생성할 때마다 `targetDate` 기준 새로 계산되므로 재생성이 곧 "앞으로 2주" rolling 창이다(별도 자동 재생성 크론은 아직 없음). `density`(선택, 기본 `"standard"`): 계획 **분량** 프리셋 — `"light"`≈주당 3세션 / `"standard"`≈5 / `"intense"`≈8. **단 목표별 슬롯이 우선한다**: `goals.frequency`(주 N회)가 있으면 그 값이 주당 세션 수가 되고 density 는 무시되며, `goals.weekly_time`(주당 시간)만 있으면 density 는 가감 배율(0.7/1.0/1.3)로 작동한다. 둘 다 없을 때만 프리셋 그대로다. ⚠️ **계획 분량의 상한은 '세션 개수'가 아니라 '분'이다**(v1.95, ADR-0009 D1) — 마감까지 담을 수 있는 총 집중 시간(`주당 세션 수 × 계획 세션 길이 × 지평 주 수`)을 넘는 지점에서 자른다. `goals.frequency`(주 N회)로 **케이던스를 명시한 목표**는 개수 상한이 함께 걸리고(먼저 닿는 쪽이 이긴다), 주당 시간만 준 목표는 분 상한만 걸린다. 세션 길이가 균일하면 결과가 종전과 같다. **빈도를 말한 목표는 배치 창도 개수 기준으로 넓힌다**(v2.30-planB) — `올림(세션 수 × 7 ÷ 주 N회)` 일이 분 기준 창보다 넓으면 그쪽을 쓴다(지평 상한 안에서). 창이 좁아 '주 5회' 가 한 주에 6회, '매일' 이 하루 두 번이 되던 것을 없앤다. 달력 주 단위 개수 상한은 걸지 않는다 — 마감 계획은 창이 주 중간에서 시작·끝나 그 상한이 도리어 첫·끝 주의 하루에 세션을 쌓았다. ⚠️ **각 세션의 `estimatedMinutes` 는 작업 내용에 따라 서로 다르다**(v1.96, ADR-0009 D2) — 상한은 `goals.session_length` → 전역 `energy.focus_duration` → 기본값(50분) 순으로 정해지고 **15분 아래로는 안 내려간다**(v2.00), 하한은 15분, 합계는 위 분 예산이다. `goals.frequency` 로 횟수를 말한 목표는 **세션 개수가 고정**이고 길이가 그 안에서 흩어지며, 주당 시간만 준 목표는 개수가 결과값이다. 어느 scope 든 이미 승인된 `scheduled_blocks` + **고정 일정(`fixed_schedules`, 수업·알바) + DB `time_policies`(온보딩 후 수정 포함)** 를 모두 busy 로 피해 배치(비파괴). Focus≤3/Maintain≤5 초과 시 422 `GOAL_TIER_LIMIT_EXCEEDED`. **목표가 미입력 자리표시자(`(미입력 목표)`)뿐이면 LLM 분해 전에 422 `COMMON_VALIDATION_ERROR`(`field="goals.list"`)**(v2.30-planB) — 예전엔 없는 목표로 일반론 계획을 지어내고, 승인하면 0건을 저장하고도 200 이 나갔다. `/plans/mandala/next-cycle` 도 같은 본체를 탄다. Draft 를 `plan_drafts`(72h)에 저장하고 실제 `planId` 반환. 응답 `isDraft=true`. `warnings[]` 에는 배치 실패 외에 **계획 분량 안내**가 실릴 수 있다 — 주당 시간에 못 미칠 때, **참고 자료를 링크로만 줬는데 그 링크를 열지 못했을 때**(#226 — 열었으면 본문이 분해에 실리고 이 안내는 나가지 않는다. 못 열었으면 사유를 담아 알린다: 로그인 필요·페이지 없음·형식 미지원 등), 마감까지 채우려고 회차를 덧붙였을 때, 계획이 마감 전에 끝날 때, **마감이 계획 첫날이라 그 하루에 몰아 잡았거나 다 넣지 못했을 때**(v2.30-planB — 이때는 항목별 배치 실패 줄 대신 원인과 "새 마감을 정해 달라"는 한 줄만 나간다), **목표를 여러 개 말했는데 계획은 가장 무거운 것 하나만 다뤘을 때**, **`milestones` 로 확정한 중간 목표가 이번 계획 트리에 자리를 못 잡았을 때**(v1.71 — 세션 수 상한에 잘렸거나 분해가 그 branch 를 안 만든 경우. 트리에 branch 로 남아 있고 세션만 없는 건 정상이라 알리지 않는다 — 그건 `구간 커버리지` 안내가 담당). ⚠️ **`milestones` 를 보냈으면 이번 계획은 그중 앞쪽 일부만 다룬다**(v1.93, ADR-0007 §1 커서 모델) — 담을 개수는 `round(남은 마일스톤 수 × 이번 창(주) ÷ 마감까지 남은 주)`, 최소 1개다. 예: 마감까지 14주 · 창 4주 · 마일스톤 4개 → **1개**. 마감이 창 안이면 전부. 그 밖의 마일스톤은 **일부러 안 다룬 것**이라 `warnings` 의 누락 고지에도 안 실린다 — 다음 주기가 이어받는다. 앞에서부터 **연속으로** 완료 표시된(`PATCH /goals/{goalId}/nodes/{nodeId}`) 마일스톤은 건너뛴다(중간 것만 완료면 아무것도 안 건너뛴다). 걷어낸 단계는 `warnings` 로 알린다. 확정 목록 **전체**는 승인 시 그대로 뼈대로 저장된다(잘라서 저장하지 않는다). ⚠️ **한 계획은 heaviest 목표 하나만 분해·배치한다** — 나머지 목표는 세션·블록이 생기지 않는다(승인 시 목표 자체는 전부 저장된다). 한 번에 하나씩 굴리는 의도된 설계이고, 그 사실을 `warnings` 로 알린다 (#32/#62/#187) |
| GET | `/plans/{planId}` | 저장된 **First Plan** Draft 미리보기 재구성(LLM 0회). 없으면 404 `PLAN_DRAFT_NOT_FOUND` (#62). **재계획 Draft(kind=replan)를 넣어도 404** — payload 모양이 달라(goal_nodes 없음) 여기서 재구성하지 않는다. 승인 endpoint 의 같은 가드와 대칭 (#117) |
| POST | `/plans/{planId}/discard` | 계획 초안 **폐기** — "이 계획 말고 다시 인터뷰할래" 경로. 초안은 비영속(계획 블록은 승인 전 DB 에 들어가지 않는다)이라 상태 전이만 일어난다: `plan_drafts.status` 를 만료와 같은 종착 상태(`expired`)로 보낸다. **204 No Content**, 본문 없음. **멱등** — 이미 폐기·만료된 초안에 다시 호출해도 204. 이미 승인된 초안은 409 `PLAN_ALREADY_APPROVED`(승인은 되돌리는 동작이 아니다). 없는 초안·타 사용자 초안은 404 `PLAN_DRAFT_NOT_FOUND`(존재 여부를 흘리지 않으려 403 이 아닌 404). `Idempotency-Key` 불필요 |
| POST | `/plans/{planId}/approve` | HITL [수락] → SAVING. **`planId` 로 저장된 Draft 로드**(body 불필요, #62 FE 계약 변경). **초안 편집 반영(v2.30-planA, 선택 본문, additive)**: `{ blocks?: [{ originId, start, end, title? }] }` — 보내면 그것이 **최종 블록 목록 전체**다. `originId` 는 초안 블록의 `originId` 그대로(나뉜 회차는 같은 값), `start`/`end` 는 KST ISO 8601 — **15분 snap 하지 않는다**(PATCH 블록 편집과 다르다): 그 카드의 초안 칸과 분 단위까지 같으면 손대지 않은 블록으로 보고 초안 시각 그대로 저장하며 다시 검사하지 않는다(초안 자체가 쉬는 시간 10분·수업 끝 직후 시작 때문에 15분 격자가 아니다), 다르면 옮긴 블록이고 보낸 시각 그대로(분 단위) 저장한다 — 손대지 않은 블록은 받은 `start`/`end` 를 그대로 돌려보낼 것, `title`(≤300자)을 바꾸면 그 카드 이름이 된다(회차 꼬리표 "(1/2)" 는 떼고 저장, 초안 제목 그대로면 개명 아님). 목록에서 **모든 블록이 빠진 카드는 만들지 않는다**(삭제), 초안에 없던 `originId`(예: `originId` 가 없는 블록에 FE 가 붙인 임시 id)는 422 `COMMON_VALIDATION_ERROR`(`field="blocks"`), 같은 초안 칸(같은 `originId`·`start`·`end`)을 두 번 보내도 422 `COMMON_VALIDATION_ERROR`, 끝≤시작은 422 `PLAN_INVALID_TIME`. **옮긴 블록만** 생성과 같은 소스로 다시 대조한다 — 살아 있는 다른 블록(이 승인이 교체할 같은 목표의 이전 계획 블록은 제외)·같은 목록의 다른 블록·고정 일정·연결된 구글 캘린더 약속과 겹치면 422 `PLAN_BLOCK_CONFLICT`(고정 일정이면 그 이름을 문구에, 캘린더는 busy 조회라 약속 제목 없이 '캘린더 약속'; 캘린더 미연결·조회 실패면 그 대조만 건너뛰고 승인은 진행), DB 시간 정책(수면·점심·노터치·심야)과 겹치면 422 `PLAN_POLICY_VIOLATION`; 활동 시간대 밖은 종전 영속화 가드가 422 `PLAN_POLICY_VIOLATION`. 승인되면 초안 스냅샷도 저장본으로 바뀐다(`GET /plans/{planId}`·멱등 재승인 응답이 실제 저장과 같다). 본문이 없거나 `{}`·`blocks: null` 이면 종전과 **완전히 같다**. goals/goal_nodes/action_items/scheduled_blocks 단일 트랜잭션 영속화(+3회 재시도). **승인 = 교체**: 같은 `targetDate` 의 이전 AI 계획 산출물 중 미시작 카드(source=goal·status=planned, **user_edit 블록을 가진 카드는 보존**)와 그 블록을 soft 정리(archived/cancelled)하고, heaviest goal 의 기존 분해 트리(goal_nodes)도 보관 후 새 계획을 영속화 — 재생성→재승인 반복 시 같은 날짜 중복 누적 방지. **마일스톤(`node_type='milestone'`)은 이 교체 대상이 아니다**(ADR-0007 PR-2) — 매 승인마다 갈아치우는 4주 트리와 달리 마감까지 살아남는 층이라, Draft 의 `milestones` 를 **확정된 뼈대 그 자체**로 보고 반영한다(PR-6): 정규화한 제목(공백 무시)이 같으면 **같은 행을 유지**하고 제목 표기·순서·요약을 갱신(노드 id 와 `completedAt` 보존 — 표기도 갱신하므로 `"기초문법"`→`"기초 문법"` 같은 편집이 저장된다), 확정 목록에서 빠진 것은 **보관**(archive, 삭제 아님), 새 제목은 삽입. 같은 목록을 다시 승인하면 아무것도 만들지도 보관하지도 않는다(멱등). 입력 정규화: **제목이 비었거나 공백뿐인 항목은 버리고**, 한 목록 안에 정규화가 같은 제목이 두 번 오면 **처음 것만** 남긴다(둘 다 조용히 처리 — 오류 아님). ⚠️ **`milestones` 가 비어 있으면 뼈대를 건드리지 않는다** — "이번엔 마일스톤 없이 세운다"(Stage A 건너뜀)와 "뼈대를 지워라"는 다른 말이고, 후자를 뜻하는 입력은 아직 없다. ⚠️ 개명은 "삭제+추가"와 구별되지 않는다(제목이 식별자다) — 서버 발급 id 왕복은 ADR-0007 §10, 소비자가 생길 때(PR-3) 함께 넣는다. `activatedGoalNodes` 는 이번 4주 트리 + 새로 만든 마일스톤 수의 합. 동시성: 시도(attempt)당 lock 재획득 + Draft 검사→영속화→승인 마킹을 **한 트랜잭션 단일 commit** 으로 묶어 동시 더블 승인의 이중 영속화 방지(lock 미획득 409 `AGENT_CONCURRENT_ACCESS`). 정책 위반 422 `PLAN_POLICY_VIOLATION` / 저장 실패 500 `PLAN_SAVE_FAILED` / 만료 410 `PLAN_DRAFT_EXPIRED`. **재계획 Draft(kind=replan)를 넣으면 404 `PLAN_DRAFT_NOT_FOUND`** — 전용 `/plans/replan/{planId}/approve` 사용(#117). 응답 `isDraft=false`. 부수: onboarding 완료 → `onboarding_state` 를 `ACTIVE` 로 마감(어느 온보딩 단계에서든, 멱등) (#32/#62). ⚠️ **tier 한도(Focus≤3/Maintain≤5) 초과분은 parked 로**(v2.14, #371, additive `warnings`) — 인터뷰가 뽑은 core_goals 를 active 로 올리다 한도를 넘기면 422 로 승인 자체를 막지 않고(사용자는 이미 인터뷰를 마쳤다) 넘긴 만큼만 조용히 `parked` 로 돌린 뒤 그 목표 제목을 `warnings` 에 싣는다. 방금 분해·배치된 목표(heaviest)는 다른 후보가 있으면 보호되지만, 기존 데이터가 이미 한도를 넘겨 있어 heaviest 하나뿐이면 그것도 내린다 — 한도를 못 지키는 예외를 두지 않는다. 이미 active 였던 다른 목표는 이번 승인과 무관하므로 건드리지 않는다. 대개 `warnings` 는 빈 배열 |
| PATCH | `/plans/{planId}/blocks/{blockId}` | 15분 snap 직접 편집 (S15) — `startAt`(필수)/`endAt` 이동 + 선택 `category`/`title` 로 목표(색·분류)·제목 수정(블록의 action_item 갱신, 같은 액션 세션 공유; 미지원 category→`other`; 정책 검사는 새 category 로). 시작/끝낸 블록은 시간 이동 불가(422 `PLAN_INVALID_TIME`, v2.30-planA — 제목·목표만 편집 가능). 시각을 지금 블록 시각 그대로 보내면(제목·목표만 편집) snap·겹침·정책·고정 일정 검사를 하지 않는다(v2.30-planA — 격자 밖 블록이 5분 밀리거나, 나중에 추가한 수업 위 블록의 이름을 못 바꾸던 문제). `title` 은 300자까지(넘으면 422 `COMMON_VALIDATION_ERROR` "제목은 300자까지…", v2.30-planA — 예전엔 일반 500). ✅ #21-B |
| POST | `/plans/{planId}/ai-edit` | 자연어 수정 (S16, P1) — diff 반환만, apply는 별도 |
| POST | `/plans/{planId}/ai-edit/apply` | diff 적용 (사용자 승인 후) |
| GET | `/plans/weekly?weekStart=YYYY-MM-DD` | 주간 그리드 (S14) — cancelled 블록(계획 교체로 취소 등)은 제외 ✅ #21-B. **v2.30-planA**: 블록마다 `completionStatus`(끝난 블록의 체크인 결과 done/partial_done/failed/over_done, 아니면 null). **v2.27**: 블록마다 `calendarConflict`(아직 시작 안 한 블록이 **지금** Google 캘린더 일정과 겹치는가) + 응답 최상단 `calendar: {status, checkedAt}` — §10 "캘린더 겹침" 과 같은 규칙. **v2.34**: 날마다 `calendarBusy`(그날의 Google 캘린더 약속 구간 `[{startAt, endAt}]` — 블록과 안 겹쳐도 싣는다, `calendar.status=ok` 일 때만) |

> ⚠️ **블록은 날짜를 넘을 수 있다** (#252) — 활동 시간대가 자정을 넘는 사용자(예: 22:00~02:00)는
> `22:00` 시작 → 다음 날 `01:00` 종료 같은 블록을 받는다. 주간 그리드에서는 `startAt` 기준 날짜에
> 담긴다. 하루 칸 안으로 가두는 렌더링이면 잘리지 않는지 확인이 필요하다.
| POST | `/plans/replan` | **주간 forward 재계획** (S21 후속). 먼저 직전 완료 주의 주간 리포트를 작성(그 회복 수락분이 백로그로 상류 반영)하고, **다음 주 월요일(`windowStart`)부터 마감까지** 남은 작업을 다시 배치. 대상 = 다음 주 이후 **미착수(`scheduled`) 블록**의 액션(actionId dedup) + **활성 블록 없는 `planned` 백로그**(수락한 회복 포함)(v2.30-planA — 단 **이번 주(오늘~`windowStart` 전)로 날짜를 잡아 둔** 블록 없는 카드는 제외: 인박스에서 '할 일로' 바꾼 오늘 카드 등. 날짜가 지났거나 없는 카드만 백로그) + **밀린 일**(시작 시각이 이미 지났는데 한 번도 착수 안 된 `scheduled` 블록의 액션). **지운(보관)·완료한 목표의 카드는 제외**(v2.30-planA — 목표 없는 인박스 카드는 그대로 대상, 보류(parked) 목표는 제외하지 않음). 시작/완료·`user_edit` 블록은 불변(실패 원본은 미래 블록이 없어 자동 제외). ⚠️ **밀린 일 회수는 2026-08-25 에 추가됐다** — 그전에는 계획만 세우고 그냥 안 한 카드가 `list_scheduled_between`(미래만)·`list_planned_without_block`(비-cancelled 블록 보유)·만료 cron(`execution_events` 기준이라 [▶시작] 안 한 카드는 영원히 안 걸림) **셋 다에서 빠져** 재계획 후보에서 통째로 누락됐다. 가장 흔한 실패 모드가 정확히 이 경우다. busy = 교체하지 않고 **남는 모든 블록**(확정(시작/완료·`user_edit`) + v2.30-planA 부터 보존 카드의 예정 회차 — 형제 세션을 착수했거나 사용자가 옮긴 카드의 나머지 회차 위에 새 블록이 겹쳐 잡히던 것 수정) + **활동 시간대 밖**(v2.30-planA — 첫 계획과 같은 조립: 최근 완료 인터뷰의 활동창(설정에서 고친 값 우선)의 여집합 + no_touch. 인터뷰가 없으면 설정의 활동 시간대, 그마저 없을 때만 23:00~08:00 기본 수면창) + DB `time_policies` + **고정 일정(`fixed_schedules`, #112 정합)** + **Google 캘린더 일정**(v2.26 — 연결한 사용자만. 재배치 창 전체를 한 번 조회하되 **60일까지**; 지평이 그보다 길면 `warnings` 에 "캘린더 일정은 M월 D일까지만 반영했어요…", 못 읽으면 "캘린더 일정을 불러오지 못해서…" 한 줄. 연결 안 한 사용자에게는 아무 말도 없다). 각 새 블록에 '교체할 옛 미래 블록' `replacesBlockId`(없으면 백로그라 `null`)를 실어 승인이 재조정하게 한다. v2.30-planA: 같은 블록에 그 옛 블록의 원래 시각 `replacesStart`/`replacesEnd`(KST, 백로그면 `null`, additive)도 싣는다 — 미리보기가 내부 id 대신 '기존 → 새' 시각을 보여 주게. ⚠️ 회차별 1:1 매핑이 아니다 — 한 카드가 여러 회차로 나뉘면 새 회차 전부가 **대표(첫) 옛 블록**의 같은 시각을 싣는다(밀린 카드면 그 과거 블록). '이 카드가 원래 있던 자리' 로 읽을 것. `horizon` = 미래 블록·backlog `targetDate` 의 최댓값; 마감 신호가 없으면 **최소 다음 주(월~일)** 로 분산(하루 붕괴 방지), 먼 미래는 1년으로 상한. **목표 마감이 지평보다 이른 카드는 자기 마감(포함)까지만 배치**(v2.30-planA — 마감이 이른 목표부터 배치하고 그 블록을 다음 묶음의 회피 대상으로 넘긴다. 마감 전에 다 못 넣은 세션은 마감 뒤로 밀지 않고 `warnings` 에 "'<목표>' 마감(M월 D일) 전에 빈 시간이 모자라 N개 일정은 넣지 못했어요…" 한 줄. 마감이 `windowStart` 전에 **이미 지난** 목표의 카드는 버리지 않고 전체 지평에 배치하되 "'<목표>' 마감(M월 D일)이 이미 지나서, 남은 일정은 그 뒤로 잡아 뒀어요…" 한 줄). **후보 분량은 액션의 전체 live 블록 기준**: 형제 세션이 하나라도 `started`/`finished` 이거나 카드에 `user_edit` 블록이 있으면 그 액션은 **통째 보존**(후보 제외, 승인 가드와 동일 규칙), 교체되지 않고 살아남는 **미래** 블록의 분(分)은 `estimatedMinutes` 에서 차감 — 주 경계를 걸친 분할 세션이 이중 배치되지 않게. Draft 를 `plan_drafts` 에 저장, `isDraft=true`. **만료(`expiresAt`)는 기본 72h 이되 자기 `windowStart` 00:00 KST 를 넘지 않는다** — 창이 시작된 뒤 승인해 과거 블록이 생기는 것 방지(늦은 승인은 410 `PLAN_DRAFT_EXPIRED`). 동시성 lock 미획득 409 `AGENT_CONCURRENT_ACCESS` (#117) |
| POST | `/plans/replan/{planId}/approve` | 재계획 Draft 승인 → **action 단위 재조정**으로 미래 블록 교체(blanket-cancel 없음). #115 스케줄러가 긴 액션을 여러 세션 블록으로 쪼개므로, payload 의 **`oldBlocks`(액션당 옛 블록 전부)** 를 권위로 액션마다 재조정: 옛 블록 중 하나라도 `started`/`finished` → 액션 **전체 보존**(skip) / **옛 블록 중 하나라도 `source='user_edit'`(생성 후 사용자가 직접 옮김) → 액션 전체 보존**(skip, 쓰기 시점 재확인 — 생성 시점 필터만으로는 HITL 검토 창 사이 편집을 놓쳐 사용자 배치를 파괴한다) / 활성(`scheduled`) 옛 블록이 하나도 없음(그새 전부 취소·삭제) → 중복 방지 skip / 그 외 → 활성 옛 블록 **전부 취소** + 새 세션 블록 **전부 생성** / 백로그(옛 블록 없음)인데 그새 활성 블록 생김 → 생성 skip / action 이 그새 아카이브(#113) → skip. Draft 로드·검사~쓰기를 `user_agent_lock`(xact-scoped) 안 **단일 commit** 으로 원자화(동시 더블 승인 봉합, #113 패턴). 만료 410 `PLAN_DRAFT_EXPIRED`. 응답 `isDraft=false` + `{cancelledBlocks, createdBlocks, skippedBlocks}` (#117) |
| POST | `/plans/mandala/subgoals` | **만다라트 Stage A**(PR5, S30). body `{ goalId }`(`goal.isUltimate=true` 여야, 아니면 404 `GOAL_NOT_FOUND`). 궁극목표 → 하위목표(축) 8개 후보(LLM 1콜, lock 없음, DB 쓰기 0). 응답 `MandalaSubgoalsResponse`(Draft Layer) — `subgoals[8]` 은 사용자가 인터뷰에서 직접 말한 축(`pillarsHint`)이면 `locked=true`·`source="user"`, LLM 생성이면 `source="llm"`, 모자라 도메인 축 카탈로그로 채워지면 `source="rule"` |
| POST | `/plans/mandala/generate` | **Stage B**(S30). body `{ goalId, subgoals[8] }` — Stage A 를 사용자가 로컬에서 확인·편집한 8축 그대로(구조 편집은 여기까지, 이후 축 개수·순서 고정). 축마다 실행 셀 최대 8개(LLM 1콜, lock 있음, `plan_drafts`(kind="mandala") 1행·72h). 응답 `MandalaDraftResponse` — `cells[≤64]` + 못 채운 칸은 `gaps[]`(억지 패딩 없음, `goal_decompose` 와 동일 원칙) |
| GET | `/plans/mandala/{planId}` | 저장된 만다라 Draft 미리보기 재구성(LLM 0회). **First Plan/재계획 draft id 를 넣으면 404** `PLAN_DRAFT_NOT_FOUND`(kind 불일치, `GET /plans/{planId}` 의 반대 방향 같은 가드) |
| POST | `/plans/mandala/{planId}/regenerate-branch` | 링(8칸) **1개만** 재생성(LLM 1콜, lock 있음, draft UPDATE). body `{ subgoalIndex, userHint?, editedSubgoals?, editedCells? }` — 나머지 칸의 현재 편집 상태를 함께 실어 보낸다(비우면 저장된 스냅샷 사용). `source="user"`인 기존 셀(사용자가 이미 직접 편집)은 절대 재생성 대상에서 빠지지 않고 그대로 보존 |
| POST | `/plans/mandala/{planId}/approve` | 승인(LLM 0콜, 단일 트랜잭션). body `{ centerWhyText?, subgoals[8], cells[] }` — 셀 편집(HITL 최하위 층)은 여기서 처음 서버에 닿는다(승인 전엔 서버 호출 0). `goal_nodes` 최대 73행(`tree_kind="mandala"`) 영속, 같은 목표의 기존 활성 만다라 트리는 보관 후 교체(재승인 누적 방지). 응답 `{ planId, isDraft:false, goalId, rootNodeId, activated, skipped, carriedOver, activatedAt }`. ⚠️ **이미 트리가 있으면 이 호출이 곧 '다시 세우기'다**(v2.05) — 옛 트리를 보관하고, 사용자가 손으로 쌓은 셋만 **제목이 같은 자리**로 이어붙인다. 축은 `title`, 칸은 `(축 title, 칸 title)` 이 키다(칸 제목만으로 맞추면 "매일 30분" 같은 흔한 칸이 엉뚱한 축으로 건너뛴다). 이어지는 것: 칸의 `completedAt`, 축의 `promotedGoalId`, 반복형 칸의 `habits.goalNodeId`. **AI 가 채운 제목·이유는 안 이어진다** — 다시 세우기의 목적 자체가 그것이다. 자리를 못 찾은 쪽은 **지우지 않는다**: 승격된 목표는 그대로 남고(축 배지만 빠짐), 습관은 `goalNodeId=null` 로 링크만 풀려 단독 습관이 된다(주간 횟수 기록 보존). `carriedOver` = `{ completedCells, promotedAxes, linkedHabits, droppedPromotedAxes[], droppedLinkedHabits[] }` — 앞 셋은 이어진 개수, 뒤 둘은 끊긴 것의 이름이다. 처음 세우면 전부 0/빈 배열. 무엇이 걸려 있는지 **승인 전에** 보려면 `GET /goals/{id}/mandala/rebuild-preflight`(§6). 멱등 — 이미 승인된 draft 재호출 시 재영속화 없이 같은 결과 반환(`carriedOver` 도 승인 시점 스냅샷 그대로). 만료 410 `PLAN_DRAFT_EXPIRED` |
| POST | `/plans/mandala/next-cycle` | **축 → 다음 2주 계획**(v2.06, U14, ADR-0008 §3·§8 "G"). 만다라트를 실행으로 잇는 진입점 — 승격 + 시드 교체 + 계획 생성을 한 번에 한다. body `{ nodeId, goalTier?="focus", targetDate?, density?="standard", useCellsAsMilestones?=true }`. **축(depth=1)만** 대상(중앙·셀은 422 `COMMON_VALIDATION_ERROR`, `promote` 의 가드와 같은 자리). ① 축을 승격한다 — **멱등**이라 이미 승격됐으면 그 목표를 그대로 쓰고 `goalTier` 는 무시된다(기존 tier 유지). 새로 승격할 때만 Focus≤3/Maintain≤5 를 재고 초과 시 422 `GOAL_TIER_LIMIT_EXCEEDED`. ② 시드는 **최근 '정상 종료' 계획 인터뷰 → 온보딩 프로필** 순으로 찾는다(v2.07). 인터뷰가 있으면 그 outcome 의 `coreGoals` 만 이 축으로 갈아끼우고 정체성·활동 시간대·선호는 사용자가 답한 값 그대로 쓴다(`seedSource="interview"`). 인터뷰가 아직 없으면 `behavioral_profiles`(온보딩·설정에 저장된 **활동 시간대**·피크·집중 길이) + `interaction_styles.recovery_tone` + `users.focus_mode_preferences` 를 슬롯으로 되돌려 쓴다(`seedSource="profile"`, `warnings` 에 그 사실이 실린다). 지어내는 게 아니라 사용자가 직접 넣은 값을 되돌리는 것이고, 못 채운 슬롯은 `build_outcome` 의 기본값으로 가되 그 키가 `unresolvedSlots` 에 남는다. ⚠️ **활동 시간대를 어디서도 모르면 422** — 인터뷰도 프로필도 없거나 프로필에 `preferred_start_time`/`preferred_end_time` 이 비어 있는 경우다. 피크·집중 길이는 기본값으로 굴러가지만 활동창은 '언제 배치해도 되는가' 라 모르면 배치가 통째로 추측이 된다. ③ `useCellsAsMilestones`(기본 on)면 **그 축의 칸들이 계획 뼈대(마일스톤)** 가 된다(완료 표시된 칸은 제외, 칸 순서 유지). **반복형(습관 링크) 칸과 규칙이 채운 자리표시 칸(`source="rule"`, 예: "건강 1단계")도 뺀다**(v2.30-goals — 반복형은 카드가 아니라 습관 횟수로 세고, 자리표시는 뼈대가 아니다). 다 빠지면 자동 분해로 간다. 끄면 분해가 축 제목만 보고 다시 지어낸다. ④ 이후는 `POST /plans/generate` 와 **완전히 같은 경로**(분해→배치→Draft 저장). **지평은 2주** — 여기에 새 규칙을 넣은 게 아니라 시드의 heaviest 제목이 승격된 목표와 같아 기존 `_max_plan_weeks` 판정이 그대로 걸린다. 응답은 `FirstPlanResponse` + `axis` (`{ nodeId, orderIndex, title, goalId, goalTier, newlyPromoted }`) + `seedSource`(`"interview"` | `"profile"`). ⚠️ **자동 적용이 아니다**(§1.4) — `isDraft=true` 이고 카드·블록은 기존 `POST /plans/{planId}/approve` 를 눌러야 생긴다. 만다라 전용 승인 경로를 만들지 않은 이유는 HITL 게이트를 두 벌로 만들지 않기 위해서다 |
| POST | `/plans/{planId}/discard` | (재사용) 만다라 draft 폐기도 이 기존 endpoint 그대로 — 204, kind 무관 |

> `milestones`·자료 검색 3단계(`materials/search-query`~`materials/confirm`)·자료 검색 파이프라인(ADR-0010, `materials/study-method`~`materials/spec-confirm`, 계획 생성 반영까지 완료)·`generate`·`/plans/{planId}`·`approve`·`weekly`·블록 편집·`replan`(+`replan/{id}/approve`)·만다라트(`mandala/subgoals`~`mandala/{id}/approve`, `mandala/next-cycle`)는 구현 완료. `ai-edit`/`ai-edit/apply` 만 미구현(P1, 라우트 없음).

> **계획 생성 흐름(권장 순서)**: 인터뷰 종료 → (선택) `materials/search-query`→`search`→`confirm` 로 참고 자료 확정 → `milestones` 로 뼈대 확인·편집 → `generate` 에 그 목록을 실어 보냄 → `{planId}/approve`. 자료·마일스톤 단계는 **건너뛸 수 있고**(둘 다 없으면 현행 자동 분해) 순서만 지키면 된다 — 자료가 뼈대에 반영되려면 `milestones` **전에** 확정돼 있어야 한다. 한 번에 세우는 계획은 최대 4주이고 그 너머는 마일스톤이 들고 있다 — 층 분리의 근거와 주기 전환 설계는 [ADR-0007](decisions/0007-milestone-layer-and-plan-cycle.md).

응답 예 `POST /plans/generate` (#32, `FirstPlanResponse` — Draft Layer):
```json
{
  "isDraft": true,
  "aiSource": "llm",
  "planId": "plan_3f8c…",
  "targetDate": "2026-06-22",
  "horizon": "2026-07-12",
  "goalNodes": [
    {"nodeId": "n1", "parentId": null, "title": "캡스톤", "nodeType": "root", "orderIndex": 0, "isLeaf": true}
  ],
  "actionItems": [
    {"nodeId": "n1", "title": "저장소 세팅 30분", "estimatedMinutes": 30, "category": "study", "firstStep": "레포 clone"}
  ],
  "blocks": [
    {"start": "2026-06-22T09:00:00+09:00", "end": "2026-06-22T09:30:00+09:00", "title": "저장소 세팅 30분", "category": "study", "origin": "goal", "originId": "n1"}
  ],
  "warnings": [],
  "policyViolations": [],
  "generatedAt": "2026-06-22T08:00:00+09:00",
  "milestones": [{"title": "기초 문법", "summary": "변수·조건문·반복문"}]
}
```
> `planId` 는 `plan_drafts` 에 저장된 Draft 의 실제 UUID (#62) — `GET /plans/{planId}` 로 재조회, `POST /plans/{planId}/approve` 로 승인. `aiSource` 는 **보여 주는 계획의 분해**가 룰 fallback 이면 `"rule"` — 검토(④층)만 폴백한 건 세지 않는다(v2.30-planB, 검토 폴백은 '그대로 승인' 이라 계획 내용과 무관하다). 분해가 폴백했으면 `warnings[0]` 이 "이번엔 AI가 세부 내용을 만들지 못해 칸만 잡아 뒀어요 …" 인데 **사유에 따라 권하는 행동이 다르다**(예산 소진이면 "…내일 다시 만들어 보세요", 금지어·톤 게이트·프롬프트 누락이면 다시 불러도 같은 자리에서 걸리므로 "…카드를 눌러 하고 싶은 내용으로 직접 채워 주세요." 만 말하고 다시 만들기를 권하지 않는다), 그 계획에는 '이어가기'·지평·확정 마일스톤 안내가 붙지 않는다(앞쪽에 내용이 있다는 전제의 문장이라). 분해가 타임아웃·예산·금지어/톤 게이트 등 **다시 불러도 같은** 사유로 폴백했거나 그래프가 이미 90초를 넘겼으면 검토·재분해를 새로 시작하지 않는다(최악 대기 단축).

`milestones` 는 additive(ADR-0007 PR-2) — 요청에 실어 보낸 확정 마일스톤을 그대로 되비출
뿐(생략하면 `[]`), `goalNodes`(이번 4주 분해)와는 별개다. **이 Draft 를 승인하면 서버가
이 목록을 다시 읽어 `node_type='milestone'` 로 영속한다** — 단 그 goal 에 아직 활성
마일스톤이 없을 때만(멱등, 재승인 시 조용히 무시). `goalNodes` 처럼 매 승인마다
갈아치워지지 않고 마감까지 살아남는다(§6 `GET /goals/{id}/nodes` 참고).

응답 예 `POST /plans/replan` (#117, `ReplanResponse` — Draft Layer). 각 블록은 기존 `actionId` 에 연결되고, `replacesBlockId` 는 이 새 블록이 교체하는 옛 미래 블록(없으면 백로그라 `null`):
```json
{
  "isDraft": true,
  "aiSource": "rule",
  "planId": "3f8c…",
  "windowStart": "2026-07-13",
  "horizon": "2026-07-17",
  "blocks": [
    {"actionId": "action_5a1b…", "title": "챕터 3 정리", "category": "study",
     "start": "2026-07-14T08:00:00+09:00", "end": "2026-07-14T08:30:00+09:00", "replacesBlockId": "block_9c2d…"},
    {"actionId": "action_7e4f…", "title": "회복: 5분만 시작", "category": "study",
     "start": "2026-07-14T18:00:00+09:00", "end": "2026-07-14T18:30:00+09:00", "replacesBlockId": null}
  ],
  "warnings": [],
  "generatedAt": "2026-07-09T12:00:00+09:00"
}
```
> 승인(`POST /plans/replan/{planId}/approve`)은 `{planId, isDraft:false, cancelledBlocks, createdBlocks, skippedBlocks, activatedAt}` 반환 — `skippedBlocks` 는 재조정으로 보존(옛 블록이 그새 시작/취소되어 교체 skip)되거나 중복 방지로 생성 skip 된 항목 수.

응답 예 `POST /plans/mandala/generate`(PR5, `MandalaDraftResponse` — Draft Layer). 좌표는 서버가 안 내린다 — `(depth, parent.orderIndex, orderIndex)` 로 FE 가 계산(`SLOT=[0,1,2,3,5,6,7,8]`, 설계서 §7.3):
```json
{
  "isDraft": true,
  "aiSource": "llm",
  "planId": "9f2c…",
  "goalId": "goal_3f8c…",
  "center": {"title": "메이저리그 8구단 드래프트 1순위", "whyText": null},
  "subgoals": [
    {"orderIndex": 0, "title": "구위", "whyText": null, "source": "user", "locked": true},
    {"orderIndex": 1, "title": "체력", "whyText": "몸이 버텨야 나머지가 의미 있다", "source": "llm", "locked": false}
  ],
  "cells": [
    {"subgoalIndex": 0, "orderIndex": 0, "title": "주 3회 불펜피칭", "source": "llm"}
  ],
  "gaps": [
    {"subgoalIndex": 1, "orderIndex": 7, "reason": "AI가 이 칸을 채우지 못했어요"}
  ],
  "generatedAt": "2026-08-20T15:00:00+09:00"
}
```
> 승인(`POST /plans/mandala/{planId}/approve`)은 `{planId, isDraft:false, goalId, rootNodeId, activated, skipped, carriedOver, activatedAt}` 반환 — `rootNodeId` 는 `node_<uuid>`(§1.8), `activated`=1(중앙)+8(축)+영속된 셀 수, `skipped`=최대 64칸 중 저장 안 된 칸 수(gaps 로 남은 만큼).

#21-B 구현 메모 (S14/S15 — 영속 `scheduled_blocks` 읽기/이동):
- Plan 테이블 없음 — `planId` 는 주(週) 논리 식별자(`plan_<weekStart>`). 편집 권한은 `blockId`.
- `GET /plans/weekly?weekStart=` — 그 주 월요일로 정규화(생략 시 이번 주). 날짜 형식이 아니면 422
  `PLAN_INVALID_TIME`, `field="weekStart"`, 메시지 "날짜 형식이 올바르지 않아요 (YYYY-MM-DD)."
  (문장에 요청 필드 이름을 싣지 않는다 — `/calendar` 의 같은 문구와 맞췄다). 7일 × `blocks[]`
  (blockId/actionId/title/category/**goalId**/startAt/endAt/blockStatus/source/**calendarConflict**(v2.27)/**completionStatus**(v2.30-planA)), KST 직렬화. 최상단 `calendar`(v2.27, §10 "캘린더 겹침").
  `blockStatus` 는 `scheduled`/`started`/`finished` 뿐이다(`done`/`failed` 는 없다) — 체크인은 결과와
  무관하게 블록을 `finished` 로 닫는다. 완료·실패 구분은 `completionStatus`
  (`done`/`partial_done`/`failed`/`over_done`, 끝난 블록의 마지막 체크인 결과 — **블록 단위**라
  나뉜 회차마다 다를 수 있다. `finished` 가 아니거나 기록이 없으면 null).
  `goalId` = 블록이 매달린 action_item 의 goal FK(`goal_<uuid>`, 미연결이면 null) — FE 가
  블록을 목표 분류(집중/유지)·색상과 연결할 수 있게 한다 (마이그레이션 없음, 기존 컬럼 노출).
- `PATCH /plans/{planId}/blocks/{blockId}` — `{ startAt, endAt? }`. **15분 snap**(가장 가까운 경계),
  `endAt` 생략 시 기존 길이 보존. 시간 충돌 422 `PLAN_BLOCK_CONFLICT`(cancelled·자기 제외,
  v2.30-planA 부터 **고정 일정(`fixed_schedules`)과의 겹침도** — "그 시간에는 '<수업>' 고정 일정이
  있어요…"), 정책 위반 422 `POLICY_VIOLATION`(sleep/lunch/late_night_block 윈도우 + v2.30-planA
  **no_touch**(요일 포함). 메시지는 한국어 — 정책 코드를 그대로 싣지 않는다), 잘못된 시각 422
  `PLAN_INVALID_TIME`, 블록 없음 404 `PLAN_BLOCK_NOT_FOUND`. 적용 시 `source='user_edit'`.
  **이미 시작/끝낸 블록(`blockStatus` started/finished)은 시간을 못 옮긴다**(v2.30-planA) — 다른
  시각이면 422 `PLAN_INVALID_TIME`("이미 시작했거나 끝낸 일정은 옮길 수 없어요…"). 지금 시각을
  그대로 보내고 `title`/`category` 만 바꾸는 편집은 200 이며, 이때 시각·`source` 는 그대로다.
- **정책 집합은 승인·재계획과 같다**(v2.30-planA 후속): DB `time_policies` + **활동 시간대 밖**
  (최근 완료 인터뷰의 활동창 — 설정에서 고친 값 우선 — 의 여집합을 수면으로, no_touch 포함.
  인터뷰가 없으면 설정의 활동 시간대, 그마저 없을 때만 23:00~08:00 기본 수면창). `POST /plans/replan`
  과 **같은 조립**이다. 예전엔 DB `time_policies` 만 봤는데 그 행을 만드는 FE 화면이 없어 실사용자는
  늘 빈 목록이었다 — 08~16 시에만 활동한다고 답한 사용자도 블록을 새벽 3시로 옮길 수 있었고, 같은
  시각이 승인에서는 422 였다.
- 정책 판정은 순수 함수 `orchestrator/plan_edit.py`. 고정 일정·`no_touch` 는 생성·승인과 같은 busy
  전개(`fixed_schedules_to_busy`/`time_policies_to_busy`)로 본다(v2.30-planA). 시각 `"24:00"`(하루 끝)은
  스케줄러와 같게 읽는다 — 예전엔 그 정책을 통째로 건너뛰어 활동창 여집합의 저녁 조각(16:00~24:00)이
  검사되지 않았다. `break_min`·캘린더 겹침은 편집 때 막지 않는다(캘린더는 `calendarConflict` 로 표시).
  DB 마이그레이션 없음.
- `GET /plans/weekly` 의 `days[].fixedSchedules`(v2.30-planA, additive): 그날의 고정 일정
  `[{title, startAt, endAt}]`(KST, 자정을 넘는 일정은 그날 안의 조각으로). 편집이 막는 시간을
  그리드에 보이게 하려는 것 — FE 는 옮길 수 없는 칸으로 그린다.
- `GET /plans/weekly` 의 `days[].calendarBusy`(v2.34, additive): 그날의 Google 캘린더 약속
  `[{startAt, endAt}]`(KST, 자정을 넘는 약속은 그날 안의 조각으로 — 주 밖의 조각은 싣지 않는다).
  **구간만** 온다 — 제목·장소는 `calendar.freebusy` 스코프라 서버도 모른다(§9). FE 는 "캘린더 일정"
  같은 이름 없는 칸으로 그린다. 블록과 **겹치지 않아도** 싣는다(겹친 블록은 종전대로 `calendarConflict`).
  `calendarConflict` 판정과 **같은 조회**(5분 캐시·2초 상한)라 Google 호출이 늘지 않는다.
  `calendar.status` 가 `ok` 가 아니면 모든 날이 빈 목록이다 — `failed` 인데 빈 목록을 "약속 없음" 으로
  그리지 말 것. 편집은 이 구간을 막지 않는다(캘린더 겹침은 편집 때 막지 않는 종전 규칙 그대로).
- `generate`/`mandala/subgoals`/`mandala/generate`(LLM 호출) 은 사용자별 일일 호출
  상한 대상(module="planning" 공유) — 초과 시 429 `RATE_LIMIT_DAILY_CALLS_EXCEEDED`(§1.10, #325).

---

## 9. Calendar (`/calendar`) — S04

> ✅ **OAuth 보류 해제** (ADR-0009 D4, 팀 합의). Issue #17 이 미뤄 뒀던 `/calendar/connect` 가 실구현이다. 계기는 계획 단위가 분으로 바뀌면서(ADR-0009) "외부 일정 앞뒤"를 모르는 스케줄러가 못 지킬 계획을 만들기 때문.
>
> **읽기 전용이다.** 스코프는 `https://www.googleapis.com/auth/calendar.freebusy` **하나** — 구간의 길이와 인접성만 있으면 스케줄러의 세 룰(전이 버퍼·부하 감쇠·자투리)이 전부 성립하고 **제목·장소는 읽지 않는다**. `calendar.readonly` 확대는 ADR 을 먼저 고쳐야 한다.
>
> `events.insert`(write-back)는 **P1 유지** — `sync-preview`/`approve-insert` 는 아직 stub 이다.
>
> ✅ **이미 세운 계획에도 반영된다(v2.27).** `GET /today/agenda` · `GET /plans/weekly` 가 화면을 열 때마다 캘린더를 읽어 겹치는 블록에 `calendarConflict` 를 달고(주간 그리드는 약속 구간 자체도 `days[].calendarBusy` 로 싣는다, v2.34), 06:00 모닝 브리프가 오늘 겹침을 알린다(§10 "캘린더 겹침"). 주기 동기화·webhook 은 없다 — webhook(`events.watch`)은 일정 제목까지 읽는 스코프가 필요해 ADR-0009 D4 범위 밖이다.
>
> ✅ **freebusy 는 계획에 반영된다.** `POST /plans/generate` 와 **`POST /plans/replan`(v2.26)** 이 각자의 지평 전체 캘린더 일정을 **한 번** 조회해 스케줄러의 busy 소스로 넣는다(고정일정·시간정책·기존 블록과 나란히). 캘린더를 못 읽어도 계획 생성은 실패하지 않고, **연결해 둔 사용자에게만** `warnings` 한 줄로 알린다(연결 안 한 사용자에게는 아무 말도 하지 않는다). 토큰 갱신이 일시적으로 실패해도(네트워크·5xx) "캘린더 일정을 불러오지 못해서…" 로 알린다(v2.30 전에는 조용히 넘어갔다). **연결이 Google 쪽에서 끊겼으면**(권한 철회·refresh token 만료 — 갱신이 `invalid_grant`) 대신 "Google 캘린더 연결이 끊겨서 이번 계획에는 캘린더 일정을 반영하지 못했어요. 설정에서 다시 연결하면 다음 계획부터 반영돼요." 를 **맨 앞에** 싣는다(v2.30) — 사용자가 다시 연결하거나 `DELETE /calendar/connect` 로 정리할 때까지 매 계획에. ⚠️ 지금은 **겹치기 회피까지**다 — 앞뒤 이동 시간(전이 버퍼)과 직전 일정 길이에 따른 부하 감쇠는 아직 없다(ADR-0009 D4 ①②).
>
> ⚠️ **기능 스위치**: `GOOGLE_CALENDAR_ENABLED=false`(기본)이거나 `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`·토큰 암호화 키가 비어 있으면 `GET`/`POST /calendar/connect` · `GET /calendar/freebusy` 는 예전처럼 `501 COMMON_NOT_IMPLEMENTED`. **`DELETE /calendar/connect`(해제)는 예외다(v2.30)** — 동의 철회는 스위치와 무관하게 204. 스위치가 꺼져 있는 동안에는 계획 생성·재계획·화면·브리프 **어느 경로도 캘린더를 읽지 않는다**(v2.30 전에는 계획 생성·재계획이 계속 읽었다). Cloud 콘솔 셋업은 사람 손이 필요해서, 준비 전에 배포돼도 사용자가 깨진 동의 화면을 만나지 않게 하는 안전핀이다. 그동안 FE 는 "수동 입력으로 시작"(`POST /fixed-schedules`) 경로를 유지한다.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/calendar/connect` | 연결 상태. 응답 `{provider, connected, scopes, needsReconnect}` — 연결이 없으면 404 가 아니라 `connected: false`, `scopes: []`. **`needsReconnect`(v2.30, additive)**: `connected: false` 인데 그게 **Google 쪽에서 끊겨서**면 `true`(권한 철회·refresh token 만료로 갱신 실패). 앱에서 해제했거나 연결한 적이 없으면 `false` — FE 는 `true` 일 때만 "연결이 끊겼어요 · 다시 연결" 을 그린다. 다시 연결하거나 `DELETE` 하면 `false`. 스위치가 꺼져 있으면 501(FE 는 '준비 중') |
| POST | `/calendar/connect` | OAuth code → 토큰 암호화 저장. **멱등** — 재연결은 새 행이 아니라 기존 연결 갱신. 응답 `{provider, connected, scopes, needsReconnect}`(`needsReconnect` 는 항상 `false`). code 는 GIS popup 코드 흐름(`initCodeClient`, 스코프 `calendar.freebusy`)으로 받는다 — 서버가 `redirect_uri=postmessage` 로 교환한다. code 가 만료·재사용이거나, 동의 화면에서 **캘린더 체크를 풀었으면** 422 `COMMON_VALIDATION_ERROR` |
| DELETE | `/calendar/connect` | 연결 해제 — `revoked_at` soft delete + Google 권한 회수(best-effort). **204, 멱등** — 연결이 없어도 204 다. **기능 스위치와 무관**(v2.30 — 꺼져 있어도 501 이 아니다). 저장된 토큰을 복호화할 수 없으면 원격 회수만 건너뛴다. Google 쪽에서 이미 끊긴 연결(`needsReconnect: true`)에 부르면 재연결 안내를 거둔다(`needsReconnect: false`, 계획 경고도 멈춘다) |
| GET | `/calendar/freebusy?from=&to=` | read-only freebusy. `from`/`to` 는 **KST 날짜**(`YYYY-MM-DD`), 양끝 포함, 최대 60일. 연결 없으면 404 `CALENDAR_NOT_CONNECTED`(빈 목록이 **아니다** — "일정 없음" 과 구분돼야 한다), Google 실패는 502. 범위 오류는 422 |
| POST | `/calendar/sync-preview` | 계획 → 캘린더 이벤트 미리보기 + 충돌 체크 |
| POST | `/calendar/events/approve-insert` | 사용자 승인 일괄 삽입 (Idempotency-Key) |

가드:
- 권한 박탈/refresh 실패(`invalid_grant` 만 — `invalid_client`·429 등 서버 설정·한도 문제로는 연결을 끊지 않는다, v2.30) → `GET /calendar/freebusy` 404 `CALENDAR_NOT_CONNECTED` + `GET /calendar/connect` 의 `needsReconnect: true` + 계획 생성·재계획 `warnings` 의 재연결 안내. 화면(`calendar.status`)은 `not_connected` 그대로다
- 충돌 발견 → 409 `CALENDAR_CONFLICT` (충돌 블록 목록 포함)

---

## 10. Today / Execution (`/today`) — S10~S13

| Method | Path | 설명 | 상태 |
| --- | --- | --- | --- |
| GET | `/today/agenda` | 어젠다 단일 조회 (`date` + `brief` + `cards` + `habits` + `fixedSchedules` + `calendar`(v2.27)) | ✅ #19-A |
| GET | `/today/actions/{actionItemId}` | 카드 상세 (S11) | ✅ #19-A |
| POST | `/today/actions/{actionItemId}/start` | [▶ 시작] → `execution_events` 생성 | ✅ #19-B |
| POST | `/today/actions/{actionItemId}/cancel` | 카드 취소 = soft delete (`archived_at`, **status 불변**) | ✅ #214 |
| POST | `/today/focus/{executionId}/pause` | [⏸] + `interruption_events` INSERT (이미 정지 중이면 200 멱등, v2.30-today) | ✅ #83 |
| POST | `/today/focus/{executionId}/resume` | [▶ 계속] (정지 중이 아니면 200 멱등, v2.30-today) | ✅ #83 |
| POST | `/today/check-ins` | Quick Check-in 4칩 | ✅ #19-B (context_snapshot 캡처는 #19-B-2) |

`completion_status`: `done` / `partial_done` / `failed` / `over_done`

**#19-A 조회 (구현)**:
- `GET /today/agenda` — KST 오늘 기준. `brief`(daily_briefs, Morning Brief cron #19-C 가 채움; 없으면 null), `cards`(action_items, 오늘 target_date, priority 오름차순), `habits`(이번 주 habit_instances 진행), `fixedSchedules`(오늘 요일에 걸린 것). ID prefix `action_`/`hinst_`/`habit_`/`fixed_`
- **자정을 넘긴 카드 (v2.30-today)** — `cards` 는 오늘 target_date 카드 **뒤에**, 날짜는 지났지만 아직 손에서 놓지 않은 카드를 이어 붙이고 `AgendaCard.carriedOver=true` 로 표시한다(오늘 날짜 카드는 항상 `false`, 중복 없음). 대상은 보관 안 된 카드 중 둘 중 하나:
  1. **진행 중(in_progress) 실행**이 회고 창 안에 있다 — 창 기준은 `/reflection/pending` 과 같다(§11, 계획·착수 시각 중 나중 ≥ 그제 0시). 23:40 에 시작한 카드가 00:00 에 화면에서 사라지지 않고, 창을 벗어나면 만료 cron 이 정리한다. `executionId` 가 실려 있어 그대로 체크인할 수 있다
  2. **어제 시작해 아직 안 끝난 블록**(`startAt` < 오늘 0시 < `endAt`, 지금 < `endAt`, 미종결)이 있다 — 23:30~00:30 블록을 00:05 에 늦게라도 시작할 수 있게. 다음 날 세션 블록은 여기 안 걸린다
  체크인으로 끝나면 다음 조회부터 빠진다. FE 는 '어제 이어서' 같은 표시만 얹으면 된다
- **여러 날로 쪼갠 카드 (v2.30-today)** — 긴 카드는 여러 날의 세션 블록으로 쪼개질 수 있다(주간 재계획 등). 카드는 1장이고 `target_date` 는 가장 이른 블록의 날짜라, 예전엔 둘째 날부터 카드가 오늘 화면에서 사라졌다. 이제 `cards` 는 target_date 가 오늘인 카드에 더해 **오늘(KST) 시작하는, 취소 안 된 세션 블록**이 있는 카드도 오늘 카드로 싣는다(`carriedOver=false`, 중복 없음, 같은 priority 정렬). `finished` 블록도 센다 — 오늘 회차를 체크인해도 카드가 오늘 화면에서 사라지지 않게. 같은 목록을 모닝 브리프·코칭 조언의 '오늘/어제 카드' 도 쓴다
- `GET /today/actions/{id}` — `action_<uuid>`. 없으면 404 `COMMON_NOT_FOUND`
**#19-B 실행 쓰기 (구현)**:
- `POST /today/actions/{id}/start` — 미종결 scheduled_block 있으면 사용, 없으면 **즉석(ad-hoc) 블록 생성**(source=`user_edit`, §5.10)으로 NOT NULL 의존 해소. 응답 `{ executionId, actionId, completionStatus, actualStartAt }` (201)
  - **같은 카드가 이미 진행 중이면 그 실행을 200 으로 돌려준다**(v2.30-today, 종전 409 `TODAY_EXECUTION_ALREADY_ACTIVE`). 새 실행·블록을 만들지 않고 카드 상태도 안 건드린다. 응답 모양은 같고 `actualStartAt` 은 **처음 시작한 시각**이다 — 앱을 다시 열어 실행 id 를 잃은 FE 가 [이어서 하기] 로 start 를 다시 불러도 같은 실행을 이어받아 체크인할 수 있다. 끝난(체크인한) 실행은 되살리지 않는다 — 그 뒤 start 는 새 실행(201)
  - 다른 카드가 진행 중이어도 시작은 막지 않는다(종전과 같음). `TODAY_EXECUTION_ALREADY_ACTIVE` 코드는 남아 있지만 이 경로는 더 이상 내보내지 않는다
- `POST /today/check-ins` — `{ executionId, completionStatus(4칩), userRating?, userFeedback? }`. execution 종결(actual_end_at·duration) + 블록 finished + **`action_item.status` 전이**(execution 레이어의 합의된 유일 지점). feedback 은 at-rest 암호화. 재체크인 409 `TODAY_ALREADY_CHECKED_IN`. 응답 `needsFailureTags=true`(failed/partial_done) → S18 → §11 태깅 → §12 Recovery 로 연결
  - **`actualDurationMinutes` = 일한 시간(v2.30-today)** — (체크인 시각 − 착수 시각) − `pauseTotalMinutes`. 예전엔 정지 시간까지 셌다. 정지 중에 체크인하면 그 정지를 체크인 시각에 닫고(재개 없이 끝냄) 그 시간도 뺀다. 주간 리뷰 `effort.actualMinutes` 의 재료다
  - **`done`/`over_done` 이면 이 카드의 남은 세션 블록을 정리한다**(v2.30-today). 쪼갠 카드의 한 회차에서 '완료' 하면 카드는 끝난 것이라, 아직 `scheduled` 인 다른 회차 블록을 `cancelled` 로 바꾼다 — 주간 그리드에 할 일로 남지 않고 '곧 시작'(pre_card) 알림도 오지 않는다. `finished`(수행 이력)·`started` 블록과 사용자가 직접 옮긴 블록(`source=user_edit`)은 건드리지 않는다. `partial_done`/`failed` 는 '아직 남았다' 라 남은 회차를 그대로 둔다 — 다음 [▶ 시작] 은 가장 이른 미종결 블록을 잡는다. `POST /reflection/batch` 도 같은 규칙. pre_card 알림은 블록 상태와 별개로 **끝낸(done/over_done) 카드의 블록엔 보내지 않는다**(이중 방어)
- `POST /today/focus/{executionId}/pause`·`/resume` — 응답 `{ executionId, actionItemId, startedAt, endedAt, status(paused|in_progress), pauseTotalMinutes }`. pause 는 user_pause 정지 구간을 열고, resume 은 그 구간을 닫아 정지 시작부터 지금까지를 `pauseTotalMinutes` 에 더한다. 체크인이 끝난 실행은 409 `TODAY_ALREADY_CHECKED_IN`, 없는 실행은 404 `TODAY_EXECUTION_NOT_FOUND`
  - **둘 다 멱등(v2.30-today)** — 이미 정지 중인데 pause 를 다시 보내면 새 구간을 열지 않고 200 `paused`, 정지 중이 아닌데 resume 을 보내면 아무것도 안 바꾸고 200 `in_progress`(종전 409 `TODAY_ALREADY_PAUSED`/`TODAY_NOT_PAUSED` — 코드 정의는 남아 있지만 이 경로는 더 이상 내보내지 않는다). 응답을 잃은 FE 의 재시도가 영영 실패하지 않게
  - 6시간 넘게 재개 안 된 정지는 cron 이 '6시간 안에 안 돌아옴' 으로 **표시만** 한다 — 정지는 열린 채라, 저녁에 돌아와 [▶ 계속] 을 눌러도 재개되고 그 시간이 `pauseTotalMinutes` 에 들어간다
- context_snapshot 캡처는 #19-B-2 후속

**카드 취소 (#214)**:
- `POST /today/actions/{id}/cancel` → **204**. `archived_at` 만 세팅하고 **`status` 는 바꾸지 않는다**(AGENTS §2 — 원본 status 는 Resilience 지표의 전제). 조회가 전부 `archived_at IS NULL` 로 걸러 오늘 어젠다·백로그에서 빠진다
- 카드에 걸린 **미종결 블록(`scheduled`/`started`)은 같은 트랜잭션에서 `cancelled`** 가 된다(v2.30-today). 예전엔 카드만 보관돼 블록이 주간 그리드(`GET /plans/weekly`)에 유령으로 남고(눌러 보면 404), 그 시간대로 다른 블록을 옮기면 422 `PLAN_BLOCK_CONFLICT` 로 막혔다. 계획 교체·만료 cron 이 '카드 보관 + 블록 취소' 를 짝으로 처리하는 것과 같은 규칙. `finished` 블록(수행 이력)은 건드리지 않는다
- **취소 가능 조건 3개 전부**: `status='planned'` + 실행 이력 없음 + `source ∈ {inbox, manual}`. `recovery_*` 는 `resulting_action_item_id` 로 회복 지표와 얽혀 있고, `goal`/`habit` 파생은 계획 정합성이 걸려 있어 제외
- 조건에 안 맞으면 422 `COMMON_VALIDATION_ERROR`(`field="actionId"`), **사유별로 다른 message** — '이미 시작한 일' 과 '계획에 묶임' 을 FE 가 구분해 안내할 수 있게
- **이미 취소된 카드에 다시 호출해도 204**(멱등). 없는 카드는 404 `COMMON_NOT_FOUND`
- 되돌리기(restore)는 **없다** — 자료의 걸음에서 다시 담으면 복구된다. FE 는 취소 직후 5초 스낵바를 띄우고 **그 뒤에** 이 API 를 호출한다(되돌리면 요청 자체를 안 보냄)
- `AgendaCard.cancellable` (파생 필드, DB 컬럼 아님) — 위 3조건의 판정 결과. **판정 규칙은 서버에만 있다**(`domain/action_cancel.py`): FE 는 `status`·`source` 만 받아 '실행 이력 없음' 을 계산할 수 없고, 규칙을 복제하면 바뀔 때 조용히 어긋난다
- 지표 영향 없음 — 취소 가능한 카드는 정의상 실행 이력이 없어 주간 KPI(`execution_events` join)에 애초에 들어간 적이 없다

**T1 미체크 배지 (근거 대장 §6.2, reaction-frontend#224)**:
- `AgendaCard.missedCheckIn` (파생 필드, DB 컬럼 아님) — 이 카드의 (취소 안 된) 블록 중
  `blockStatus='scheduled'`(아직 [▶ 시작] 전) 이고 `startAt` + **유예**가 지난 것이 있으면
  `true`. 유예는 **블록 길이 × 0.3, 단 [5분, 20분]** 이다(v1.98, ADR-0009 D5) — 약 67분부터는
  상한에 닿아 종전(고정 20분)과 같고, 짧은 블록만 완화된다. ⚠️ 상한 20분은 그대로다(근거 대장
  §6.2 T1). 15분짜리 블록에 20분 유예를 주면 배지가 뜰 때 이미 블록이 끝나 있어, "지금
  시작하라" 가 아니라 "이미 지나갔다" 는 사후 통보가 되기 때문에 완화한다. 기준은 카드의
  `estimatedMinutes` 가 아니라 **블록의 실제 길이**(사용자가 주간 편집기로 바꿀 수 있고,
  배지가 가리키는 대상이 블록이라서). **push 아님** — 잠금 3규칙이 push 클래스를 3종으로 고정해 새 클래스를 못
  만든다. FE 가 이 필드로 인앱 배지를 그린다(표시 빈도·소멸 조건은 FE 책임)
- 판정 규칙은 서버에만 있다(`domain/missed_check_in.py`) — `cancellable` 과 같은 원칙
- ⚠️ **최근 앱 세션·무응답 누적에 따른 억제는 아직 없다** — 그 신호(근거 대장 §6.2)는
  `app_sessions` 테이블 없이는 계산 불가능해 이번 범위 밖. 카드가 미체크 조건을
  만족하면 항상 `true` 다

**캘린더 겹침 (v2.27)** — `GET /today/agenda` · `GET /plans/weekly` 공통:
- 계획은 **만들 때** 캘린더를 피한다(§9). 그 뒤 캘린더에 생긴 약속은 이미 승인한 블록이 모른다 —
  그래서 화면을 **열 때마다** 그 구간의 Google 캘린더를 읽어 표시한다. **옮기지는 않는다**
  (AI 결과 자동 적용 금지). 옮기는 건 블록 편집(`PATCH /plans/{planId}/blocks/{blockId}`)·재계획으로.
- `AgendaCard.calendarConflict` / `WeeklyBlock.calendarConflict` (파생 필드) — `blockStatus='scheduled'`
  이고 **아직 끝나지 않은**(`endAt > now`) 블록이 캘린더 일정과 겹치면 `true`. 맞닿기만 한 건 겹침이
  아니다(10:00 에 끝나는 수업 뒤 10:00 블록). 아젠다는 카드의 블록 중 하나라도 겹치면 `true`.
  판정은 서버 하나(`domain/calendar_conflict.py`) — `missedCheckIn` 과 같은 원칙. **push 아님**(인앱 배지)
- 응답 최상단 `calendar: {status, checkedAt}`
  - `ok` — 읽었다. `checkedAt` 은 실제로 읽은 시각(KST) — **5분 캐시**라 최대 5분 전일 수 있다
  - `failed` — 연결돼 있는데 못 읽었다(Google 지연·오류, **2초 상한**). 이때 `calendarConflict=false` 는
    "겹침 없음" 이 **아니다** — FE 는 "캘린더를 확인하지 못했어요" 로 구분해 안내할 것. 화면 자체는 정상 응답
  - `not_connected` — 연결 안 함, 또는 서버에서 기능이 꺼짐. 아무 안내도 하지 않는다
  - `checkedAt` 은 `ok` 가 아니면 `null`
- `WeeklyPlanDay.calendarBusy` (v2.34, 주간만) — 같은 조회로 읽은 약속 구간 자체. `ok` 일 때만 채우고
  아니면 빈 목록이다. 오늘 화면(`GET /today/agenda`)에는 없다
- 연결·해제(`POST`/`DELETE /calendar/connect`) 직후에는 캐시를 비운다 — 바로 다음 화면부터 반영
- **모닝 브리프**(06:00 cron)도 같은 판정으로 오늘 겹치는 카드를 `brief.adjustmentHints` **맨 앞**에
  싣는다 — "오늘 14:00 'ERD 그려보기' 시간이 캘린더 일정과 겹쳐요. 시간을 옮겨 볼까요?" (카드당 한 번,
  최대 2문장 + "그 밖에 N개 카드도…" 한 줄). 새 알림 클래스는 만들지 않는다. 브리프는 06:00 스냅샷이라
  그 뒤 생긴 겹침은 위 `calendarConflict` 로만 보인다

---

## 11. Reflection (`/reflection`) — S17, S18

| Method | Path | 설명 | 상태 |
| --- | --- | --- | --- |
| GET | `/reflection/pending` | 오늘+어제+그제 미체크 카드 (3일 누적). 창 기준은 **계획 시각과 실제 착수 시각 중 나중** — 지난 블록을 뒤늦게 [▶시작] 한 카드도 착수일 기준 3일간 노출된다(#20). 창을 벗어난 카드는 매일 04:00 KST `expire_reflections` cron(`SCHEDULER_ENABLED=true` 일 때만 구동)이 같은 기준식의 여집합으로 `system_failure_reason='reflection_skipped'` + soft delete 만료하므로 목록에 나타나지 않는다 | ✅ #83 |
| POST | `/reflection/batch` | 미체크 카드 일괄 종결 (Idempotency-Key 필수). 트랜잭션 | ✅ #20 |
| GET | `/reflection/failure-tags` | 13종 마스터 (`is_active=true`) | ✅ #19-B |
| POST | `/reflection/failure-tags/{executionId}` | 0~2개 태깅 + `memoEncrypted` + `taskAversiveness` | ✅ #19-B, #299 |

`POST /reflection/batch` — S17 저녁 일괄 회고. 요청 `{ items: [{ executionId, completionStatus(4칩),
failureTags?(0~2), memo?, taskAversiveness? }] }` (빈 배열 no-op, 상한 50건). 각 항목을 `POST /today/check-ins` 와
동일하게 종결(execution + 블록 finished + `action_item.status`)하고 failed/partial_done 항목엔
실패 사유를 함께 기록한다. 단 **소급 종결이라 `actual_duration_minutes` 는 비워 둔다**(v2.30-today) —
회고한 시각은 끝낸 시각이 아니다(예전엔 13:00 에 시작한 30분 카드를 21:30 에 회고하면 510분이 됐다).
주간 리뷰 `effort.actualMinutes` 는 이 실행을 0 으로 센다. **전량 사전 검증 후 단일 트랜잭션 적용** — 하나라도 무효(없음
404 `TODAY_EXECUTION_NOT_FOUND` · 이미 체크인 409 `TODAY_ALREADY_CHECKED_IN` · 중복 executionId 422
`COMMON_VALIDATION_ERROR` · non-failure 에 태그 422 `REFLECT_NOT_FAILED` · 무효 태그 422 `REFLECT_INVALID_TAG`
· non-failure 에 정서 문항 422 `REFLECT_NOT_FAILED` · non-failure 에 memo 422 `REFLECT_NOT_FAILED`
· 태그 0개인데 memo 422 `COMMON_VALIDATION_ERROR`(`field: "memo"`, #203) · 재태깅 409
`REFLECT_ALREADY_TAGGED`)면 **전체 롤백(부분 적용 없음)**. 응답 `{ processedCount, taggedCount,
needsFailureTags[] }`(사유 미기록 실패 항목의 executionId). `memo` 는 서버 at-rest 암호화 —
**`failureTags` 가 1개 이상일 때만 유효**(#203) — `memo` 는 태그 행에 얹혀 저장되므로
태그 없이 보내면 저장할 곳이 없다.

**`taskAversiveness`(#299, FE #222)** — "이 일이 얼마나 하기 싫었나요" 1(전혀 안 싫음)~5(매우
싫음), 선택 사항. `failureTags`/`memo` 와 독립적으로 유효(태그 없이 정서 문항만 보내도 됨)하되
`completionStatus` 는 똑같이 failed/partial_done 이어야 한다(그 외엔 422 `REFLECT_NOT_FAILED`).
저장 위치는 `execution_events.task_aversiveness` — `context_snapshots.overwhelm_level`("얼마나
벅찼는지")과는 다른 축이다. 생략하면 NULL 유지(강제 아님).

**일별 '하루 에너지'는 이 계약에 없다 — 설계에 없기 때문이다** (#141). S17 회고는 **실행 단위
종결**만 다룬다. 이 문단이 없어서 "저장할 자리가 있는데 BE 가 안 만들었다"는 오해가 반복됐다:

- `context_snapshots.estimated_energy_level` 은 **하루 총평이 아니라 실행 1건의 그 순간 추정치**다
  (`focus_level`/`overwhelm_level`/`noise_level` 과 같은 "state 1~5 척도" 4형제, DB 설계서 §5.18).
  쓰는 주체·시점도 다르다 — architecture.md §5 표: `Execution Logger | check-in 입력 |
  execution_event + context_snapshot`. **캡처 자체가 #19-B-2 유예 중**(위 §10 `/today/check-ins` 행).
  `execution_id` 가 NOT NULL 이라 **미체크 실행이 0건인 날엔 저장할 행 자체가 없다**.
- 지속형 에너지 **선호**의 단일 진실 소스는 `/settings/profile`(`behavioral_profiles.energy_cycle`,
  §17). 인터뷰가 채우며 일별 기록과는 다른 개념이다.

즉 일별 에너지를 도입하려면 **새 저장소 + 마이그레이션**(AGENTS.md §8 사람 합의 대상)이고, 그 전에
**읽을 사람(소비처)부터 정해야 한다** — `context_snapshots` 가 설계·마이그레이션·모델을 다 갖추고도
INSERT/SELECT 0곳인 채 남아 있는 게 "저장부터 하면 언젠가 읽는다"가 거짓이라는 레포 내부 물증이다.

#19-B 태깅 메모: failed/partial_done 실행만 허용 (422 `REFLECT_NOT_FAILED`), 무효 코드 422
`REFLECT_INVALID_TAG`, 재태깅 409 `REFLECT_ALREADY_TAGGED` (hard delete 회피), memo 는
`encrypt_memo` at-rest 암호화. `tagCodes` 가 0개인데 `memo` 를 보내면 422
`COMMON_VALIDATION_ERROR`(`field: "memo"`, #203) — memo 는 태그 행에 얹혀 저장되므로
태그 없이는 저장할 곳이 없다. 이 태그가 §12 Recovery 룰 엔진의 입력이 된다.

13종 enum: `TIME_SHORTAGE` / `LOW_ENERGY` / `HARD_TO_START` / `PRIORITY_SHIFT`
/ `PLAN_TOO_BIG` / `FATIGUE` / `AMBIGUITY` / `CONFLICT` / `OVERRUN` / `AVOIDANCE`
/ `DISTRACTION` / `EMERGENCY` / `CONTEXT_LOSS`

---

## 12. Recovery (`/recovery`, `/replan`) — S19, S20

| Method | Path | 설명 | 상태 |
| --- | --- | --- | --- |
| POST | `/recovery/proposals/generate` | Recovery Coach (LLM thinking 0 + ≤ 12s — ADR-0003 addendum(#128), 룰 fallback) → 후보 2~4개 | ✅ #20-A |
| POST | `/recovery/decisions` | 사용자 선택 저장 — 수락/**수정**/스킵 (Idempotency) | ✅ #20-A |
| GET | `/replan/{executionId}` | before/after diff (S20) | ✅ #20-B |
| POST | `/replan/{executionId}/approve` | 최종 적용 (Idempotency) | ✅ #20-B |

#20-A 구현 메모:
- `POST /recovery/proposals/generate` 요청 `{ executionId }` — completion_status 가
  `failed`/`partial_done` 인 실행만 허용 (422 `RECOVERY_NOT_ELIGIBLE`).
  pending 카드가 있으면 그대로 반환 (재호출 안전). 응답은 Draft Layer
  (`isDraft=true`, `aiSource=llm|rule`) + `cards[]` (attemptId/optionGroup/strategyType/
  labelKo/suggestedActionText/minRecoveryUnitMinutes/allowRestMode/triggerTag/
  obstacle/copingClause/acknowledgment).
- `obstacle`/`copingClause`/`acknowledgment`(모두 nullable) — 실패 태그에 `AVOIDANCE`
  가 있을 때만 personalize 가 v3 프롬프트로 라우팅되고, 그 배치의 **선두 카드에만** 값이
  실린다. 그 외 카드(형제·비-AVOIDANCE 배치·룰 폴백)는 셋 다 null. `acknowledgment` 는
  v3 안에서도 조건부라 obstacle/copingClause 만 있고 이건 null 인 경우가 있다.
  **선두 카드여도 문장이 깨졌으면 그 필드만 null**(v2.30-recovery) — 비었거나, 길이 초과
  (acknowledgment 60자, obstacle/copingClause 120자), 한글·영문·숫자·흔한 문장부호 밖의 글자,
  날짜/시각 흔적, **문장이 끝났는데 다음 문장이 공백·문장부호 없이 바로 붙음**
  ("…있어요네", "…했어요그리고", v2.30-recovery2), 카드 제목에 없는 3글자 이상 영단어.
  `suggestedActionText` 는 그대로다.
  FE 는 값이 있을 때만 그리면 된다(없는 게 정상인 경우가 많다).
- **`recoveryMode: "standard" | "goal_renegotiation"`(#328, 근거 대장 §5.2 L3)** — 동일
  목표 4회 연속 실패 또는 회복 2회 연속 rejected(skipped 포함)면 `goal_renegotiation` 이고,
  이때 `cards` 는 태그 매칭과 무관하게 **DOWNSCOPE/RESCHEDULE/PARK 각 1장, 정확히 3장**
  고정이다(CARRY_OVER 제외 — "내일로 미루기"는 재협상 취지와 안 맞음). 이 모드에선
  LLM personalize 도 건너뛴다(L2 와 같은 이유 — 개인화가 "이번엔 다를 거예요" 식 역효과).
  카드 스키마·`/recovery/decisions`·replan 흐름은 모드와 무관하게 동일 — FE 는 기존 회복
  카드 목록·수락/수정/거절 인터랙션을 그대로 쓰고, 이 필드가 `goal_renegotiation` 일 때만
  상단에 "같은 목표가 반복해서 막혔어요. 이번에는 계획 자체를 조정해볼까요?" 안내를 얹는다
  (FE #223). `standard` 가 기본값이라 기존 클라이언트는 이 필드를 몰라도 동작한다.
  **이미 결정된 실행(pending 0건 + 결정 이력 있음)은 `RECOVERY_ALREADY_DECIDED`(409)** —
  회복 카드 세트는 실행 1건당 1세트다. 재생성을 허용하면 `/recovery/decisions` 의 409 가
  무력화돼 같은 실패에 회복 ActionItem 이 여러 개 생기고, replan 은 `created_at` 오름차순의
  **첫** 채택 카드에 고정돼 사용자가 다시 고른 최신 회복이 영영 배치되지 않는다.
  → FE 는 회복 화면 재진입 시 409 를 "이미 결정함"으로 처리한다(에러 토스트 X).
- **`personalizationSkipped: boolean`(v2.30-recovery, 추가 필드 — 기본 `false`)** — L2(단서
  전환)·L3(`goal_renegotiation`)처럼 **제품 규칙으로 LLM 개인화를 일부러 건너뛴** 세트면 `true`.
  이때도 `aiSource` 는 계약대로 `"rule"` 이다. 타임아웃·키 없음 같은 **룰 폴백**이면 `false`.
  → FE 는 "오프라인 모드(룰 기반)로 제안했어요" 안내를 `aiSource === "rule" &&
  !personalizationSkipped` 일 때만 띄운다(예전엔 L2/L3 마다 AI 가 안 되는 것처럼 보였다).
  pending 재반환 경로도 같은 판정(다시 계산한 에스컬레이션 레벨)을 쓴다.
- 룰 선택: `recovery_strategy_catalog.primary_trigger_tags` ↔ 실패 태그 매칭,
  그룹별 최고 1장, 최소 2장 패딩 (orchestrator/recovery.py).
- `POST /recovery/decisions` 요청 `{ executionId, decision: accepted|edited|skipped,
  acceptedAttemptId?, editedActionText?, decisionReason?, reEngagementAnchorAt? }` — accepted 시
  나머지 pending 은 rejected. DOWNSCOPE/CARRY_OVER 수락 → 새 ActionItem(source=`recovery_downscope`/
  `recovery_carryover`, `parent_action_item_id` 혈통) 생성. RESCHEDULE/PARK 는 생성 없음.
  **같은 실행에 결정이 겹쳐 와도 하나만 적힌다**(v2.32) — 뒤 요청은 앞 결정의 커밋을 기다렸다가
  `RECOVERY_ALREADY_DECIDED`(409)를 받는다(순차 재호출과 같은 응답).
- **새 회복 카드의 `title` = 원본 카드 제목 + 그룹 꼬리표**(v2.30-recovery) — DOWNSCOPE
  `"<원본> · 가볍게 다시"`, CARRY_OVER `"<원본> · 이어서"`(최대 300자, 회복을 또 회복해도
  꼬리표는 한 번만, 원본을 못 읽으면 `"다시 해보기"`). ⚠️ 그전에는 `suggestedActionText` 를
  그대로 제목으로 썼다 — 선두 카드만 LLM 이 다듬고 나머지는 카탈로그 템플릿이라, 형제 카드를
  고르면 "내일 같은 슬롯으로 그대로 옮겨드릴까요?" 같은 질문이 오늘 화면·주간 그리드·아침 알림의 카드 이름이 됐다. 제안 문구는
  DOWNSCOPE 카드의 `firstStep`(오늘 화면 '첫 걸음')으로 옮기고(L1/L2 컴백 프리픽스는 뗀다),
  CARRY_OVER 는 원본의 `firstStep` 을 물려받는다. `decision="edited"` 면 종전대로 사용자
  문구가 제목이다(이때 DOWNSCOPE `firstStep` 은 비운다 — 사용자가 AI 문구 대신 자기 말을
  골랐다). `recovery_attempts.suggested_action_text`(AI 원문)는 그대로 보존.
- **회복 카드의 `estimatedMinutes` 는 원본 카드에서 파생한다** (2026-08-28, ADR-0009 D6):
  **CARRY_OVER = 원본 그대로**('내일로 그대로 옮기기'라 길이를 줄이지 않는다),
  **DOWNSCOPE = 원본의 40%** 를 5분 눈금으로 반올림하고 `[min(minRecoveryUnitMinutes, 원본),
  원본]` 으로 클램프. 원본 카드를 못 읽으면(삭제 등) 종전대로 `minRecoveryUnitMinutes`.
  ⚠️ 그전에는 두 그룹 모두 전략 카탈로그의 `minRecoveryUnitMinutes`(5~30분)를 그대로 썼다 —
  CARRY_OVER 가 원본 길이를 잃었고(3시간 카드 → 5분 카드), 최소 단위가 원본보다 큰 조합에서는
  DOWNSCOPE 가 오히려 **확대**됐다(15분 원본 + 30분 단위 전략 → 30분).
  `minRecoveryUnitMinutes` **필드 자체의 의미는 그대로**다(전략의 최소 회복 단위) — 이제
  카드 길이가 아니라 그 하한으로 쓰인다.
- **`reEngagementAnchorAt`(#327, FE #221)** — PARK/CARRY_OVER 수락(accepted/edited)에만
  유효. 시간대 포함 ISO 8601(예: `2026-09-01T09:00:00+09:00`) 이어야 한다(시간대 없으면 422
  `COMMON_VALIDATION_ERROR`). 생략하면 서버가 `orchestrator.recovery.re_engagement_anchor_at`
  로 전략별 기본값을 계산: **CARRY_OVER** = 다음날 09시, **PARK** = 다음 주 월요일 09시(오늘이
  월요일이어도 다음 주로 민다). 그 외 그룹(DOWNSCOPE/RESCHEDULE)이나 `decision="skipped"` 에
  값을 보내면 422 `COMMON_VALIDATION_ERROR` — 앵커 개념이 없는 결정에 조용히 버려지는 값을
  만들지 않는다. 응답 `reEngagementAnchorAt` 는 확정된(명시값 또는 계산된 기본값) 시점을 항상
  KST 로 반환하며, PARK/CARRY_OVER 가 아니면 `null`. 저장 위치는
  `recovery_attempts.re_engagement_anchor_at`.
  **CARRY_OVER 에 명시값을 보내면 새 회복 카드의 `targetDate` 도 그 KST 날짜가 된다**
  (v2.30-recovery) — 단 내일보다 이르면 내일('이어가기'는 오늘 안으로 당기지 않는다). 생략하면
  종전대로 내일. ⚠️ 그전에는 앵커만 저장되고 카드는 무조건 내일에 놓여, "금요일에 다시
  확인할게요"라고 고른 사용자의 할 일이 내일 오늘 화면에 떴다. 앵커의 **시각**은 여전히 쓰지
  않는다 — 재관여 알림은 그날 아침 알림 시각에 한 번 간다(§15 morning_brief).
- **`decision="edited"`(잠금 결정 [수락/수정/거절] 의 '수정')** — `acceptedAttemptId` +
  `editedActionText`(trim 후 1~300자) 필수. 부수효과는 accepted 와 **동일**(형제 rejected,
  새 ActionItem 생성, replan 대상)이고 **새 카드 title 만 사용자 문구**가 된다.
  AI 원문 `suggestedActionText` 는 **보존**한다(덮어쓰지 않음) — "AI 제안을 얼마나 고쳐
  썼나"가 Draft Layer 의 효과 지표다. 사용자 문구는 금지어 필터를 거치지 않는다(톤 잠금은
  AI 출력 대상). 새 카드를 안 만드는 RESCHEDULE/PARK 은 문구를 담을 곳이 없어 422 —
  이 그룹의 조정은 문구가 아니라 시간이고 S15 주간 편집기(`PATCH /plans/{planId}/blocks/{blockId}`)
  소관이다. 응답 스키마는 **불변**(채택 카드는 `acceptedAttemptId` 로 반환).
- 회복 지표(`resilience_rate`·`average_recovery_minutes`)는 `accepted` 와 `edited` 를
  **함께** 회복으로 센다.
- 에러: `RECOVERY_EXECUTION_NOT_FOUND`(404) / `RECOVERY_NOT_ELIGIBLE`(422) /
  `RECOVERY_NO_PROPOSAL`(422) / `RECOVERY_ATTEMPT_NOT_FOUND`(404) /
  `RECOVERY_ALREADY_DECIDED`(409) / `RECOVERY_EDIT_NOT_SUPPORTED`(422) /
  `RATE_LIMIT_DAILY_CALLS_EXCEEDED`(429, §1.10, #325).
- `generate` 의 일일 호출 상한(§1.10)은 **신규 카드 생성**에만 적용된다 — pending 카드가
  있어 캐시를 그대로 반환하는 재호출(새로고침)은 오케스트레이션이 안 일어나므로 상한에
  안 걸린다(가드가 그 두 short-circuit 뒤, 실제 LLM/룰 생성 직전에 있다).

#20-B 구현 메모 (replan S20):
- `GET /replan/{executionId}` — 수락한 회복의 일정 변화 프리뷰. 응답 Draft Layer
  (`isDraft=true`, `aiSource=llm|rule`) + `optionGroup` + `before`/`after`
  (각각 actionItemId/title/targetDate/startAt/endAt/estimatedMinutes, 시각은 KST)
  + `alreadyApproved`. `before`=원본 실패 카드 계획 시각, `after`=회복 카드 제안 시각
  (원본 시간대를 회복 `targetDate` 로 일(day) 단위 시프트 — 룰 기반, freebusy 무관).
  **날짜는 시프트가 정하고, 시각은 과거 배치 보정이 정한다** — 시프트 결과가 이미 지난
  시각이면 `조회/승인 시각 + 10분`을 15분 격자로 올린 시각(`earliest`)까지 앞당긴 뒤,
  **사용자의 활동 시간대 안**에 놓는다(v2.30-recovery2). 창 = 설정의 활동 시간대
  (`focus_mode_preferences.activity_start/end`) > 온보딩 인터뷰가 저장한
  `behavioral_profiles.preferred_start_time/end_time` > **모르면 07:00~23:00**(quiet hours
  경계 — 종전 동작). 한쪽만 아는 창은 모르는 것으로 본다.
  (a) `earliest` 가 창 안이고 `earliest + estimatedMinutes` 가 창 안에서 끝나면 `earliest`,
  (b) 아니면 **다음에 그 창이 열리는 시각**(15분 격자 올림). 창은 자정을 넘길 수 있다
  (`end <= start`, 예: 22:00~02:00) — 그 사용자의 00:30 은 '어제 저녁에 열린 창' 안이지
  다음날 아침이 아니다. `start == end` 는 "하루 종일"로 읽어 미루지 않는다.
  과거에 박힌 블록은 **원리적으로 완주가 불가능**해서(#258) 하루 늦게라도 미래에 놓는다.
  ⚠️ 회복 길이가 원본에서 파생되면서(ADR-0009 D6) 긴 원본의 DOWNSCOPE 는 (a) 를 통과하지
  못해 (b) 로 가는 빈도가 늘어난다.
  ⚠️ **활동 시간대를 모르는 사용자의 동작은 종전과 완전히 같다**(07/23 기본창).
  **(b) 경로의 카드 날짜(v2.30-recovery)**: approve 가 블록을 만들 때 회복 카드의 `targetDate`
  를 **블록의 KST 날짜로 맞춘다**(새 회복 카드만, 원본 카드는 불변). GET 프리뷰의
  `after.targetDate` 도 같은 값(블록이 놓이는 날)이다. ⚠️ 그전에는 카드 날짜가 결정한 날에
  남아, 오늘 화면(`targetDate` 로 카드를 고른다)이 블록이 잡힌 다음날 그 카드를 보여주지 않았다.
  시프트 결과는 항상 **15분 격자로 올림**한다(v2.30-recovery) — 계획 블록 없이 바로 시작한
  카드는 `planStartAt` 이 클릭 시각(초·마이크로초 포함)이라 그대로 옮기면 격자에서 어긋났다.
  왜: 회복 결정은 21시 일괄 회고(잠금 결정)에서만 일어나고 DOWNSCOPE 는 day_delta 가 0 이라,
  보정이 없으면 결과가 항상 **이미 지나간 원본 슬롯**이 된다. 과거 블록은 `pre_card` 알림
  창(`[now+2m, now+7m)`, 5분 폴)을 영영 만나지 못한다. 왜 창 밖으로는 안 미는가: 블록 생성
  경로는 시간 정책 검사를 하지 않는데 S15 주간 편집기는 활동 시간대 밖을
  `POLICY_VIOLATION`(422)으로 거부한다(그 정책은 활동 시간대의 여집합 = 수면이다) —
  서버가 사용자보다 느슨한 블록을 만들지 않기 위한 하한선.
  `freebusy`·`time_policies` 는 **여전히 보지 않는다**(명시적 비목표) — 방금 승인된 5~30분
  행동이라 슬롯 탐색을 하지 않는다. 정책 인지 배치는 후속.
  `alreadyApproved=true` 면 `after.startAt`/`endAt` 는 **실제 배치된 블록** 시각이다.
  미승인 프리뷰는 "지금 승인하면 여기"라 조회 시각에 따라 달라진다.
- `POST /replan/{executionId}/approve` (Idempotency-Key 필수) — 회복 ActionItem 을
  `scheduled_blocks`(source=`recovery`) 로 배치. 멱등: 이미 배치돼 있으면 같은 block 반환
  (중복 INSERT 방지). 응답 `{ executionId, scheduledBlockId, actionItemId, startAt, endAt,
  isDraft=false }`. 원본 `action_item.status` 불변. 이 멱등은 **동시 호출에도** 성립한다(v2.32)
  — 겹친 요청은 모두 같은 `scheduledBlockId` 를 받는다.
  멱등·`alreadyApproved` 판정은 **블록 소스와 무관**하게 그 회복 카드의 미취소 블록 유무로
  한다 — S15 이동이 `source`를 `user_edit` 로 덮거나 주간 재계획이 `ai_plan` 으로 만들어도
  '이미 배치됨'이다. 소스로 거르면 CTA 가 되살아나 블록이 중복 생성된다.
- 재배치 대상은 **새 ActionItem 을 만든 그룹(DOWNSCOPE/CARRY_OVER)** 뿐. skipped/
  RESCHEDULE/PARK 는 `RECOVERY_NO_REPLAN`(422) — RESCHEDULE/PARK 의 시간 조정은 S15
  주간 편집기에서 처리.
- 에러: `RECOVERY_EXECUTION_NOT_FOUND`(404) / `RECOVERY_NO_REPLAN`(422).

UX 4 그룹 / 내부 13 전략 (v1.60, #257 — 태그 구멍 3개 + PARK 도달 경로 gap-fill):
```
DOWNSCOPE  → NANO_STEP · DOWNSCOPE_DEFAULT · ENVIRONMENT_SHIFT · CONTEXT_REWARMING
           · SELF_FORGIVENESS_NANO(신설)
RESCHEDULE → RESCHEDULE_DEFAULT · ACTIVE_RECOVERY · TIMEBOX_REBUDGET(신설) · BUFFER_INSERT(신설)
CARRY_OVER → CARRYOVER_DEFAULT · FREEZE_SLOT
PARK       → PARK_DEFAULT · GOAL_RECHECK(신설)
```
GOAL_RECHECK(정적 태그: AVOIDANCE, PRIORITY_SHIFT) 가 PARK 그룹의 실제 도달 경로다.
PARK_DEFAULT 는 여전히 정적 태그가 없다(동적 조건 overwhelm≥4 미구현).

원본 `action_item.status` (FAILED 등) 절대 변경 X.

---

## 13. Reviews (`/reviews`) — S21, S22

| Method | Path | 설명 | 상태 |
| --- | --- | --- | --- |
| GET | `/reviews/weekly?weekStart=YYYY-MM-DD` | 주간 리뷰 (회고 창이 닫힌 주는 확정 집계, 그 전엔 즉석 계산 — v2.30) | ✅ #21-A |
| POST | `/reviews/weekly/generate` | 수동 재생성 (디버그) | ✅ #21-A |
| GET | `/reviews/habit-penalty` | 3주 미달 빈도 재설계 후보 (S22) | ✅ #21-C |
| POST | `/reviews/habit-penalty/{habitId}/accept` | 3주 미달 페널티 수락 (Idempotency) | ✅ #21-C |
| POST | `/reviews/habit-penalty/{habitId}/reject` | '지금대로 유지' — 4주 동안 다시 제안 안 함 (도메인 멱등) | ✅ v2.30 |

핵심 필드: `adherenceRate`, `consistencyDays`, `resilienceRate`, `categorySuccessRate`,
`peakWindow`, `drainWindow`, `policyUpdateCandidates`, `topFailureContexts`(#301),
`effort`(v1.99), `unstartedBlocks`(v2.30), `habits`(v2.30)

`habits`(v2.30, 기본 `[]`): 만다라에 걸리지 않은 **활성** 습관의 그 주 체크인 —
`{ habitId("habit_<uuid>"), title, doneCount, targetCount }`. `habit_instances` 의 그 주 행에서
조회 시점에 파생한다(저장 안 함). 만다라 반복형 칸에 링크된 습관은 `mandala.habits` 에 이미
있으므로 여기서 뺀다(중복 나열 없음). KPI(`adherenceRate` 등)는 카드 실행만 세므로 습관만 쓰는
주에는 KPI 가 null 이어도 이 배열은 채워진다 — FE 는 "집계할 활동이 없어요" 를 실행도 습관
체크인도 없을 때만 보여야 한다.

`unstartedBlocks`(v2.30, int, 기본 0): 그 주(`[월 00:00, 다음 월 00:00)` KST)에 시작했어야
하는데 **한 번도 [▶ 시작] 하지 않고 지나간** 블록(세션) 수. 조건: `block_status='scheduled'`,
블록이 이미 끝남(`end_at <= now`), 카드가 보관되지 않았고 아직 결론이 안 남(`planned`/
`in_progress`). 취소된 블록(옮기거나 지운 것)과 이미 완료·실패로 체크인한 카드의 남은 블록은
세지 않는다. `adherenceRate`·`effort` 는 **시작한 카드(실행)만** 세므로 이 수는 그 분모
밖이다 — 1장 끝내고 9장을 손도 안 댄 주는 `adherenceRate=1.0` 이면서 `unstartedBlocks=9`.
⚠️ 준수율 정의는 바꾸지 않는다(과거 주와 비교 불가능해진다). `period_summaries` 에 저장하지
않고 매 요청 파생한다(마이그레이션 없음, 확정 저장본 경로에서도 같은 값). FE 는 "잘 했어요"
류 헤드라인을 이 값과 함께 판단하고, `adherenceRate` 가 null 이어도 이 값이 0보다 크면
"활동 없음" 이 아니라 "시작 못 한 카드가 있었다" 로 안내해야 한다.

`effort`(v1.99, ADR-0009 D5): 같은 주를 **분**으로 다시 센 요약 —
`{ plannedMinutes, completedMinutes, actualMinutes, adherenceRate }`. `adherenceRate`(건수
비율) 옆에 나란히 두는 값이다. 계획 세션 길이가 작업 내용을 따라가면(v1.96) 건수와 시간이
갈린다 — 15분짜리 9개를 끝내고 3시간짜리 1개를 못 하면 건수로는 90% 지만 실제로는 계획의
43% 다. ⚠️ **기존 `adherenceRate` 정의는 바꾸지 않는다**(바꾸면 과거 주차와 비교할 수 없다).
표본은 `adherenceRate` 와 **같다**(종결 실행) — `plannedMinutes` 는 그 분모, `completedMinutes`
는 그 분자에 해당하는 집합의 계획 시간 합이다. `actualMinutes` 는 **완료한 실행에 실제로
쓴** 시간 합이라, `actualMinutes / completedMinutes` 가 "예상이 맞았나" 를 말해준다
(1.0 = 예상대로, 1.3 = 30% 더 걸림). 중단된 실행의 소요는 계획 시간과 비교할 대상이 아니라
빼고 센다. 계획 길이는 카드의 `estimatedMinutes` 가 아니라 **블록 시각**
(`planEndAt - planStartAt`)에서 파생한다 — 사용자가 주간 편집기로 블록을 옮기거나 리사이즈하면
둘이 갈라지고, 그 주에 실제로 계획돼 있던 시간은 블록 쪽이다.
⚠️ **`period_summaries` 에 저장하지 않는다** — `mandala`/proposals 와 같이 조회 시점에
파생하므로 DB 마이그레이션이 없고, precomputed 경로와 즉석 계산 경로가 같은 값을 낸다.
표본이 없으면 `{0, 0, 0, null}`.

`topFailureContexts`(#301, 근거 A5, BCT 2.3 Self-monitoring): 최근 28일(조회 대상 주
일요일 기준 역산) 실패/부분완료 실행의 실패 사유 상위 **최대 3개** —
`{ tagCode, labelKo, count, share }`. `labelKo` 는 `/reflection/failure-tags` 와 같은
마스터(`failure_reason_tags`)에서 조인해 온다(이중 관리 없음). `share` 는 0~1 비율이고
**LIMIT 이전(그 기간의 태그 전체)을 분모**로 하므로, 태그가 4개 이상인 주는 반환된 3건의
share 합이 1.0 이 안 될 수 있다. 실패 태그가 하나도 없으면 빈 배열 — `mandala`/
`nextCycleProposals` 처럼 `period_summaries` 에 저장하지 않고 매 요청마다 파생한다.

#21-C Habit Penalty 메모 (S22 — 비난 아닌 빈도 재설계):
- 감지: 직전 완료 주 기준 **최근 3주 연속** `done_count < target_count*0.5`. 순수 함수
  `orchestrator/habit_penalty.py`. `suggestedFrequency` = 3주 평균 달성(round, 최소 1, 현재보다 작게).
  **더 줄일 수 없는 주 1회 습관은 후보가 아니다**(v2.30-goals — 예전엔 "주 1회 → 1회" 카드가 떴다).
- `GET /reviews/habit-penalty` — 후보(habitId/title/current·suggestedFrequency/recentWeeks/message).
  이미 이번 사이클 결정한 habit(`last_penalty_evaluated_at` ≥ 직전 완료 주)과 **거절 후 4주 이내**
  (`last_penalty_decision='rejected'` 이고 `last_penalty_evaluated_at` 이 28일 안)인 habit 은 제외.
  `message` 는 3주 **합계**로 말한다("주 5회 목표로 2회를 해냈어요." — 예전 "평균 0.67회" 같은
  소수 없음).
- `POST /reviews/habit-penalty/{habitId}/accept` — **Idempotency-Key 필수**(§1.7 미들웨어). 조건
  미충족/중복 시 422 `HABIT_PENALTY_NOT_ELIGIBLE`, 습관 없음 404 `HABIT_NOT_FOUND`. 수락 시
  `frequency_per_week`=`target_count`=suggested, `last_penalty_decision='accepted'`. **이번 주
  인스턴스의 `targetCount` 도 새 빈도로**(이미 한 횟수는 새 목표에서 멈춤, v2.30-goals). DB 마이그레이션 없음.
- `POST /reviews/habit-penalty/{habitId}/reject`(v2.30) — '지금대로 유지'. 빈도는 그대로 두고
  `last_penalty_decision='rejected'`·`last_penalty_evaluated_at=now` 를 기록해 **4주 cooldown**
  (그동안 GET 후보에서 빠지고 accept 도 422). 응답 `{ habitId, frequency, message }`.
  **도메인 멱등** — cooldown 중 재호출은 아무것도 바꾸지 않고 같은 응답(기준 시각 유지)이라
  Idempotency-Key 를 **요구하지 않는다**(보내도 무해). 제안 조건(3주 미달)을 다시 따지지 않는다 —
  카드를 본 뒤 주가 바뀌어도 유지 버튼이 실패하지 않게. 이번 사이클에 이미 수락했으면 422
  `HABIT_PENALTY_NOT_ELIGIBLE`(수락을 거절로 덮지 않음), 습관 없음 404 `HABIT_NOT_FOUND`.
  DB 마이그레이션 없음(컬럼·enum 값 기존).

#21-A 구현 메모 (룰 기반, LLM 한 줄 평은 P2):
- `weekStart` 는 해당 주 **월요일**로 정규화(아무 날 넣어도 그 주로 스냅). 생략 시 이번 주.
  형식 오류 → 422 `REVIEW_INVALID_WEEK`.
- `GET` 은 그 주의 **확정본**만 저장값으로 쓴다(v2.30) — 확정본 = 회고 창이 닫힌 뒤, 즉
  **다음 주 목요일 00:00 KST 이후에 집계된** `period_summaries`(period_type=`weekly`) 행. 그 전에는
  (진행 중인 주, 늦은 회고가 아직 들어올 수 있는 지난주, 확정본이 없는 과거 주) 저장된 행이
  있어도 무시하고 **즉석 계산(쓰기 X)** 한다. 예전엔 저장된 행이면 무조건 반환해 일요일 18:00
  폴의 스냅샷이 그 주 내내 잠겼고, 같은 응답의 `effort`·`mandala` 는 매번 새로 세어 한 화면에서
  두 시점의 숫자가 섞였다. `POST generate` 는 종전대로 즉시 영속화(덮어쓰기)하고 방금 집계한
  값을 돌려준다. 응답 스키마는 그대로.
- 집계 소스: `execution_events`(완료/실패), `recovery_attempts`(수락=resilience 분자),
  `action_items.category`. 집계는 순수 함수 `orchestrator/weekly_review.py`.
- `peakWindow`/`drainWindow` = `"<요일>_<시간대>"`(예: `tuesday_afternoon`). 시간대는 `morning`
  (05~11시)·`afternoon`(12~17시)·`evening`(18~23시)·`night`(00~04시, v2.30). **`night` 는 전날
  요일로 붙는다** — 화요일 01:00 은 `monday_night`(사람에게는 월요일 밤의 끝). 예전엔 달력 날짜
  그대로 `tuesday_evening` 으로 잡혀 한 줄 평이 17시간 엇나갔다. 기준 시각은 계획 시작 시각.
- `avgDelayMinutes` = 계획 대비 실제 시작의 **늦은 정도** 평균(분). v2.30 부터 일찍 시작한 실행은
  **0분**으로 센다(음수 없음) — 예전엔 음수를 그대로 평균해 "평균 지연 -3시간 20분" 이 나왔고,
  일찍 시작한 날이 늦은 날을 상쇄했다.
- `resilienceRate` = 실패(`failed`/`partial_done`) 중 회복 카드 **수락** 비율(#21-A 정의).
  "회복 후 24h 내 완료" 정밀화는 #20-B(replan 완료) 데이터 확보 후.
- `restartSuccessRate`·`repeatedFailureCount`(interruption·failure_tag 조인) / `policyUpdateCandidates`(P2)
  는 #21-A 에서 `null`/`[]`.
- 일요일 18~23시 30분 폴 KST precompute cron = `scheduler/weekly_review_precompute.py`
  (idempotent, v2.30 부터 매 폴 다시 집계 `force=True`) + **매일 04:30 확정 집계**
  (`weekly_review_finalize` — 회고 창까지 닫힌 가장 최근 주를 덮고, 이미 확정본이면 skip).
  예전엔 일요일 03:00 고정 1회였다 — `week_window()` 가 재는 주 경계
  `[월 00:00, 다음 월 00:00)` 라 03:00 시점엔 그 주 일요일 활동 대부분이 아직 안 일어난
  상태였다. 18시 이후로 옮겨 그날 활동 대부분을 반영한다(ADR-0008 §4.1). 고정 1회 대신
  폴로 바꾼 이유는 `habit_instances` 와 같다 — jobstore 가 MemoryJobStore 라 그 시간대에
  재기동하면 주 1회짜리 job 이 유실된다. `run_weekly_review_sweep` 은 일요일이 아니면
  즉시 no-op.

만다라 절 (ADR-0008 §8 "E"):
- 응답에 `mandala: MandalaWeeklySummary | null` — 사용자에게 궁극목표(`Goal.is_ultimate`)가
  없거나 아직 만다라를 승인 안 했으면(트리 없음) `null`(응답에서 생략된 것처럼 취급).
- 필드: `completedThisWeek`/`completedTotal`/`totalLeaves`(끝낸 칸, 이번 주/누적/전체),
  `touchedThisWeek`(이번 주에 손댄 칸 — 완료 체크 또는 습관 체크인), `untouchedAxisTitles`
  (이번 주 아무 활동도 없던 축 제목 목록 — **칸을 다 끝낸 축은 빠진다**: 반복형 칸 없이 모든 칸이 완료
  표시면 할 게 남지 않은 축이라 `staleAxisProposals` 대상도 아니다, v2.30-goals), `habits`(반복형 칸별 `axisTitle`/`cellTitle`/
  `doneCount`/`targetCount`).
- `period_summaries` 에 저장하지 않고 `GET`/`POST generate` 둘 다 조회 시점에 파생
  (`mandala_adapter.compute_weekly_stat`, 순수 함수) — `goal_nodes.progress` 컬럼을 안
  두는 것과 같은 이유. 마이그레이션 없음.

다음 주기 제안 (ADR-0008 §8 "G" + ADR-0007 §5 PR-4, 2026-08-25 일반형으로 확장):
- 응답에 `nextCycleProposals: NextCycleProposal[]`(빈 배열이 기본, 없으면 `[]`) —
  `{ goalId, goalTitle, axisTitle? }`. `week_start` 와 무관하게 항상 **현재** 상태만 본다
  (과거 주를 조회해도 이 카드는 지금 열어도 되는지를 말한다).
- 대상은 두 스코프를 합친다 — 승격된 만다라 축 목표 중 `status='active'`인 것(**만다라
  2주**, `axisTitle` 이 채워짐) + 마일스톤이 있는 임의 목표 중 위와 안 겹치는 것
  (**일반형**, `axisTitle: null`). 판정 공통부: 그 목표의 **현재 활성** 계획 트리
  (`tree_kind='plan'`) leaf 에 매달린 action_item 중 (a) **아직 날짜가 안 지난**
  (`targetDate >= 오늘`) 미종결(`planned`/`in_progress`) 카드가 없고 (b) 종결(`done`/
  `partial_done`/`failed`/`over_done`) 카드가 하나 이상 있으면 제안. 카드 자체가 없으면
  (승인 직후 등) 제안하지 않는다 (`orchestrator/cycle_proposal.should_propose_next_cycle`).
- **일반형만의 세 번째 조건**: 마일스톤이 있는 목표는 **열린 마일스톤**(`completed_at`
  이 안 찍힌 것)이 하나 이상 있어야 제안한다 — 없으면 '다음 주기'가 아니라 '목표 완료
  확인' 대상이다(아래). 만다라 2주 스코프는 마일스톤 층이 없을 수 있어 이 조건이 없다.

목표 완료 확인 (v1.91, ADR-0007 6b):
- 응답에 `goalCompletionProposals: GoalCompletionProposal[]`(빈 배열이 기본) —
  `{ goalId, goalTitle }`. 마일스톤이 **전부** 완료된 목표에 대해 나간다.
- `nextCycleProposals` 와 **배타적**이다 — 같은 가드(`열린 마일스톤이 있는가`)의 양쪽
  갈래라 한 목표가 두 카드에 동시에 뜨지 않는다.
- ⚠️ **만다라에서 승격된 목표는 마일스톤이 전부 완료돼도 이 카드를 못 받는다.** 판정 루프가
  승격 목표를 통째로 건너뛰기 때문이다(위 "다음 주기 제안" 의 교집합 제거). 마일스톤 층이
  없어서가 아니라 **아직 그 조합을 다루지 않아서**다 — 완료 확인이 필요하면
  `POST /goals/{goalId}/complete` 를 직접 부르면 된다.
- 확정은 `POST /goals/{goalId}/complete`. 확정하고 나면 그 목표는 **어느 카드에도 안
  뜬다**(대상 조회가 `status='active'` 로 좁혀져 있다) — 매주 같은 카드를 다시 받지 않는다.
- **날짜가 지난 미종결 카드는 판정에서 빠진다** — '남은 일'이 아니라 '밀린 일'이라서.
  이걸 세면, 한 번도 시작 안 한 `planned` 카드는 만료 cron(`in_progress` 실행만 대상)이
  영영 못 쓸어내므로 밀린 카드 한 장 때문에 제안이 영구히 안 뜬다(2026-08-25 정정).
- **승인은 새 엔드포인트가 없다** — FE 는 기존 `POST /plans/generate` + `POST
  /plans/{id}/approve` 를 그대로 쓴다. 마감 없는 만다라 목표는 다시 2주로 캡된다(§8 "D"),
  일반형은 기존 4주 캡 그대로.
  ✅ **`nextCycleProposals[].goalId` 를 `POST /plans/generate` 의 `goalId` 로 그대로 넘길 것**
  (v2.23, #398). 안 넘기면 종전대로 최근 완료 인터뷰를 재투영하는데, 목표를 여러 개 굴리는
  사용자에게는 그게 **다른 목표**일 수 있다 — 목표 A 의 제안을 열었는데 최근 인터뷰 목표 B
  의 계획이 생성·승인되던 위험이 여기 "미해결" 로 적혀 있던 항목이다. 이제 해소됐다.

손 못 댄 축 축소 제안 (ADR-0008 §6, §8 "H"):
- 응답에 `staleAxisProposals: StaleAxisProposal[]`(빈 배열이 기본) — `{ axisId, axisTitle }`.
  `weekStart` 와 무관하게 항상 **현재**(실제 `now`) 기준 최근 3주를 본다. 궁극목표가
  없거나 승인된 만다라 트리가 없으면 `[]`(`mandala` 절과 같은 전제).
- 판정: 최근 3개 주(이번 주·지난 주·2주 전) 전부에서 손 못 댄(완료 체크도 습관 체크인도
  없는) 축만, 그것도 그 축이 3주 전 이미 존재했을 때만(막 만든 축은 데이터 없음만으로
  방치 취급하지 않는다) — `mandala_adapter.compute_stale_axes`.
- **수정도 새 엔드포인트가 없다** — 이미 있는 걸 그대로 쓴다: 칸/축 텍스트는
  `PATCH /goals/mandala/nodes/{id}`(U9), 축 8칸 재생성은
  `POST /plans/mandala/{planId}/regenerate-branch`(U5, 승인 전 초안 한정). 마일스톤
  재조정(ADR-0007 §10)은 이번 스코프에 없다 — 마일스톤 있는 임의 목표의 주기 전환
  트랙(ADR-0007 PR-3·4) 자체가 아직 없어 "재조정할 주기"가 없다.

---

## 14. Policy Snapshot (`/policy-snapshot`)

| Method | Path | 설명 | 상태 |
| --- | --- | --- | --- |
| GET | `/policy-snapshot/current` | 현재 활성. 없으면 404 `POLICY_NOT_FOUND` | ✅ #83 |
| GET | `/policy-snapshot/history` | 버전 이력(최신 앞, 비활성 포함). 없으면 `items: []` — **404 아님** | ✅ #168 |
| POST | `/policy-snapshot/preview-update` | 다음 버전 후보 (Draft, **저장 안 함**) | ✅ #168 |
| POST | `/policy-snapshot/apply` | 사용자 승인 → 새 버전 INSERT (201) | ✅ #168 |
| POST | `/policy-snapshot/rollback/{version}` | 지난 버전 값을 **새 버전으로** 되살림 (201) | ✅ #168 |

4 영역: `behavioralProfile` / `executionConstraints` / `interactionStyle` / `recoveryPolicy`
— 각 값은 JSONB 를 그대로 노출하므로 **내부 키는 snake_case** 다(필드명만 camelCase).

#168 구현 메모 — 그전까지 `current` 하나만 존재했고 `policy_snapshots` 에 행을 넣는 코드가
레포 전체에 0곳이라 **프로덕션에서 항상 404** 였다(FE 주간 리뷰의 '다음 주 정책 자동 보정'
은 카운트-only 폴백을 영구 유지 중이었다). 라우트 버그가 아니라 생산 경로가 통째로 없었다.

- **후보 산출은 룰**(`orchestrator/policy_update.py`) — LLM 아님, `source="rule"`. 승인 화면이
  "왜 이 값이 됐나"를 숫자로 보여줘야 하고(`changes[].why`), 정책은 이후 모든 계획 생성의
  입력이라 비결정적이면 추적이 끊긴다. `source` 는 rule/llm/user_manual 을 전부 허용하므로
  나중에 LLM 판단을 같은 자리에 끼울 수 있다.
- **HITL** (AGENTS §1 자동 적용 금지): `preview-update` 는 `isDraft=true` + `aiSource="rule"`
  로 내려가고 **아무것도 저장하지 않는다**. `apply` 를 눌러야 INSERT 된다. `changes` 가 비면
  이번 주엔 바꿀 근거가 없다는 뜻 — FE 는 [적용] 을 비활성화하면 된다.
- `apply` 본문은 미리보기 응답 그대로(=룰 그대로) 또는 사용자가 고친 값이다. **서버가 다시
  계산해 덮어쓰지 않는다** — 본 것이 저장된다. 고쳤는지는 `source`(`rule`|`user_manual`)로
  FE 가 알려준다(`/recovery/decisions` 의 accepted/edited 와 같은 관례).
- **append-only** (ADR-0001 §3.2): 새 버전은 INSERT, 이전 활성 행은 `is_active=false` +
  `valid_to` 로 닫는다. 기존 행의 4 영역 JSONB 는 절대 수정하지 않는다.
- **롤백은 옛 행을 되살리지 않는다** — 값을 복사한 새 버전을 만든다. 옛 행의 `is_active` 를
  다시 켜면 그 행의 `valid_from` 이 최초 활성화 시각 그대로라 **언제 롤백했는지가 이력에서
  사라진다**(정책 이력 = 감사 기록). v5 에서 v2 로 롤백하면 v2 의 값을 가진 v6 이 생긴다.
  없는 버전 404 `POLICY_NOT_FOUND`, 이미 활성인 버전 409 `POLICY_ALREADY_ACTIVE`.
- ⚠️ **자동 적용 규칙은 넣지 않았다** — 옛 docstring 의 "주간 KPI 가 전주 대비 10%↓ 이면
  자동 롤백" 은 잠금 결정(자동 적용 금지)과 정면으로 부딪히므로 팀 결정(AGENTS §8) 대상이다.

---

## 15. Notifications (`/notifications`) — S08

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/notifications/settings` | 내 알림 설정 |
| PATCH | `/notifications/settings` | morningTime / eveningTime / preCardEnabled |
| GET | `/notifications/vapid-public-key` | FE `applicationServerKey` 용 공개키 |
| POST | `/notifications/subscribe` | Web Push subscription 등록 (201, 갱신된 설정 반환) |
| DELETE | `/notifications/subscribe` | 구독 해제 (204, 멱등 — 구독 없어도 204) |
| POST | `/notifications/{notificationId}/opened` | 이 알림을 열었다고 표시 (204, 멱등) |

`GET /notifications/vapid-public-key` → `{ "publicKey": string | null }`:

- `publicKey` 는 서버 private key 의 **짝**. FE 는 구독 직전 이걸 **런타임에 받아** 쓴다 —
  하드코딩·빌드타임 주입은 키 rotate 시 조용히 깨진다(구독은 옛 키에 묶인 채 발송이 push
  서비스 403 으로 전부 실패, 구독 자체는 성공하므로 알아채기 어렵다).
- `publicKey: null` = 서버에 VAPID 미설정. FE 는 **구독을 만들지 말고** '알림 미지원' 표시.
- 인증 필수(구독 흐름 자체가 로그인 후). 값은 사용자 무관한 서버 상수.

`POST /notifications/subscribe` 요청 = 브라우저 `PushSubscription.toJSON()`:

```json
{
  "endpoint": "https://fcm.googleapis.com/…",
  "keys": { "p256dh": "…", "auth": "…" }
}
```

- `keys.p256dh` / `keys.auth` 누락·빈 값 → 422 `COMMON_VALIDATION_ERROR` (발송 암호화에 필수)
- `endpoint` 는 알려진 push 서비스의 **https** 주소만 받는다 — `fcm.googleapis.com`,
  `updates.push.services.mozilla.com`(`*.push.services.mozilla.com`), `web.push.apple.com`
  (`*.push.apple.com`), `*.notify.windows.com`. IP 리터럴·http·443 외 포트·목록 밖 호스트는
  422 `COMMON_VALIDATION_ERROR`(`field: "endpoint"`) — 서버가 이 URL 로 요청을 보내기 때문
  (SSRF 차단, v2.30-auth). 발송은 리다이렉트를 따라가지 않는다
- 재구독은 덮어쓰기 (1 device 1 subscription — Issue #16)
- 같은 `endpoint`(= 한 기기의 브라우저)는 **마지막으로 구독한 사용자에게만** 남는다 — 다른
  사용자 행에 같은 endpoint 가 있으면 그쪽 구독을 지운다(공용 기기에서 남의 알림이 뜨지 않게,
  v2.30-auth). FE 는 로그아웃할 때 `DELETE /notifications/subscribe` 를 먼저 부르는 것을 권장
- 응답은 `GET /notifications/settings` 와 같은 형태. `pushSubscribed` 는 저장된 구독 유무에서 파생

가드 (서버 측 enforce — 발송 게이트 `safety/push_gate.py` 단일 지점, ADR-0006):
- `morningTime` 06~10시, `eveningTime` 19~23시 외 → 422 `NOTIF_TIME_RANGE`
- 23~07시 자동 푸시 금지 — 구간은 `[23:00, 07:00)`. `eveningTime` 이 22:55 를 넘으면
  **22:55 로 클램프해 발송**한다 (quiet hours 전 마지막 5분 폴 — 미발송 사각지대 방지,
  ADR-0006 §7). FE 는 22:55 상한 노출 권장
- 주 ≤ 3건 — **사용자별 · 3 클래스(morning_brief/pre_card/evening_reflection) 합산 ·
  rolling 7일 · 실발송만 카운트**. cron 은 매일 시도하지만 실제 수신은 주 3건이 상한이다
  (알림 피로 최소화 — 베이스라인 §1.4 잠금의 문면 그대로, 해석 근거 ADR-0006 §2)
- 같은 클래스 하루(KST) 1건 — "24h 중복 금지"의 달력일 구현 (래칫 방지, ADR-0006 §3)
- 저녁 회고 알림은 **회고할 카드가 있을 때만** (경계는 `GET /reflection/pending` 과 동일).
  **일요일은 문구만 갈라진다**(`title`/`body`) — 같은 클래스에 주간 만다라 리포트 예고를
  얹는다. 새 클래스·새 발송 조건 없음(ADR-0008 §4, §8 "F")
- push payload 의 `url` 은 모든 클래스가 **`/`**(v2.30-auth). FE 라우터가 `/` 하나만 그려서
  예전 `/today`·`/reflection`·`/reviews/weekly` 는 알림을 누르면 빈 화면이었다. SW 가
  `?notificationId=` 를 붙여 열람 기록은 그대로 남는다. FE 가 화면별 경로를 지원하면 클래스별
  딥링크로 되돌린다
- pre_card 는 opt-in(`preCardEnabled`) + 시작 2~7분 전 (2분 리드 + 5분 폴)
- 전달 유효 시간(RFC 8030 TTL)·Urgency — pre_card 7분·`high`, evening_reflection 23:00 까지,
  morning_brief 3시간, 모두 23:00(quiet hours 시작)에서 자른다. 기기가 잠깐 꺼져 있어도 이
  시간 안에 켜지면 받는다(예전 TTL 0 은 "즉시 못 전하면 버림"이었다 — v2.30-auth)
- morning_brief 는 **재관여 대상이 있을 때만** — 오늘이 채택된 PARK/CARRY_OVER 회복의
  재관여 앵커 날짜인 사용자에게, `morning_brief` 클래스를 재사용해 발송한다(새 클래스
  아님, 근거 대장 §6.2 T2). 대상 없으면 그날은 발송 없음. `morningTime` 이 06:00~06:59
  면 07:00 으로 클램프해 발송(quiet hours 끝나는 시각 — evening 의 22:55 클램프와 대칭).
  **더는 권할 게 없는 회복은 대상이 아니다**(v2.30-recovery) — 이미 완주한 회복
  (`recovery_result='completed'`), 이어가기 카드를 끝냈거나(done/over_done) 목표 완료·계획
  교체로 치워진 경우, 원본 카드의 목표가 완료·보관된 경우. 만료(`reflection_skipped`)로 보관된
  카드와 해 보다 못 끝낸 회복은 그대로 대상이다. 앵커의 **시각**은 쓰지 않는다(날짜만)

`POST /notifications/{notificationId}/opened` — **⚠️ 아직 이 endpoint 를 부르는 FE 콜백이
없다.** push `notificationclick` 이벤트 핸들러가 준비되면 그 알림의 push payload 에 실린
`id` 필드(`notif_` 접두어 + PK, 예: `"notif_5f2a…"`)를 그대로 이 path 로 보내면 된다.
- 204 — 멱등(재호출도 204, 최초 1회만 `openedAt` 내부 기록)
- 404 `NOTIF_NOT_FOUND` — id 형식이 틀렸거나, 존재하지 않거나, **다른 사용자 소유**(id 를
  안다고 다 열리지 않는다)
- 근거: 근거 대장 §6.1 — S9 재알림 T1 억제 조건·근접 효과 측정의 선행 조건.

---

## 16. Settings / Privacy (`/settings`, `/privacy`) — S23, S28

| Method | Path | 설명 | 상태 |
| --- | --- | --- | --- |
| GET | `/settings` | 내 설정 메타 (tone, language, timezone, 알림 요약) | ✅ #23-A |
| PATCH | `/settings/tone-mode` | `gentle` / `strict` / `encouraging` | ✅ #23-A |
| GET | `/settings/profile` | 지속형 프로필 메모리 — behavioral(energyCycle·attentionSpan·timeChunkPreference·선호시각) + interaction(recoveryTone·suggestionStyle·explanationDepth·reminderFrequency). 인터뷰가 아직 안 채웠으면 각 항목 null. 인터뷰 종료는 **사용자가 답한 칸만** 쓴다 — [충분해요]·이탈로 비어 끝난 칸의 안전 기본값(피크 '변동'·활동창 09~23시·톤·최소 단위 10분·휴식 수용)은 쓰지 않고, 묻지 않은 집중 길이·최소 단위는 내 정보에서 고친 값을 그대로 둔다 (v2.30-interview) | ✅ #A |
| PATCH | `/settings/profile` | 프로필 메모리 부분 수정 — 지정 필드만 갱신(미지정 유지), 행 없으면 생성. enum 외 값 422 | ✅ #A |
| POST | `/settings/anonymize` | 즉시 익명화 (2단계 확인 토큰 필수) | ✅ #23-B |
| GET | `/privacy/consent` | 동의 기록 | ✅ #23-B |
| POST | `/privacy/consent` | 신규 동의 (마케팅/연구 등) | ✅ #23-B |
| POST | `/settings/delete-account` | 계정 삭제 (2단계 확인 토큰 필수) | ✅ #321 |

`GET /settings` 응답:

```json
{
  "toneMode": "gentle",          // gentle|strict|encouraging|null (인터뷰 전 null)
  "language": "ko",              // MVP 잠금 (한국어 only, DevBaseline §1.4)
  "timezone": "Asia/Seoul",
  "notifications": {             // §15 알림 설정 요약. 미설정 시 null (GET 은 행 미생성)
    "morningBriefTime": "08:00",
    "eveningReflectionTime": "21:00",
    "preCardEnabled": false
  }
}
```

- `PATCH /settings/tone-mode` 요청 `{ "toneMode": "strict" }` → 갱신된 `GET /settings` 형태 반환. 그 외 값은 422 `COMMON_VALIDATION_ERROR`. onboarding 상태 전이 없음.
- `/settings/profile` — 지속형 선호(에너지·시간·톤)의 **단일 진실 소스**. 온보딩 딥 인터뷰 완료 시 자동 영속(`behavioral_profiles`·`interaction_styles`), 이후 이 endpoint 로 조회/편집(#A). 인터뷰를 다시 하지 않아도 값 변경 가능. `PATCH` 는 부분 갱신(미지정 필드 유지), 행 없으면 생성. **계획 반영(v2.30-planA)**: 인터뷰가 끝난 뒤에 고친 `attentionSpan`(5~240)·`energyCycle` 은 계획 생성·마일스톤·재계획·만다라 다음 주기가 인터뷰 답 대신 쓴다(인터뷰 답에서 나올 값과 다를 때만 — 집중 길이를 고치면 목표별 세션 길이 대신 이 값, 집중 시간대는 전역 피크만 바꾸고 목표별 선호 시간은 유지). `reminderFrequency` 는 아직 어느 알림에도 쓰이지 않는다.
- 톤모드 적용: 시스템 프롬프트 prefix 1줄(`llm/prompt_compose.py`). `aiClient.run(tone_mode=...)` 배선 완료(ADR-0003 addendum 0003-llm-tool-executor.md) — **모든 LLM 호출**: inbox·recovery·morning_brief(#23-C) + interview·first_plan(#23-D, LangGraph는 config 채널).
- S28 Privacy(anonymize·consent)는 #23-B — consent 는 append-only `user_consents` 테이블(마이그레이션 동반).
- 자동 익명화: `last_active_at < now()-90d` 매일 04:00 KST cron (`last_active_at` 은 Google
  로그인과 `POST /auth/refresh` 성공 때 갱신 — v2.30-auth) — **구현 완료**(#24,
  `scheduler/anonymize_inactive.py`). `POST /settings/anonymize` 와 **같은 정의**의
  익명화이되 트리거만 다르다(사람 vs 시간). email 은 양쪽 다 안 건드린다 — 로그인 1차
  키라 마스킹하면 익명화가 아니라 사실상 계정 삭제가 되기 때문(그건 `/settings/delete-account`
  소관, #321). API 계약 변경 없음 — endpoint·스키마·에러코드 그대로.
- 익명화는 **그때까지의** 텍스트를 가리는 일이다. 익명화된 사용자가 Google 로 다시 로그인하면
  `is_anonymized`/`anonymized_at` 이 내려가 새 활동 기간이 시작된다(v2.30-auth) — 알림·습관
  sweep 에 다시 포함되고, 또 90일 비활성이면 새로 쓴 텍스트도 다시 익명화된다. 이미 가린
  과거 텍스트는 되살리지 않는다.

#23-B 구현 메모:
- `GET /privacy/consent` — consent_type(`required`/`marketing`/`research`) 별 **최신 1행**(`{ consentType, isGranted, updatedAt }`). 미기록 시 `[]`.
- `POST /privacy/consent` `{ consentType, granted }` — **append-only** 새 행 INSERT 후 갱신 현황 반환. 잘못된 type 422 `COMMON_VALIDATION_ERROR`.
- `POST /settings/anonymize` — **2단계**: 본문 없으면 `confirmationToken` 발급(`status="confirmation_required"`, 5분 TTL, HMAC). 토큰 동봉 재요청 시 검증 후 `_encrypted` 컬럼 7종 + 이름을 `[anonymized]` 마스킹 + `is_anonymized`/`anonymized_at` set(`status="anonymized"`). 토큰 위조/만료 422 `PRIVACY_INVALID_CONFIRMATION`, 이미 익명화 409 `PRIVACY_ALREADY_ANONYMIZED`. hard delete 아님(행 보존).
- **마스킹 범위(v2.30-auth)** — 익명화(수동·90일 cron·삭제 공통, `PrivacyRepo.anonymize_user`):
  `_encrypted` 컬럼 7종 + 이름 + **그 평문 사본**(인박스에서 만든 할 일 `source=inbox` 과
  인박스에서 승격한 목표의 제목·whyNow·firstStep) + **자유서술**(인터뷰 자유 입력 답 — 붙여넣은
  자료 포함, 빈 답·선택지 답 제외 / 회복 결정 사유 `decision_reason`). 캘린더 연결은 `revoked_at`
  까지 찍어 끊고, Google 쪽 권한도 원래 refresh token 으로 회수한다(best-effort, 커밋 뒤).
  직접 만든 목표·습관·일정 같은 계획 구조와 push 구독은 남긴다(계정을 계속 쓰는 경우).
- 2단계 확인 `message` 는 화면에 그대로 띄울 수 있는 문구다 — 무엇이 가려지는지·5분 안에 한 번
  더 눌러야 한다는 것을 말한다. 만료·위조 422 문구는 "확인 시간이 지났어요. 처음부터 한 번 더
  눌러 주세요." (FE: 이 코드면 확인 단계를 처음으로 되돌리면 된다.)
- ⚠️ **새 마이그레이션** `c2d3e4f5a6b7`(user_consents) — AGENTS §8 팀 합의 동반.
- 톤 prefix 의 `aiClient.run()` 배선은 **여전히 후속**(ADR-0003 addendum) — #23-B 범위 아님.

#321 구현 메모 (Play Store 계정 삭제 요구, FE #237 §4):
- `POST /settings/delete-account` — `anonymize` 와 같은 **2단계 확인**(별도 purpose, 토큰
  섞어 쓰기 불가). 확인 후: `PrivacyRepo.anonymize_user()` 로 `_encrypted` 컬럼 마스킹 +
  이름 `[anonymized]` + **email 을 `deleted-{userId}@reaction.invalid` 로 마스킹**(email
  에 hard UNIQUE 제약이 있어 원본을 남기면 그 주소로 재가입이 영구히 막힌다) + **`archivedAt`
  set(soft delete, hard delete 아님 — AGENTS §2)**.
- 삭제는 익명화 범위에 더해 **나머지 텍스트도 가린다**(`PrivacyRepo.purge_account_text`,
  v2.30-auth): 목표·할 일·습관·고정 일정·만다라 노드의 제목과 설명, 회복 제안 문구, 브리프·
  리뷰 문구, 중단 지점 메모, 계획 초안 스냅샷(`{}`), push 구독(NULL). 상태·카테고리·시각·숫자는
  통계용으로 남긴다. 행은 전부 보존(UPDATE 만).
- `archivedAt` 이 서기 되는 순간 `UserRepo.get_by_id`/`get_by_email` 의 기존
  `archived_at IS NULL` 필터에 걸린다 — 이미 발급된 **access token 은 다음 요청부터**
  `get_current_user` 에서 401 `AUTH_INVALID_TOKEN`(새 블랙리스트 불필요). **refresh
  token** 도 동일 필터를 타도록 `/auth/refresh` 를 이번에 함께 고쳤다(예전엔 jti revoke
  여부만 보고 사용자 존재를 확인하지 않아, 계정을 삭제해도 refresh 로는 계속 새 access
  를 받을 수 있었다 — 개별 jti 를 모르는 다중 기기 세션 전부를 막으려면 사용자 조회
  자체가 막혀야 했다).
- `anonymize`(계정은 유지한 채 과거 텍스트만 마스킹)와는 별개 작업 — 계정 삭제는 로그인
  자체를 막는다. 토큰 재사용 방지를 위해 confirm purpose 를 분리했다(`delete_account` vs
  `anonymize`).
- 에러: 토큰 위조/만료 422 `PRIVACY_INVALID_CONFIRMATION`(anonymize 와 코드 공유 —
  "확인 토큰이 틀렸다"는 의미가 같아 도메인을 더 안 쪼갰다).

---

## 17. Health (`/health`)

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/health` | `{ status, app, version, env, server_time }` — 인증 불필요 |

- **HTTP 상태 코드(v2.33)**: `status: "ok"` 면 **200**, `status: "degraded"` 면 **503**. 본문 모양은
  둘 다 같다(`ErrorResponse` 가 아니다). 상태 코드만 보는 외부 업타임 감시·Docker HEALTHCHECK 가
  DB 장애를 잡게 하려는 것이다 — ⚠️ 그전에는 degraded 도 200 이었다.
- `db` = `{ ok, latency_ms, error }`. DB 에 닿지 못하면 `status: "degraded"`, `db.error:
  "db_unavailable"` — **고정 값**이다(v2.30-auth). 공개 경로라 DB 예외 원문(내부 주소·DB 사용자
  이름)은 싣지 않고 서버 로그에만 남긴다. DB 미설정이면 종전대로 `"DATABASE_URL not configured"`.

---

## 18. Inbox (`/inbox`) — S24, S25

자연어 1줄 캡처 + AI 분류(Sequential Agent) + Triage 변환. DB: `inbox_items`.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/inbox` | 내 inbox 항목. `?status=captured\|classified\|archived\|promoted` 필터. 없는 값은 422 `COMMON_VALIDATION_ERROR`(`field=status`, v2.30-goals — 예전엔 500) |
| POST | `/inbox` | 1줄 캡처 — `{ rawText }`. `aiClient.run("inbox/classify")` 동기 호출(8s timeout) + 룰 fallback. 응답 시 `aiCategoryGuess` 채워짐 (`status=classified`) |
| PATCH | `/inbox/{id}` | `userCategory` override (6종 enum) 또는 `status` 변경. (v2.30-goals) 옮긴(`promoted`) 항목을 `captured`/`classified` 로 되돌리려 하면 409 `INBOX_ALREADY_PROMOTED` — 옮기기 버튼이 다시 살아나 같은 메모로 목표·카드가 또 생기던 경로다. `status="archived"` 는 `POST /inbox/{id}/archive` 와 같은 보관(`archivedAt` 도 찍힌다 — 예전엔 status 만 바뀌어 활성 목록과 보관함에 동시에 떴다) |
| POST | `/inbox/{id}/convert-to-goal` | Goal 생성 (tier=`maintain`, 한도 enforce → 422 `GOAL_TIER_LIMIT_EXCEEDED`) + inbox `status=promoted` + `promotedGoalId` 연결 (`promotedTo="goal"`). (v2.30-goals) **멱등** — 이미 목표로 옮긴 항목이면 새 목표를 만들지 않고 지금 항목을 200 으로(같은 `promotedGoalId`). 할 일로 옮긴 항목이면 409 `INBOX_ALREADY_PROMOTED`(`field=inboxId`). 두 번 탭이 동시에 와도 항목 행 잠금 + 사용자 단위 tier lock 으로 목표는 하나다. 목표 제목은 메모 앞 200자(`goals.title` 길이, 넘으면 끝에 `…`) — 원문은 인박스 항목에 그대로 남는다(예전엔 200자 넘는 메모가 500) |
| POST | `/inbox/{id}/convert-to-action` | ActionItem 생성 (`source=inbox`, `targetDate=today`) + inbox `status=promoted` (`promotedTo="action"`). (v2.30-goals) **멱등** — 이미 할 일로 옮긴 항목이면 카드를 또 만들지 않고 지금 항목을 200 으로. 목표로 옮긴 항목이면 409 `INBOX_ALREADY_PROMOTED`(`field=inboxId`) |
| POST | `/inbox/{id}/archive` | soft delete (`archived_at` + `status=archived`). 이후 `?status=archived` 로 조회, `restore` 로 복원 |
| POST | `/inbox/{id}/restore` | 보관 취소 — `archived_at` 클리어 + `status`→classified/captured. **이미 목표·할 일로 옮긴 항목은 `promoted` 로 돌아온다**(v2.30-goals — `promotedGoalId` 가 있거나 이 항목에서 만든 카드가 있으면. 예전엔 되살리면 옮기기 버튼이 다시 떠 카드·목표가 또 생겼다). 활성 항목이면 멱등. 없으면 404 `INBOX_NOT_FOUND` |
| GET | `/inbox/resources/{slug}` | 시스템 항목이 가리키는 정적 자료 본문 — `{ slug, title, markdown, steps }`. **인증만 필요하고 소유권 검사는 하지 않는다**(레포에 커밋된 공용 콘텐츠라 소유권 개념이 없다). 없으면 404 `COMMON_NOT_FOUND` |
| GET | `/inbox/coaching-advice` | 내 활성 목표·습관·오늘/어제 실행 기록을 서버에서 조합한 개인화 조언 최대 3건. 각 항목은 `adviceId`, `category`, `title`, `body`, `rationale`, `evidence`, `action`, `generatedAt`, `source`, `fallbackUsed` 를 반환. 기록이 없으면 빈 배열. 다른 조언이 없을 때의 "지금은 ‘…’에 집중해 볼까요?" 는 **진행 중(`active`)이고 보류(`parked`)·궁극목표가 아닌 목표**에서만 고른다 — 없으면 말하지 않는다(v2.30-goals — 예전엔 보류·완료한 목표를 "지금 집중할 목표" 라고 불렀다) |
| POST | `/inbox/{id}/adopt-step` | 자료가 제안한 한 걸음을 오늘 할 일로 채택 — `{ stepIndex }` → `{ actionId, title, targetDate, resourceSlug }`. `ActionItem(source=inbox)` 생성 + `inbox_item_id` 로 자료에 연결. 카드의 `category` 는 **자료의 카테고리**(9종 원본) — `userCategory` 재분류가 있으면 그게 우선. **자료 항목은 promoted 로 바뀌지 않는다**(다른 걸음을 또 채택하거나 다시 읽을 수 있다). **도메인 멱등(#213)**: 같은 걸음(`stepIndex` 동일)을 오늘 다시 채택하면 새 카드를 만들지 않고 기존 활성 카드의 `actionId` 를 200 으로 반환 — 날짜가 바뀌거나 카드가 보관된 뒤에는 다시 새 카드가 생긴다. 없거나 보관된 항목이면 404 `INBOX_NOT_FOUND`, 자료 파일이 사라졌으면 404 `COMMON_NOT_FOUND`, system 항목이 아니면 422 `COMMON_VALIDATION_ERROR`(`field=inboxId`), 없는 인덱스면 422(`field=stepIndex`) |

- `status`: `captured` / `classified` / `archived` / `promoted`. `GET /inbox` 는 기본 활성(archived 제외), `?status=archived` 로 보관함 조회
- `promotedTo`: `status=promoted` 일 때만 `"goal"`(promotedGoalId 로 딥링크) / `"action"`(오늘 실행 화면). 그 외 `null` — **파생 필드**(promoted + goalId 유무로 계산, DB 컬럼 아님)
- `category` enum (6종): `study` / `project` / `health` / `routine` / `schedule` / `other` (Goal/Action 9종의 subset)
- **원문(`rawText`)은 at-rest AES-256-GCM 암호화** (`raw_text_encrypted`, `safety.encrypt_inbox_text`). 응답에는 복호화된 평문
- `aiCategoryGuess` 는 LLM 호출 또는 룰 fallback 결과. `userCategory` 가 우선 (override). 둘 다 없으면 `other`
- ID prefix: `inbox_<uuid>`
- `source` (#171): `user`(사용자 캡처) / `system`(목표 카테고리에 맞춰 자동으로 넣어 준 추천 자료). `promotedTo` 와 달리 **저장 컬럼 그대로**이며 파생 필드가 아니다
- `resourceSlug` (#171): system 항목이 가리키는 자료 slug. user 항목은 `null`. **ID prefix 를 붙이지 않는다** — 그대로 `GET /inbox/resources/{slug}` 경로에 들어간다
- **system 항목은 승격할 수 없다** — `convert-to-goal` / `convert-to-action` 둘 다 422 `COMMON_VALIDATION_ERROR` (`field="inboxId"`). 자료를 목표로 올리는 건 의미가 없고, 자료→목표→새 자료 재귀가 생긴다
- `steps` (#171 후속): 자료가 제안하는 한 걸음 1~5개. 채택하면 오늘 할 일이 되고 그 뒤는 **기존 실행·회고·회복 루프가 그대로** 처리한다 — 회복은 여전히 21시 일괄 회고에서만 시작된다
- 자료 삽입은 목표가 **active 로 생긴 순간**에만, 카테고리당 1건, 멱등(**보관한 것도 '이미 있음'**). 삽입 실패는 목표 생성을 막지 않는다(best-effort)

---

## 19. Fixed Schedules (`/fixed-schedules`) — S05

캘린더 미연결 사용자의 수업·알바·정기 약속. DB: `fixed_schedules`.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/fixed-schedules` | 내 고정 일정 전체 |
| POST | `/fixed-schedules` | 신규 — `{ title, daysOfWeek, startTime, endTime }` |
| PATCH | `/fixed-schedules/{id}` | 부분 수정 |
| DELETE | `/fixed-schedules/{id}` | soft delete (`archived_at`) |

- `daysOfWeek`: `["mon","tue",…]` 배열 — 비어 있으면 422(PATCH 도), 중복은 한 번만 저장(v2.30). 목록 밖 값이 섞이면 422 `field="daysOfWeek"`, 메시지 "알 수 없는 요일이 있어요: <값, 값> — 요일을 다시 골라 주세요."(v2.30 — 예전엔 파이썬 목록 표기 `['monday', 'wednesday']` 가 그대로 화면에 떴다). `startTime`/`endTime`: `HH:MM`. **`endTime: "24:00"`(밤 12시까지)을 받는다**(v2.30 — 그날 끝까지 막고, 응답에는 `"23:59"` 로 보인다)
- `title`: 앞뒤 공백을 걷어 저장, 비었거나 200자를 넘으면 422 `field="title"`(v2.30 — 예전엔 공백뿐인 제목이 저장되고 201자는 500 이었다)
- **자정을 넘는 일정(`startTime > endTime`, 예: 22:00–02:00)은 422** `field="startTime"`, 메시지 "자정을 넘기는 일정은 둘로 나눠 넣어 주세요. 예: 금 22:00–24:00, 토 00:00–02:00". **단 자정을 넘겨 읽었을 때 12시간을 넘는 범위(예: 11:00–10:00 → 23시간)는 거꾸로 적은 것으로 보고** "종료 시각이 시작 시각보다 늦어야 해요. 시작과 종료가 바뀌지 않았는지 확인해 주세요." 를 준다(v2.30 — 같은 422·같은 `field`, 문구만 갈린다). 시작 = 종료는 "시작 시각은 종료 시각보다 빨라야 해요." 형식 오류 메시지에는 필드 코드 대신 "시작 시각"/"종료 시각" 이 들어간다(기계용은 `field`)
- 같은 요일 시간 겹치면 409 `FIXED_SCHEDULE_OVERLAP` — **v2.30 부터 실제로 검사한다**(그전엔 문서에만 있었다). 맞닿는 건(10:00 끝·10:00 시작) 겹침이 아니다. 사용자별 lock 안에서 검사·저장해 [추가] 연타도 한 줄만 생긴다. PATCH 는 요일·시각을 바꿀 때만 검사한다(자기 자신 제외 — 이미 겹쳐 저장된 예전 일정도 제목은 고칠 수 있다). 온보딩 진행에 최소 1개 필요

---

## 20. 변경 절차

1. 변경 PR에 본 문서 수정 포함 + [`api-change-log.md`](api-change-log.md) 항목 추가
2. FE/BE 리뷰어 모두 지정
3. 기존 endpoint의 호환 깨는 변경은 `/v2/` prefix 신설 후 단계 deprecate
4. 에러 코드 신설 시 §1.4 표 갱신
5. 응답 envelope·에러·Idempotency·시간 규약 변경은 [ADR-0002](decisions/0002-api-contract-freeze.md) 수정 PR 경유
