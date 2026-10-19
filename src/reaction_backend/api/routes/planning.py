"""Planning — Weekly plan (S06, S14, S15, S16).

핵심 흐름 (Orchestrator 1 — Goal Structuring, ADR-0005 §2.5.1):
  VALIDATING → PLANNING → REVIEWING → HITL → SAVING

규칙:
- 입력: Deep Interview(#6) 의 경계 계약 `InterviewOutcome` 하나(인라인 또는 세션 로드).
- LLM(②③④)은 `first_plan.py` 노드 내부 `aiClient.run(...)` 만 (AGENTS §2). 스케줄링은 룰만.
- horizon = focus goals 의 가장 먼 deadline (outcome 파생).
- 출력: goal_nodes + action_items + scheduled_blocks 미리보기 (항상 Draft).
- 모든 변경은 사용자 [승인] 후 적용 (Draft Layer, AGENTS §1.4).

흐름 (#62):
- generate 가 Draft 를 `plan_drafts` 에 저장하고 실제 `planId` 반환.
- `GET /plans/{planId}` 가 Draft 미리보기 재구성(LLM 0회).
- `POST /plans/{planId}/approve` 가 Draft 를 로드해 goal 트리(goals/goal_nodes/action_items/
  scheduled_blocks)로 단일 트랜잭션 영속화(+3회 재시도) → 활성화.

DB: plan_drafts, goals, goal_nodes, action_items, scheduled_blocks, llm_runs.

예정 endpoint:
- POST  /plans/generate                 — 첫 계획 생성 (S06) ✅
- GET   /plans/{plan_id}                — Draft 미리보기 ✅
- POST  /plans/{plan_id}/approve        — 사용자 승인 → 활성화 ✅
- PATCH /plans/{plan_id}/blocks/{id}    — 직접 편집 (S15, 15분 snap)
- POST  /plans/{plan_id}/ai-edit        — 자연어 수정 (S16, P1)
- GET   /plans/weekly?week=...          — 주간 그리드 데이터 (S14)

구현 위치: orchestrator/first_plan.py (LangGraph) + orchestrator/goal_structuring.py (룰 스케줄러)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from http import HTTPStatus
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.action_item import ACTION_CATEGORY_VALUES
from reaction_backend.db.models.behavioral_profile import BehavioralProfile
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.plan_draft import PlanDraft
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.db.session import get_db
from reaction_backend.integrations.google_calendar import freebusy
from reaction_backend.orchestrator import (
    continuation_fill,
    first_plan,
    first_plan_adapter,
    first_plan_milestones,
    goal_cycle,
    goal_policy,
    inbox_resources,
    interview_adapter,
    interview_projection,
    mandala,
    mandala_adapter,
    mandala_cycle,
    profile_memory,
    replan,
    ultimate_adapter,
)
from reaction_backend.orchestrator._common import user_agent_lock
from reaction_backend.orchestrator.goal_structuring import (
    PolicyViolationError,
    fixed_schedules_to_busy,
    time_policies_to_busy,
)
from reaction_backend.orchestrator.plan_edit import (
    DraftBlockEdit,
    DraftEditError,
    EditedDraft,
    apply_draft_edits,
    card_title_from,
    find_policy_violation,
    first_busy_overlap,
    snap_to_15min,
    spanned_days,
)
from reaction_backend.repositories.action_item_repo import ActionItemRepo, get_action_item_repo
from reaction_backend.repositories.fixed_schedule_repo import (
    FixedScheduleRepo,
    get_fixed_schedule_repo,
)
from reaction_backend.repositories.goal_repo import GoalRepo, get_goal_repo
from reaction_backend.repositories.inbox_repo import InboxRepo, get_inbox_repo
from reaction_backend.repositories.interview_repo import InterviewRepo, get_interview_repo
from reaction_backend.repositories.plan_draft_repo import PlanDraftRepo, get_plan_draft_repo
from reaction_backend.repositories.profile_repo import ProfileRepo, get_profile_repo
from reaction_backend.repositories.review_repo import ReviewRepo, get_review_repo
from reaction_backend.repositories.scheduled_block_repo import (
    ScheduledBlockRepo,
    get_scheduled_block_repo,
)
from reaction_backend.repositories.time_policy_repo import TimePolicyRepo, get_time_policy_repo
from reaction_backend.repositories.user_repo import UserRepo, get_user_repo
from reaction_backend.safety import endpoint_rate_limit
from reaction_backend.scheduler.weekly_review_precompute import run_weekly_review_for_user
from reaction_backend.schemas.calendar import CalendarCheck
from reaction_backend.schemas.common import KST, now_kst, to_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.goals import GoalTier
from reaction_backend.schemas.interview import InterviewOutcome, TimeRange
from reaction_backend.schemas.mandala import (
    MandalaApproveRequest,
    MandalaApproveResponse,
    MandalaCarryOverSummary,
    MandalaCell,
    MandalaCenterPreview,
    MandalaCycleAxis,
    MandalaDraftResponse,
    MandalaGap,
    MandalaGenerateRequest,
    MandalaNextCycleRequest,
    MandalaNextCycleResponse,
    MandalaRegenerateBranchRequest,
    MandalaSubgoal,
    MandalaSubgoalsRequest,
    MandalaSubgoalsResponse,
)
from reaction_backend.schemas.planning import (
    ActionItemDraft,
    BlockCompletionStatus,
    BlockEditRequest,
    BlockEditResponse,
    FirstPlanApproveRequest,
    FirstPlanApproveResponse,
    FirstPlanGenerateRequest,
    FirstPlanResponse,
    GoalNodeDraft,
    MilestoneDraft,
    MilestoneListResponse,
    PolicyViolation,
    ReplanBlockPreview,
    ReplanResponse,
    ScheduledBlockPreview,
    WeeklyBlock,
    WeeklyCalendarBusy,
    WeeklyFixedSchedule,
    WeeklyPlanDay,
    WeeklyPlanResponse,
    WeeklyReplanApproveResponse,
)
from reaction_backend.schemas.ultimate_goal import UltimateGoalOutcome

router = APIRouter(prefix="/plans", tags=["planning"])

# ADR-0005 §7.6 — Planning 동시성 lock 의 agent 식별자 (Interview/Recovery 와 공용 메커니즘).
# 한 번의 재계획에서 자리표시자를 채울 목표 수 상한 — LLM 호출 수 상한이기도 하다.
# focus tier 상한(3)이 사실상 막지만, 그 규칙이 바뀌어도 비용이 안 새게 여기서도 자른다.
_MAX_FILL_GOALS = 3
_LOCK_AGENT = "planning"

# 만다라트는 First Plan 과 완전히 독립된 흐름이라 별도 lock 네임스페이스를 쓴다 — 같은 user 가
# 계획을 생성하는 동안 만다라트를 만들어도(또는 그 반대) 서로 막지 않는다.
_MANDALA_LOCK_AGENT = "mandala"

# `routes/goals.py::_ID_PREFIX` 와 동일 — 이 파일에서도 goalId 를 받으므로 같은 규약을 쓴다.
_GOAL_PREFIX = "goal_"

# ADR-0005 §7.8 — Planning Draft 72h 미응답 만료.
_DRAFT_TTL = timedelta(hours=72)

logger = logging.getLogger(__name__)

RepoDep = Annotated[InterviewRepo, Depends(get_interview_repo)]
UserRepoDep = Annotated[UserRepo, Depends(get_user_repo)]
DraftRepoDep = Annotated[PlanDraftRepo, Depends(get_plan_draft_repo)]
ProfileRepoDep = Annotated[ProfileRepo, Depends(get_profile_repo)]
BlockRepoDep = Annotated[ScheduledBlockRepo, Depends(get_scheduled_block_repo)]
ActionRepoDep = Annotated[ActionItemRepo, Depends(get_action_item_repo)]
PolicyRepoDep = Annotated[TimePolicyRepo, Depends(get_time_policy_repo)]
GoalRepoDep = Annotated[GoalRepo, Depends(get_goal_repo)]
InboxRepoDep = Annotated[InboxRepo, Depends(get_inbox_repo)]
FixedRepoDep = Annotated[FixedScheduleRepo, Depends(get_fixed_schedule_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]

# S14/S15 (#21-B) — 주간 그리드/블록 편집. planId 는 주 논리 식별자(Plan 테이블 없음), 편집 권한은 blockId.
_BLOCK_PREFIX = "block_"
_ACTION_PREFIX = "action_"
_WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _config(session: AsyncSession, tone_mode: str | None = None) -> RunnableConfig:
    """노드가 예산 가드·llm_runs 기록에 쓰는 세션 채널 (ADR-0005 §7.1) + 톤(#23-D)."""
    return {"configurable": {"session": session, "tone_mode": tone_mode}}


def _resolve_target_date(raw: str | None) -> str:
    """target_date 정규화 — 미지정 시 오늘(KST). 형식 오류는 422."""
    if raw is None:
        return now_kst().date().isoformat()
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "targetDate 는 YYYY-MM-DD 형식이어야 해요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="targetDate",
        ) from exc


# 투영은 `orchestrator/interview_projection.py` 한 곳에만 둔다 — 자료 확정(#259)도 같은
# outcome 을 봐야 하는데, 라우터마다 조립하면 end_reason·analysis_source 유도가 갈라진다.
_project_session_outcome = interview_projection.project_session_outcome


async def _resolve_outcome(
    body: FirstPlanGenerateRequest, user_id: UUID, repo: InterviewRepo
) -> tuple[InterviewOutcome, datetime | None]:
    """요청에서 First Plan 시드 `InterviewOutcome` 을 확정한다.

    우선순위: ① 인라인 `outcome` → ② `interviewSessionId` 로 종료 세션 투영 →
    ③ **빈 본문이면 최근 '정상 종료' 인터뷰 세션으로 자동 복구** — FE 가 새로고침 등으로
    sessionId(메모리 보관)를 잃어도 계획 생성이 가능하도록 (abandoned 제외).
    셋 다 불가하면 422.

    두 번째 값은 그 인터뷰가 끝난 시각 — 그 뒤에 설정에서 고친 프로필을 얹을지 가른다
    (`_with_settings_edits`). 인라인 outcome 이면 알 수 없어 None.
    """
    if body.outcome is not None:
        return body.outcome, None
    if body.interview_session_id:
        try:
            session_uuid = UUID(body.interview_session_id)
        except ValueError as exc:
            raise _interview_not_found() from exc
        row = await repo.get_active(user_id, session_uuid)
        if row is None:
            raise _interview_not_found()
        return await _project_session_outcome(row, repo), row.ended_at
    latest = await repo.get_latest_finished(user_id)
    if latest is not None:
        return await _project_session_outcome(latest, repo), latest.ended_at
    raise ApiError(
        ErrorCode.COMMON_VALIDATION_ERROR,
        # 화면에 그대로 뜨는 문구다 — 요청 필드 이름 같은 개발자용 말을 넣지 않는다 (planA-16).
        "목표 인터뷰를 먼저 마쳐 주세요. 인터뷰가 끝나면 계획을 만들어 드릴게요.",
        http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
    )


