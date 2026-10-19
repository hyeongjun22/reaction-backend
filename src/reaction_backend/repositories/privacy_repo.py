"""Privacy repository — S28 즉시 익명화 (Issue #23-B) · 계정 삭제 (#321) · 90일 cron (#24).

두 층으로 나뉜다:

- `PrivacyRepo` — SQL 마스킹만. `anonymize_user` 는 익명화(수동·cron·삭제 공통)의 텍스트
  마스킹, `purge_account_text` 는 계정 삭제에서만 더하는 나머지 텍스트 마스킹이다.
- `anonymize_account` / `revoke_calendar_grant` — 세 진입점(수동 익명화·계정 삭제·90일 cron)이
  **똑같이** 밟는 순서(캘린더 연결 회수 → 마스킹 → 플래그·이름)를 한 곳에 모은다. 예전엔 세
  호출부가 플래그 쓰기를 각자 복붙했고, 이미 한 곳(삭제)이 갈라져 있었다 — 규칙을 하나 더하면
  한 경로가 빠지기 쉽다(auth-15).

sentinel 은 `[anonymized]` (safety.encryption: decrypt 가 sentinel 을 그대로 반환 → 복호화
깨지지 않음). commit 은 호출자 책임. hard delete 아님 (AGENTS §2) — 행은 보존, 내용만 마스킹.

## 무엇을 가리는가

익명화(`anonymize_user`, 계정 유지):
- `*_encrypted` 컬럼 전부 (캘린더 토큰·실행 피드백·인박스 원문·중단 메모·LLM 입출력 요약·실패 메모)
- **그 평문 사본** — 인박스에서 만든 할 일(`action_items.source='inbox'`)·목표
  (`inbox_items.promoted_goal_id`)의 제목·why_now·first_step. 암호문만 가리고 사본을 남기면
  익명화가 무의미했다(data-3: 인박스 '○○ 교수님 면담 010-…' 가 할 일 제목으로 그대로 남았다).
- 사용자가 직접 쓴 자유서술 — 인터뷰 자유 입력 답(`interview_slot_answers` 의 text 답, 붙여넣은
  자료 포함)·회복 결정 사유(`recovery_attempts.decision_reason`).
- 캘린더 연결은 `revoked_at` 도 찍는다 — 토큰을 sentinel 로 덮은 행이 "연결됨"으로 남으면 화면은
  연결됐다고 보이는데 호출은 전부 실패한다.

계정 삭제(`purge_account_text`, 추가로): 목표·할 일·습관·고정 일정·만다라 노드 제목과 설명,
회복 제안 문구, 브리프·리뷰 문구, 중단 지점 메모, 계획 초안 스냅샷, push 구독(endpoint 도
기기 식별자다). 상태·카테고리·시각·숫자는 통계용으로 남긴다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import Depends
from sqlalchemy import Update, case, func, literal, null, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.calendar_connection import CalendarConnection
from reaction_backend.db.models.daily_brief import DailyBrief
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag
from reaction_backend.db.models.fixed_schedule import FixedSchedule
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.inbox_item import InboxItem
from reaction_backend.db.models.interruption_event import InterruptionEvent
from reaction_backend.db.models.interview_session import InterviewSession
from reaction_backend.db.models.interview_slot_answer import InterviewSlotAnswer
from reaction_backend.db.models.llm_run import LlmRun
from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.db.models.period_summary import PeriodSummary
from reaction_backend.db.models.plan_draft import PlanDraft
from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
from reaction_backend.db.models.user import User
from reaction_backend.db.session import get_db
from reaction_backend.integrations.google_calendar import oauth, token_store
from reaction_backend.safety.encryption import ANONYMIZED_SENTINEL

_log = logging.getLogger(__name__)

_S = ANONYMIZED_SENTINEL

# 인터뷰 자유 입력 답을 가린 모양 — 읽는 쪽(interview_adapter)이 기대하는 text 형태를 유지한다.
_MASKED_TEXT_ANSWER: dict[str, Any] = {"type": "text", "raw": _S, "normalized": []}


def _mask_nullable(column: InstrumentedAttribute[str | None]) -> Any:
    """NULL 은 NULL 로 두고 값이 있으면 sentinel — "원래 비어 있었다"는 사실은 남긴다."""
    return case((column.is_(None), null()), else_=literal(_S))


class PrivacyRepo:
    """사용자 텍스트 일괄 마스킹 (모듈 docstring 의 범위)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _run(self, stmt: Update) -> int:
        result = await self._session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def anonymize_user(self, user_id: UUID) -> int:
        """익명화 마스킹 — 암호화 컬럼 + 그 평문 사본 + 자유서술. 변경 행 수 반환."""
        masked = 0
        masked += await self._run(
            update(CalendarConnection)
            .where(CalendarConnection.user_id == user_id)
            .values(
                access_token_encrypted=_S,
                refresh_token_encrypted=_S,
                # 토큰을 덮은 행은 더는 연결이 아니다 — 이미 끊긴 행의 시각은 그대로 둔다.
                revoked_at=func.coalesce(CalendarConnection.revoked_at, func.now()),
            )
        )
        masked += await self._run(
            update(ExecutionEvent)
            .where(
                ExecutionEvent.user_id == user_id,
                ExecutionEvent.user_feedback_encrypted.is_not(None),
            )
            .values(user_feedback_encrypted=_S)
        )
        masked += await self._run(
            update(InboxItem).where(InboxItem.user_id == user_id).values(raw_text_encrypted=_S)
        )
        masked += await self._run(
            update(InterruptionEvent)
            .where(
                InterruptionEvent.user_id == user_id,
                InterruptionEvent.interrupt_context_note_encrypted.is_not(None),
            )
            .values(interrupt_context_note_encrypted=_S)
        )
        masked += await self._run(
            update(LlmRun)
            .where(LlmRun.user_id == user_id)
            .values(input_summary_encrypted=_S, output_summary_encrypted=_S)
        )
        # execution_failure_tags 는 user_id 없음 → execution 조인 서브쿼리.
        exec_ids = select(ExecutionEvent.id).where(ExecutionEvent.user_id == user_id)
        masked += await self._run(
            update(ExecutionFailureTag)
            .where(
                ExecutionFailureTag.execution_id.in_(exec_ids),
                ExecutionFailureTag.memo_encrypted.is_not(None),
            )
            .values(memo_encrypted=_S)
        )

        # ── 암호화한 인박스 원문의 평문 사본 (convert-to-action / convert-to-goal) ──
        masked += await self._run(
            update(ActionItem)
            .where(ActionItem.user_id == user_id, ActionItem.source == "inbox")
            .values(
                title=_S,
                why_now=_mask_nullable(ActionItem.why_now),
                first_step=_mask_nullable(ActionItem.first_step),
            )
        )
        promoted_goal_ids = select(InboxItem.promoted_goal_id).where(
            InboxItem.user_id == user_id, InboxItem.promoted_goal_id.is_not(None)
        )
        masked += await self._run(
            update(Goal)
            .where(Goal.user_id == user_id, Goal.id.in_(promoted_goal_ids))
            .values(
                title=_S,
                why_now=_mask_nullable(Goal.why_now),
                first_step=_mask_nullable(Goal.first_step),
            )
        )

        # ── 사용자가 직접 쓴 자유서술 ──
        # 인터뷰 자유 입력 답(붙여넣은 자료 포함). 예전 스킵 표식(빈 텍스트)은 그대로 둔다 —
        # 가리면 건너뛴 슬롯이 답한 슬롯으로 바뀐다. 지금 스킵(`type: skip`)은 원문을 담지 않아
        # 가릴 게 없다(`domain/slot_value`). chip·range·spec 답은 선택지라 남긴다.
        session_ids = select(InterviewSession.id).where(InterviewSession.user_id == user_id)
        masked += await self._run(
            update(InterviewSlotAnswer)
            .where(
                InterviewSlotAnswer.session_id.in_(session_ids),
                InterviewSlotAnswer.value["type"].astext == "text",
                func.coalesce(InterviewSlotAnswer.value["raw"].astext, "") != "",
            )
            .values(value=_MASKED_TEXT_ANSWER)
        )
        masked += await self._run(
            update(RecoveryAttempt)
            .where(
                RecoveryAttempt.user_id == user_id,
                RecoveryAttempt.decision_reason.is_not(None),
            )
            .values(decision_reason=_S)
        )
        return masked

    async def purge_account_text(self, user_id: UUID) -> int:
        """계정 삭제 전용 — 익명화가 남기는 나머지 텍스트까지 가린다. 변경 행 수 반환.

        익명화는 계정을 계속 쓰는 사람(또는 돌아올 사람)의 계획 구조를 남기지만, 삭제는 다시
        들어올 수 없는 계정이라 남길 이유가 없다(Play Store 데이터 삭제 요구). 상태·카테고리·
        시각·숫자는 통계용으로 남긴다. `anonymize_user` 와 함께 부른다(대신이 아니다).
        """
        masked = 0
        masked += await self._run(
            update(ActionItem)
            .where(ActionItem.user_id == user_id)
            .values(
                title=_S,
                why_now=_mask_nullable(ActionItem.why_now),
                first_step=_mask_nullable(ActionItem.first_step),
            )
        )
        masked += await self._run(
            update(Goal)
            .where(Goal.user_id == user_id)
            .values(
                title=_S,
                why_now=_mask_nullable(Goal.why_now),
                first_step=_mask_nullable(Goal.first_step),
            )
        )
        goal_ids = select(Goal.id).where(Goal.user_id == user_id)
        masked += await self._run(
            update(GoalNode)
            .where(GoalNode.goal_id.in_(goal_ids))
            .values(title=_S, why_text=_mask_nullable(GoalNode.why_text))
        )
        masked += await self._run(update(Habit).where(Habit.user_id == user_id).values(title=_S))
        masked += await self._run(
            update(FixedSchedule).where(FixedSchedule.user_id == user_id).values(title=_S)
        )
        masked += await self._run(
            update(RecoveryAttempt)
            .where(RecoveryAttempt.user_id == user_id)
            .values(
                suggested_action_text=_mask_nullable(RecoveryAttempt.suggested_action_text),
                obstacle=_mask_nullable(RecoveryAttempt.obstacle),
                coping_clause=_mask_nullable(RecoveryAttempt.coping_clause),
                acknowledgment=_mask_nullable(RecoveryAttempt.acknowledgment),
            )
        )
        masked += await self._run(
            update(InterruptionEvent)
            .where(
                InterruptionEvent.user_id == user_id,
                InterruptionEvent.suspended_step.is_not(None),
            )
            .values(suspended_step=_S)
        )
        masked += await self._run(
            update(DailyBrief).where(DailyBrief.user_id == user_id).values(headline_text=_S)
        )
        masked += await self._run(
            update(PeriodSummary)
            .where(PeriodSummary.user_id == user_id)
            .values(
                llm_one_liner=_mask_nullable(PeriodSummary.llm_one_liner),
                failure_analysis=_mask_nullable(PeriodSummary.failure_analysis),
            )
        )
        # 초안 스냅샷은 목표·할 일 제목을 통째로 복사해 둔다 — 상태 행은 남기고 내용만 비운다.
        masked += await self._run(
            update(PlanDraft).where(PlanDraft.user_id == user_id).values(payload={})
        )
        # push endpoint 는 기기를 가리키는 식별자다 — 삭제한 계정의 알림이 그 기기로 갈 일도 없다.
        masked += await self._run(
            update(NotificationSetting)
            .where(
                NotificationSetting.user_id == user_id,
                NotificationSetting.push_subscription.is_not(None),
            )
            .values(push_subscription=null())
        )
        return masked


