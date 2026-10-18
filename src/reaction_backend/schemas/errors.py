"""에러 코드 레지스트리 + `ApiError` 예외.

re:action API 에러 규약 (ADR-0002 §2.2 / api-contract.md §1.3):

- 모든 4xx/5xx 응답은 `schemas.common.ErrorResponse` 한 형태로 직렬화된다.
- 에러 코드는 **도메인 prefix + UPPER_SNAKE_CASE**.
- 도메인 코드는 본 `ErrorCode` 의 해당 도메인 구역에 **append-only** 로 추가한다.
  (다른 도메인 구역은 수정하지 않는다 — PR 간 머지 충돌 방지.)
- 라우터·서비스는 `raise ApiError(ErrorCode.X, "메시지")` 로 던지고,
  전역 핸들러(`api/exception_handlers.py`)가 `ErrorResponse` 로 변환한다.
"""

from __future__ import annotations

from enum import StrEnum
from http import HTTPStatus


class ErrorCode(StrEnum):
    """API 에러 코드. 도메인 prefix + UPPER_SNAKE_CASE.

    #3-A 는 공통(`COMMON_`)·Idempotency(`IDEMPOTENCY_`) 구역만 정의한다.
    도메인 코드(`AUTH_*`, `INTERVIEW_*` …)는 #3-B~#3-H 에서 도메인 구역에 추가한다.
    """

    # ── 공통 (COMMON_) — #3-A ──
    COMMON_VALIDATION_ERROR = "COMMON_VALIDATION_ERROR"
    COMMON_NOT_FOUND = "COMMON_NOT_FOUND"
    COMMON_METHOD_NOT_ALLOWED = "COMMON_METHOD_NOT_ALLOWED"
    COMMON_NOT_IMPLEMENTED = "COMMON_NOT_IMPLEMENTED"
    COMMON_INTERNAL_ERROR = "COMMON_INTERNAL_ERROR"

    # ── Idempotency (IDEMPOTENCY_) — #3-A ──
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_KEY_MISMATCH = "IDEMPOTENCY_KEY_MISMATCH"

    # ── 도메인 코드는 아래에 도메인별 구역으로 append (#3-B~#3-H) ──

    # ── Auth (AUTH_) — #3-B / #16 ──
    AUTH_INVALID_ID_TOKEN = "AUTH_INVALID_ID_TOKEN"
    AUTH_INVALID_TOKEN = "AUTH_INVALID_TOKEN"
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
    # 가입 게이트(#324) — 신규 가입(신규 email)에만 해당, 기존 사용자 로그인은 겪지 않는다.
    AUTH_INVALID_INVITE_CODE = "AUTH_INVALID_INVITE_CODE"
    AUTH_INVITE_CODE_ALREADY_USED = "AUTH_INVITE_CODE_ALREADY_USED"
    AUTH_SIGNUP_CAPACITY_REACHED = "AUTH_SIGNUP_CAPACITY_REACHED"
    AUTH_SIGNUPS_DISABLED = "AUTH_SIGNUPS_DISABLED"

    # ── Interview (INTERVIEW_) — #3-B ──
    INTERVIEW_SESSION_EXISTS = "INTERVIEW_SESSION_EXISTS"
    INTERVIEW_SESSION_NOT_FOUND = "INTERVIEW_SESSION_NOT_FOUND"
    INTERVIEW_SLOT_LOCKED = "INTERVIEW_SLOT_LOCKED"
    # 답 제출의 `clientTurn` 이 세션의 현재 턴과 다르다 — 이미 처리된 답의 재전송(409).
    INTERVIEW_STALE_TURN = "INTERVIEW_STALE_TURN"

    # ── Time Policies (POLICY_) — #3-C ──
    POLICY_NOT_FOUND = "POLICY_NOT_FOUND"
    # 블록 편집(S15)이 활성 시간 정책 윈도우(sleep/lunch/late_night_block)에 진입 — #21-B
    POLICY_VIOLATION = "POLICY_VIOLATION"
    # 이미 활성인 버전으로 롤백 요청 → 409 (#168). 같은 값을 한 번 더 쌓을 이유가 없다.
    POLICY_ALREADY_ACTIVE = "POLICY_ALREADY_ACTIVE"

    # ── Planning / Blocks (PLAN_) — #21-B ──
    PLAN_BLOCK_NOT_FOUND = "PLAN_BLOCK_NOT_FOUND"
    PLAN_BLOCK_CONFLICT = "PLAN_BLOCK_CONFLICT"
    PLAN_INVALID_TIME = "PLAN_INVALID_TIME"

    # ── Calendar (CALENDAR_) — #3-C ──
    CALENDAR_NOT_CONNECTED = "CALENDAR_NOT_CONNECTED"
    CALENDAR_CONFLICT = "CALENDAR_CONFLICT"

    # ── Fixed Schedules (FIXED_SCHEDULE_) — #3-C ──
    FIXED_SCHEDULE_NOT_FOUND = "FIXED_SCHEDULE_NOT_FOUND"
    FIXED_SCHEDULE_OVERLAP = "FIXED_SCHEDULE_OVERLAP"

    # ── Notifications (NOTIF_) — #3-C ──
    NOTIF_TIME_RANGE = "NOTIF_TIME_RANGE"
    NOTIF_NOT_FOUND = "NOTIF_NOT_FOUND"

    # ── Goals (GOAL_) — #3-D / #22 ──
    GOAL_NOT_FOUND = "GOAL_NOT_FOUND"
    GOAL_FOCUS_LIMIT = "GOAL_FOCUS_LIMIT"
    GOAL_MAINTAIN_LIMIT = "GOAL_MAINTAIN_LIMIT"
    # Issue #22 + ADR-0005 §2.5.1 — Focus≤3 / Maintain≤5 단일 422 코드 (위 둘은 deprecated, 잔존)
    GOAL_TIER_LIMIT_EXCEEDED = "GOAL_TIER_LIMIT_EXCEEDED"

    # ── Planning (PLAN_) — #32 / #62 ──
    # First Plan 승인(SAVING) 시 절대 시간 정책 위반 → 트랜잭션 롤백 후 422.
    PLAN_POLICY_VIOLATION = "PLAN_POLICY_VIOLATION"
    # 승인 트랜잭션 영속화 실패(부분 실패 롤백 후) → 500.
    PLAN_SAVE_FAILED = "PLAN_SAVE_FAILED"
    # plan_id 에 해당하는 Draft 없음(또는 타 사용자) → 404. (#62)
    PLAN_DRAFT_NOT_FOUND = "PLAN_DRAFT_NOT_FOUND"
    # Draft 72h 만료(ADR-0005 §7.8) → 410. (#62)
    PLAN_DRAFT_EXPIRED = "PLAN_DRAFT_EXPIRED"
    # 이미 승인된 Draft 를 폐기(discard)하려 함 → 409. 승인은 되돌리는 동작이 아니다.
    PLAN_ALREADY_APPROVED = "PLAN_ALREADY_APPROVED"

    # ── Habits (HABIT_) — #3-D ──
    HABIT_NOT_FOUND = "HABIT_NOT_FOUND"
    # 3주 연속 미달 조건 미충족 / 이번 사이클 이미 처리 — #21-C
    HABIT_PENALTY_NOT_ELIGIBLE = "HABIT_PENALTY_NOT_ELIGIBLE"

    # ── Privacy (PRIVACY_) — #23-B ──
    # 익명화 2단계 확인 토큰 누락/위조/만료 → 422.
    PRIVACY_INVALID_CONFIRMATION = "PRIVACY_INVALID_CONFIRMATION"
    # 이미 익명화된 계정 → 409.
    PRIVACY_ALREADY_ANONYMIZED = "PRIVACY_ALREADY_ANONYMIZED"

    # ── Inbox (INBOX_) — #3-D ──
    INBOX_NOT_FOUND = "INBOX_NOT_FOUND"
    INBOX_ALREADY_PROMOTED = "INBOX_ALREADY_PROMOTED"

    # ── Today / Execution (TODAY_) — #19-B ──
    TODAY_EXECUTION_NOT_FOUND = "TODAY_EXECUTION_NOT_FOUND"
    TODAY_EXECUTION_ALREADY_ACTIVE = "TODAY_EXECUTION_ALREADY_ACTIVE"
    TODAY_ALREADY_CHECKED_IN = "TODAY_ALREADY_CHECKED_IN"
    TODAY_ALREADY_PAUSED = "TODAY_ALREADY_PAUSED"
    TODAY_NOT_PAUSED = "TODAY_NOT_PAUSED"

    # ── Reflection (REFLECT_) — #19-B ──
    REFLECT_INVALID_TAG = "REFLECT_INVALID_TAG"
    REFLECT_NOT_FAILED = "REFLECT_NOT_FAILED"
    REFLECT_ALREADY_TAGGED = "REFLECT_ALREADY_TAGGED"

    # ── Recovery (RECOVERY_) — #20-A ──
    RECOVERY_EXECUTION_NOT_FOUND = "RECOVERY_EXECUTION_NOT_FOUND"
    RECOVERY_NOT_ELIGIBLE = "RECOVERY_NOT_ELIGIBLE"
    RECOVERY_NO_PROPOSAL = "RECOVERY_NO_PROPOSAL"
    RECOVERY_ATTEMPT_NOT_FOUND = "RECOVERY_ATTEMPT_NOT_FOUND"
    RECOVERY_ALREADY_DECIDED = "RECOVERY_ALREADY_DECIDED"
    # #20-B — replan: 수락된 회복(새 ActionItem)이 없어 일정 재배치할 대상이 없음
    RECOVERY_NO_REPLAN = "RECOVERY_NO_REPLAN"
    RECOVERY_EDIT_NOT_SUPPORTED = "RECOVERY_EDIT_NOT_SUPPORTED"

    # ── Reviews (REVIEW_) — #21-A ──
    REVIEW_INVALID_WEEK = "REVIEW_INVALID_WEEK"

    # ── Agent 동시성 (AGENT_) — ADR-0005 §7.6 ──
    # user_id × agent advisory lock 미획득 (다른 디바이스에서 진행 중). Interview/Planning/Recovery 공용.
    AGENT_CONCURRENT_ACCESS = "AGENT_CONCURRENT_ACCESS"

    # ── Rate Limit (RATE_LIMIT_) — #325 ──
    # 비싼 엔드포인트(interview turn/plans generate/mandala generate/recovery proposals)의
    # 사용자별 일일 호출 횟수 상한. 전역 LLM 예산(BudgetExceeded)과는 다른 축이다 — 이건
    # "AI 가 꺼져서"가 아니라 "이 사람이 이 엔드포인트를 오늘 너무 많이 불러서" 막는 것이라
    # 룰 폴백으로 조용히 내려주지 않고 명시 거절한다(재시도 시점을 아는 게 사용자에게 낫다).
    RATE_LIMIT_DAILY_CALLS_EXCEEDED = "RATE_LIMIT_DAILY_CALLS_EXCEEDED"


class ApiError(Exception):
    """도메인 코드가 던지는 표준 에러.

    전역 핸들러가 `ErrorResponse{code, message, field, server_time}` 로 변환한다.

    Args:
        code: `ErrorCode` 레지스트리의 코드.
        message: 사용자 노출 가능한 한국어 메시지.
        http_status: HTTP 상태 코드 (기본 400).
        field: 입력 검증 에러일 때 해당 필드명.
        headers: 추가 응답 헤더(예: 429 의 `Retry-After`) — 에러 본문(JSON) 형태(ADR-0002
            §2.2)는 절대 안 늘린다는 원칙을 지키면서 "언제 풀리는지" 를 표준 HTTP 메커니즘
            으로 전달하기 위함(#325). 대부분의 에러는 안 쓴다(기본 None).
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        http_status: HTTPStatus | int = HTTPStatus.BAD_REQUEST,
        field: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code: ErrorCode = code
        self.message = message
        self.http_status = int(http_status)
        self.field = field
        self.headers = headers
        super().__init__(f"{code.value}: {message}")