def _interview_not_found() -> ApiError:
    return ApiError(
        ErrorCode.INTERVIEW_SESSION_NOT_FOUND,
        "해당 인터뷰 세션을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _goal_missing() -> ApiError:
    """계획할 실제 목표가 없을 때(placeholder 만 남은 outcome) — LLM 을 부르기 전에 되돌린다."""
    return ApiError(
        ErrorCode.COMMON_VALIDATION_ERROR,
        "계획을 세울 목표가 아직 없어요. 하고 싶은 일을 한 가지만 알려 주시면 거기서부터 세울게요.",
        http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
        field="goals.list",
    )


def _tier_limit_exceeded() -> ApiError:
    return ApiError(
        ErrorCode.GOAL_TIER_LIMIT_EXCEEDED,
        "집중 목표는 최대 3개, 유지 목표는 최대 5개까지예요. 기존 목표를 보관(park)하고 다시 시도해 주세요.",
        http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Draft payload ↔ schema 변환 (#62) — 저장 스냅샷은 snake key, 재구성은 model_validate.
# ─────────────────────────────────────────────────────────────────────────────


def _build_payload(
    *,
    outcome: InterviewOutcome,
    goal_nodes: list[GoalNodeDraft],
    action_items: list[ActionItemDraft],
    blocks: list[ScheduledBlockPreview],
    warnings: list[str],
    policy_violations: list[PolicyViolation],
    generated_at: datetime,
    milestones: list[MilestoneDraft] | None = None,
) -> dict[str, Any]:
    return {
        "outcome": outcome.model_dump(mode="json"),
        "goal_nodes": [n.model_dump(mode="json") for n in goal_nodes],
        "action_items": [a.model_dump(mode="json") for a in action_items],
        "blocks": [b.model_dump(mode="json") for b in blocks],
        "warnings": list(warnings),
        "policy_violations": [v.model_dump(mode="json") for v in policy_violations],
        "generated_at": generated_at.isoformat(),
        # 사용자가 확인·편집해 확정한 마일스톤(#milestones Stage B) — 지금까지는 분해
        # 프롬프트 힌트로만 쓰이고 버려졌다. 여기 실어 승인(approve_plan) 때 읽어
        # node_type='milestone' 로 영속한다(ADR-0007 PR-2).
        "milestones": [m.model_dump(mode="json") for m in (milestones or [])],
    }


def _draft_to_response(draft: PlanDraft) -> FirstPlanResponse:
    """저장된 Draft → 미리보기 응답 재구성 (LLM 0회)."""
    p = draft.payload
    return FirstPlanResponse(
        is_draft=True,
        ai_source=cast(Literal["llm", "rule"], draft.ai_source),
        plan_id=str(draft.id),
        target_date=draft.target_date.isoformat(),
        horizon=draft.horizon,
        goal_nodes=[GoalNodeDraft.model_validate(n) for n in p["goal_nodes"]],
        action_items=[ActionItemDraft.model_validate(a) for a in p["action_items"]],
        blocks=[ScheduledBlockPreview.model_validate(b) for b in p["blocks"]],
        warnings=list(p.get("warnings", [])),
        policy_violations=[
            PolicyViolation.model_validate(v) for v in p.get("policy_violations", [])
        ],
        generated_at=datetime.fromisoformat(p["generated_at"]),
        # .get 기본값 — 이 필드가 생기기 전에 저장된 Draft(72h TTL 이라도 무중단 배포 창에
        # 걸릴 수 있다)를 역직렬화할 때 KeyError 로 죽지 않게.
        milestones=[MilestoneDraft.model_validate(m) for m in p.get("milestones", [])],
    )


def _draft_not_found() -> ApiError:
    return ApiError(
        ErrorCode.PLAN_DRAFT_NOT_FOUND,
        "해당 계획 초안을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


async def _load_draft(repo: PlanDraftRepo, user_id: UUID, plan_id: str) -> PlanDraft:
    try:
        draft_id = UUID(plan_id)
    except ValueError as exc:
        raise _draft_not_found() from exc
    draft = await repo.get_by_id(user_id, draft_id)
    if draft is None:
        raise _draft_not_found()
    return draft


# ─────────────────────────────────────────────────────────────────────────────
# endpoints
# ─────────────────────────────────────────────────────────────────────────────


def _apply_edited_availability(outcome: InterviewOutcome, user: User) -> InterviewOutcome:
    """설정에서 편집한 활동 시간대(users.focus_mode_preferences)가 있으면 outcome 의 활동창을
    그 값으로 덮는다 — 재인터뷰 없이 계획 배치 시간대를 바로잡게(#editable-activity-window)."""
    fmp = user.focus_mode_preferences or {}
    start, end = fmp.get("activity_start"), fmp.get("activity_end")
    if not start or not end:
        return outcome
    availability = outcome.availability.model_copy(
        update={"activity_window": TimeRange(start=start, end=end)}
    )
    return outcome.model_copy(update={"availability": availability})


# 인터뷰 완료(finalize)는 같은 요청에서 프로필을 쓴다 — 그 쓰기와 '나중에 설정에서 고친 것'을
# 가르는 여유. 설정 편집은 계획 화면을 지난 뒤라 분 단위로 늦다.
_PROFILE_EDIT_SLACK = timedelta(minutes=1)


def _apply_edited_profile(
    outcome: InterviewOutcome,
    behavioral: BehavioralProfile | None,
    *,
    interview_ended_at: datetime | None,
) -> InterviewOutcome:
    """'내 정보'에서 고친 **집중 길이·집중 시간대**를 outcome 에 얹는다 (critic-5).

    설정 화면은 "여기서 바꾸면 인터뷰를 다시 하지 않아도 반영돼요" 라고 약속하는데, 계획은
    인터뷰 outcome 만 읽어 `behavioral_profiles` 수정이 어디에도 안 닿았다(활동 시간대만
    `_apply_edited_availability` 로 반영됐다).

    **인터뷰가 끝난 뒤에 프로필이 바뀌었고, 그 값이 인터뷰 답에서 나올 값과 다를 때만** 덮는다.
    인터뷰 완료가 쓴 프로필(같은 값)로 덮으면 목표별 세션 길이 같은 더 구체적인 답을 잃는다.
    - 집중 길이(attention_span): 전역 집중 길이로 쓰고, 목표별 세션 길이는 비운다 — 설정의
      '한 번에 집중하는 길이' 를 바꿨는데 목표별 값이 이기면 바꾼 게 안 보인다.
    - 집중 시간대(energy_cycle): 전역 피크 칩으로. 목표별 선호 시간은 그대로(더 구체적인 답).
    """
    if behavioral is None or interview_ended_at is None:
        return outcome
    updated_at = behavioral.updated_at
    if updated_at is None or updated_at <= interview_ended_at + _PROFILE_EDIT_SLACK:
        return outcome
    seed = profile_memory.seed_slots_from_profile(
        behavioral=behavioral, interaction=None, focus_mode_prefs={}
    )
    update: dict[str, Any] = {}
    span = behavioral.attention_span
    # 설정 검증 범위(5~240분) 밖은 오염된 값이다 — 계획을 그 값으로 누르지 않는다.
    if (
        isinstance(span, int)
        and 5 <= span <= 240
        and span != (outcome.preferences.focus_duration_min or 30)
    ):
        update["preferences"] = outcome.preferences.model_copy(update={"focus_duration_min": span})
        update["core_goals"] = [
            g.model_copy(update={"session_length_min": None}) for g in outcome.core_goals
        ]
    peak = seed.get("time.peak_window", {}).get("values")
    if peak and behavioral.energy_cycle != profile_memory.energy_cycle_from_peak(
        outcome.availability.peak_window
    ):
        update["availability"] = outcome.availability.model_copy(update={"peak_window": list(peak)})
    return outcome.model_copy(update=update) if update else outcome


async def _with_settings_edits(
    outcome: InterviewOutcome,
    interview_ended_at: datetime | None,
    *,
    user: User,
    profile_repo: ProfileRepo,
) -> InterviewOutcome:
    """설정에서 고친 값을 인터뷰 outcome 에 얹는다 — 활동 시간대 + 집중 길이·시간대."""
    outcome = _apply_edited_availability(outcome, user)
    return _apply_edited_profile(
        outcome,
        await profile_repo.get_behavioral(user.id),
        interview_ended_at=interview_ended_at,
    )


async def _max_plan_weeks(session: AsyncSession, user_id: UUID, outcome: InterviewOutcome) -> int:
    """이 계획의 heaviest 목표가 만다라 축에서 승격됐는지에 따라 계획 지평 상한을 정한다
    (ADR-0008 §3). `first_plan_adapter`/`first_plan` 은 DB 무관을 지키므로 이 판정은
    여기(라우터)에서 한다 — 만다라 축에서 왔으면 2주, 아니면 전역 기본(4주).
    """
    # 계획이 실제로 다루는 목표와 **같은 규칙**(is_heaviest 없으면 첫 목표)으로 고른다 —
    # 예전엔 is_heaviest 가 없으면 None 으로 떨어져, 첫 목표가 만다라 승격 목표여도 4주로
    # 잡혔다(planB-18).
    heaviest = first_plan_adapter.heaviest_goal_or_none(outcome)
    if heaviest is None:
        return first_plan_adapter.max_plan_weeks_for(is_mandala_derived=False)
    promoted_titles = await mandala_adapter.fetch_promoted_goal_titles_for_user(session, user_id)
    return first_plan_adapter.max_plan_weeks_for(
        is_mandala_derived=heaviest.title in promoted_titles
    )


@router.post("/milestones")
async def generate_milestones(
    body: FirstPlanGenerateRequest,
    user: CurrentUser,
    repo: RepoDep,
    profile_repo: ProfileRepoDep,
    session: SessionDep,
) -> MilestoneListResponse:
    """Stage A(#milestones) — 목표를 중간 목표 3~5개로. 사용자가 확인·편집 후 generate 로 넘긴다.

    입력은 generate 와 동일(interviewSessionId/outcome + density). LLM 1콜 + 룰 폴백이라 가볍다.

    **이미 확정·영속된 뼈대가 있으면 LLM 을 돌리지 않고 그걸 그대로 돌려준다**
    (`aiSource="saved"`, ADR-0007 PR-2.5). 마일스톤은 매 주기 교체되는 leaf 트리와 달리
    마감까지 살아남는 층이라(§1), 2주기에 새로 지어내면 사용자가 1주기에 확정한 뼈대와
    다른 목록이 나오고 — 그 새 목록으로 계획이 만들어지는데 승인 경로는 이미 마일스톤이
    있다는 이유로 저장을 건너뛰므로 — DB 의 뼈대와 실제 계획이 갈라진 채 굳는다.
    부수 효과로 2주기 이후 이 endpoint 의 LLM 콜이 0 이 된다.
    """
    outcome = await _with_settings_edits(
        *await _resolve_outcome(body, user.id, repo), user=user, profile_repo=profile_repo
    )
    # 계획할 실제 목표가 없으면 LLM 을 부르기 전에 되돌린다 (planB-1) — 일반론 마일스톤을
    # 지어내지 않는다. `/plans/generate` 는 `_run_first_plan` 안에서 같은 검사를 한다.
    if first_plan.plannable_goal_missing(outcome):
        raise _goal_missing()
    goal_id = await first_plan_adapter.heaviest_goal_id(session, user_id=user.id, outcome=outcome)
    if goal_id is not None:
        saved = await first_plan_adapter.fetch_confirmed_milestones(session, goal_id=goal_id)
        if saved:
            return MilestoneListResponse(milestones=saved, ai_source="saved")
    # 교재 카드를 확정한 목표는 그 책의 단원 단계가 곧 뼈대다 — LLM 을 부르지 않는다.
    # 주기 길이를 같이 넘기는 이유: 단계 수를 주기 수의 배수로 잡아야 주기마다 고르게 나뉜다
    # (`curriculum_plan.phase_count_for`).
    curriculum = first_plan_milestones.curriculum_milestones(
        outcome, cycle_weeks=await _max_plan_weeks(session, user.id, outcome)
    )
    if curriculum is not None:
        return MilestoneListResponse(milestones=curriculum, ai_source="rule")
    # `body.density` 를 안 넘긴다 — 마일스톤 크기는 `total_capacity`(인터뷰 답)가 정하고
    # density 파생 변수는 이 프롬프트에 등장하지 않는다(`generate_milestones` docstring).
    milestones, fell_back = await first_plan_milestones.generate_milestones(
        outcome=outcome,
        session=session,
        tone_mode=user.tone_mode,
        user_id=user.id,
    )
    # ⚠️ **LLM 을 불렀으면 커밋한다.** `record_run` 은 `llm_runs` 행을 `session.add` 만 하고
    # 커밋은 호출자 책임인데(`safety/llm_budget.py`), 이 라우터는 "DB 쓰기가 없다" 고 보고
    # 커밋하지 않았다 — 요청이 끝나며 행이 통째로 롤백됐다. 그러면 Stage A 의 LLM 호출이
    # 토큰 예산·엔드포인트 호출 상한·원가 리포트 **어디에도 안 잡힌다**(#370 과 같은 계열의
    # 계측 구멍). 라이브 실측(2026-08-29): 온보딩 4회에 `planning/plan_milestones` 행 0개.
    await session.commit()
    return MilestoneListResponse(milestones=milestones, ai_source="rule" if fell_back else "llm")


@router.post("/generate")
async def generate_plan(
    body: FirstPlanGenerateRequest,
    user: CurrentUser,
    repo: RepoDep,
    goal_repo: GoalRepoDep,
    draft_repo: DraftRepoDep,
    profile_repo: ProfileRepoDep,
    session: SessionDep,
) -> FirstPlanResponse:
    """첫 주간/horizon 계획 생성 — First Plan orchestrator(LangGraph) 실행 → Draft 저장.

    흐름: VALIDATING(tier 게이트) → decompose(LLM) → schedule(룰) → review(LLM) → Draft 저장.
    Focus≤3 / Maintain≤5 초과 시 LLM 분해 전에 422 `GOAL_TIER_LIMIT_EXCEEDED`.
    Draft 를 `plan_drafts`(72h 만료)에 저장하고 실제 `planId` 를 반환. 항상 `is_draft=true`.

    **`goalId` 를 주면 그 목표로 계획을 세운다** (#398, additive). 안 주면 종전대로 최근 완료
    인터뷰의 heaviest 를 재투영하는데, 목표를 여러 개 굴리는 사용자에게는 그게 **다른 목표**일
    수 있었다 — 주간 리포트의 `nextCycleProposals` 는 `goalId` 를 주면서 승인은 목표를 못
    지정하는 `POST /plans/generate` 로 안내해서, 목표 A 의 제안을 열었는데 최근 인터뷰 목표 B
    의 계획이 생성·승인되는 일이 가능했다.

    가용 시간·선호·정체성은 **인터뷰 답 그대로** 두고 `core_goals` 만 갈아끼운다
    (`goal_cycle.seed_outcome`) — 지어내지 않는다. 만다라 승격 목표의 2주 지평도 그대로
    걸린다: `_max_plan_weeks` 가 heaviest **제목**으로 판정하기 때문이다.

    동시성 lock(ADR-0005 §7.6): 다중 디바이스 동시 생성으로 인한 state race 방지.
    """
    await endpoint_rate_limit.enforce(session, user_id=user.id, module="planning")
    outcome = await _with_settings_edits(
        *await _resolve_outcome(body, user.id, repo), user=user, profile_repo=profile_repo
    )
    if body.goal_id is not None:
        goal = await _load_plannable_goal(goal_repo, user.id, body.goal_id)
        outcome = goal_cycle.seed_outcome(base=outcome, goal=goal)
    return await _run_first_plan(
        outcome=outcome,
        milestones=body.milestones,
        target_date=body.target_date,
        scope=body.scope,
        density=body.density,
        user=user,
        draft_repo=draft_repo,
        session=session,
    )


async def _run_first_plan(
    *,
    outcome: InterviewOutcome,
    milestones: list[MilestoneDraft] | None,
    target_date: str | None,
    scope: Literal["week", "horizon"],
    density: Literal["light", "standard", "intense"],
    user: User,
    draft_repo: PlanDraftRepo,
    session: AsyncSession,
) -> FirstPlanResponse:
    """First Plan 그래프 실행 → Draft 저장 (`/plans/generate` 본체).

    `/plans/mandala/next-cycle`(U14) 이 같은 경로를 타야 해서 라우트 밖으로 뺐다 — 시드를
    만드는 방법만 다르고 분해·배치·Draft 저장은 한 곳이어야 두 입구가 어긋나지 않는다.
    호출자가 outcome 을 확정해서 넘긴다(rate limit·가용시간 덮어쓰기도 호출자 몫).
    """
    resolved_target = _resolve_target_date(target_date)
    # 미입력 placeholder 만 있으면 분해할 목표가 없다 — 일반론 계획을 지어내지 않는다.
    if first_plan.plannable_goal_missing(outcome):
        raise _goal_missing()
    max_plan_weeks = await _max_plan_weeks(session, user.id, outcome)

    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        config = _config(session, user.tone_mode)
        # 이번 주기가 몇 번째 마일스톤부터인가 — 영속된 `completed_at` 을 보고 여기서 잰다.
        # 그래프는 DB 무관이라 직접 못 읽는다(`max_plan_weeks` 와 같은 관례, ADR-0007 §1).
        milestone_cursor = 0
        if milestones:
            cursor_goal_id = await first_plan_adapter.heaviest_goal_id(
                session, user_id=user.id, outcome=outcome
            )
            if cursor_goal_id is not None:
                milestone_cursor = await first_plan_adapter.completed_milestone_cursor(
                    session, goal_id=cursor_goal_id
                )
        state = first_plan.initial_state(
            user_id=user.id,
            outcome=outcome,
            target_date=resolved_target,
            scope=scope,
            density=density,
            milestones=milestones,
            max_plan_weeks=max_plan_weeks,
            milestone_cursor=milestone_cursor,
        )
        # Validation Agent — LLM 분해 전에 Focus≤3 / Maintain≤5 게이트 (LLM 0회, 룰만).
        # 노드가 아니라 **순수 판정 함수**를 부른다: `validate_inputs` 는 #226 이후 참고
        # 링크를 여는 I/O 노드라, 게이트로 통째로 부르면 그래프 진입 노드가 같은 링크를
        # 또 열어 한 번의 요청에 외부 사이트를 2회 두드리고 8s 타임아웃을 2회 태운다.
        if first_plan.tier_violation_for(outcome) is not None:
            raise _tier_limit_exceeded()

        graph = first_plan.build_first_plan_graph()
        final = await graph.ainvoke(state, config=config)

        gp = final["goal_plan"]
        ai_source: Literal["llm", "rule"] = first_plan.plan_ai_source(final)
        payload = _build_payload(
            outcome=outcome,
            goal_nodes=gp.goal_nodes if gp is not None else [],
            action_items=gp.action_items if gp is not None else [],
            blocks=final["scheduled_blocks"],
            warnings=final["schedule_warnings"],
            policy_violations=gp.policy_violations if gp is not None else [],
            generated_at=now_kst(),
            milestones=milestones,
        )
        draft = await draft_repo.create(
            user.id,
            target_date=date.fromisoformat(resolved_target),
            horizon=final["horizon"],
            ai_source=ai_source,
            payload=payload,
            expires_at=now_kst() + _DRAFT_TTL,
        )
        await session.commit()

    return _draft_to_response(draft)


# ─────────────────────────────────────────────────────────────────────────────
# S14 Weekly Plan View + S15 직접 편집 (#21-B). `/weekly` 는 `/{plan_id}` 보다 먼저 선언.
# ─────────────────────────────────────────────────────────────────────────────


def _monday_of(day: date) -> date:
    """그 날이 속한 주의 월요일 (월=0)."""
    return day - timedelta(days=day.weekday())


def _week_bounds(monday: date) -> tuple[datetime, datetime]:
    start_dt = datetime.combine(monday, datetime.min.time(), tzinfo=KST)
    return start_dt, start_dt + timedelta(days=7)


def _parse_week_start(raw: str | None) -> date:
    """`weekStart` → 그 주 월요일. 미지정이면 이번 주.

    문장에 요청 필드 이름(`weekStart`)을 싣지 않는다 — FE 가 422 `message` 를 그대로 띄우는데
    사용자에게 'weekStart' 는 아무 뜻도 아니다(`/calendar` 의 같은 문구와 맞췄다). 어느 값이
    문제인지는 `field` 가 그대로 들고 있다.
    """
    if raw is None:
        return _monday_of(now_kst().date())
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as e:
        raise ApiError(
            ErrorCode.PLAN_INVALID_TIME,
            "날짜 형식이 올바르지 않아요 (YYYY-MM-DD).",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="weekStart",
        ) from e
    return _monday_of(parsed)


def _block_not_found() -> ApiError:
    return ApiError(
        ErrorCode.PLAN_BLOCK_NOT_FOUND,
        "해당 일정 블록을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _parse_block_id(raw: str) -> UUID:
    if not raw.startswith(_BLOCK_PREFIX):
        raise _block_not_found()
    try:
        return UUID(raw[len(_BLOCK_PREFIX) :])
    except ValueError as e:
        raise _block_not_found() from e


def _parse_block_dt(raw: str, field: str) -> datetime:
    """ISO 8601 → KST aware. naive 면 KST 로 간주. 형식 오류 422."""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as e:
        raise ApiError(
            ErrorCode.PLAN_INVALID_TIME,
            "시각은 ISO 8601 형식이어야 해요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field=field,
        ) from e
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def _block_view(
    block: ScheduledBlock,
    title: str,
    category: str,
    goal_id: UUID | None,
    *,
    calendar_conflict: bool = False,
    completion_status: str | None = None,
) -> WeeklyBlock:
    return WeeklyBlock(
        block_id=f"{_BLOCK_PREFIX}{block.id}",
        action_id=f"{_ACTION_PREFIX}{block.action_item_id}",
        title=title,
        category=category,
        # 블록 → 목표 연결 (action_item.goal_id 경유) — FE 가 목표 분류/색을 붙일 수 있게.
        goal_id=f"goal_{goal_id}" if goal_id is not None else None,
        start_at=block.start_at,
        end_at=block.end_at,
        block_status=block.block_status,
        source=block.source,
        calendar_conflict=calendar_conflict,
        completion_status=cast(BlockCompletionStatus | None, completion_status),
    )


@router.get("/weekly")
async def get_weekly_plan(
    user: CurrentUser,
    repo: BlockRepoDep,
    fixed_repo: FixedRepoDep,
    session: SessionDep,
    week_start: Annotated[str | None, Query(alias="weekStart")] = None,
) -> WeeklyPlanResponse:
    """주간 블록 그리드 (S14). weekStart 생략 시 이번 주 월요일 기준.

    캘린더를 연결한 사용자는 그 주 구간의 Google 캘린더를 **열 때마다** 확인해, 아직 시작 안 한
    블록이 그 뒤 생긴 약속과 겹치면 `calendarConflict` 를 단다(5분 캐시·2초 상한). 이미 끝난
    블록은 표시하지 않는다 — 지난주 그리드에 배지가 차면 지금 볼 겹침이 묻힌다.

    같은 조회로 읽은 약속 구간은 `days[].calendarBusy` 로도 싣는다(v2.34) — 예전엔 블록과 겹친
    약속만 배지로 드러나고, 빈 시간의 약속은 그리드 어디에도 없었다.
    """
    monday = _parse_week_start(week_start)
    start_dt, end_dt = _week_bounds(monday)
    rows = await repo.list_week(user.id, start_dt, end_dt)
    calendar = await freebusy.screen_conflicts(
        session,
        user_id=user.id,
        start=start_dt,
        end=end_dt,
        blocks=[(block.id, block.block_status, block.start_at, block.end_at) for block, *_ in rows],
        now=now_kst(),
    )
    # 끝난 블록의 체크인 결과 — `finished` 만으로는 완료·실패가 구분되지 않는다(planA-10).
    completion = await repo.completion_by_block(
        user.id, [block.id for block, *_ in rows if block.block_status == "finished"]
    )
    # 조회가 토큰을 갱신·회수했으면 확정한다 — freebusy 는 commit 하지 않는다(호출자 몫).
    await session.commit()

    # 고정 일정(수업·알바)도 그날 칸에 싣는다 (planA-13) — 블록 편집이 막는 시간을 보이게.
    fixed: list[Any] = list(await fixed_repo.list_active(user.id))
    # 캘린더 약속도 그날 칸에 — 자정을 넘는 약속은 날짜별 조각으로(`ok` 가 아니면 빈 dict).
    calendar_by_day = freebusy.split_by_day(calendar.intervals)
    days = [
        WeeklyPlanDay(
            date=monday + timedelta(days=offset),
            weekday=_WEEKDAY_NAMES[offset],
            fixed_schedules=[
                WeeklyFixedSchedule(title=b.label, start_at=b.interval.start, end_at=b.interval.end)
                for b in fixed_schedules_to_busy(monday + timedelta(days=offset), fixed)
            ],
            calendar_busy=[
                WeeklyCalendarBusy(start_at=b.interval.start, end_at=b.interval.end)
                for b in calendar_by_day.get(monday + timedelta(days=offset), [])
            ],
        )
        for offset in range(7)
    ]
    by_date = {d.date: d for d in days}
    for block, title, category, goal_id in rows:
        bucket = by_date.get(to_kst(block.start_at).date())
        if bucket is not None:
            bucket.blocks.append(
                _block_view(
                    block,
                    title,
                    category,
                    goal_id,
                    calendar_conflict=block.id in calendar.keys,
                    completion_status=(
                        completion.get(block.id) if block.block_status == "finished" else None
                    ),
                )
            )

    return WeeklyPlanResponse(
        plan_id=f"plan_{monday.isoformat()}",
        week_start=monday,
        week_end=monday + timedelta(days=6),
        days=days,
        calendar=CalendarCheck(status=calendar.status, checked_at=calendar.checked_at),
    )


# 정책 종류 → 사용자에게 보여 줄 말 (영문 코드를 그대로 내보내지 않는다).
_POLICY_LABELS = {"sleep": "수면", "lunch": "점심", "late_night_block": "심야 휴식"}


async def _ensure_clear_of_fixed_and_no_touch(
    start: datetime,
    end: datetime,
    *,
    policies: list[Any],
    fixed_repo: FixedScheduleRepo,
    user: User,
) -> None:
    """옮길 자리가 고정 일정(수업·알바)·노터치 시간과 겹치면 422 (planA-13).

    예전엔 다른 블록과의 겹침과 수면·점심·심야 정책만 봤다. 주간 그리드엔 고정 일정이 안
    보여서 사용자가 수업 시간 위로 블록을 끌어도 200 이었고, 그 블록은 `user_edit` 이라
    재계획도 다시는 안 고쳤다. 생성·승인 때 쓰는 것과 같은 busy 전개로 본다.
    """
    fixed: list[Any] = list(await fixed_repo.list_active(user.id))
    no_touch = [p for p in policies if p.policy_type == "no_touch"]
    for day in spanned_days(start, end):
        hit = first_busy_overlap(
            start,
            end,
            [*fixed_schedules_to_busy(day, fixed), *time_policies_to_busy(day, no_touch)],
        )
        if hit is None:
            continue
        if hit.source == "fixed_schedule":
            raise ApiError(
                ErrorCode.PLAN_BLOCK_CONFLICT,
                f"그 시간에는 '{hit.label}' 고정 일정이 있어요. 다른 시간으로 옮겨 주세요.",
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                field="startAt",
            )
        raise ApiError(
            ErrorCode.POLICY_VIOLATION,
            "이 시간대는 비워 두기로 한 시간과 겹쳐요. 다른 시간으로 옮겨 주세요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="startAt",
        )


@router.patch("/{plan_id}/blocks/{block_id}")
async def edit_block(
    plan_id: str,  # noqa: ARG001 — 논리 식별자(주). 편집 권한은 blockId.
    block_id: str,
    body: BlockEditRequest,
    user: CurrentUser,
    repo: BlockRepoDep,
    action_repo: ActionRepoDep,
    policy_repo: PolicyRepoDep,
    fixed_repo: FixedRepoDep,
    interview_repo: RepoDep,
    profile_repo: ProfileRepoDep,
    session: SessionDep,
) -> BlockEditResponse:
    """블록 15분 snap 이동 + 목표(category)/제목 수정 (S15).

    충돌 422 `PLAN_BLOCK_CONFLICT`(다른 블록 **또는 고정 일정**) / 정책 422 `POLICY_VIOLATION`
    (수면·점심·심야 차단 **·노터치**). `category`/`title` 을 주면
    블록이 매달린 action_item 을 갱신한다(같은 액션의 모든 세션 블록 공유). 정책 검사는
    **변경된 category** 로 수행하고, 변경 반영은 성공 commit 시에만 영속된다(422 면 롤백).
    시각을 지금 그대로 보낸 편집(제목·목표만)은 snap·겹침·정책 검사 없이 시각을 유지한다.

    정책 집합은 DB `time_policies` + **인터뷰(·설정)의 활동 시간대 밖**으로, 승인·재계획과
    똑같이 `_replan_policies` 로 조립한다 — 같은 시각이 편집기에서는 통과하고 승인에서는
    막히는 일이 없게.
    """
    block = await repo.get_block(user.id, _parse_block_id(block_id))
    if block is None:
        raise _block_not_found()

    raw_start = _parse_block_dt(body.start_at, "startAt")
    raw_end = _parse_block_dt(body.end_at, "endAt") if body.end_at is not None else None
    new_start = snap_to_15min(raw_start)
    if raw_end is not None:
        new_end = snap_to_15min(raw_end)
    else:
        new_end = new_start + (block.end_at - block.start_at)  # 길이 보존

    # 이미 시작했거나 끝낸 블록은 **시간을 못 옮긴다** (planA-9). 옮기면 수행 기록이 미래로
    # 가고, 카드 날짜(target_date)가 따라 움직여 오늘 끝낸 카드가 오늘 화면에서 사라졌다.
    # 제목·목표만 바꾸는 편집(시각은 그대로 보냄)은 허용한다 — 그때는 시각·출처를 건드리지 않는다.
    locked = block.block_status in ("started", "finished")
    same_time = raw_start == block.start_at and (raw_end is None or raw_end == block.end_at)
    if locked:
        if not same_time and (new_start, new_end) != (block.start_at, block.end_at):
            raise ApiError(
                ErrorCode.PLAN_INVALID_TIME,
                "이미 시작했거나 끝낸 일정은 옮길 수 없어요. 제목이나 목표만 바꿀 수 있어요.",
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                field="startAt",
            )
        new_start, new_end = block.start_at, block.end_at
    elif same_time:
        # 시각을 그대로 보낸 제목·목표 편집 — snap 하지도, 겹침·정책을 다시 보지도 않는다(리뷰
        # 반영, planA-13). 스케줄러 블록은 15분 격자가 아니라(10:50 시작 등) snap 하면 제목만
        # 바꿨는데 5분 옮겨졌고, 나중에 추가한 수업 위에 이미 놓인 블록은 이름조차 못 바꿨다.
        new_start, new_end = block.start_at, block.end_at
    elif new_end <= new_start:
        raise ApiError(
            ErrorCode.PLAN_INVALID_TIME,
            "종료 시각이 시작 시각보다 늦어야 해요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="endAt",
        )
    # 시간을 실제로 옮기는 편집만 겹침·정책·고정 일정 검사를 거친다.
    moving = not locked and not same_time

    if moving:
        conflicts = await repo.list_overlapping(
            user.id, new_start, new_end, exclude_block_id=block.id
        )
        if conflicts:
            raise ApiError(
                ErrorCode.PLAN_BLOCK_CONFLICT,
                "그 시간에 이미 다른 일정이 있어요.",
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                field="startAt",
            )

    action = await action_repo.get_by_id(user.id, block.action_item_id)
    # 목표(category)/제목 변경을 action_item 에 반영 — 미지정 필드는 유지. 정책 검사·응답이
    # 새 값을 쓰도록 커밋 전에 적용(422 면 커밋 안 돼 롤백). category 미지원값은 'other'.
    if action is not None:
        if body.category is not None:
            action.category = body.category if body.category in ACTION_CATEGORY_VALUES else "other"
        if body.title is not None and body.title.strip():
            action.title = body.title.strip()
    category = action.category if action is not None else "other"
    if moving:
        # 승인·재계획과 **같은 정책 집합**으로 본다 (calendar-P1). 예전엔 DB `time_policies`
        # 만 봤는데, 그 행을 만드는 FE 화면이 없어 실사용자는 늘 빈 목록이었다 — 편집기는
        # 사실상 아무것도 막지 않았고, 활동 시간대를 08~16 이라 답한 학생이 블록을 새벽 3시로
        # 끌어도 200 이었다. 같은 사용자가 승인·재계획에서는 02:00 이 막힌다(그 둘은 인터뷰
        # 활동 시간대의 여집합을 수면으로 본다). 한 화면 안에서 규칙이 갈리지 않게 재계획과
        # 같은 조립(`_replan_policies`)을 그대로 쓴다 — 여기서 한 벌 더 만들면 또 갈라진다.
        outcome = await _replan_outcome(user, interview_repo, profile_repo)
        policies = _replan_policies(list(await policy_repo.list_active(user.id)), outcome, user)
        violated = find_policy_violation(to_kst(new_start), to_kst(new_end), category, policies)
        if violated is not None:
            raise ApiError(
                ErrorCode.POLICY_VIOLATION,
                f"이 시간대는 {_POLICY_LABELS.get(violated, '쉬는')} 시간과 겹쳐요. "
                "다른 시간으로 옮겨 주세요.",
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                field="startAt",
            )
        await _ensure_clear_of_fixed_and_no_touch(
            to_kst(new_start), to_kst(new_end), policies=policies, fixed_repo=fixed_repo, user=user
        )
    if not locked:
        block.start_at = new_start
        block.end_at = new_end
        block.source = "user_edit"
    # 카드의 target_date 는 자기 블록(가장 이른 활성 블록)의 날짜를 따른다 (#222).
    # 블록을 다른 날로 옮기면 오늘 아젠다도 그 날로 따라가야 한다 — 아젠다는
    # target_date 로 조회하므로, 안 옮기면 카드가 옛 날짜에 유령으로 남는다.
    if action is not None and not locked:
        siblings = await repo.list_by_action_item(user.id, action.id)
        active_starts = [
            to_kst(b.start_at)
            for b in siblings
            if b.block_status != "cancelled" and b.id != block.id
        ]
        active_starts.append(to_kst(new_start))
        action.target_date = min(active_starts).date()
    await session.commit()

    return BlockEditResponse(
        block_id=f"{_BLOCK_PREFIX}{block.id}",
        action_id=f"{_ACTION_PREFIX}{block.action_item_id}",
        title=action.title if action is not None else "",
        category=category,
        # GET /plans/weekly 와 동일하게 목표 연결을 에코 — 이동 후에도 FE 분류/색 유지.
        goal_id=(
            f"goal_{action.goal_id}" if action is not None and action.goal_id is not None else None
        ),
        start_at=block.start_at,
        end_at=block.end_at,
        block_status=block.block_status,
        source=block.source,
    )


@router.get("/{plan_id}")
async def get_plan(plan_id: str, user: CurrentUser, draft_repo: DraftRepoDep) -> FirstPlanResponse:
    """저장된 First Plan Draft 미리보기 — LLM 재호출 없이 스냅샷 재구성.

    재계획(`kind='replan'`)·만다라(`kind='mandala'`) Draft 는 payload 모양이 달라(예:
    `goal_nodes` 가 없거나 다른 뜻) 여기서 다루지 않는다 — 가드가 없으면 `_draft_to_response`
    가 `KeyError` 로 500 을 낸다. **allowlist**(`kind` 없음 또는 `"first_plan"` 만 통과) 로
    막는다 — denylist(`kind == "replan"` 만 걸음)였으면 `kind="mandala"` 가 그냥 통과해
    같은 500 을 냈다(PR4, `1ee508b967ba` 이후 payload 종류가 하나 더 늘어난 계기).
    First Plan Draft 는 `kind` 키가 아예 없다(`_build_payload` 가 안 넣는다) → 기본값
    `"first_plan"` 으로 읽어 배포 시점 기존 draft 를 무중단으로 통과시킨다.
    승인 endpoint 의 반대편 가드(:725)와 대칭.
    """
    draft = await _load_draft(draft_repo, user.id, plan_id)
    if draft.payload.get("kind", "first_plan") != "first_plan":
        raise ApiError(
            ErrorCode.PLAN_DRAFT_NOT_FOUND,
            "계획 초안을 찾을 수 없어요.",
            http_status=HTTPStatus.NOT_FOUND,
        )
    return _draft_to_response(draft)


@router.post("/{plan_id}/discard", status_code=HTTPStatus.NO_CONTENT)
async def discard_plan(
    plan_id: str,
    user: CurrentUser,
    draft_repo: DraftRepoDep,
    session: SessionDep,
) -> None:
    """계획 초안 폐기 — "이 계획 말고 다시 인터뷰할래" 경로.

    지금까지는 초안을 버릴 방법이 없어서 사용자가 새로고침으로 화면을 끊었고, 초안은 만료
    (3일)까지 승인 대기 상태로 남았다. 명시적으로 버리면 그 자리에서 종착 상태가 된다.

    초안은 애초에 비영속(계획 블록은 승인 전 DB 에 안 들어간다)이라 되돌릴 것이 없다 —
    상태 전이만 하면 된다. 이미 승인된 초안은 되돌리는 게 아니므로 409 로 막는다.
    멱등: 이미 폐기·만료된 초안에 다시 호출해도 204.
    """
    draft = await _load_draft(draft_repo, user.id, plan_id)
    if draft.status == "approved":
        raise ApiError(
            ErrorCode.PLAN_ALREADY_APPROVED,
            "이미 승인된 계획이라 버릴 수 없어요.",
            http_status=HTTPStatus.CONFLICT,
        )
    if draft.status == "draft":
        await draft_repo.mark_discarded(draft)
        await session.commit()


async def _attach_goal_resources(
    session: AsyncSession,
    inbox_repo: InboxRepo,
    goal_repo: GoalRepo,
    *,
    user_id: UUID,
) -> None:
    """승인으로 active 가 된 목표들의 추천 자료를 인박스에 넣는다 (#171). best-effort.

    ⚠️ 반드시 **가드 트랜잭션 바깥**(= `db_apply_first_plan` 이 반환해 이미 커밋된 뒤)에서
    부른다. 안에서 부르면 자료 삽입 실패가 PostgreSQL 트랜잭션을 abort 시켜 계획 승인
    전체(goal_nodes·action_items·blocks)를 같이 날린다 — best-effort 의 정반대다.
    `_apply_once` 는 최대 3회 재시도되므로 안에 두면 여러 번 돌기도 한다.

    자료가 없는 카테고리는 아무 일도 하지 않는다. 인터뷰 유래 목표는 대부분
    `category='other'` 로 남으므로(실카테고리는 heaviest 하나만 파생) 실제 삽입은
    많아야 몇 건이고, 같은 자료는 멱등 검사로 한 번만 들어간다.
    """
    try:
        goals = await goal_repo.list_active(user_id)
        if not goals:
            return
        inserted = await inbox_resources.ensure_resources_best_effort(
            session, inbox_repo, user_id=user_id, goal_categories=[g.category for g in goals]
        )
        if inserted:
            await session.commit()
    except Exception:  # noqa: BLE001 — 자료 삽입이 계획 승인 응답을 깨지 않게
        logger.warning("goal resource attach failed; plan approve continues", exc_info=True)
        await session.rollback()


def _parse_draft_edits(body: FirstPlanApproveRequest | None) -> list[DraftBlockEdit] | None:
    """승인 본문의 편집본 → 도메인 값. 본문이 없거나 `blocks` 가 없으면 None(초안 그대로).

    시각은 naive 면 KST 로 보고 KST aware 로만 맞춘다 — **15분 snap 은 하지 않는다**.
    초안 시각 자체가 15분 격자가 아니라(쉬는 시간 10분·수업 끝 직후 시작), 여기서 snap 하면
    손대지 않은 블록이 초안과 달라져 "옮긴 블록"으로 잡혔다(`apply_draft_edits` 참고).
    형식 오류는 결정적이라 lock·재시도 전에 거른다.
    """
    if body is None or body.blocks is None:
        return None
    edits: list[DraftBlockEdit] = []
    for b in body.blocks:
        edits.append(
            DraftBlockEdit(
                origin_id=b.origin_id,
                start=to_kst(_parse_block_dt(b.start, "blocks")),
                end=to_kst(_parse_block_dt(b.end, "blocks")),
                title=b.title,
            )
        )
    return edits


def _apply_draft_edits_or_422(
    blocks: list[ScheduledBlockPreview],
    action_items: list[ActionItemDraft],
    edits: list[DraftBlockEdit],
) -> EditedDraft:
    try:
        return apply_draft_edits(blocks=blocks, action_items=action_items, edits=edits)
    except DraftEditError as exc:
        raise ApiError(
            ErrorCode(exc.code),
            exc.message,
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="blocks",
        ) from exc


async def _ensure_moved_blocks_fit(
    session: AsyncSession,
    *,
    user_id: UUID,
    outcome: InterviewOutcome,
    edited: EditedDraft,
    block_repo: ScheduledBlockRepo,
    fixed_repo: FixedScheduleRepo,
    policy_repo: TimePolicyRepo,
) -> None:
    """사용자가 **옮긴** 블록만 기존 일정·고정 일정·시간 정책·캘린더와 대조한다 (planA-2).

    초안 그대로인 블록은 생성 단계에서 이미 같은 busy 를 피해 놓였다. 옮긴 블록은 그 검사를
    안 거쳤으므로 생성과 같은 소스로 다시 본다 — 활동 시간대(outcome 정책)는 영속화의
    `policy_guarded_transaction` 이 따로 막는다.

    기존 일정은 **이 승인이 교체해 비울 카드**(`superseded_card_ids`, 같은 목표의 손대지
    않은 이전 계획)를 빼고 본다 — 재생성 계획이 그 자리를 쓰는 건 정상이다(#118 과 같은 규칙).
    """
    if not edited.moved:
        return
    goal_id = await first_plan_adapter.heaviest_goal_id(session, user_id=user_id, outcome=outcome)
    replaced = await first_plan_adapter.superseded_card_ids(
        session, user_id=user_id, goal_id=goal_id
    )
    lo = min(b.start for b in edited.moved)
    hi = max(b.end for b in edited.moved)
    existing = [
        b
        for b in await block_repo.list_busy_between(user_id, lo, hi)
        if b.action_item_id not in replaced
    ]
    # ORM 행은 스케줄러 Protocol 을 런타임에 만족하지만 mypy 는 Mapped[...] 를 못 맞춘다 —
    # generate_replan 과 같은 이유로 Any 로 넘긴다.
    fixed: list[Any] = list(await fixed_repo.list_active(user_id))
    db_policies: list[Any] = list(await policy_repo.list_active(user_id))
    # 구글 캘린더 약속 — 생성 단계도 이걸 피해 놓는다(`first_plan._calendar_busy_by_day`).
    # 안 보면 사용자가 끌어 옮긴 블록만 약속 위에 얹혀 저장된다. 조회 실패·미연결이면
    # 그냥 넘어간다(생성과 같은 규칙) — 캘린더 장애가 승인을 막으면 안 된다.
    # freebusy 는 commit 하지 않는다 — 이 lock 은 트랜잭션 단위라 중간 commit 이 풀어 버린다.
    cal_by_day, cal_status = await freebusy.fetch_busy_by_day(
        session, user_id=user_id, start_day=lo.date(), end_day=hi.date()
    )
    calendar_busy = cal_by_day if cal_status == "ok" else {}

    def _conflict(message: str, code: ErrorCode = ErrorCode.PLAN_BLOCK_CONFLICT) -> ApiError:
        return ApiError(code, message, http_status=HTTPStatus.UNPROCESSABLE_ENTITY, field="blocks")

    for moved in edited.moved:
        start, end = to_kst(moved.start), to_kst(moved.end)
        label = card_title_from(moved.title) or moved.title
        if any(b.start_at < end and b.end_at > start for b in existing):
            raise _conflict(
                f"'{label}' 블록을 옮긴 시간에 이미 다른 일정이 있어요. 다른 시간으로 옮겨 주세요."
            )
        if any(
            other is not moved and other.start < end and other.end > start
            for other in edited.blocks
        ):
            raise _conflict(
                f"'{label}' 블록을 옮긴 시간에 이 계획의 다른 블록이 있어요. 한쪽을 옮겨 주세요."
            )
        for day in spanned_days(start, end):
            hit = first_busy_overlap(
                start,
                end,
                [
                    *fixed_schedules_to_busy(day, fixed),
                    *time_policies_to_busy(day, db_policies),
                    *calendar_busy.get(day, []),
                ],
            )
            if hit is None:
                continue
            if hit.source == "fixed_schedule":
                raise _conflict(f"'{label}' 블록을 옮긴 시간이 '{hit.label}' 고정 일정과 겹쳐요.")
            if hit.source == "calendar":
                # 일정 제목은 싣지 않는다 — busy 조회라 서버도 모른다(label 은 '캘린더 일정').
                raise _conflict(
                    f"'{label}' 블록을 옮긴 시간에 캘린더 약속이 있어요. 다른 시간으로 옮겨 주세요."
                )
            raise _conflict(
                f"'{label}' 블록을 옮긴 시간이 {hit.label} 시간과 겹쳐요. 다른 시간으로 옮겨 주세요.",
                ErrorCode.PLAN_POLICY_VIOLATION,
            )


@router.post("/{plan_id}/approve")
async def approve_plan(
    plan_id: str,
    user: CurrentUser,
    user_repo: UserRepoDep,
    draft_repo: DraftRepoDep,
    goal_repo: GoalRepoDep,
    inbox_repo: InboxRepoDep,
    block_repo: BlockRepoDep,
    fixed_repo: FixedRepoDep,
    policy_repo: PolicyRepoDep,
    session: SessionDep,
    body: FirstPlanApproveRequest | None = None,
) -> FirstPlanApproveResponse:
    """First Plan Draft 승인 → SAVING (goal 트리 단일 가드 트랜잭션 영속화, ADR-0005 §2.5.1).

    `plan_id` 로 저장된 Draft 를 로드해 goals/goal_nodes/action_items/scheduled_blocks 를
    단일 트랜잭션으로 영속화(+최대 3회 재시도). `policy_guarded_transaction`(PR #30 재사용)이
    절대 시간 정책 위반 시 롤백 → 422 `PLAN_POLICY_VIOLATION`, 그 외 실패는 롤백 후 500
    `PLAN_SAVE_FAILED`. 만료된 Draft 는 410 `PLAN_DRAFT_EXPIRED`. 이미 승인된 Draft 는 멱등.

    승인 = 교체: **같은 goal 의** 이전 AI 계획 산출물 중 사용자가 손대지 않은
    카드(source=goal·status=planned, user_edit 블록 없는 것)와 그 블록을 soft 정리
    (archived/cancelled)하고, heaviest goal 의 기존 분해 트리도 보관한 뒤 새 계획을
    영속화한다 — 재생성→재승인 반복으로 카드/블록/노드가 겹겹이 누적되던 문제 방지
    (`first_plan_adapter.supersede_previous_plan`). ⚠️ #223 이후 카드 날짜는 goal 승인
    시점이 아니라 자기 블록 날짜를 따르므로(4주에 흩어짐), 교체 단위는 **날짜가 아니라
    goal 전체**다 — 날짜로 좁히면 뒷날짜 카드가 교체를 피해 누적된다.

    동시성(더블클릭·다중 디바이스 동시 승인): advisory lock 은 **트랜잭션 스코프**
    (`pg_advisory_xact_lock`) 라 commit/rollback 마다 풀린다. 그래서 시도(attempt)마다
    lock 을 새로 잡고, Draft 로드·만료·멱등 검사 → 영속화 → Draft 승인 마킹·온보딩
    전이(`on_success` — 가드 트랜잭션 내부)까지를 **한 트랜잭션 단일 commit** 으로 묶는다.
    lock 이 풀리는 순간엔 항상 status=approved 가 이미 커밋돼 있어, 대기하던 요청은
    멱등 응답으로 빠진다. 재시도(ADR-0005 §2.5.1, 3회)는 이 라우터 루프가 담당한다
    (adapter 내부 재시도는 rollback 으로 lock 을 잃은 채 돌게 되므로 `max_retries=1`).

    부수 효과: 첫 계획 승인 = 온보딩 완료 → onboarding_state 를 `ACTIVE` 로 전이(멱등,
    어느 온보딩 단계에서든). 원설계(FIRST_PLAN → NOTIFICATIONS)는 실제 FE 흐름에서 상태가
    WELCOME 에 고정돼 새로고침 시 재-온보딩되던 문제가 있어 승인에서 ACTIVE 로 마감
    (api-contract §3).
    응답은 명시 승인이므로 `is_draft=false` (ADR-0005 §7.2).

    **초안 편집 반영**(HITL '수정', planA-2, additive): 본문 `blocks` 가 오면 그것이 최종
    블록 목록이다 — 옮긴 시각·지운 카드·바꾼 제목을 반영해 영속화한다(`apply_draft_edits`).
    예전엔 본문을 아예 안 받아, 초안 화면에서 끌어 옮기고 지운 것이 승인 때 전부 버려지고
    AI 원안이 그대로 저장됐다 — '수정' 버튼이 있는데 수정이 안 되는 HITL 이었다. 옮긴 블록은
    생성과 같은 소스(기존 일정·고정 일정·시간 정책·캘린더)로 다시 대조하고(`_ensure_moved_blocks_fit`),
    활동 시간대는 영속화 가드가 그대로 막는다. 본문이 없으면 종전과 같다.
    """
    # ⚠️ 원시 값으로 먼저 잡아 둔다 (planA-3). 시도가 실패하면 가드 트랜잭션이 rollback 하는데,
    # rollback 은 세션의 ORM 인스턴스를 **전부 만료**시킨다 — 같은 세션에서 읽은 `user` 도.
    # 다음 시도에서 `user.id` 를 읽으면 async 세션이 지연 로드를 못 해 MissingGreenlet 으로
    # 터졌고, 3회 재시도는 한 번도 돌지 못한 채 문서화된 PLAN_SAVE_FAILED 대신 일반 500 이
    # 나갔다(가짜 세션 테스트는 만료를 흉내내지 않아 통과했다).
    user_id = user.id
    edits = _parse_draft_edits(body)
    last_exc: Exception | None = None
    for attempt in range(first_plan_adapter.MAX_SAVE_RETRIES):
        if attempt > 0:
            # `_finalize` 의 advance_onboarding 이 user 의 상태 컬럼을 읽는다 — 다시 채운다.
            try:
                await session.refresh(user)
            except Exception as exc:  # noqa: BLE001 — 다시 읽지도 못하면 저장 실패로 마감
                last_exc = exc
                break
        async with user_agent_lock(session, user_id, _LOCK_AGENT):
            # 검사→영속화→승인 마킹이 lock 을 쥔 한 트랜잭션 — 이중 영속화 방지.
            draft = await _load_draft(draft_repo, user_id, plan_id)
            if draft.status == "expired" or draft.expires_at < now_kst():
                raise ApiError(
                    ErrorCode.PLAN_DRAFT_EXPIRED,
                    "오래 두신 계획 초안이 만료됐어요. 다시 만들어 볼까요?",
                    http_status=HTTPStatus.GONE,
                )

            payload = draft.payload
            # 재계획(kind=replan)·만다라(kind=mandala) Draft 를 이 First Plan 승인에 넣으면
            # payload["outcome"] 가 없어 KeyError→500 이 난다(#117). allowlist 로 막는다 —
            # denylist(kind=="replan" 만 걸음)였으면 kind="mandala" 가 통과했다(PR4).
            # First Plan Draft 는 kind 키가 없어 기본값 "first_plan" 으로 무중단 통과.
            if payload.get("kind", "first_plan") != "first_plan":
                raise ApiError(
                    ErrorCode.PLAN_DRAFT_NOT_FOUND,
                    "이 초안은 재계획 초안이에요. 재계획 승인으로 진행해 주세요.",
                    http_status=HTTPStatus.NOT_FOUND,
                )
            if draft.status == "approved":  # 멱등 — 이미 영속화됨, 재저장하지 않음
                return _approved_response(plan_id, payload)

            outcome = InterviewOutcome.model_validate(payload["outcome"])
            goal_nodes = [GoalNodeDraft.model_validate(n) for n in payload["goal_nodes"]]
            action_items = [ActionItemDraft.model_validate(a) for a in payload["action_items"]]
            blocks = [ScheduledBlockPreview.model_validate(b) for b in payload["blocks"]]
            milestones = [MilestoneDraft.model_validate(m) for m in payload.get("milestones", [])]
            policies = first_plan_adapter.time_policies_from_outcome(outcome)
            if edits is not None:
                edited = _apply_draft_edits_or_422(blocks, action_items, edits)
                await _ensure_moved_blocks_fit(
                    session,
                    user_id=user_id,
                    outcome=outcome,
                    edited=edited,
                    block_repo=block_repo,
                    fixed_repo=fixed_repo,
                    policy_repo=policy_repo,
                )
                blocks, action_items = edited.blocks, edited.action_items
                # 편집본으로 승인하면 초안 스냅샷도 **저장한 그대로**로 바꿔 둔다 — 재조회
                # (`GET /plans/{id}`)와 멱등 재승인 응답이 실제로 저장된 것과 같아야 한다.
                # AI 원안은 `ai_blocks`/`ai_action_items` 로 남긴다(무엇을 고쳤는지 추적).
                approved_payload: dict[str, Any] | None = {
                    **payload,
                    "blocks": [b.model_dump(mode="json") for b in blocks],
                    "action_items": [a.model_dump(mode="json") for a in action_items],
                    "ai_blocks": payload["blocks"],
                    "ai_action_items": payload["action_items"],
                    "user_edited": True,
                }
            else:
                approved_payload = None

            async def _finalize(
                draft: PlanDraft = draft, approved_payload: dict[str, Any] | None = approved_payload
            ) -> None:
                """영속화와 같은 가드 트랜잭션(단일 commit) 안에서 실행되는 부수 기록.

                첫 계획 승인 = 온보딩 완료 신호 → onboarding_state 를 ACTIVE 로 마감(멱등).
                원설계는 FIRST_PLAN→NOTIFICATIONS(그 뒤 알림 설정에서 ACTIVE)였으나, 실제
                FE 흐름은 (a) 알림 설정이 계획 승인보다 먼저 끝나고 (b) 인터뷰~캘린더 단계
                전이가 항상 트리거되지 않아 onboarding_state 가 WELCOME 에 고정 → 새로고침
                시 재-온보딩·계획 중복 누적 문제가 있었다. 승인 시점에 어느 온보딩 단계에
                있든 ACTIVE 로 올려 이를 없앤다. 이미 ACTIVE 면 no-op.
                """
                if approved_payload is not None:
                    draft.payload = approved_payload
                await draft_repo.mark_approved(draft, approved_at=now_kst())
                await user_repo.advance_onboarding(
                    user,
                    expected_from=(
                        "WELCOME",
                        "ONBOARDING_INTERVIEW",
                        "ONBOARDING_CONFIRM",
                        "ONBOARDING_CALENDAR",
                        "ONBOARDING_MANUAL_SCHEDULE",
                        "ONBOARDING_POLICIES",
                        "ONBOARDING_FIRST_PLAN",
                        "ONBOARDING_NOTIFICATIONS",
                    ),
                    to="ACTIVE",
                )

            try:
                result = await first_plan_adapter.db_apply_first_plan(
                    session,
                    user_id=user_id,
                    target_date=draft.target_date,
                    outcome=outcome,
                    goal_nodes=goal_nodes,
                    action_items=action_items,
                    blocks=blocks,
                    time_policies=policies,
                    milestones=milestones,
                    max_retries=1,  # 재시도는 이 라우터 루프가 lock 재획득과 함께 수행
                    on_success=_finalize,
                )
            except PolicyViolationError as exc:
                raise ApiError(
                    ErrorCode.PLAN_POLICY_VIOLATION,
                    "계획에 수면·노터치 같은 보호 시간과 겹치는 블록이 있어요. 시간을 옮겨 다시 시도해 주세요.",
                    http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                ) from exc
            except (
                Exception
            ) as exc:  # 이 시도 실패 — 이미 롤백됨(가드 트랜잭션), lock 재획득 후 재시도
                last_exc = exc
                continue

            await _attach_goal_resources(session, inbox_repo, goal_repo, user_id=user_id)

            tier_warning = first_plan_adapter.tier_park_notice(result.tier_parked_goals)
            return FirstPlanApproveResponse(
                plan_id=plan_id,
                activated_goals=result.goals,
                activated_goal_nodes=result.goal_nodes,
                activated_action_items=result.action_items,
                activated_blocks=result.scheduled_blocks,
                activated_at=now_kst(),
                warnings=[tier_warning] if tier_warning else [],
            )

    # MAX_SAVE_RETRIES 회 모두 실패 (ADR-0005 §2.5.1)
    raise ApiError(
        ErrorCode.PLAN_SAVE_FAILED,
        "계획 저장에 잠시 문제가 있어요. 잠시 후 다시 시도해 주세요.",
        http_status=HTTPStatus.INTERNAL_SERVER_ERROR,
    ) from last_exc


def _approved_response(plan_id: str, payload: dict[str, Any]) -> FirstPlanApproveResponse:
    """이미 승인된 Draft 재승인 — 저장 스냅샷 길이로 멱등 응답(재영속화 없음)."""
    core_goals = payload.get("outcome", {}).get("core_goals", [])
    return FirstPlanApproveResponse(
        plan_id=plan_id,
        activated_goals=len(core_goals),
        activated_goal_nodes=len(payload.get("goal_nodes", [])),
        activated_action_items=len(payload.get("action_items", [])),
        activated_blocks=len(payload.get("blocks", [])),
        activated_at=now_kst(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 만다라트(Mandala) — 궁극목표 8축×8칸 생성/승인 (U2~U6, §5.5).
# Stage A(subgoals, lock 없음·DB 쓰기 0) → Stage B(generate, lock 있음·plan_drafts 1행) →
# [regenerate-branch]* → approve(goal_nodes 73행 영속, tree_kind='mandala'). U1(POST
# /goals/ultimate) 은 routes/goals.py, U7(discard) 은 기존 `/plans/{plan_id}/discard` 재사용.
# ─────────────────────────────────────────────────────────────────────────────


def _goal_not_found() -> ApiError:
    return ApiError(
        ErrorCode.GOAL_NOT_FOUND,
        "해당 목표를 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


_NODE_PREFIX = "node_"


def _parse_node_id(node_id: str) -> UUID:
    """`routes/goals.py::_parse_node_id` 와 동일 규약 — nodeId 를 FE 표기(`node_<uuid>`) 그대로."""
    raw = node_id[len(_NODE_PREFIX) :] if node_id.startswith(_NODE_PREFIX) else node_id
    try:
        return UUID(raw)
    except ValueError as e:
        raise ApiError(
            ErrorCode.GOAL_NOT_FOUND,
            "해당 칸을 찾을 수 없어요.",
            http_status=HTTPStatus.NOT_FOUND,
        ) from e


def _parse_goal_id(goal_id: str) -> UUID:
    """`routes/goals.py::_parse_goal_id` 와 동일 규약 — 이 파일도 goalId 를 FE 표기 그대로 받는다."""
    if not goal_id.startswith(_GOAL_PREFIX):
        raise _goal_not_found()
    try:
        return UUID(goal_id[len(_GOAL_PREFIX) :])
    except ValueError as e:
        raise _goal_not_found() from e


async def _load_plannable_goal(repo: GoalRepo, user_id: UUID, goal_id: str) -> Goal:
    """`POST /plans/generate` 의 `goalId` — 계획을 세워도 되는 목표만 통과시킨다 (#398).

    - 없는 목표 · **남의 목표** · 보관된 목표 → 404 `GOAL_NOT_FOUND`.
      `GoalRepo.get_by_id` 가 `user_id` 와 `archived_at IS NULL` 을 함께 걸어 세 경우가 한
      갈래로 모인다 — 남의 목표에 "있지만 권한 없음" 을 돌려주면 존재 여부가 새어 나간다.
    - **완료한 목표** → 422. 404 로 묶지 않는 이유: 목표는 실제로 있고 화면에도 보이므로
      "없다" 는 거짓말이 된다. 되돌리면 다시 계획할 수 있다는 걸 문구로 알린다.

    `proposed`(계획 미승인 잠정 목표)는 **막지 않는다** — 인터뷰가 목표를 proposed 로 먼저
    저장하므로(#96) 첫 계획을 세우는 정상 경로가 그 상태다.
    """
    goal = await repo.get_by_id(user_id, _parse_goal_id(goal_id))
    if goal is None:
        raise _goal_not_found()
    if goal.status == "completed":
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "완료한 목표예요. 다시 하려면 목표 화면에서 완료를 되돌린 뒤 계획을 세워 주세요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="goalId",
        )
    return goal


async def _load_ultimate_goal(repo: GoalRepo, user_id: UUID, goal_id: str) -> Goal:
    """user 소유 + `is_ultimate=True` 인 목표만 통과시킨다 — 일반 목표를 만다라 대상으로 못 씀."""
    goal = await repo.get_by_id(user_id, _parse_goal_id(goal_id))
    if goal is None or not goal.is_ultimate:
        raise _goal_not_found()
    return goal


async def _resolve_ultimate_outcome(repo: InterviewRepo, user_id: UUID) -> UltimateGoalOutcome:
    outcome = await ultimate_adapter.resolve_outcome(repo, user_id)
    if outcome is None:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "완료된 궁극목표 인터뷰가 없어요. 먼저 궁극목표 인터뷰를 진행해 주세요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    return outcome


def _require_mandala_kind(draft: PlanDraft) -> None:
    """다른 kind(first_plan/replan) draft 를 만다라 endpoint 에 잘못 넣으면 404 — allowlist(PR4)
    와 반대 방향의 같은 원칙: 이 endpoint 들이 다루는 건 `kind == 'mandala'` 뿐이다."""
    if draft.payload.get("kind") != "mandala":
        raise _draft_not_found()


def _build_mandala_payload(
    *,
    goal_id: UUID,
    center: MandalaCenterPreview,
    subgoals: list[MandalaSubgoal],
    cells: list[MandalaCell],
    gaps: list[MandalaGap],
    generated_at: datetime,
) -> dict[str, Any]:
    """§3.7 스키마 — `plan_drafts.payload`(kind='mandala') 저장 스냅샷."""
    return {
        "kind": "mandala",
        "goal_id": str(goal_id),
        "center": center.model_dump(mode="json"),
        "subgoals": [s.model_dump(mode="json") for s in subgoals],
        "cells": [c.model_dump(mode="json") for c in cells],
        "gaps": [g.model_dump(mode="json") for g in gaps],
        "generated_at": generated_at.isoformat(),
    }


def _mandala_draft_to_response(draft: PlanDraft) -> MandalaDraftResponse:
    """저장된 만다라 Draft → 미리보기 응답 재구성 (LLM 0회)."""
    p = draft.payload
    return MandalaDraftResponse(
        is_draft=True,
        ai_source=cast(Literal["llm", "rule"], draft.ai_source),
        plan_id=str(draft.id),
        goal_id=f"{_GOAL_PREFIX}{p['goal_id']}",
        center=MandalaCenterPreview.model_validate(p["center"]),
        subgoals=[MandalaSubgoal.model_validate(s) for s in p["subgoals"]],
        cells=[MandalaCell.model_validate(c) for c in p["cells"]],
        gaps=[MandalaGap.model_validate(g) for g in p.get("gaps", [])],
        generated_at=datetime.fromisoformat(p["generated_at"]),
    )


def _to_carry_over_summary(
    carried: mandala_adapter.MandalaCarryOver,
) -> MandalaCarryOverSummary:
    """`persist_mandala` 의 승계 결과(도메인 dataclass) → 경계 스키마."""
    return MandalaCarryOverSummary(
        completed_cells=carried.completed_cells,
        promoted_axes=carried.promoted_axes,
        linked_habits=carried.linked_habits,
        dropped_promoted_axes=list(carried.dropped_promoted_axes),
        dropped_linked_habits=list(carried.dropped_linked_habits),
    )


def _approved_mandala_response(plan_id: str, payload: dict[str, Any]) -> MandalaApproveResponse:
    """이미 승인된 만다라 Draft 재승인 — 저장 스냅샷으로 멱등 응답(재영속화 없음).

    `root_node_id`/`activated`/`skipped` 는 최초 승인 때 `payload` 에 덧붙여 둔다
    (`_approved_response` 와 같은 패턴 — 재조회 없이 스냅샷만으로 재현).
    """
    return MandalaApproveResponse(
        plan_id=plan_id,
        goal_id=f"{_GOAL_PREFIX}{payload['goal_id']}",
        root_node_id=f"node_{payload['root_node_id']}",
        activated=int(payload.get("activated", 0)),
        skipped=int(payload.get("skipped", 0)),
        carried_over=MandalaCarryOverSummary.model_validate(payload.get("carried_over") or {}),
        activated_at=now_kst(),
    )


@router.post("/mandala/subgoals")
async def generate_mandala_subgoals(
    body: MandalaSubgoalsRequest,
    user: CurrentUser,
    goal_repo: GoalRepoDep,
    repo: RepoDep,
    session: SessionDep,
) -> MandalaSubgoalsResponse:
    """Stage A(U2) — 궁극목표 → 하위목표(축) 8개. LLM 1콜, lock 없음, 도메인 쓰기 0.

    사용자가 이 8개를 로컬에서 확인·편집한 뒤 `POST /plans/mandala/generate`(U3) 로 넘긴다.

    "DB 쓰기 0" 이 아니다 — LLM 을 부르면 `llm_runs` 1행이 딸려 온다. 커밋 이유는
    `generate_milestones` 주석 참고(같은 계측 구멍이 이 라우터에도 있었다).
    """
    await endpoint_rate_limit.enforce(session, user_id=user.id, module="planning")
    goal = await _load_ultimate_goal(goal_repo, user.id, body.goal_id)
    outcome = await _resolve_ultimate_outcome(repo, user.id)
    subgoals, fell_back = await mandala.generate_subgoals(
        outcome=outcome, session=session, user_id=user.id, tone_mode=user.tone_mode
    )
    await session.commit()
    return MandalaSubgoalsResponse(
        is_draft=True,
        ai_source="rule" if fell_back else "llm",
        goal_id=f"{_GOAL_PREFIX}{goal.id}",
        center=MandalaCenterPreview(title=goal.title, why_text=goal.why_now),
        subgoals=subgoals,
    )


@router.post("/mandala/generate")
async def generate_mandala_draft(
    body: MandalaGenerateRequest,
    user: CurrentUser,
    goal_repo: GoalRepoDep,
    repo: RepoDep,
    draft_repo: DraftRepoDep,
    session: SessionDep,
) -> MandalaDraftResponse:
    """Stage B(U3) — 확정된 8축 → 축당 8칸. LLM 1콜, lock 있음, `plan_drafts` 1행(72h)."""
    await endpoint_rate_limit.enforce(session, user_id=user.id, module="planning")
    goal = await _load_ultimate_goal(goal_repo, user.id, body.goal_id)
    outcome = await _resolve_ultimate_outcome(repo, user.id)

    async with user_agent_lock(session, user.id, _MANDALA_LOCK_AGENT):
        cells, gaps, fell_back = await mandala.generate_cells(
            outcome=outcome,
            subgoals=body.subgoals,
            session=session,
            user_id=user.id,
            tone_mode=user.tone_mode,
        )
        ai_source: Literal["llm", "rule"] = "rule" if fell_back else "llm"
        generated_at = now_kst()
        payload = _build_mandala_payload(
            goal_id=goal.id,
            center=MandalaCenterPreview(title=goal.title, why_text=goal.why_now),
            subgoals=list(body.subgoals),
            cells=cells,
            gaps=gaps,
            generated_at=generated_at,
        )
        draft = await draft_repo.create(
            user.id,
            # 만다라엔 target_date 가 의미 없다(§3.7) — plan_drafts.target_date 는 NOT NULL
            # 이라 nullable 전환 대신(마이그레이션 회피) 오늘로 채우고 payload 에서 안 쓴다.
            target_date=generated_at.date(),
            horizon=None,
            ai_source=ai_source,
            payload=payload,
            expires_at=generated_at + _DRAFT_TTL,
        )
        await session.commit()
    return _mandala_draft_to_response(draft)


@router.get("/mandala/{plan_id}")
async def get_mandala_draft(
    plan_id: str, user: CurrentUser, draft_repo: DraftRepoDep
) -> MandalaDraftResponse:
    """저장된 만다라 Draft 미리보기 — LLM 재호출 없이 스냅샷 재구성(U4)."""
    draft = await _load_draft(draft_repo, user.id, plan_id)
    _require_mandala_kind(draft)
    return _mandala_draft_to_response(draft)


@router.post("/mandala/{plan_id}/regenerate-branch")
async def regenerate_mandala_branch(
    plan_id: str,
    body: MandalaRegenerateBranchRequest,
    user: CurrentUser,
    goal_repo: GoalRepoDep,
    repo: RepoDep,
    draft_repo: DraftRepoDep,
    session: SessionDep,
) -> MandalaDraftResponse:
    """링(8칸) 1개만 재생성(U5). LLM 1콜, lock 있음, draft UPDATE. `locked`(source='user') 칸 보존.

    `body.edited_subgoals`/`edited_cells` 는 재생성 대상이 아닌 나머지 칸의 **현재 편집 상태**
    — 비어 있으면 저장된 draft 스냅샷을 그대로 쓴다(HITL, 서버는 검증하지 않고 그대로 반영).
    """
    async with user_agent_lock(session, user.id, _MANDALA_LOCK_AGENT):
        draft = await _load_draft(draft_repo, user.id, plan_id)
        _require_mandala_kind(draft)
        if draft.status == "expired" or draft.expires_at < now_kst():
            raise ApiError(
                ErrorCode.PLAN_DRAFT_EXPIRED,
                "오래 두신 만다라트 초안이 만료됐어요. 다시 만들어 볼까요?",
                http_status=HTTPStatus.GONE,
            )
        stored = _mandala_draft_to_response(draft)
        goal = await _load_ultimate_goal(goal_repo, user.id, stored.goal_id)
        outcome = await _resolve_ultimate_outcome(repo, user.id)

        subgoals = list(body.edited_subgoals) if body.edited_subgoals else stored.subgoals
        target = next((sg for sg in subgoals if sg.order_index == body.subgoal_index), None)
        if target is None:
            raise ApiError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                "존재하지 않는 축이에요.",
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                field="subgoalIndex",
            )
        sibling_titles = [sg.title for sg in subgoals if sg.order_index != body.subgoal_index]

        current_cells = list(body.edited_cells) if body.edited_cells else stored.cells
        locked_cells = [
            c for c in current_cells if c.subgoal_index == body.subgoal_index and c.source == "user"
        ]
        new_cells, new_gaps, fell_back = await mandala.regenerate_branch(
            outcome=outcome,
            subgoal=target,
            sibling_titles=sibling_titles,
            user_hint=body.user_hint,
            locked_cells=locked_cells,
            session=session,
            user_id=user.id,
            tone_mode=user.tone_mode,
        )

        other_cells = [c for c in current_cells if c.subgoal_index != body.subgoal_index]
        other_gaps = [g for g in stored.gaps if g.subgoal_index != body.subgoal_index]
        draft.payload = _build_mandala_payload(
            goal_id=goal.id,
            center=stored.center,
            subgoals=subgoals,
            cells=[*other_cells, *new_cells],
            gaps=[*other_gaps, *new_gaps],
            generated_at=now_kst(),
        )
        draft.ai_source = "rule" if fell_back else draft.ai_source
        await session.commit()
        await session.refresh(draft)
    return _mandala_draft_to_response(draft)


@router.post("/mandala/{plan_id}/approve")
async def approve_mandala_draft(
    plan_id: str,
    body: MandalaApproveRequest,
    user: CurrentUser,
    goal_repo: GoalRepoDep,
    draft_repo: DraftRepoDep,
    session: SessionDep,
) -> MandalaApproveResponse:
    """승인(U6) — LLM 0콜, 단일 트랜잭션. 편집본을 `goal_nodes` 73행(≤)으로 영속.

    검사→영속화→승인 마킹이 lock 을 쥔 한 트랜잭션(`approve_plan` 과 동일 패턴) — 이중
    영속화 방지.
    """
    async with user_agent_lock(session, user.id, _MANDALA_LOCK_AGENT):
        draft = await _load_draft(draft_repo, user.id, plan_id)
        _require_mandala_kind(draft)
        if draft.status == "expired" or draft.expires_at < now_kst():
            raise ApiError(
                ErrorCode.PLAN_DRAFT_EXPIRED,
                "오래 두신 만다라트 초안이 만료됐어요. 다시 만들어 볼까요?",
                http_status=HTTPStatus.GONE,
            )
        payload = draft.payload
        if draft.status == "approved":  # 멱등 — 이미 영속화됨, 재저장하지 않음
            return _approved_mandala_response(plan_id, payload)

        goal = await goal_repo.get_by_id(user.id, UUID(payload["goal_id"]))
        if goal is None or not goal.is_ultimate:
            raise _goal_not_found()

        center_why_text = body.center_why_text
        if center_why_text is None:
            center_why_text = payload.get("center", {}).get("why_text")
        root, activated, carried = await mandala_adapter.persist_mandala(
            session,
            goal=goal,
            center_why_text=center_why_text,
            subgoals=body.subgoals,
            cells=body.cells,
        )
        activated_at = now_kst()
        skipped = 64 - len(body.cells)
        carried_over = _to_carry_over_summary(carried)
        draft.payload = {
            **payload,
            "root_node_id": str(root.id),
            "activated": activated,
            "skipped": skipped,
            "carried_over": carried_over.model_dump(mode="json"),
        }
        await draft_repo.mark_approved(draft, approved_at=activated_at)
        await session.commit()
    return MandalaApproveResponse(
        plan_id=plan_id,
        goal_id=f"{_GOAL_PREFIX}{goal.id}",
        root_node_id=f"node_{root.id}",
        activated=activated,
        skipped=skipped,
        carried_over=carried_over,
        activated_at=activated_at,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 축 → 다음 2주 계획 (U14, ADR-0008 §3·§8 "G") — 만다라트를 실행으로 잇는 마지막 조각.
# 만다라트를 다시 세우면 축이 통째로 바뀌는데, `/plans/generate` 의 heaviest 는 여전히
# **인터뷰 당시** 고른 목표다. 이 endpoint 가 "이 축으로 다음 2주" 를 한 번에 연다:
# 승격(멱등) → 시드 교체 → 같은 First Plan 경로 → Draft. 승인은 기존 approve 그대로다.
# ─────────────────────────────────────────────────────────────────────────────


async def _promote_axis_for_cycle(
    session: AsyncSession,
    goal_repo: GoalRepo,
    *,
    node: GoalNode,
    user_id: UUID,
    goal_tier: GoalTier,
) -> tuple[Goal, bool]:
    """축 → 승격된 Goal. 이미 승격돼 있으면 그 행을 그대로(멱등, `promote_mandala_node` 규칙).

    반환: (Goal, 이 호출이 새로 승격했는지). tier 한도는 **새로 만들 때만** 잰다 — 이미 있는
    목표를 다시 여는 데 한도를 걸면 Focus 가 꽉 찬 사용자가 자기 목표의 다음 주기를 못 연다.
    """
    # 승격 규칙(멱등 판정을 lock 뒤에서 다시 읽기·한도·목표 만들기)은 `promote` 와 한 벌이다.
    return await goal_policy.promote_axis(
        session, goal_repo, node=node, user_id=user_id, goal_tier=goal_tier
    )


async def _cycle_seed_outcome(
    user: User, repo: InterviewRepo, profile_repo: ProfileRepo
) -> tuple[InterviewOutcome, Literal["interview", "profile"]]:
    """U14 시드 — ① 최근 계획 인터뷰 → ② 온보딩 프로필 → ③ 둘 다 없으면 422.

    ①이 언제나 우선이다. 프로필은 인터뷰 답의 **파생**(`persist_profile_from_outcome`)이라
    원본이 있으면 원본을 쓴다 — 프로필엔 목표별 슬롯(주당 시간·세션 길이)이 없어 정보가 준다.

    ②는 온보딩·설정에서 활동 시간대를 직접 넣었지만 계획 인터뷰는 아직 안 한 사용자를 위한
    길이다. 지어내는 게 아니라 사용자가 넣은 값을 되돌리는 것이고, 못 채운 슬롯은
    `build_outcome` 의 문서화된 기본값 + `unresolved_slots` 로 드러난다.

    ③ 활동 시간대를 어디서도 모르면 422 로 인터뷰를 안내한다 — 배치할 창을 모르는 채로 만든
    계획은 추측이라, Draft 로 보여줄 값어치가 없다.
    """
    latest = await repo.get_latest_finished(user.id)
    if latest is not None:
        outcome = await _project_session_outcome(latest, repo)
        edited = await _with_settings_edits(
            outcome, latest.ended_at, user=user, profile_repo=profile_repo
        )
        return edited, "interview"

    behavioral = await profile_repo.get_behavioral(user.id)
    if not mandala_cycle.has_usable_profile(behavioral):
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "활동 시간대를 아직 몰라요. 계획 인터뷰를 진행하거나 설정에서 활동 시간대를 정해 주세요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    interaction = await profile_repo.get_interaction(user.id)
    slots = mandala_cycle.slots_from_profile(
        behavioral=behavioral,
        interaction=interaction,
        focus_mode_prefs=user.focus_mode_preferences or {},
    )
    outcome = interview_adapter.build_outcome(
        session_id=f"profile_{user.id}",  # 인터뷰 세션이 아니다 — 출처를 id 에 남긴다
        slot_answers=slots,
        ambiguity_final=0.0,
        end_reason="completed",
        analysis_source="rule",  # LLM 정규화를 거친 값이 아니라 저장된 프로필 그대로
    )
    return _apply_edited_availability(outcome, user), "profile"


@router.post("/mandala/next-cycle")
async def open_mandala_next_cycle(
    body: MandalaNextCycleRequest,
    user: CurrentUser,
    goal_repo: GoalRepoDep,
    repo: RepoDep,
    profile_repo: ProfileRepoDep,
    draft_repo: DraftRepoDep,
    session: SessionDep,
) -> MandalaNextCycleResponse:
    """축 하나로 **다음 2주 계획 Draft** 를 연다(U14). LLM 1콜(분해), `plan_drafts` 1행.

    여기까지가 만다라트를 실행으로 잇는 마지막 조각이다. 지금까지는 축을 승격해도
    `POST /plans/generate` 의 heaviest 가 **인터뷰 당시** 고른 목표라, 만다라트를 다시 세워
    축이 바뀌어도 계획은 옛 목표를 분해했다. 이 endpoint 는 시드의 `core_goals` 를 이 축으로
    갈아끼우고(`goal_cycle.seed_outcome`) 축의 칸 8개를 계획 뼈대(마일스톤)로 넘긴다 —
    사용자가 만다라트에서 확정한 분해를 계획이 그대로 따르게 하는 지점이다.

    ⚠️ **자동 적용이 아니다**(§1.4). 돌려주는 건 `POST /plans/generate` 와 **같은 Draft** 이고,
    카드·블록은 사용자가 기존 `POST /plans/{planId}/approve` 를 눌러야 생긴다.

    지평이 2주인 이유는 여기에 규칙을 새로 넣어서가 아니라, 시드의 heaviest 제목이 승격된
    목표와 같아 기존 `_max_plan_weeks`(ADR-0008 §3) 판정이 그대로 걸리기 때문이다.

    **축(depth=1)만 대상**이다 — 중앙·칸은 422(`promote` 의 가드와 같은 자리). 계획 인터뷰를
    한 번도 안 했으면 가용 시간·선호를 지어내지 않고 422 로 안내한다.
    """
    await endpoint_rate_limit.enforce(session, user_id=user.id, module="planning")

    node = await goal_repo.get_mandala_node(user.id, _parse_node_id(body.node_id))
    if node is None:
        raise ApiError(
            ErrorCode.GOAL_NOT_FOUND,
            "해당 칸을 찾을 수 없어요.",
            http_status=HTTPStatus.NOT_FOUND,
        )
    if node.depth != 1:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "하위목표(축)만 이번 주기로 열 수 있어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="nodeId",
        )

    base_outcome, seed_source = await _cycle_seed_outcome(user, repo, profile_repo)

    promoted, newly = await _promote_axis_for_cycle(
        session, goal_repo, node=node, user_id=user.id, goal_tier=body.goal_tier
    )
    outcome = goal_cycle.seed_outcome(base=base_outcome, goal=promoted)

    milestones: list[MilestoneDraft] | None = None
    if body.use_cells_as_milestones:
        cells = [
            n
            for n in await goal_repo.list_nodes(node.goal_id, tree_kind="mandala")
            if n.parent_node_id == node.id and n.depth == 2
        ]
        repeat_cells = await mandala_adapter.fetch_habits_for_nodes(session, [c.id for c in cells])
        milestones = mandala_cycle.cells_as_milestones(cells, exclude_ids=set(repeat_cells)) or None

    # 승격은 계획 생성 전에 커밋한다 — 분해(LLM)가 실패해도 축이 목표로 남아야 사용자가
    # 다시 눌렀을 때 중복 목표가 생기지 않는다(`_promote_axis_for_cycle` 의 멱등 전제).
    await session.commit()

    plan = await _run_first_plan(
        outcome=outcome,
        milestones=milestones,
        target_date=body.target_date,
        scope="horizon",  # 2주 상한은 `_max_plan_weeks` 가 건다 — 여기서 주 단위로 좁히지 않는다
        density=body.density,
        user=user,
        draft_repo=draft_repo,
        session=session,
    )
    if seed_source == "profile":
        plan.warnings = [
            *plan.warnings,
            "계획 인터뷰 대신 저장된 활동 시간대로 배치했어요. 시간이 안 맞으면 설정에서 고쳐 주세요.",
        ]
    return MandalaNextCycleResponse(
        **plan.model_dump(by_alias=False),
        seed_source=seed_source,
        axis=MandalaCycleAxis(
            node_id=f"{_NODE_PREFIX}{node.id}",
            order_index=node.order_index,
            title=node.title,
            goal_id=f"{_GOAL_PREFIX}{promoted.id}",
            goal_tier=cast(GoalTier, promoted.goal_tier),
            newly_promoted=newly,
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 주간 forward 재계획 (S21 후속) — 남은 작업을 이후 구간에 다시 배치.
# 주간 리포트를 먼저 작성하고, 다음 주부터 마감까지 재배치한다. 기존 goal/node/action 재사용,
# 미래 미착수 블록만 교체(중복 0). 승인은 blanket-cancel 없이 **block-id 재조정**(#117).
# ─────────────────────────────────────────────────────────────────────────────

ReviewRepoDep = Annotated[ReviewRepo, Depends(get_review_repo)]

# 재계획 튜닝 폴백 — 완료 인터뷰가 없어 outcome 을 못 얻을 때만 사용.
# 정상 경로는 `_replan_tuning_for` 가 First Plan 과 동일한 개인화(세션 길이·선호 시간)를 유도한다.
_REPLAN_TUNING = replan.ReplanTuning(
    peak_windows=(),
    focus_chunk_min=60,
    break_min=10,
    daily_focus_cap_min=first_plan_adapter.DEFAULT_DAILY_FOCUS_CAP_MIN,
)


async def _replan_outcome(
    user: User, repo: InterviewRepo, profile_repo: ProfileRepo
) -> InterviewOutcome | None:
    """재계획이 First Plan 과 같은 개인화를 쓰도록 최근 '정상 종료' 인터뷰 outcome 을 복구한다.

    설정에서 고친 값(활동 시간대·집중 길이·집중 시간대, `_with_settings_edits`)까지 얹은
    값이다. 완료 인터뷰가 없거나 투영이 실패하면 None — 호출부가 기본값으로 폴백한다(재계획을
    막지 않는다). 튜닝과 활동 시간대(busy)가 **같은 outcome 하나**를 봐야 해서 한 번만 구해
    둘 다에 쓴다.
    """
    latest = await repo.get_latest_finished(user.id)
    if latest is None:
        return None
    try:
        outcome = await _project_session_outcome(latest, repo)
    except Exception:  # noqa: BLE001 — 투영 실패 시 재계획을 막지 말고 기본값으로 진행
        return None
    return await _with_settings_edits(
        outcome, latest.ended_at, user=user, profile_repo=profile_repo
    )


def _replan_tuning_for(outcome: InterviewOutcome | None) -> replan.ReplanTuning:
    """재계획 스케줄러 튜닝을 **First Plan 과 동일하게** outcome 에서 유도한다.

    재계획이 세션 길이(goals.session_length)·선호 시간(goals.preferred_time)을 무시하고
    60분 청크·free-time 아무데나 배치하면, First Plan 에서 넣은 개인화가 매주 리셋된다.
    `schedule_blocks` 와 같은 헬퍼로 튜닝을 조립한다. outcome 이 없으면 기본값으로 폴백한다.

    density 는 재계획 시점에 요청 본문이 없어 알 수 없으므로 daily cap 은 기본(standard)을 쓴다.
    """
    if outcome is None:
        return _REPLAN_TUNING
    return replan.ReplanTuning(
        peak_windows=tuple(first_plan_adapter.peak_windows_for_plan(outcome)),
        focus_chunk_min=first_plan_adapter.focus_chunk_min_from_outcome(outcome),
        break_min=first_plan_adapter.break_min_from_outcome(outcome),
        daily_focus_cap_min=first_plan_adapter.DEFAULT_DAILY_FOCUS_CAP_MIN,
    )


class _RulePolicy:
    """`TimePolicyLike` 최소 구현 — 정책 미설정 유저의 기본 수면창용."""

    def __init__(self, policy_type: str, payload: dict[str, Any]) -> None:
        self.policy_type = policy_type
        self.payload = payload
        self.is_active = True


def _active_or_default_policies(rows: list[Any]) -> list[Any]:
    """활성 time_policies. 하나도 없으면 기본 수면창(23:00~08:00)만 적용해 주간 시간대 배치."""
    if rows:
        return list(rows)
    return [_RulePolicy("sleep", {"start_time": "23:00", "end_time": "08:00"})]


def _edited_window_sleep(user: User) -> list[Any] | None:
    """설정에서 정한 활동 시간대(`focus_mode_preferences`)의 여집합을 수면으로 — 인터뷰가 없을 때.

    인터뷰 outcome 이 있으면 `time_policies_from_outcome` 이 같은 값을 이미 반영한다
    (`_apply_edited_availability`). 인터뷰 없이 설정만 해 둔 사용자가 23~08 기본값에 묶이지
    않게 하는 폴백이다. "24:00" 은 자정(00:00)으로 읽는다(설정 검증의 자정 계약).
    설정이 없으면 None(→ 기본값), 하루 종일(시작=끝)이면 빈 목록 — 사용자가 정한 대로 둔다.
    """
    fmp = user.focus_mode_preferences or {}
    start, end = fmp.get("activity_start"), fmp.get("activity_end")
    if not isinstance(start, str) or not isinstance(end, str) or not start or not end:
        return None
    start = "00:00" if start == "24:00" else start
    end = "00:00" if end == "24:00" else end
    if start == end:
        return []
    return [_RulePolicy("sleep", {"start_time": end, "end_time": start})]


def _replan_policies(db_rows: list[Any], outcome: InterviewOutcome | None, user: User) -> list[Any]:
    """재계획이 피할 시간 정책 — **첫 계획과 같은 조립**(planA-5/calendar-4).

    첫 계획은 `time_policies_from_outcome(outcome)`(활동 시간대의 여집합 = 수면, no_touch)에
    DB 정책을 더한다. 재계획은 DB 정책만 보고, 없으면 23:00~08:00 기본값을 썼다 — FE 에는
    time_policies 를 만드는 경로가 없어 실사용자는 **늘 기본값**이었다. 그래서 저녁에만 된다고
    답한 학생도, 새벽형도 재계획만 누르면 08시부터 블록이 깔렸다. outcome 이 있으면 첫 계획과
    똑같이 조립하고, 없으면 설정의 활동 시간대 → 그마저 없으면 기본값 순으로 폴백한다.
    """
    if outcome is not None:
        return [*first_plan_adapter.time_policies_from_outcome(outcome), *db_rows]
    edited = _edited_window_sleep(user)
    if edited is not None:
        return [*edited, *db_rows]
    return _active_or_default_policies(db_rows)


# 재계획이 카드를 다시 배치하지 않는 목표 상태 — 보관(삭제)·완료 (planA-8).
_RETIRED_GOAL_STATUSES = frozenset({"archived", "completed"})


def _block_minutes(block: ScheduledBlock) -> int:
    """블록 길이(분) — 재계획이 '이미 배정된 몫'을 남은 분량에서 뺄 때 쓴다."""
    return max(0, int((block.end_at - block.start_at).total_seconds() // 60))


def _optional_dt(raw: object) -> datetime | None:
    """초안 payload 의 선택 시각 — 없거나(옛 초안) 비면 None."""
    return datetime.fromisoformat(raw) if isinstance(raw, str) and raw else None


def _replan_response(draft: PlanDraft) -> ReplanResponse:
    """저장된 재계획 Draft → 응답(재조회·생성 공용)."""
    payload = draft.payload
    blocks = [
        ReplanBlockPreview(
            action_id=str(b["actionId"]),
            title=str(b["title"]),
            category=str(b["category"]),
            start=datetime.fromisoformat(str(b["start"])),
            end=datetime.fromisoformat(str(b["end"])),
            replaces_block_id=b.get("replacesBlockId"),
            replaces_start=_optional_dt(b.get("replacesStart")),
            replaces_end=_optional_dt(b.get("replacesEnd")),
        )
        for b in payload.get("blocks", [])
    ]
    return ReplanResponse(
        plan_id=str(draft.id),
        ai_source="rule",
        window_start=str(payload.get("window_start", "")),
        horizon=payload.get("horizon"),
        blocks=blocks,
        warnings=list(payload.get("warnings", [])),
        generated_at=now_kst(),
    )


async def _fill_continuation_cards(
    session: Any,
    *,
    user_id: UUID,
    actions: list[Any],
    candidate_ids: set[UUID],
    horizon: str | None,
    action_repo: ActionItemRepo,
    goal_repo: GoalRepo,
    tone_mode: str | None = None,
) -> list[continuation_fill.FilledCard]:
    """재계획 후보 중 자리표시자를 찾아 내용을 채운다 (#454). 실패하면 빈 목록.

    ⚠️ **후보에 든 카드만** 대상이다. 후보 선정이 이미 걸러 준 것들 — 시작·완료한 카드,
    사용자가 손댄 카드(`protected_card_ids`), 이번 창 밖의 카드 — 을 여기서 다시 판정하지
    않는다. 그 규칙이 두 벌이 되면 어느 한쪽만 고쳐질 때 사용자가 손댄 카드를 덮어쓴다.

    목표별로 한 번씩 부른다 — 자리표시자끼리 **순서**가 있어서 묶어 봐야 이어지는 내용이
    나온다. 목표 수는 focus tier 상한(3)이 사실상 막지만 명시적으로도 잘라 둔다.
    """
    targets = [
        a
        for a in actions
        if a.id in candidate_ids and a.goal_node_id is not None and a.goal_id is not None
    ]
    if not targets:
        return []
    rule_nodes = await goal_repo.rule_filled_node_ids([a.goal_node_id for a in targets])
    if not rule_nodes:
        return []

    by_goal: dict[UUID, list[Any]] = defaultdict(list)
    for a in targets:
        if a.goal_node_id in rule_nodes:
            by_goal[a.goal_id].append(a)

    out: list[continuation_fill.FilledCard] = []
    budget = continuation_fill.MAX_FILL_PER_RUN
    for goal_id in list(by_goal)[:_MAX_FILL_GOALS]:
        rows = sorted(by_goal[goal_id], key=lambda a: (a.target_date, a.title))
        placeholders = continuation_fill.select_fillable(
            [
                continuation_fill.PlaceholderCard(
                    action_id=a.id, title=a.title, node_id=a.goal_node_id
                )
                for a in rows
            ],
            limit=budget,
        )
        if not placeholders:
            break
        goal = await goal_repo.get_by_id(user_id, goal_id)
        if goal is None:
            continue
        placeholder_ids = {p.action_id for p in placeholders}
        out.extend(
            await continuation_fill.fill_cards(
                session,
                user_id=user_id,
                goal_title=goal.title,
                horizon=horizon or (goal.deadline.isoformat() if goal.deadline else ""),
                placeholders=placeholders,
                completed_titles=await action_repo.recent_done_titles(user_id, goal_id),
                # 자리표시자가 아닌 남은 카드 — LLM 이 이미 있는 것과 겹치지 않게.
                remaining_titles=[
                    a.title
                    for a in actions
                    if a.goal_id == goal_id
                    and a.id in candidate_ids
                    and a.id not in placeholder_ids
                    and a.goal_node_id not in rule_nodes
                ],
                # 사용자가 고른 말투 — 계획의 다른 LLM 호출과 같게 (planA-19).
                tone_mode=tone_mode,
            )
        )
        budget -= len(placeholders)
        if budget <= 0:
            break
    return out


@router.post("/replan", status_code=201)
async def generate_replan(
    user: CurrentUser,
    block_repo: BlockRepoDep,
    action_repo: ActionRepoDep,
    policy_repo: PolicyRepoDep,
    fixed_repo: FixedRepoDep,
    draft_repo: DraftRepoDep,
    review_repo: ReviewRepoDep,
    goal_repo: GoalRepoDep,
    repo: RepoDep,
    profile_repo: ProfileRepoDep,
    session: SessionDep,
) -> ReplanResponse:
    """주간 리포트를 작성하고, 남은 작업 + 수락한 회복을 **다음 주부터 마감까지** 다시 배치.

    - 대상: 다음 주 이후 미착수 블록의 액션 + 활성 블록 없는 planned 백로그(수락한 회복 포함).
      과거·시작/완료·user_edit 블록은 불변. 실패 원본은 미래 블록이 없어 자동 제외.
    - busy = 교체하지 않고 **남는 모든 블록**(확정 + 보존 카드의 예정 회차, planA-7)
      + **활동 시간대 밖(인터뷰·설정, 첫 계획과 같은 조립 — `_replan_policies`)**
      + DB 시간정책 + **고정일정(#112 정합)**
      + **Google 캘린더 일정**(첫 계획과 같은 다섯 번째 소스, ADR-0009 D4).
    - 각 새 블록에 '교체할 옛 블록 id'(replacesBlockId)를 실어, 승인이 blanket-cancel 없이
      그 블록만 현재 상태로 재조정 취소하게 한다(#117). 산출물은 Draft — 자동 적용 금지.
    """
    # 재계획은 이제 LLM 을 부른다 (#454 자리표시자 채우기) — 계획 쪽 다른 생성 엔드포인트와
    # 같은 사용자당 일일 상한을 건다. 붙이지 않으면 재계획만 상한 밖에서 비싸진다.
    # ⚠️ **lock 획득 전에** 건다 — 다른 세 곳과 같은 순서다(막힌 요청이 lock 을 잡지 않게).
    await endpoint_rate_limit.enforce(session, user_id=user.id, module="planning")
    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        today = now_kst().date()
        this_monday = today - timedelta(days=today.weekday())
        # 직전 완료 주의 주간 리포트 작성(그 데이터가 회복 수락→백로그로 상류 반영됨).
        await run_weekly_review_for_user(
            user.id, this_monday - timedelta(days=7), now_kst(), repo=review_repo, force=True
        )

        window_start = replan.next_week_start(today)
        scan_start, scan_end = replan.day_bounds_kst(
            window_start, window_start + timedelta(days=365)
        )
        scheduled_pairs = await block_repo.list_scheduled_between(user.id, scan_start, scan_end)
        backlog = await action_repo.list_planned_without_block(user.id)
        # **밀린 일** — 시작 시각이 이미 지났는데 한 번도 착수 안 된 블록. 위 세 조회 중
        # 어느 것에도 안 잡히고 만료 cron 도 못 쓸어내던 구멍이다(`list_stale_scheduled_before`
        # docstring 의 표 참고). "계획만 세워두고 그냥 안 한" 카드가 재계획 후보에서 통째로
        # 빠지면, 가장 도움이 필요한 순간에 재계획이 빈손으로 돈다.
        stale_pairs = await block_repo.list_stale_scheduled_before(user.id, now_kst())

        # 지운(보관)·완료한 목표의 카드는 다시 배치하지 않는다 (planA-8). 위 조회들은 카드의
        # 보관 여부만 보고 목표 상태는 안 봐서, 목표를 지워도 남아 있던 카드(예: 삭제 정리 전
        # 데이터)를 '남은 일' 로 다음 주부터 새로 깔았다. 목표 없는 카드(인박스 등)는 그대로다.
        # 보류(parked)는 빼지 않는다 — 한도 초과로 자동 보류된 목표도 카드는 살아 있는 게
        # 현재 계약이다(`first_plan_adapter._park_tier_overflow_on_approval`).
        goals_by_id = {
            g.id: g
            for g in await goal_repo.list_active(user.id)
            if g.status not in _RETIRED_GOAL_STATUSES
        }

        def _goal_is_live(action: Any) -> bool:
            return action.goal_id is None or action.goal_id in goals_by_id

        scheduled_pairs = [(b, a) for b, a in scheduled_pairs if _goal_is_live(a)]
        stale_pairs = [(b, a) for b, a in stale_pairs if _goal_is_live(a)]
        # 블록 없이 **이번 주(오늘~다음 주 월요일 전)** 로 날짜를 잡아 둔 카드는 밀린 일이 아니라
        # 사용자가 이번 주에 하려고 둔 일이다 (planA-17) — 인박스 메모를 '할 일로' 바꾼 오늘
        # 카드가 대표적이다. 백로그로 집으면 승인 때 다음 주로 옮겨져 오늘 화면에서 사라졌다.
        # 날짜가 지났거나 없는 카드만 진짜 백로그다.
        backlog = [
            a
            for a in backlog
            if _goal_is_live(a)
            and (a.target_date is None or not today <= a.target_date < window_start)
        ]

        # 후보(action_id dedup) + 각 후보가 교체할 옛 블록 **전부**.
        # #115 스케줄러가 긴 액션을 여러 세션 블록으로 쪼개므로 한 액션에 옛 블록이 여러 개일
        # 수 있다. 1개만 잡으면 승인 때 나머지가 유령으로 남거나 새 세션이 드롭된다(리뷰 지적).
        cand: dict[UUID, replan.ReplanCandidate] = {}
        old_blocks_by_action: dict[UUID, list[UUID]] = {}
        actions_by_id: dict[UUID, Any] = {}
        # 밀린 블록을 미래 블록보다 **먼저** 넣는다 — 아래 `covered` 산수가 "교체 대상(old_ids)"
        # 과 "살아남는 미래 블록"을 가르는데, 밀린 블록도 교체 대상에 들어가야 그 몫이 남은
        # 분량에서 이중으로 빠지지 않는다.
        old_block_times: dict[UUID, tuple[datetime, datetime]] = {}
        for block, action in (*stale_pairs, *scheduled_pairs):
            actions_by_id[action.id] = action
            old_blocks_by_action.setdefault(action.id, []).append(block.id)
            old_block_times[block.id] = (block.start_at, block.end_at)

        # 후보 분량은 액션의 **전체 live 블록**을 보고 정한다. scheduled_pairs 는 스캔 창
        # [window_start, +365d] 안의 'scheduled' 블록만 주는데, 세션 분할(#115 _split_minutes)이
        # 한 액션을 여러 날에 흩기 때문에 액션은 일상적으로 주 경계를 걸친다. 창 밖(이번 주)
        # 블록은 '보존'되어 취소되지 않으므로, 전체 estimated_minutes 를 다시 배치하면 그만큼
        # 이중 배치된다(120분 액션에 180분). 창 밖 블록은 여기서 안 보이므로 액션 단위로 다시 조회한다.
        now = now_kst()
        for action_id, action in actions_by_id.items():
            old_ids = set(old_blocks_by_action[action_id])
            live = await block_repo.list_by_action_item(user.id, action_id)  # cancelled 제외
            # 아래 두 가드는 approve 의 가드와 **같은 규칙**이어야 한다. generate 가 후보로
            # 올렸는데 approve 가 skip 하면, 사용자는 '재계획됐다'는 미리보기를 보고 승인해도
            # 아무 일도 안 일어난다.
            if any(b.block_status in ("started", "finished") for b in live):
                old_blocks_by_action.pop(action_id, None)  # 착수한 액션은 통째 보존
                continue
            if first_plan_adapter.protected_card_ids(live):
                old_blocks_by_action.pop(action_id, None)  # 사용자가 옮긴 카드는 통째 보존(#113)
                continue
            # 교체되지 않고 살아남는 **미래** 블록의 시간은 이미 배정된 몫 → 남은 분량에서 뺀다.
            # (과거의 미착수 블록은 '밀린 일' 이라 배정으로 치지 않는다.)
            covered = sum(
                _block_minutes(b) for b in live if b.id not in old_ids and to_kst(b.start_at) >= now
            )
            remaining = (action.estimated_minutes or 30) - covered
            if remaining <= 0:  # 살아남는 블록만으로 이미 충분 → 손대지 않는다
                old_blocks_by_action.pop(action_id, None)
                continue
            cand[action_id] = replan.ReplanCandidate(
                action_id=action_id,
                title=action.title,
                category=action.category,
                estimated_minutes=remaining,
            )
        for action in backlog:
            cand.setdefault(
                action.id,
                replan.ReplanCandidate(
                    action_id=action.id,
                    title=action.title,
                    category=action.category,
                    estimated_minutes=action.estimated_minutes or 30,
                ),
            )
        candidates = list(cand.values())

        # 회피할 기존 블록 = 창 안의 **이번 재계획이 교체하지 않는** 모든 블록 (planA-7).
        # 예전엔 '확정'(시작/완료·user_edit)만 넣었는데, 위 루프가 **남겨 두기로 한** 예정
        # 블록 — 형제 세션을 착수한 카드·사용자가 옮긴 카드의 나머지 회차, 이미 충분히 배정된
        # 카드 — 도 그대로 살아남는다. 그걸 빼면 새 블록이 그 위에 겹쳐 잡혀, 승인 후 캘린더에
        # 같은 시간 블록 두 개가 생겼다. 교체될 옛 블록만 비워 준다(그 자리는 새 배치가 쓴다).
        replaced_ids = {bid for bids in old_blocks_by_action.values() for bid in bids}
        committed_blocks = [
            b
            for b in await block_repo.list_busy_between(user.id, scan_start, scan_end)
            if b.id not in replaced_ids
        ]

        # 규칙이 마감까지 채워 둔 '이어가기' 자리표시자에 **지금의 진행 상황으로** 내용을
        # 넣는다 (#454). 첫 계획 때 내용을 비워 둔 건 그때 사용자가 어디까지 갈지 몰랐기
        # 때문이고, 제품은 그 사실을 고지하며 "다음 재계획 때 채워집니다" 라고 말한다.
        # 여기가 그 약속을 지키는 자리다 — 재계획은 크론이 없어 사용자가 누를 때만 돈다.
        filled = await _fill_continuation_cards(
            session,
            user_id=user.id,
            actions=[*actions_by_id.values(), *backlog],
            candidate_ids={c.action_id for c in candidates},
            horizon=None,
            action_repo=action_repo,
            goal_repo=goal_repo,
            tone_mode=user.tone_mode,
        )
        if filled:
            # 채운 제목이 **드래프트 미리보기에 보여야** 한다 — 사용자가 승인하는 건
            # 이 화면이다. 옛 제목으로 보여주고 다른 걸 저장하면 승인의 의미가 없다.
            by_action = {f.action_id: f for f in filled}
            candidates = [
                (
                    replan.ReplanCandidate(
                        action_id=c.action_id,
                        title=by_action[c.action_id].title,
                        category=c.category,
                        estimated_minutes=c.estimated_minutes,
                    )
                    if c.action_id in by_action
                    else c
                )
                for c in candidates
            ]

        deadline = window_start
        for block, _action in scheduled_pairs:
            deadline = max(deadline, to_kst(block.start_at).date())
        for action in backlog:
            if action.target_date is not None:
                deadline = max(deadline, action.target_date)
        # 미래 블록이 없고 backlog target_date 가 전부 과거/None 이면 deadline 이 window_start
        # 에 머물러 창이 '하루'로 붕괴 → 남은 일이 next Monday 하루에 몰린다(cramming). 마감
        # 신호가 없을 때는 최소 한 주(다음 주 월~일)에 걸쳐 분산하도록 지평을 넓힌다(#117).
        if deadline <= window_start:
            deadline = window_start + timedelta(days=6)
        # 먼 미래 backlog target_date 로 지평이 몇 년까지 벌어져 busy 루프·분산이 폭주하지
        # 않도록 스캔 창(1년)으로 상한. 그보다 먼 카드는 다음 재계획이 다시 당겨온다.
        deadline = min(deadline, window_start + timedelta(days=365))

        outcome = await _replan_outcome(user, repo, profile_repo)
        policies = _replan_policies(list(await policy_repo.list_active(user.id)), outcome, user)
        fixed: list[Any] = list(await fixed_repo.list_active(user.id))
        committed = replan.committed_busy_from_blocks(
            [(b.start_at, b.end_at) for b in committed_blocks]
        )
        day = window_start
        while day <= deadline:
            committed.extend(time_policies_to_busy(day, policies))
            committed.extend(fixed_schedules_to_busy(day, fixed))
            day += timedelta(days=1)

        # 캘린더 일정 — 예전엔 재계획만 이걸 안 읽어서, 캘린더를 연결해도 재배치한 카드가
        # 수업·약속 위에 잡혔다. 첫 계획과 같은 호출이라 지평 전체를 **한 번** 조회한다
        # (상한 `freebusy.MAX_RANGE_DAYS`). 출처가 `calendar` 라 `build_forward_replan` 의
        # 여백·하루 상한 대상(확정 블록)에는 섞이지 않고 회피 대상에만 들어간다.
        # freebusy 는 commit 하지 않는다 — 이 lock 은 트랜잭션 단위라 중간 commit 이 풀어 버린다.
        calendar_busy, calendar_status = await freebusy.fetch_busy_by_day(
            session, user_id=user.id, start_day=window_start, end_day=deadline
        )
        for day_blocks in calendar_busy.values():
            committed.extend(day_blocks)

        # 목표마다 **자기 마감 안에** 배치한다 (planA-6). 지평 하나로 균등 분산하면 금요일 시험
        # 목표의 남은 세션이 한 달짜리 프로젝트 지평에 섞여 절반이 시험 뒤로 밀렸다.
        goal_of = {a.id: a.goal_id for a in (*actions_by_id.values(), *backlog)}
        goal_deadlines: dict[UUID, replan.GoalDeadline] = {}
        for c in candidates:
            gid = goal_of.get(c.action_id)
            goal = goals_by_id.get(gid) if gid is not None else None
            if goal is not None and goal.deadline is not None:
                goal_deadlines[c.action_id] = replan.GoalDeadline(
                    day=goal.deadline, goal_title=goal.title
                )
        blocks, warnings = replan.build_forward_replan_by_deadline(
            window_start=window_start,
            horizon_day=deadline,
            candidates=candidates,
            committed_busy=committed,
            tuning=_replan_tuning_for(outcome),
            deadlines=goal_deadlines,
        )
        # 연결해 둔 사용자에게만 알린다 — 연결 안 한 사용자에게 매번 말하면 알림 피로다.
        calendar_limit = window_start + timedelta(days=freebusy.MAX_RANGE_DAYS)
        if calendar_status == "failed":
            warnings = [first_plan.CALENDAR_FAILED_WARNING, *warnings]
        elif calendar_status == "reconnect_required":
            warnings = [freebusy.CALENDAR_RECONNECT_WARNING, *warnings]
        elif calendar_status == "ok" and deadline > calendar_limit:
            warnings = [
                f"캘린더 일정은 {calendar_limit.month}월 {calendar_limit.day}일까지만 "
                "반영했어요. 그 뒤에 잡힌 카드는 캘린더와 겹치는지 확인해 주세요.",
                *warnings,
            ]

        def _replaced_times(action_id: UUID) -> dict[str, str | None]:
            """대표 옛 블록의 원래 시각(KST) — 미리보기의 '기존 → 새' 비교용 (planA-15).

            회차별 1:1 매핑이 **아니다** — 한 카드가 여러 회차로 나뉘면 새 회차 전부가 이
            같은 값(첫 옛 블록, 밀린 카드면 그 과거 블록)을 싣는다. '이 카드가 원래 있던
            자리' 라는 뜻이고, 계약 문서에도 그렇게 적었다.
            """
            bids = old_blocks_by_action.get(action_id)
            if not bids:
                return {"replacesStart": None, "replacesEnd": None}
            start, end = old_block_times[bids[0]]
            return {
                "replacesStart": to_kst(start).isoformat(),
                "replacesEnd": to_kst(end).isoformat(),
            }

        payload: dict[str, Any] = {
            "kind": "replan",
            "window_start": window_start.isoformat(),
            "horizon": deadline.isoformat(),
            "blocks": [
                {
                    "actionId": f"{_ACTION_PREFIX}{b.action_id}",
                    "title": b.title,
                    "category": b.category,
                    "start": b.start.isoformat(),
                    "end": b.end.isoformat(),
                    "replacesBlockId": (  # 미리보기용(대표 1개) — 재조정 권위는 아래 oldBlocks.
                        f"{_BLOCK_PREFIX}{old_blocks_by_action[b.action_id][0]}"
                        if b.action_id in old_blocks_by_action
                        else None
                    ),
                    **_replaced_times(b.action_id),
                }
                for b in blocks
            ],
            # 재조정 권위 소스: 액션당 교체할 옛 블록 **전부**. 승인이 액션 단위로 옛 블록 집합을
            # 통째 취소하고 새 세션 블록을 전부 생성한다(#117 다중 세션 손실·유령 봉합, 리뷰 대응).
            "oldBlocks": {
                f"{_ACTION_PREFIX}{aid}": [f"{_BLOCK_PREFIX}{bid}" for bid in bids]
                for aid, bids in old_blocks_by_action.items()
            },
            # 채운 자리표시자 — 승인이 이 내용을 카드에 쓴다. 드래프트에 실어야
            # 승인 시점에 다시 LLM 을 부르지 않고, 사용자가 본 그대로가 저장된다.
            "filledCards": [
                {
                    "actionId": f"{_ACTION_PREFIX}{f.action_id}",
                    "title": f.title,
                    "firstStep": f.first_step,
                }
                for f in filled
            ],
            "warnings": warnings,
        }
        # 만료는 **자기 window_start 를 넘지 못한다**. 이 draft 의 블록은 전부 window_start
        # 이후인데, window_start 가 미래인 건 *생성 시점* 에만 보장된다 — 기본 TTL(72h)만
        # 쓰면 금·토·일에 만든 draft(next_week_start 가 일요일엔 '내일')를 그 주가 시작된 뒤
        # 승인할 수 있고, 그러면 살아있는 미래 블록을 취소하고 **과거 블록을 새로 만든다**.
        # window_start 00:00 KST 로 상한을 두면 승인 시점에 모든 블록이 미래임이 보장되고,
        # 늦은 승인은 조용한 손실 대신 문서화된 410 PLAN_DRAFT_EXPIRED 로 떨어진다.
        window_start_dt, _ = replan.day_bounds_kst(window_start, window_start)
        draft = await draft_repo.create(
            user.id,
            target_date=window_start,
            horizon=deadline.isoformat(),
            ai_source="rule",
            payload=payload,
            expires_at=min(now_kst() + _DRAFT_TTL, window_start_dt),
        )
        await session.commit()

    return _replan_response(draft)


@router.post("/replan/{plan_id}/approve")
async def approve_replan(
    plan_id: str,
    user: CurrentUser,
    block_repo: BlockRepoDep,
    action_repo: ActionRepoDep,
    draft_repo: DraftRepoDep,
    goal_repo: GoalRepoDep,
    session: SessionDep,
) -> WeeklyReplanApproveResponse:
    """재계획 Draft 승인 → **action 단위 재조정**으로 미래 블록 교체(#117 재작업).

    #115 스케줄러가 긴 액션을 **여러 세션 블록**으로 쪼개므로, 재조정은 개별 블록이 아니라
    **액션당 '옛 블록 집합'(payload `oldBlocks`)** 을 통째 다룬다 — 옛 블록 1개만 취소하면
    나머지가 유령으로 남거나 새 세션이 드롭되던 문제 방지(리뷰 대응). 액션마다 현재 DB 상태로:
    - 옛 블록 집합 중 하나라도 `started/finished`(사용자 착수) → 이 액션 **전체 보존**(skip).
    - 활성(`scheduled`) 옛 블록이 하나도 없음(그새 전부 취소·삭제) → 중복 방지 skip.
    - 그 외 → 활성 옛 블록 **전부 취소** + 새 세션 블록 **전부 생성**.
    - 백로그(옛 블록 없음): 그새 그 action 이 활성 블록을 얻었으면 생성 skip.
    - action 이 그새 아카이브/삭제됐으면(예: #113 First Plan 교체) 전체 skip(좀비 블록 방지).

    Draft 로드·검사~쓰기를 `user_agent_lock`(xact-scoped) 안에서 단일 commit 으로 원자화한다
    (동시 더블 승인 봉합, #113 패턴). 과거·시작/완료·user_edit 블록은 불변. 항상 Draft→HITL.
    """
    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        draft = await _load_draft(draft_repo, user.id, plan_id)
        payload = draft.payload
        if payload.get("kind") != "replan":
            raise ApiError(
                ErrorCode.PLAN_DRAFT_NOT_FOUND,
                "재계획 초안을 찾을 수 없어요.",
                http_status=HTTPStatus.NOT_FOUND,
            )
        if draft.status == "expired" or draft.expires_at < now_kst():
            raise ApiError(
                ErrorCode.PLAN_DRAFT_EXPIRED,
                "오래 두신 재계획 초안이 만료됐어요. 다시 만들어 볼까요?",
                http_status=HTTPStatus.GONE,
            )
        if draft.status == "approved":  # 멱등 — lock 안 확인이라 동시 승인이 직렬화됨
            return WeeklyReplanApproveResponse(
                plan_id=plan_id,
                cancelled_blocks=0,
                created_blocks=len(payload.get("blocks", [])),
                skipped_blocks=0,
                activated_at=now_kst(),
            )

        # payload 새 블록을 액션 단위로 묶는다 — 한 액션이 여러 세션 블록으로 나뉠 수 있으므로
        # (분할). 재조정은 '액션당 옛 블록 집합'을 통째 다루어 손실·유령을 막는다.
        new_by_action: dict[UUID, list[dict[str, Any]]] = {}
        for b in payload.get("blocks", []):
            aid = UUID(str(b["actionId"]).removeprefix(_ACTION_PREFIX))
            new_by_action.setdefault(aid, []).append(b)
        old_map: dict[str, list[str]] = payload.get("oldBlocks", {})
        # 생성 때 채운 자리표시자 내용 (#454). 없으면 빈 dict — 이 기능 이전에 만들어진
        # 초안도 그대로 승인된다(payload 는 스키마가 아니라 dict 라 키가 없어도 된다).
        filled_by_action: dict[UUID, dict[str, str]] = {}
        for f in payload.get("filledCards", []):
            try:
                filled_by_action[UUID(str(f["actionId"]).removeprefix(_ACTION_PREFIX))] = {
                    "title": str(f["title"]),
                    "firstStep": str(f["firstStep"]),
                }
            except (KeyError, ValueError):  # 손상된 초안 하나가 승인 전체를 막지 않게
                continue
        refilled_nodes: list[UUID] = []

        cancelled = created = skipped = 0
        for action_id, new_blocks in new_by_action.items():
            n = len(new_blocks)
            # generate~approve 사이 action 이 아카이브/삭제됐으면(#113 supersede) 전체 skip.
            action = await action_repo.get_by_id(user.id, action_id)
            if action is None:
                skipped += n
                continue
            old_ids = [
                UUID(str(x).removeprefix(_BLOCK_PREFIX))
                for x in old_map.get(f"{_ACTION_PREFIX}{action_id}", [])
            ]
            if old_ids:  # 교체 경로 — 옛 블록 집합을 재조정
                # 취소 전에 옛 블록을 **모두** 먼저 로드(autoflush 로 앞 취소가 뒤 조회에 새는
                # 것 방지). 하나라도 시작/완료면 액션 전체 보존.
                olds = [await block_repo.get_block(user.id, bid) for bid in old_ids]
                present = [o for o in olds if o is not None]
                if any(o.block_status in ("started", "finished") for o in present):
                    skipped += n
                    continue
                # user_edit 은 **쓰기 시점에 다시** 확인한다(TOCTOU). generate 쪽
                # list_scheduled_between 의 user_edit 필터는 이 승인보다 수 초~수 시간 앞서
                # 돌기 때문에, 그 사이 사용자가 HITL 검토 중 블록을 드래그하면 — edit_block 이
                # source='user_edit' 로 바꾸되 block_status 는 'scheduled' 로 남긴다 — 아래
                # 취소가 **사용자가 손으로 옮긴 계획을 지운다**. user_agent_lock 도 방어가 안
                # 된다: edit_block 은 lock 을 잡지 않아 replan↔edit 은 직렬화되지 않는다.
                # 보존 단위는 #113 이 확립한 카드(action) 단위 계약을 그대로 재사용한다.
                if first_plan_adapter.protected_card_ids(present):
                    skipped += n
                    continue
                active = [o for o in present if o.block_status == "scheduled"]
                # 활성 옛 블록이 하나도 없으면(그새 전부 취소·삭제) 중복 방지로 skip.
                if not active:
                    skipped += n
                    continue
                for o in active:
                    o.block_status = "cancelled"
                    cancelled += 1
            elif await block_repo.list_by_action_item(user.id, action_id):
                # 백로그인데 그새 활성 블록이 생겼으면 중복 방지로 skip.
                skipped += n
                continue
            # 이 액션의 새 세션 블록을 전부 생성.
            for nb in new_blocks:
                await block_repo.create_block(
                    user_id=user.id,
                    action_item_id=action_id,
                    start_at=datetime.fromisoformat(str(nb["start"])),
                    end_at=datetime.fromisoformat(str(nb["end"])),
                    source="ai_plan",
                )
                created += 1

            # 카드 날짜 = 자기 블록(가장 이른 활성 블록)의 KST 날짜 (#222/#223 이 정한 규칙,
            # edit_block 과 동일). 이 경로(주간 forward 재계획 승인)는 옛 블록을 cancel 하고
            # 새 블록을 만들면서 target_date 를 안 건드려 왔다 — #223 이후에도 재계획을
            # 승인할 때마다 "카드 날짜 ≠ 블록 날짜" 가 새로 생겼고, #229 로 이어졌다.
            # list_by_action_item 은 cancelled 를 제외하므로 방금 취소한 옛 블록은 안 잡히고
            # (autoflush 로 취소·생성이 이 SELECT 전에 반영된다 — 위 옛 블록 로드 주석과 동일
            # 전제), 방금 만든 새 블록만(또는 남은 활성 블록까지) 잡힌다.
            # `if siblings:` 는 방어적이다 — new_by_action 은 setdefault(aid, []).append(b)
            # 로 채워지므로 이 루프에 들어온 action_id 는 new_blocks 가 최소 1개고, 바로 위
            # 루프가 그걸 전부 생성했다. 그래서 siblings 가 비는 경로는 지금 코드에서 없다.
            # 다만 이 함수를 고칠 사람이 그 불변식을 깨도(예: 조건부 생성으로 바뀌어도)
            # min() 이 ValueError 로 죽지 않게 남겨 둔다.
            siblings = await block_repo.list_by_action_item(user.id, action_id)
            if siblings:
                action.target_date = min(to_kst(b.start_at) for b in siblings).date()

            # 자리표시자였던 카드에 생성 때 채운 내용을 쓴다 (#454). 생성 시점의 값을
            # 그대로 쓰는 이유는 **사용자가 승인한 게 그 화면**이기 때문이다 — 여기서
            # LLM 을 다시 부르면 사용자가 본 것과 다른 게 저장된다.
            #
            # ⚠️ status 는 안 건드린다 (AGENTS §2). 내용만 바꾼다.
            fill = filled_by_action.get(action_id)
            if fill is not None:
                action.title = fill["title"]
                action.first_step = fill["firstStep"]
                if action.goal_node_id is not None:
                    refilled_nodes.append(action.goal_node_id)

        # 채운 노드의 출처를 `llm` 로 — 컬럼의 뜻이 "누가 **채웠는가**" 이고, 채운 건
        # LLM 이다. 같은 카드가 다음 재계획에서 다시 후보가 되지 않게 하는 것이 실질이다.
        await goal_repo.mark_nodes_filled(refilled_nodes)

        await draft_repo.mark_approved(draft, approved_at=now_kst())
        await session.commit()

        return WeeklyReplanApproveResponse(
            plan_id=plan_id,
            cancelled_blocks=cancelled,
            created_blocks=created,
            skipped_blocks=skipped,
            activated_at=now_kst(),
        )