class PrivacyMasker(Protocol):
    """`anonymize_account` 가 쓰는 마스킹 인터페이스 — 실 repo 와 테스트 fake 공용."""

    async def anonymize_user(self, user_id: UUID) -> int: ...

    async def purge_account_text(self, user_id: UUID) -> int: ...


@dataclass(slots=True)
class AnonymizeOutcome:
    """`anonymize_account` 결과 — commit **뒤에** 할 원격 정리를 호출자에게 넘긴다."""

    masked: int
    calendar_refresh_token: str | None
    """Google 쪽 권한 회수(`revoke_calendar_grant`)에 넘길 원래 refresh token. 연결 없으면 None."""


async def _revoke_calendar_connection(session: AsyncSession, user_id: UUID) -> str | None:
    """살아 있는 캘린더 연결을 우리 쪽에서 끊고, 원격 회수용 원래 refresh token 을 돌려준다.

    토큰을 sentinel 로 덮기 **전에** 읽어야 한다 — 덮은 뒤에는 Google 쪽 권한을 영영 회수할 수
    없다(Google 계정의 "액세스 권한이 있는 앱"에 re:action 이 계속 남고, 같은 계정으로 다시
    가입하면 Google 이 refresh token 을 안 줘 첫 연결이 실패했다 — calendar-3).
    복호화가 안 되면(키 교체 등) 원격 회수만 건너뛰고 우리 쪽 해제는 그대로 한다.
    """
    connection = await token_store.get_active(session, user_id=user_id)
    if connection is None:
        return None
    refresh: str | None
    try:
        refresh = token_store.refresh_token_of(connection)
    except Exception:  # noqa: BLE001 — 원격 회수는 best-effort, 익명화 자체를 막지 않는다
        _log.warning("calendar refresh token unreadable on anonymize: user=%s", user_id)
        refresh = None
    await token_store.mark_revoked(session, connection)
    await session.flush()
    return refresh


async def anonymize_account(
    session: AsyncSession,
    user: User,
    *,
    privacy_repo: PrivacyMasker,
    now: datetime,
    delete: bool = False,
) -> AnonymizeOutcome:
    """익명화 한 건 — 수동 익명화·계정 삭제·90일 cron 이 **같은 순서**로 부른다.

    1. 캘린더 연결 해제(`revoked_at`) + 원래 refresh token 확보 (마스킹 전에)
    2. 텍스트 마스킹 (`anonymize_user`, 삭제면 `purge_account_text` 까지)
    3. `is_anonymized`·`anonymized_at`·이름 마스킹 (삭제면 email·`archived_at` 까지)

    commit 은 호출자 책임. commit 한 **뒤** `revoke_calendar_grant(outcome.calendar_refresh_token)`
    를 불러 Google 쪽 권한을 회수한다(우리 DB 를 먼저 확정 — `disconnect_calendar` 와 같은 순서).
    """
    refresh = await _revoke_calendar_connection(session, user.id)
    masked = await privacy_repo.anonymize_user(user.id)
    if delete:
        masked += await privacy_repo.purge_account_text(user.id)
        # email 에 hard UNIQUE 제약 — 원본을 남기면 그 주소로 재가입이 영영 막힌다.
        user.email = f"deleted-{user.id}@reaction.invalid"
        user.archived_at = now
    user.is_anonymized = True
    user.anonymized_at = now
    user.name = ANONYMIZED_SENTINEL
    return AnonymizeOutcome(masked=masked, calendar_refresh_token=refresh)


async def revoke_calendar_grant(refresh_token: str | None) -> None:
    """Google 쪽 캘린더 권한 회수 — best-effort, 절대 예외를 올리지 않는다(commit 뒤에 부른다)."""
    if not refresh_token or refresh_token == ANONYMIZED_SENTINEL:
        return
    try:
        await oauth.revoke(refresh_token)
    except Exception:  # noqa: BLE001 — 이미 커밋된 익명화/삭제 응답을 깨지 않는다
        _log.warning("calendar grant revoke failed after anonymize", exc_info=True)


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_privacy_repo(session: SessionDep) -> PrivacyRepo:
    return PrivacyRepo(session)
