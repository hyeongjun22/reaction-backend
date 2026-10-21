"""First Plan Orchestrator (#32) — Sequential + 룰 fallback (ADR-0005 §2.5.1).

상태머신 (architecture.md §2.1):
    VALIDATING → PLANNING → REVIEWING → (HITL) → SAVING → DONE
                    ▲___________ feedback (≤2회) ___________│

흐름:

    validate_inputs → decompose_goal → schedule_blocks ─┬→ review_plan ─┐
                            ▲                             │ after_schedule │ after_review
                            │                             └→ END (검토 생략)│
                            └──────────── replan (≤2회) ────────────────────┤
                                                              approve ─────┴→ END

- 입력은 Deep Interview(#6) 의 경계 계약 `InterviewOutcome` **하나**. InterviewState 를
  절대 import 하지 않는다 → 두 이슈 병렬 개발, 계약만 고정.
- LLM(②③④)은 Node 안 `aiClient.run(...)` 만. 스케줄링은 **룰만**(LLM 0회) —
  `plan_scheduler.schedule_actions_multiday` + `goal_structuring.py` busy 계산(ADR-0005
  §1.2). 분해·검토는 호출마다 `config.llm_planning_timeout_seconds`(45초) × 재시도 —
  타임아웃·예산·금지어/톤 게이트면 룰 fallback. 최악 대기 상한은 `after_schedule` 참고.
- 산출물은 비활성 Draft. 실제 영속화는 사용자 [수락] 후 승인 라우트의 단일 트랜잭션
  (`first_plan_adapter.db_apply_first_plan`) — AGENTS.md §1.4 자동 적용 금지.
"""

from __future__ import annotations

import json
import time as _time
from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from reaction_backend.config import get_settings
from reaction_backend.integrations.google_calendar import freebusy
from reaction_backend.llm import aiClient
from reaction_backend.orchestrator import (
    curriculum_plan,
    escalation,
    first_plan_adapter,
    materials_resolver,
)
from reaction_backend.orchestrator.goal_structuring import (
    BusyBlock,
    TimeInterval,
    fixed_schedules_to_busy,
    pad_busy,
    time_policies_to_busy,
)
from reaction_backend.orchestrator.interview_adapter import is_placeholder_goal
from reaction_backend.orchestrator.plan_scheduler import schedule_actions_multiday
from reaction_backend.repositories.fixed_schedule_repo import FixedScheduleRepo
from reaction_backend.repositories.recovery_repo import RecoveryOutcomeContext, RecoveryRepo
from reaction_backend.repositories.review_repo import ReviewRepo, TopFailureContext
from reaction_backend.repositories.scheduled_block_repo import ScheduledBlockRepo
from reaction_backend.repositories.time_policy_repo import TimePolicyRepo
from reaction_backend.schemas.common import KST, now_kst, to_kst
from reaction_backend.schemas.interview import InterviewOutcome
from reaction_backend.schemas.planning import (
    ActionItemDraft,
    GoalDecomposition,
    GoalNodeDraft,
    MilestoneDraft,
    PlanReview,
    ScheduledBlockPreview,
)

__all__ = [
    "FirstPlanState",
    "build_first_plan_graph",
    "initial_state",
    "should_replan",
]

MAX_REPLAN = 2  # Review feedback cycle 최대 2회, 3회째 그대로 HITL (무한 cycle 방지)

# 검토·재분해를 **새로 시작해도 되는** 그래프 경과 시간(초). 분해 한 번이 정상일 때 25~35초
# (config.llm_planning_timeout_seconds 주석의 실측)라, 첫 분해+검토가 건강하면 이 안에 끝난다.
# 그 뒤에 검토(최악 3×45초)나 재분해(최악 3×45초)를 또 시작하면 사용자는 스피너만 몇 분을 본다
# (planB-4 실측 추정: 최악 4.5~8분). 넘겼으면 지금 계획을 그대로 HITL 로 넘긴다 — 검토 폴백도
# 원래 '그대로 승인' 이라 잃는 건 재분해 한 번의 기회뿐이다.
_REVIEW_DEADLINE_SECONDS = 90.0

# 분해 폴백 사유 중 **곧바로 다시 불러 봐야 결과가 같은** 것들 (`RunResult.reason`).
# 예전엔 자리표시자 계획도 검토에 넘겼고, 검토가 그걸 반려하면 같은 실패를 한 번 더 겪었다 —
# 타임아웃이면 135초를 또 기다리고, 예산·톤 게이트면 토큰만 쓰고 같은 자리표시자가 나온다.
# `validation`·`provider_error` 는 일시적일 수 있어 종전대로 검토(→ 재분해 기회)를 남긴다.
_RETRY_FUTILE_FALLBACKS = frozenset(
    {"timeout", "budget", "rate_limited", "unavailable", "no_prompt", "banned", "tone_gate"}
)

# 스케줄러가 배치 실패 시 내는 문구의 식별 조각 (`plan_scheduler` 와 동기화).
# 같은 원인으로 전부 실패했을 때 이 줄들을 한 줄로 대체하기 위해 쓴다 (#252).
_UNPLACED_MARKER = "배치할 가용 시간을 찾지 못했어요"


class FirstPlanState(TypedDict):
    """First Plan short-lived 상태. 입력은 InterviewOutcome 하나(경계 계약)."""

    user_id: UUID
    outcome: InterviewOutcome
    target_date: str  # "YYYY-MM-DD" (KST 기준)
    # 배치 범위: "horizon"(기본, 마감까지 전 구간) | "week"(target_date 가 속한 달력 주만).
    scope: Literal["week", "horizon"]
    # 계획 분량(밀도) — light/standard/intense. decompose 프롬프트의 '주당 세션 수' 하한으로 전개.
    density: str
    # 사용자가 확정한 중간 목표(#milestones Stage B). 있으면 분해가 이걸 branch 로 고정.
    milestones: list[MilestoneDraft] | None
    # 계획 지평 상한(주) — 기본 4주, 만다라 유래 목표(축 승격)는 2주(ADR-0008 §3).
    # 호출자(routes/planning.py)가 이 goal 이 만다라에서 왔는지 판정해 넘긴다 — 이 그래프는
    # DB 무관을 지키므로 여기서 직접 조회하지 않는다.
    max_plan_weeks: int
    # 이미 끝낸 마일스톤 수 — 이번 주기가 몇 번째부터 분해할지(ADR-0007 §1 커서).
    # `max_plan_weeks` 와 같은 관례로 호출자가 영속된 `completed_at` 을 보고 넘긴다.
    milestone_cursor: int
    # 이번 주기 범위 밖이라 걷어낸 branch 제목 — 조용히 빼지 않고 warnings 로 알린다
    # (`drop_waiting_steps` 와 같은 원칙, #225).
    out_of_cycle_dropped: list[str]

    # VALIDATING
    # 이 목표에서 연속 실패가 쌓여 분량 프리셋을 낮췄으면 **원래 고른 값**. 안 낮췄으면 None.
    # `density` 자체는 낮춘 값으로 덮어써서 분해·룰 폴백·하루 상한이 한 값을 보게 한다.
    density_damped_from: str | None
    # 그래프가 시작된 시각(`time.monotonic()`) — 검토·재분해를 새로 시작할지 가르는 기준.
    started_at: float
    # 링크로만 준 참고 자료를 열어봤는가 (#226). 열었으면 되묻지 않고, 못 열었으면
    # 그 사유가 담긴 문구를 warnings 로 내보낸다.
    materials_fetched: bool
    materials_notice: str | None

    # PLANNING
    planning_context: dict[str, Any]
    goal_plan: GoalDecomposition | None
    # 마감까지 커버하려고 덧붙인 '이어가기' 회차 수 (0이면 LLM 분해만으로 충분했다는 뜻).
    coverage_extended: int
    # 교재 카드로 짠 계획인가(`curriculum_plan`) — 분해 LLM 을 건너뛰었으므로 검토 LLM 도 돌리지
    # 않는다(같은 입력이면 같은 결과라 반려→재분해가 아무것도 바꾸지 못한다).
    curriculum_plan: bool
    # 교재 단원이 이번 주기 세션 길이에 다 안 들어갈 때의 고지 — 단원은 버리지 않는다.
    curriculum_notice: str | None
    # 세션에서 걷어낸 '외부 대기' 단계 제목들 — warnings 고지용 (#225).
    waiting_dropped: list[str]
    # **마지막** 분해가 룰 폴백이었으면 그 사유(`RunResult.reason`), LLM 이 만들었으면 None.
    # 응답 aiSource 와 '칸만 잡아 뒀어요' 고지는 이 값을 본다 — 검토 폴백과 섞지 않는다.
    decompose_fallback_reason: str | None

    # PLANNING (룰 스케줄러 산출 — LLM 0회)
    scheduled_blocks: list[ScheduledBlockPreview]
    schedule_warnings: list[str]

    # REVIEWING
    review: PlanReview | None
    replan_count: int

    # 공통
    horizon: str | None
    used_fallback: bool  # 어느 LLM 노드든 fell_back 누적 → 응답 ai_source


def initial_state(
    *,
    user_id: UUID,
    outcome: InterviewOutcome,
    target_date: str,
    scope: Literal["week", "horizon"] = "horizon",
    density: str = "standard",
    milestones: list[MilestoneDraft] | None = None,
    max_plan_weeks: int = first_plan_adapter.max_plan_weeks_for(is_mandala_derived=False),
    milestone_cursor: int = 0,
) -> FirstPlanState:
    return FirstPlanState(
        user_id=user_id,
        outcome=outcome,
        target_date=target_date,
        scope=scope,
        density=density,
        milestones=milestones,
        max_plan_weeks=max_plan_weeks,
        milestone_cursor=milestone_cursor,
        out_of_cycle_dropped=[],
        density_damped_from=None,
        started_at=_time.monotonic(),
        materials_fetched=False,
        materials_notice=None,
        planning_context={},
        goal_plan=None,
        coverage_extended=0,
        curriculum_plan=False,
        curriculum_notice=None,
        waiting_dropped=[],
        decompose_fallback_reason=None,
        scheduled_blocks=[],
        schedule_warnings=[],
        review=None,
        replan_count=0,
        horizon=outcome.horizon,
        used_fallback=False,
    )


def _session(config: RunnableConfig) -> Any:
    return config.get("configurable", {}).get("session")


def _tone_mode(config: RunnableConfig) -> str | None:
    """config["configurable"]["tone_mode"] 안전 추출 (#23-D)."""
    raw = config.get("configurable", {}).get("tone_mode")
    return raw if isinstance(raw, str) else None


# ─────────────────────────────────────────────────────────────────────────────
# 룰 fallback — 같은 schema 로 환원.
# ─────────────────────────────────────────────────────────────────────────────


def _rule_decomposition(state: FirstPlanState) -> GoalDecomposition:
    """LLM 분해 실패 시 룰 폴백 — heaviest 목표를 density 만큼 균등 '회차' 세션으로 환원.

    Gemini 미가용이라 내용 분해는 못 하지만, 사용자가 고른 분량(density → 주당 세션 수)만큼
    회차 세션을 만들어 빈 계획으로 떨어지지 않게 한다. category 는 영속화(approve) 시
    `_normalize_category` 가 enum 으로 정규화한다.
    """
    heaviest = first_plan_adapter.heaviest_goal(state["outcome"])
    # LLM 경로와 동일하게, 주당 가용 시간(weekly_hours)이 있으면 그 시간 기반으로 세션 수를 잡고
    # 없으면 density 프리셋으로 폴백 — 룰 폴백도 사용자의 실제 시간에 맞춘 분량을 낸다.
    session_count = first_plan_adapter.target_sessions_per_week(state["outcome"], state["density"])
    # 룰 폴백 세션 길이도 LLM 경로와 같은 기준 — 빈도로 주당 시간을 나눈 계획 세션 길이
    # (없으면 목표별 세션 길이 → 전역/기본). 하드코딩 30분 대신.
    session_len = first_plan_adapter.planned_session_min_for(state["outcome"])

    root = GoalNodeDraft(
        node_id="tmp-root",
        parent_id=None,
        title=heaviest.title,
        node_type="root",
        order_index=0,
        is_leaf=False,
    )
    nodes = [root]
    actions: list[ActionItemDraft] = []
    for i in range(session_count):
        leaf_id = f"{first_plan_adapter.FALLBACK_NODE_PREFIX}-{i}"
        label = f"{heaviest.title} {i + 1}회차"
        nodes.append(
            GoalNodeDraft(
                node_id=leaf_id,
                parent_id="tmp-root",
                title=label,
                node_type="leaf",
                order_index=i,
                is_leaf=True,
            )
        )
        actions.append(
            ActionItemDraft(
                node_id=leaf_id,
                title=label,
                estimated_minutes=session_len,
                category=heaviest.category,
                first_step=first_plan_adapter.FALLBACK_FIRST_STEP,
            )
        )
    return GoalDecomposition(goal_nodes=nodes, action_items=actions, policy_violations=[])


def _rule_review(state: FirstPlanState) -> PlanReview:
    """리뷰 LLM 실패 시 룰: 그대로 승인(무한 cycle 방지, HITL 이 최종 게이트)."""
    return PlanReview(approved=True, feedback=[])


def _replan_feedback(state: FirstPlanState) -> str:
    """직전 REVIEWING 피드백 → decompose 프롬프트 변수(`{{review_feedback}}`).

    replan 엣지(review_plan → decompose_goal)로 재진입할 때 직전 리뷰의 미승인 사유를 실어,
    재분해가 **동일 결과를 반복하지 않고 실제로 다듬어지게** 한다(과거엔 같은 프롬프트를
    그대로 재실행해 cycle 이 무의미했다). 첫 분해(리뷰 이전)엔 피드백이 없으므로 빈 신호.
    """
    review = state.get("review")
    if review is None or not review.feedback:
        return "(첫 분해 — 이전 피드백 없음)"
    return "\n".join(f"- {item}" for item in review.feedback)


def _format_milestones(milestones: list[MilestoneDraft] | None) -> str:
    """확정 마일스톤 → decompose 프롬프트 변수(`{{milestones}}`). 없으면 '(없음)'.

    있으면 분해가 이 목록을 branch 로 고정하고 각 안에서만 leaf 를 만든다(#milestones Stage B).
    """
    if not milestones:
        return "(없음)"
    lines = []
    for i, m in enumerate(milestones, 1):
        summary = f" — {m.summary}" if m.summary else ""
        lines.append(f"{i}. {m.title}{summary}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Nodes
# ─────────────────────────────────────────────────────────────────────────────


def tier_violation_for(outcome: InterviewOutcome) -> str | None:
    """Focus ≤ 3 / Maintain ≤ 5 초과 여부 (DevBaseline §1.4 잠금). 위반 없으면 None.

    `validate_inputs` 에서 떼어낸 **순수** 함수다. 라우트가 LLM 분해 전에 값싸게 422 를
    던지려면 이 판정만 필요한데, `validate_inputs` 는 #226 이후 링크를 여는 I/O 노드가
    됐다. 라우트가 게이트 용도로 노드를 통째로 부르면 그래프 진입 노드가 같은 일을 또 해서
    **사용자 링크를 버튼 한 번에 두 번** 요청하고 8s 타임아웃을 두 번 태운다. 판정만
    떼어내 양쪽이 같은 규칙을 쓰되 I/O 는 노드 한 곳에만 남긴다.
    """
    focus_count = sum(1 for g in outcome.core_goals if g.tentative_tier == "focus")
    maintain_count = sum(1 for g in outcome.core_goals if g.tentative_tier == "maintain")
    if focus_count > 3:
        return "focus_cap_exceeded"
    if maintain_count > 5:
        return "maintain_cap_exceeded"
    return None


def plannable_goal_missing(outcome: InterviewOutcome) -> bool:
    """계획을 세울 **실제 목표**가 하나도 없는가 — 미입력 placeholder(#88)만 남은 outcome.

    `tier_violation_for` 와 같은 자리의 순수 판정이다. 첫 질문에서 [충분해요]를 누르면
    outcome 에는 '(미입력 목표)' 하나만 남는데, 예전엔 그걸 그대로 분해해 LLM 이 일반론
    20세션을 지어냈고(미러 실측), 승인하면 소속시킬 목표가 없어 0건이 저장되는데도 200 이
    나가 온보딩이 텅 빈 채 끝났다. 라우트가 LLM 을 부르기 **전에** 이걸로 되돌려 보낸다.
    """
    return all(is_placeholder_goal(g) for g in outcome.core_goals)


async def _failure_contexts(
    session: Any, user_id: UUID, reference_date: date, goal_id: UUID | None
) -> tuple[list[TopFailureContext], Literal["goal", "user"]]:
    """최근 28일 실패 사유 상위 3개 — decompose 프롬프트 `failure_summary` 재료.

    **이 목표 먼저, 없으면 사용자 전체.** 처음(#345 2단계)에는 사용자 전체 집계만 실었는데,
    그러면 목표 A 를 분해하면서 목표 B 에서 쌓인 태그를 근거로 분량을 줄인다. 지금 세우는
    목표의 이력이 있으면 그게 훨씬 강한 근거라 먼저 본다.

    되돌아가는 조건은 `has_enough_failure_sample` — 목표 단위가 표본 부족이면 어차피
    `_failure_summary` 가 '(없음)' 으로 떨어뜨릴 값이라, 그럴 바엔 사용자 전체 집계를 준다
    ("계획이 컸어요" 같은 성향은 목표를 건너 옮겨 다니는 편이다). 이 판정을 어댑터와 공유
    하지 않으면 목표 단위가 조용히 비어도 폴백이 안 걸리는 사각이 생긴다.

    `d0=d1=reference_date` 로 넘겨 [reference_date-27, reference_date] 창을 만드는 건
    `routes/review.py::_top_failure_contexts` 와 같은 관례.

    session 이 없으면(단위 테스트/시스템) 빈 리스트 → `_failure_summary` 가 '(없음)' 으로 내림.
    """
    if session is None:
        return [], "user"
    repo = ReviewRepo(session)
    if goal_id is not None:
        scoped = list(
            await repo.get_goal_failure_contexts(user_id, goal_id, reference_date, reference_date)
        )
        if first_plan_adapter.has_enough_failure_sample(scoped):
            return scoped, "goal"
    return list(
        await repo.get_top_failure_contexts(user_id, reference_date, reference_date)
    ), "user"


async def _recovery_contexts(
    session: Any, user_id: UUID, reference_date: date
) -> list[RecoveryOutcomeContext]:
    """최근 28일 전략별 회복 결과 — decompose 프롬프트 `recovery_summary` 재료.

    회복 이력은 그동안 에스컬레이션 판정(L1/L2/L3)에서만 읽혔다. 계획은 사용자가 **말한**
    선호만 보고 실제로 **해 본** 조정이 통했는지는 몰랐다 — 창·폴백 규약은 `_failure_contexts`
    와 같다(같은 프롬프트 줄에 나란히 들어가는 재료라 창이 달라지면 안 된다).

    목표 단위로 좁히지 않는다. `recovery_attempts` 는 실행(execution)에 매달려 있어 목표까지
    두 단계를 더 조인해야 하는데, "어떤 조정이 이 사람에게 통하는가" 는 애초에 목표를 건너
    옮겨 다니는 성향이라(`list_recovery_results` 가 카드·목표 무관으로 보는 것과 같은 이유)
    좁혀서 얻을 게 없다.
    """
    if session is None:
        return []
    return list(
        await RecoveryRepo(session).list_recovery_outcome_contexts(
            user_id, reference_date, reference_date
        )
    )


async def _goal_failure_streak(session: Any, user_id: UUID, goal_id: UUID | None) -> int:
    """이 목표의 **연속 실패 수** — 계획 분량을 낮출지 판정하는 유일한 신호.

    회복이 L3(재협상)를 판정할 때 쓰는 것과 **같은 이력, 같은 세는 규칙**이다
    (`RecoveryRepo.list_goal_outcomes` + `escalation.compute_consecutive_failure_count`).
    두 기능이 각자 세면 같은 사용자에게 서로 다른 숫자로 서로 다른 말을 하게 된다.

    에스컬레이션의 다섯 신호 중 이것만 쓴다:
    - `consecutive_failure_count`/`same_tag_failure_count` 는 **특정 카드·태그**가 있어야
      뜻이 생긴다 — 아직 만들지도 않은 계획에는 그 대상이 없다.
    - `recovery_rejected_streak`(L3 의 다른 조건)은 **사용자 전체** 스코프다. 그걸 쓰면
      다른 목표에서 거절한 회복이 이 목표의 분량을 깎는다 — 목표 단위 집계로 좁히면서
      막으려던 바로 그 문제라 일부러 뺐다.

    goal_id 가 없으면(첫 계획·저장 전) 0 — 이력이 없으니 낮출 근거도 없다.
    """
    if session is None or goal_id is None:
        return 0
    outcomes = await RecoveryRepo(session).list_goal_outcomes(user_id, goal_id)
    return escalation.compute_consecutive_failure_count(list(outcomes))


async def validate_inputs(state: FirstPlanState, config: RunnableConfig) -> FirstPlanState:
    """VALIDATING — 분해에 쓸 재료(자료·실패 이력·분량)를 모은다.

    입력 검증은 라우트가 그래프에 들어오기 **전에** 한다 — Focus ≤ 3 / Maintain ≤ 5 초과
    (`tier_violation_for` → 422 GOAL_TIER_LIMIT_EXCEEDED, DevBaseline §1.4 잠금)와 실제 목표
    없음(`plannable_goal_missing` → 422). 예전엔 여기서 `missing_fields`·`tier_violation`
    을 상태에 적었지만 아무도 읽지 않아, 검증처럼 보이는 죽은 값이 '목표 없음' 계획을
    통과시킨 채 남아 있었다(planB-1·planB-17).
    """
    outcome = state["outcome"]
    target_date = date.fromisoformat(state["target_date"])
    # 참고 자료를 링크로만 줬으면 여기서 한 번 열어본다 (#226). I/O 는 이 노드가 하고
    # 컨텍스트 조립은 순수 함수로 남긴다. 실패해도 예외는 안 나오고, 그때는 예전처럼
    # '(없음)' 으로 내려가 프롬프트의 지어내기 방지 가드가 그대로 작동한다.
    heaviest = first_plan_adapter.heaviest_goal_or_none(outcome)
    materials = await materials_resolver.resolve(heaviest.materials_note if heaviest else None)
    # 목표 해석은 **한 번만** 한다 — 아래 세 조회가 같은 goal_id 를 쓴다.
    session = _session(config)
    goal_id = (
        None
        if session is None
        else await first_plan_adapter.heaviest_goal_id(
            session, user_id=state["user_id"], outcome=outcome
        )
    )
    failure_contexts, failure_scope = await _failure_contexts(
        session, state["user_id"], target_date, goal_id
    )
    recovery_contexts = await _recovery_contexts(session, state["user_id"], target_date)
    # 이 목표에서 연속으로 무너지고 있으면 분량 프리셋을 낮춘다. 낮춘 사실은 숨기지 않고
    # `density_damped_from` 에 남겨 배치 단계가 warnings 로 고지한다.
    requested_density = state["density"]
    density = first_plan_adapter.dampened_density(
        requested_density,
        consecutive_goal_failures=await _goal_failure_streak(session, state["user_id"], goal_id),
    )
    return {
        **state,
        "density": density,
        "density_damped_from": requested_density if density != requested_density else None,
        "materials_fetched": materials.ok,
        "materials_notice": materials.notice,
        "planning_context": first_plan_adapter.context_from_outcome(
            outcome,
            density=density,
            target_date=target_date,
            fetched_materials=materials.text,
            failure_contexts=failure_contexts,
            failure_scope=failure_scope,
            recovery_contexts=recovery_contexts,
            max_weeks=state["max_plan_weeks"],
        ),
    }


def _out_of_cycle_note(state: FirstPlanState) -> str:
    """이번 계획에서 **다루면 안 되는** 단계를 제목으로 못박아 준다 (ADR-0007 PR-5).

    "범위를 넘지 마라" 같은 추상 지시는 안 먹혔다 — 룰로 branch 를 걷어낸 뒤에도 LLM 은
    **남긴 branch 안에서** 뒷 단계를 세션으로 썼다(실측: 2주기 12세션 중 9~12번이 통째로
    다음 마일스톤의 Git·배포·이력서였고, 1번은 이미 끝낸 마일스톤의 환경 세팅이었다).
    제목을 그대로 보여주면 경계가 구체가 된다.

    이미 끝낸 것도 함께 적는다 — 안 그러면 완료한 단계를 다시 시킨다.
    """
    all_ms = list(state.get("milestones") or [])
    if not all_ms:
        return "(없음)"
    window = _cycle_milestones(state)
    window_titles = {m.title for m in window}
    cursor = int(state.get("milestone_cursor") or 0)
    done = [m.title for m in all_ms[:cursor]]
    later = [m.title for m in all_ms[cursor:] if m.title not in window_titles]
    lines: list[str] = []
    if done:
        lines.append("- 이미 끝낸 단계(다시 시키지 말 것): " + " / ".join(done))
    if later:
        lines.append("- 다음 주기가 받을 단계(여기서 시작하지 말 것): " + " / ".join(later))
    return "\n".join(lines) if lines else "(없음)"


def _cycle_milestones(state: FirstPlanState) -> list[MilestoneDraft]:
    """이번 주기가 다룰 마일스톤 구간 — **분해 프롬프트와 누락 고지가 같은 것을 봐야 한다.**

    둘이 갈리면 "이번 구간 밖이라 일부러 안 만든 마일스톤" 을 누락으로 잘못 알린다
    (#307 이 잡으려던 건 '세션 수 상한에 잘려 조용히 사라진 것' 이지 이 경우가 아니다).

    `state["milestones"]` 는 사용자가 확정한 **전체** 목록 그대로 둔다 — 승인이 그걸
    뼈대로 영속한다(PR-6a). 좁히는 건 이번 분해에 넘기는 몫뿐이다.
    """
    milestones = state.get("milestones")
    if not milestones:
        return []  # 마일스톤 없이 세우는 계획 — 잘라낼 것도, 마감을 볼 이유도 없다.
    ctx = state.get("planning_context")
    prompt_vars = ctx.get("prompt_vars", {}) if isinstance(ctx, dict) else {}
    try:
        horizon_weeks = int(prompt_vars.get("horizon_weeks", "1"))
    except (TypeError, ValueError):
        horizon_weeks = 1
    target = state.get("target_date")
    outcome = state.get("outcome")
    return first_plan_adapter.cycle_milestone_window(
        milestones,
        cursor=int(state.get("milestone_cursor") or 0),
        horizon_weeks=horizon_weeks,
        full_horizon_weeks=first_plan_adapter.full_horizon_weeks(
            date.fromisoformat(target) if target else None,
            outcome.horizon if outcome is not None else None,
        ),
    )


def _curriculum_decomposition(state: FirstPlanState) -> curriculum_plan.CurriculumPlan | None:
    """목표에 단원 구조가 있는 자료(교재 카드·목차 있는 도서·재생목록)가 확정돼 있으면 그 단원으로
    이번 주기 세션을 짠다 — 아니면 None.

    세션 수는 '이어가기' 보충(`extend_action_plan_to_horizon`)이 채우려는 개수와 **같은 식**
    이다 — LLM 경로에서 보충까지 거친 계획과 분량이 같아야 카드 유무로 계획 크기가 달라지지
    않는다. 마일스톤을 확정하지 않고 바로 세우는 계획이면 카드 단계를 그 자리에서 만들어 창을
    고른다(그 목록은 상태에 남기지 않는다 — '확정하신 중간 목표' 고지가 사용자가 확정한 적 없는
    것을 확정했다고 말하게 된다).
    """
    # `.get` — 분해 노드만 떼어 부르는 호출(톤 배선 테스트 등)은 outcome 없이 들어온다. LLM 경로도
    # outcome 을 분해 결과가 있을 때만 읽는다 — 카드 판정이 그 앞에서 KeyError 를 내면 안 된다.
    outcome = state.get("outcome")
    if outcome is None:
        return None
    heaviest = first_plan_adapter.heaviest_goal_or_none(outcome)
    session_minutes = first_plan_adapter.session_min_for(outcome)
    books = curriculum_plan.books_for(heaviest, session_minutes=session_minutes)
    if heaviest is None or not books:
        return None
    target_day = date.fromisoformat(state["target_date"])
    max_weeks = state["max_plan_weeks"]
    all_milestones = state.get("milestones") or curriculum_plan.milestones(
        books,
        full_weeks=first_plan_adapter.full_horizon_weeks(target_day, outcome.horizon),
        cycle_weeks=max_weeks,
    )
    n_sessions = first_plan_adapter.cadence_session_cap(
        outcome, state["density"], target_date=target_day, max_weeks=max_weeks
    ) or first_plan_adapter.horizon_session_target(
        outcome, state["density"], target_date=target_day, max_weeks=max_weeks
    )
    return curriculum_plan.decomposition(
        goal_title=heaviest.title,
        books=books,
        all_milestones=all_milestones,
        window=_cycle_milestones({**state, "milestones": list(all_milestones)}),
        n_sessions=n_sessions,
        session_minutes=session_minutes,
    )


async def decompose_goal(state: FirstPlanState, config: RunnableConfig) -> FirstPlanState:
    """PLANNING (LLM ②③) — goal_node 트리 + action_item 분해.

    replan 재진입 시엔 직전 리뷰 피드백(`_replan_feedback`)을 프롬프트에 실어, 재분해가
    같은 결과를 반복하지 않고 검토 지적을 반영하도록 닫힌 루프를 만든다.

    교재 카드가 확정된 목표는 LLM 대신 `_curriculum_decomposition` 이 책 단원 순서로 짠다.
    짝이 안 맞으면(사용자가 마일스톤을 추가·삭제했다) 아래 LLM 경로로 그대로 내려간다.
    """
    curriculum = _curriculum_decomposition(state)
    if curriculum is not None:
        return {
            **state,
            "goal_plan": curriculum.decomposition,
            "curriculum_plan": True,
            "curriculum_notice": curriculum.notice,
            "out_of_cycle_dropped": [],
            "coverage_extended": 0,
            "waiting_dropped": [],
            "decompose_fallback_reason": None,
        }
    ctx = state["planning_context"]
    prompt_vars = ctx.get("prompt_vars", {}) if isinstance(ctx, dict) else {}
    settings = get_settings()
    result = await aiClient.run(
        module="planning",
        schema=GoalDecomposition,
        # ⚠️ **버전을 명시한다 — `latest()` 에 맡기지 않는다.**
        # 버전을 생략하면 registry 가 최신 활성 버전으로 해석한다. 그래서 `goal_decompose.v3.md`
        # 를 **파일로 만든 것만으로** 프로덕션 분해가 배포 없이 v2 → v3 로 갈아탔다(#466).
        # 의도된 승격이었지만 승격 자체가 리뷰 대상이 아니었고, **오프라인 하네스도 같이
        # 옮겨갔다.** 게다가 L1-6·L1-7 원자료는 버전을 남기지 않아 provenance 가 불충분했다
        # (M33 은 clean git SHA 로 사후 복원할 수 있었지만 원자료만으로 즉시 식별되진 않았다).
        # 승격은 이 줄을 고치는 **의도된 변경**이어야 한다.
        # `tests/test_prompt_version_pins.py` 가 이 계약을 지킨다.
        prompt_id="planning/goal_decompose@v3",
        fallback=lambda: _rule_decomposition(state),
        timeout=settings.llm_planning_timeout_seconds,
        variables={
            **prompt_vars,
            "review_feedback": _replan_feedback(state),
            "milestones": _format_milestones(_cycle_milestones(state)),
            "out_of_cycle": _out_of_cycle_note(state),
        },
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
        thinking_budget=settings.llm_planning_thinking_budget,
    )
    # 세션 길이·개수 결정적 보정 — LLM 이 목표별 세션 길이를 무시하고 너무 짧게(9분) 내거나 세션을
    # 과다 생성해도, 밴드로 가두고 주당 시간만큼으로 잘라 이번 주 분량이 weekly_hours 에 맞게
    # 한다(#per-goal). 목표별 입력이 없으면 no-op.
    goal_plan = result.value
    # 분해 결과가 없으면(값 없는 폴백) 목표를 볼 일도 없다 — 그때는 outcome 을 읽지 않는다.
    heaviest_goal = (
        first_plan_adapter.heaviest_goal_or_none(state["outcome"])
        if goal_plan is not None
        else None
    )
    if goal_plan is not None and heaviest_goal is not None:
        # 금지어 치환이 사용자가 쓴 목표 제목·확정 마일스톤 제목까지 바꿔 놓았으면 원문으로
        # 되돌린다(planB-3) — LLM·룰 폴백 두 경로 모두 여기를 지난다. 마일스톤 제목도 넣는
        # 이유: 사용자가 고친 제목이 치환되면 branch 제목과 어긋나 `missing_milestone_titles`
        # 가 멀쩡한 단계를 '빠졌다' 고 알린다.
        goal_plan = first_plan_adapter.restore_user_phrases(
            goal_plan,
            [heaviest_goal.title, *(m.title for m in state.get("milestones") or [])],
        )
    if goal_plan is not None:
        # 트리에 없는 노드를 가리키는 카드는 root 아래 leaf 로 달아 준다(planB-14) — 아래
        # 걷어내기·가지치기·누락 고지와 승인의 goal_node_id 연결이 모두 같은 트리를 보게.
        goal_plan = first_plan_adapter.attach_orphan_actions(goal_plan)
    extended = 0
    waiting_dropped: list[str] = []
    out_of_cycle: list[str] = []
    if goal_plan is not None and goal_plan.action_items:
        target_day = date.fromisoformat(state["target_date"])
        # '외부 대기' 단계를 세션 목록에서 뺀다(노드는 유지) — 프롬프트 규칙의 결정적
        # 백스톱(#225). shape 이전에 빼야 세션 수 target 이 실제 실행 가능한 일로 채워진다.
        goal_plan, waiting_dropped = first_plan_adapter.drop_waiting_steps(goal_plan)
        # 이번 주기 범위 밖 branch 도 같은 자리에서 걷어낸다(ADR-0007 PR-5) — 프롬프트에
        # "주어진 마일스톤이 전부다" 를 넣어도 LLM 은 "총 N개 세션" 압력을 범위를 넓혀
        # 푼다(실측 5/5). extend 이전이라 잘린 분량은 이 범위 안의 회차로 다시 채워진다.
        # ⚠️ **되채울 수 있을 때만** 걷어낸다. 잘린 분량을 다시 채우는 건
        # `extend_action_plan_to_horizon` 인데, 그 함수는 케이던스를 명시한 목표
        # (frequency_per_week)에만 회차를 붙인다 — '몰아서·상관없음' 목표는 반복이
        # 자연스럽지 않아서다. 그런 목표에서 걷어내면 되채울 수단이 없어 4주 계획이
        # 이틀치로 무너지고, `volume_shortfall_warning` 은 **배치된 구간** 기준이라
        # 침묵한다(계획이 짧아지면 그 안에서는 비율이 맞아 보인다). 실측: 12세션 → 3세션.
        can_refill = bool(heaviest_goal and (heaviest_goal.frequency_per_week or 0) > 0)
        if can_refill:
            goal_plan, out_of_cycle = first_plan_adapter.drop_out_of_cycle_branches(
                goal_plan, _cycle_milestones(state)
            )
        goal_plan = first_plan_adapter.shape_action_plan(
            state["outcome"],
            state["density"],
            goal_plan,
            target_date=target_day,
            max_weeks=state["max_plan_weeks"],
        )
        # 마감까지 못 미치면 회차 세션으로 보충 — 자르기만 하고 채우지 않아 두 달짜리 목표에
        # 일주일 계획만 나오던 문제(#coverage). 프롬프트로 1차 방어하되 순응에 걸지 않는다.
        before = len(goal_plan.action_items)
        goal_plan = first_plan_adapter.extend_action_plan_to_horizon(
            state["outcome"],
            state["density"],
            goal_plan,
            target_date=target_day,
            max_weeks=state["max_plan_weeks"],
        )
        extended = len(goal_plan.action_items) - before
    return {
        **state,
        "out_of_cycle_dropped": out_of_cycle,
        "goal_plan": goal_plan,
        "coverage_extended": extended,
        "waiting_dropped": waiting_dropped,
        "decompose_fallback_reason": (result.reason or "unknown") if result.fell_back else None,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


def _schedule_end(start_day: date, horizon: str | None, scope: str) -> date:
    """배치 종료일(포함)을 scope 로 결정한다.

    - "week": target_date 가 속한 **달력 주의 일요일**까지 (마감이 더 이르면 마감으로 캡).
      주 중간에 시작하면 지난 날은 배치하지 않으므로 실질 범위는 [target_date, 일요일].
    - "horizon": 마감(horizon)까지 전 구간. 마감이 없으면 target_date 하루.

    **이미 지난 마감은 없는 것으로 본다** (#231). 그대로 두면 scope 와 무관하게 마지막
    `max(end, start_day)` 가 배치 창을 **오늘 하루로 붕괴**시켜, 세션 대부분이 "가용 시간을
    찾지 못했어요" 경고로 남는다(실측: 마감 11일 지난 목표 → 3세션 중 1개만 배치).
    """
    deadline = date.fromisoformat(horizon) if horizon else None
    if deadline is not None and deadline < start_day:
        deadline = None
    if scope == "week":
        sunday = start_day + timedelta(days=6 - start_day.weekday())  # 월=0 → 그 주 일요일
        end = min(sunday, deadline) if deadline is not None else sunday
    else:
        end = deadline if deadline is not None else start_day
    return max(end, start_day)


async def _existing_busy_by_day(
    config: RunnableConfig,
    user_id: UUID,
    start_day: date,
    end_day: date,
    exclude_target_date: date | None = None,
    exclude_goal_id: UUID | None = None,
) -> dict[date, list[BusyBlock]]:
    """승인된 `scheduled_blocks` 를 날짜별 busy 로 — 재계획 시 비파괴 fit-around 용.

    `exclude_target_date` + `exclude_goal_id` 가 주어지면 그 날짜 승인 시 supersede 로 취소될
    **같은 목표의 자기 이전 First Plan** 산출물의 블록만 busy 에서 뺀다(#118) — 재생성 계획이
    '곧 자기 손으로 비울' 슬롯을 피해 나쁘게(또는 빈 채로) 배치되지 않게.

    **다른 목표의 블록은 반드시 busy 로 남긴다.** 예전엔 날짜만 보고 제외해서, 같은 날 만든
    다른 목표의 계획이 회피 대상에서 빠졌다 → 남의 슬롯 한복판에 배치되고(실측: MVP
    17:15~19:15 위에 토익 18:15~18:49) 승인이 그걸 지웠다. busy 로 남기면 `_earliest_fit` 이
    선호 윈도우 **안에서** 비어 있는 더 늦은 지점을 잡는다(18시가 차 있으면 20~21시).

    시작된 카드·user_edit·다른 날짜·다른 소스(inbox/recovery) 블록도 그대로 busy(회피 유지).
    session 이 없으면 빈 dict.
    """
    session = _session(config)
    if session is None:
        return {}
    stale_ids: set[UUID] = set()
    if exclude_target_date is not None:
        # exclude_target_date 는 '재생성 계획인가'의 게이트로만 쓴다 — 교체 대상 산정
        # 자체는 goal 단위다(#222, superseded_card_ids 참고).
        stale_ids = await first_plan_adapter.superseded_card_ids(
            session, user_id=user_id, goal_id=exclude_goal_id
        )
    start_dt = datetime.combine(start_day, time(0, 0), tzinfo=KST)
    end_dt = datetime.combine(end_day + timedelta(days=1), time(0, 0), tzinfo=KST)
    rows = await ScheduledBlockRepo(session).list_busy_between(user_id, start_dt, end_dt)
    busy: dict[date, list[BusyBlock]] = defaultdict(list)
    for row in rows:
        if row.action_item_id in stale_ids:
            continue  # 승인 시 supersede 될 자기 이전 계획 — busy 로 세지 않음(#118)
        s, e = to_kst(row.start_at), to_kst(row.end_at)
        if e <= s:
            continue
        busy[s.date()].append(BusyBlock(TimeInterval(s, e), "scheduled_block", "기존 일정"))
    return busy


#: 캘린더를 연결해 뒀는데 못 읽었을 때의 안내 — 첫 계획·재계획이 같은 문구를 쓴다.
CALENDAR_FAILED_WARNING = (
    "캘린더 일정을 불러오지 못해서 이번 계획에는 반영하지 못했어요. "
    "겹치는 일정이 있으면 시간을 옮겨 주세요."
)


async def _calendar_busy_by_day(
    config: RunnableConfig,
    user_id: UUID,
    start_day: date,
    end_day: date,
) -> tuple[dict[date, list[BusyBlock]], str]:
    """Google 캘린더 일정을 날짜별 busy 로 (ADR-0009 D4). 실패해도 계획은 계속된다.

    반환의 두 번째 값은 상태다 — `"ok"` / `"not_connected"` / `"failed"`.
    **연결하지 않은 사용자는 조용히 넘어가지만**(대다수가 그렇다), 연결해 둔 사용자에게
    조용히 실패하면 안 된다: "캘린더를 연결했는데 수업 위에 계획이 잡혔다" 는 사용자가
    스스로 알아챌 수 없다. 그래서 `failed` 는 호출자가 경고로 surface 한다.

    session 이 없으면(단위 테스트/시스템 경로) 조회하지 않는다.
    """
    session = _session(config)
    if session is None:
        return {}, "not_connected"
    return await freebusy.fetch_busy_by_day(
        session, user_id=user_id, start_day=start_day, end_day=end_day
    )


async def _db_time_policies(config: RunnableConfig, user_id: UUID) -> list[Any]:
    """활성 DB `time_policies`(S07, 온보딩 후 수정 포함) — outcome 스냅샷과 합쳐 busy 로.

    session 이 없으면(단위 테스트/시스템) 빈 리스트 → outcome 정책만 사용.
    """
    session = _session(config)
    if session is None:
        return []
    return list(await TimePolicyRepo(session).list_active(user_id))


async def _fixed_schedules(config: RunnableConfig, user_id: UUID) -> list[Any]:
    """활성 `fixed_schedules`(수업·알바, S05) — AI 블록이 그 위에 겹치지 않도록 busy 로.

    session 이 없으면 빈 리스트.
    """
    session = _session(config)
    if session is None:
        return []
    return list(await FixedScheduleRepo(session).list_active(user_id))


def _ceil_quarter(dt: datetime) -> datetime:
    """15분 경계로 올림 — 오늘 '지금' 이후 배치의 깔끔한 시작 경계."""
    dt = dt.replace(second=0, microsecond=0)
    rem = dt.minute % 15
    return dt if rem == 0 else dt + timedelta(minutes=15 - rem)


async def schedule_blocks(state: FirstPlanState, config: RunnableConfig) -> FirstPlanState:
    """PLANNING (룰 only, LLM 0회) — 분해 action_item 을 다일 스케줄러로 배치한다.

    이전에는 `reserve_habit_sessions`(습관 하루 1세션)를 재사용해 `target_date` 하루에
    모든 카드를 몰아넣었다(#calendar-cramming). 이제 `plan_scheduler.schedule_actions_multiday`
    로 배치 범위(`scope`)에 걸쳐 분산 배치한다:
    - scope="horizon"(기본): **마감까지** 전 구간 — 실행이 마감 전 여러 날에 분배된다.
      한 주가 지나면 주간 재계획이 실패·잔여를 반영해 이후를 다시 쓴다(후속).
    - scope="week": target_date 가 속한 **달력 주**만 (가벼운 단기 계획).
    - 하루 집중 총량 상한을 채우면 다음 날로 넘기고, 피크 시간대 free 를 먼저 쓰며,
      긴 카드는 focus_duration 세션으로 쪼개고 카드 사이 휴식(break_pattern)을 둔다.

    busy 는 (1) 수면/노터치(outcome + **DB `time_policies`**, 온보딩 후 수정 반영) +
    (2) **이미 승인된 `scheduled_blocks`** + (3) **`fixed_schedules`(수업·알바)** 를 모두 합친다
    → 기존·고정·수정된 일정 위에 겹쳐 잡지 않는다(비파괴 fit-around, HITL 승인 보존). (#112)
    단 **재생성**이면 승인 시 supersede 로 취소될 자기 같은-날짜 이전 계획은 busy 에서 뺀다
    (#118) — 그렇지 않으면 새 계획이 곧 비워질 슬롯을 피해 나쁘게 배치된다.

    배치 못 한 항목은 `schedule_warnings` 로 남겨 REVIEWING 의 conflict_report 에 합류한다.
    산출물은 미영속 미리보기(`ScheduledBlockPreview`) — 자동 적용 금지(AGENTS §1.4).
    """
    gp = state["goal_plan"]
    action_items = list(gp.action_items) if gp is not None else []
    outcome = state["outcome"]
    user_id = state["user_id"]

    start_day = date.fromisoformat(state["target_date"])
    # 이미 지난 마감(#231) — 배치 창을 '마감 없음' 처럼 펴고, 마지막에 그 사실을 고지한다.
    overdue_deadline = first_plan_adapter.is_overdue_deadline(outcome.horizon, start_day)
    schedule_end = _schedule_end(start_day, outcome.horizon, state["scope"])
    # 먼 마감 희석 방지(#weekly-rate): weekly_hours 는 '주당' rate 다. 이번 분해량(action 수)을
    # 주당 rate 로 담는 데 필요한 주 수만큼으로 배치 창을 좁힌다. 그러지 않으면 scope="horizon"
    # 기본값에서 ~1주치 세션이 먼 마감(예: 이번 학기) 전체에 균등 분산돼 이번 주가 텅 빈다.
    # 마감이 그보다 가까우면 _schedule_end 캡이 그대로 이겨(마감까지 몰기) 유지된다. 이후 주는
    # 주간 재계획이 채운다(비지속 초안이라 안전).
    per_week = first_plan_adapter.requested_sessions_per_week(outcome)
    if action_items:
        # 배치 창 너비 — 근거와 산식은 `placement_days_needed` 참고 (ADR-0009 D1).
        days_needed = first_plan_adapter.placement_days_needed(
            sum(a.estimated_minutes for a in action_items),
            first_plan_adapter.weekly_minutes(outcome, state["density"]),
        )
        if per_week is not None:
            # 빈도를 말한 목표는 **개수**로도 창을 잰다 — 분 기준만 쓰면 짧은 세션이 섞일 때
            # 창이 줄어 같은 개수가 더 적은 날에 몰린다(`cadence_days_needed`). 둘 중 넓은
            # 쪽을 쓰고, 계획 지평(주)을 넘지는 않는다 — 마감 없는 경로도 바운드가 남는다.
            days_needed = max(
                days_needed,
                min(
                    first_plan_adapter.cadence_days_needed(len(action_items), per_week),
                    state["max_plan_weeks"] * 7,
                ),
            )
        density_end = start_day + timedelta(days=days_needed - 1)
        if state["scope"] == "horizon" and (not outcome.horizon or overdue_deadline):
            # 마감 없는 습관형 목표(예: '매일 운동')는 _schedule_end 가 배치 창을 **하루로
            # 붕괴**시킨다(horizon=None → end=start_day). 그러면 주당 rate 만큼의 세션이 전부
            # 첫날에 몰려 '매일'이 '하루 몰빵'이 된다. rate 를 담을 days_needed 로 창을 펴
            # 세션이 서로 다른 날에 분산되게 한다(#per-goal-frequency, 마감없음 케이스).
            # 상한은 shape_action_plan 이 잡는다: 마감 없는 목표의 세션 수를 주당 rate ×
            # _MAX_PLAN_WEEKS 로 캡하므로 days_needed ≤ 4주 → 무한 미래 배치가 없다.
            # (예전엔 이 캡이 1주여서 습관형 계획이 7일 3세션으로 끝났다. '마감 없음' 을
            #  '짧다' 로 읽던 문제 — `_horizon_weeks` 참고.) 4주 이후는 주간 재계획이 잇는다.
            schedule_end = density_end
        else:
            schedule_end = max(min(schedule_end, density_end), start_day)
    # 정책 = outcome 스냅샷 + DB 정책(온보딩 후 수정 포함). union → compute_free_blocks 가 병합.
    policies = [
        *first_plan_adapter.time_policies_from_outcome(outcome),
        *await _db_time_policies(config, user_id),
    ]
    fixed = await _fixed_schedules(config, user_id)
    actions = first_plan_adapter.plan_actions_from_decomposition(action_items)
    node_by_action = {a.id: a.node_id for a in actions}

    # exclude_target_date + exclude_goal_id: 재생성 시 승인이 곧 supersede 할 **같은 목표의**
    # 자기 이전 계획만 busy 에서 제외 (#118). 첫 계획이면 교체 대상이 없어 no-op. 다른 목표의
    # 계획은 busy 로 남아 스케줄러가 뒤 시간대로 밀어 피한다.
    session = _session(config)
    exclude_goal_id = (
        await first_plan_adapter.heaviest_goal_id(session, user_id=user_id, outcome=outcome)
        if session is not None
        else None
    )
    existing_busy = await _existing_busy_by_day(
        config,
        user_id,
        start_day,
        schedule_end,
        exclude_target_date=start_day,
        exclude_goal_id=exclude_goal_id,
    )
    # 사용자의 Google 캘린더 일정 — 다섯 번째 busy 소스 (ADR-0009 D4).
    # 지평 전체를 **한 번에** 조회한다(`busy_for_day` 안에서 부르면 날짜마다 API 왕복).
    calendar_busy, calendar_status = await _calendar_busy_by_day(
        config, user_id, start_day, schedule_end
    )

    # 오늘 계획을 저녁에 만들어도 이미 지난 시간대(예: 18:40 생성 → 12:00)에 세션이 잡히지
    # 않도록, '오늘'의 [00:00, 지금(15분 올림)) 구간을 busy 로 넣어 과거 배치를 막는다.
    now = now_kst()
    past_cutoff = _ceil_quarter(now)
    day_zero = datetime.combine(now.date(), time(0, 0), tzinfo=KST)

    def busy_for_day(day: date) -> list[BusyBlock]:
        extra: list[BusyBlock] = []
        if day == now.date() and past_cutoff > day_zero:
            extra.append(BusyBlock(TimeInterval(day_zero, past_cutoff), "past", "지난 시간"))
        return [
            *time_policies_to_busy(day, policies),
            *fixed_schedules_to_busy(day, fixed),
            *existing_busy.get(day, []),
            *calendar_busy.get(day, []),
            *extra,
        ]

    break_min = first_plan_adapter.break_min_from_outcome(outcome)
    # 하루 상한은 density 프리셋과 **이번 계획의 최장 세션** 중 큰 쪽 — 세션 하나가 상한을
    # 넘으면 1차 배치가 모든 '이미 뭔가 있는 날' 을 걸러내 케이던스가 무너진다. 집중 용량이
    # 아니라 실제 최장 세션을 쓰는 이유는 `daily_cap_for_plan` 참고 (ADR-0009 D3).
    # ⚠️ **한 번만 계산해 배치와 과부하 안내가 같은 값을 본다** (planB-13). 예전엔 안내만
    # 최장 세션 없이 따로 계산해(= 집중 용량 240분) 배치가 180분 상한을 넘긴 날을 놓쳤고,
    # 잡더라도 기준 시간을 틀리게 말했다.
    daily_cap = first_plan_adapter.daily_cap_for_plan(
        outcome,
        state["density"],
        longest_action_min=max((a.estimated_minutes for a in actions), default=0),
    )

    def roomy_busy_for_day(day: date) -> list[BusyBlock]:
        """1차 배치용 busy — **기존 승인 블록에만** 앞뒤 휴식 여백을 덧댄다(#191).

        계획 안에서는 카드 사이에 `break_min` 휴식을 두면서 다른 목표의 계획과는 0분으로
        딱 붙던 문제(실측: 19:15~21:15 → 21:15~22:15 → 22:15~23:15, 4시간 연속)를 막는다.
        수면·고정일정에는 덧대지 않는다 — 그건 '쉴 수 없는 시간'이지 집중 작업이 아니고,
        여백을 주면 기상 직후·수업 직후 시간이 통째로 날아간다.
        """
        return [
            *time_policies_to_busy(day, policies),
            *fixed_schedules_to_busy(day, fixed),
            *pad_busy(existing_busy.get(day, []), break_min),
            # 캘린더 일정에는 여백을 **덧대지 않는다** — 고정일정(수업)과 같은 취급이다.
            # 그건 '쉴 수 없는 시간'이지 집중 작업이 아니라, 여백을 주면 일정 직후가
            # 통째로 날아간다(v1.97 결정). 앞뒤 이동 시간을 두는 '전이 버퍼'는 성격이
            # 다른 별개 룰이고(ADR-0009 D4 ①) 아직 구현하지 않았다.
            *calendar_busy.get(day, []),
            *(
                [BusyBlock(TimeInterval(day_zero, past_cutoff), "past", "지난 시간")]
                if day == now.date() and past_cutoff > day_zero
                else []
            ),
        ]

    placed, warnings = schedule_actions_multiday(
        start_day=start_day,
        horizon_day=schedule_end,
        actions=actions,
        busy_for_day=busy_for_day,
        peak_windows=first_plan_adapter.peak_windows_for_plan(outcome),
        focus_chunk_min=first_plan_adapter.focus_chunk_min_from_outcome(outcome),
        break_min=break_min,
        daily_focus_cap_min=daily_cap,
        committed_min_by_day=first_plan_adapter.committed_minutes_by_day(existing_busy),
        roomy_busy_for_day=roomy_busy_for_day,
        # ⚠️ 달력 주(월~일) 단위 개수 상한은 **걸지 않는다** (planB-6 리뷰). 마감 계획의 세션
        # 수는 '주 N회 × 올림(일수/7)' 이라 창이 주 중간에 시작·끝나면 온전한 주가 상한에
        # 막히고, 넘친 몫이 짧은 첫·끝 주의 며칠에 하루 여러 개로 쌓였다(실측: '매일' 토요일
        # 시작·16일 창 → 일요일 하루 5~6개). 케이던스는 위의 개수 기준 창 넓히기가 지킨다.
    )

    # 세션 하나가 활동창의 연속 가용 길이보다 길어 어디에도 안 들어간 경우(#252):
    # 배치 실패는 **전부 같은 원인**이므로, 항목마다 같은 문장을 반복하는 대신 원인과 다음
    # 행동을 담은 한 줄로 바꾼다(실측: 같은 경고 12줄 → 1줄). 실패가 없으면 아무것도 안 한다.
    unplaced_count = sum(1 for w in warnings if _UNPLACED_MARKER in w)
    narrow_window = first_plan_adapter.narrow_activity_window_notice(outcome)
    if narrow_window and warnings:
        warnings = [
            narrow_window,
            *(w for w in warnings if _UNPLACED_MARKER not in w),
        ]
    # 마감이 계획 첫날이면 창이 그 하루로 줄어 전부 몰리거나 못 들어간다(planB-10). 없는
    # 블록을 '옮겨볼까요?' 로 되풀이하지 않고 원인과 다음 행동을 한 줄로 말한다 — 위와 같은 수법.
    same_day = first_plan_adapter.same_day_deadline_notice(
        outcome.horizon,
        start_day=start_day,
        today=now.date(),
        placed=len(placed),
        unplaced=unplaced_count,
    )
    if same_day:
        warnings = [same_day, *(w for w in warnings if _UNPLACED_MARKER not in w)]

    # 캘린더를 연결해 뒀는데 못 읽은 경우만 알린다 (ADR-0009 D4). 연결하지 않은 사용자
    # (대다수)에게는 아무 말도 하지 않는다 — 캘린더는 선택 기능이라 매번 권유가 되면
    # 알림 피로가 된다. 반대로 연결한 사용자에게 조용히 넘어가면 "연결했는데 수업 위에
    # 계획이 잡혔다" 를 스스로 알아챌 방법이 없다.
    if calendar_status == "failed":
        warnings = [CALENDAR_FAILED_WARNING, *warnings]
    elif calendar_status == "reconnect_required":
        warnings = [freebusy.CALENDAR_RECONNECT_WARNING, *warnings]

    blocks = [
        ScheduledBlockPreview(
            start=b.interval.start,
            end=b.interval.end,
            title=b.title,
            category=b.category,
            origin="goal",
            origin_id=(node_by_action.get(b.origin_id) or None)
            if b.origin_id is not None
            else None,
        )
        for b in placed
    ]
    # 실제로 배치된 분량이 사용자가 말한 주당 시간에 못 미치면 알린다 — 조용히 줄이지 않는다.
    # 입력이 아니라 **배치 결과**에서 역산해야 분해가 적게 나온 경우까지 정직하게 잡힌다.
    placed_minutes = sum(
        round((b.interval.end - b.interval.start).total_seconds() / 60) for b in placed
    )
    shortfall = first_plan_adapter.volume_shortfall_warning(
        outcome,
        planned_minutes=placed_minutes,
        span_days=(schedule_end - start_day).days + 1,
    )
    if shortfall:
        warnings = [shortfall, *warnings]
    # 참고 자료를 링크로만 준 경우 — 열어봤으면 되물을 게 없고(#226), 못 열었으면 그 사유를
    # 담아 알린다. LLM 이 flag 를 남기든 말든(순응 불확실) 이건 우리가 확실히 아는 사실이다.
    # 분량을 낮췄으면 그 사실을 맨 앞에 밝힌다 — 사용자가 고른 값과 다르게 만든 것이라
    # 배치 경고보다 먼저 읽혀야 한다(확정 마일스톤 누락 고지와 같은 우선순위).
    damped = first_plan_adapter.density_damped_notice(
        damped_from=state.get("density_damped_from"), density=state["density"]
    )
    if damped:
        warnings = [damped, *warnings]
    # 교재 단원이 세션 길이에 다 안 들어간다 — 분량을 말없이 늘리거나 단원을 빼지 않고 알린다.
    curriculum_notice = state.get("curriculum_notice")
    if curriculum_notice:
        warnings = [curriculum_notice, *warnings]
    link_only = first_plan_adapter.materials_link_only_warning(
        outcome,
        fetched=bool(state.get("materials_fetched")),
        fetch_notice=state.get("materials_notice"),
    )
    if link_only:
        warnings = [link_only, *warnings]
    # 사용자가 **확정한** 중간 목표가 이번 계획에 안 들어갔으면 알린다 (ADR-0007 §배경 ①).
    # 세션 수 상한(shape_action_plan)이 자르든 LLM 이 애초에 안 만들든, 사용자에겐 '내가
    # 확인한 단계가 계획에 없다' 로 같다. 다른 축소는 전부 고지하면서 **사용자가 직접
    # 확인한 뼈대**만 침묵하고 있었다. 앞쪽에 싣는다 — 자기가 승인한 구조에 관한 말이라
    # 배치 경고보다 먼저 읽혀야 한다.
    # 이번 주기가 **다루기로 한** 구간만 본다 — 뒤쪽 마일스톤은 일부러 안 만든 것이라
    # 누락이 아니다(`_cycle_milestones` 참고). 프롬프트에 넘긴 것과 같은 목록이어야 한다.
    confirmed = _cycle_milestones(state)
    missing_notice = first_plan_adapter.missing_milestones_notice(
        first_plan_adapter.missing_milestone_titles(confirmed, gp) if gp is not None else [],
        # 개수는 **사용자가 확정한 전체** 다 — 창 크기를 세면 4개를 확정한 사용자에게
        # "확정하신 중간 목표 1개 중…" 이라고 말한다. 필터링은 창으로, 개수는 전체로.
        confirmed=len(state.get("milestones") or []),
    )
    # 분해가 룰 폴백이면 계획 전체가 '{목표} N회차' 자리표시자다(planB-5). 그 사실을 맨 앞에
    # 밝히고, 폴백과 어긋나는 설명은 싣지 않는다 — 확정 마일스톤·'이어가기'·지평 안내는 모두
    # "앞쪽엔 내용이 있다" 를 전제로 한 문장이라, 자리표시자 계획에 붙으면 사실과 달라진다
    # (미러 실측: 빈 칸 12장에 "'이어가기' 회차 9개를 덧붙였어요", 1주 폴백에 "4주까지만 잡아요").
    fallback_reason = state.get("decompose_fallback_reason")
    placeholder_plan = fallback_reason is not None
    if missing_notice and not placeholder_plan:
        warnings = [missing_notice, *warnings]
    fallback_notice = first_plan_adapter.decompose_fallback_notice(fallback_reason)
    if fallback_notice:
        warnings = [fallback_notice, *warnings]
    # 선호 시간이 활동창과 전혀 안 겹쳐 창 **밖**에 배치한 경우 — 확장은 의도된 동작이지만
    # (#per-goal-time-availability), 말해주지 않으면 사용자가 창 밖 블록을 버그로 읽는다.
    extension = first_plan_adapter.preferred_time_extension_warning(outcome)
    if extension:
        warnings = [*warnings, extension]
    # 대기 단계를 세션에서 뺐으면 알린다 — 조용히 빼면 사용자는 단계가 사라졌다고 읽는다(#225).
    waiting = first_plan_adapter.waiting_steps_notice(list(state.get("waiting_dropped", [])))
    # 구간 밖이라 이번에 안 만든 마일스톤도 **이름을 불러** 알린다 — LLM 이 만들었다가
    # 걷어낸 경우(`out_of_cycle_dropped`)만 말하고 애초에 안 만든 경우는 침묵했는데,
    # 사용자에겐 둘이 같은 일이다(내가 확인한 단계가 계획에 없다).
    out_of_cycle_note = first_plan_adapter.out_of_cycle_notice(
        first_plan_adapter.next_cycle_milestone_titles(
            state.get("milestones"),
            cycle=confirmed,
            cursor=int(state.get("milestone_cursor") or 0),
            goal_plan=gp,
            already_dropped=list(state.get("out_of_cycle_dropped", [])),
        )
    )
    if waiting:
        warnings = [*warnings, waiting]
    if out_of_cycle_note and not placeholder_plan:
        warnings = [*warnings, out_of_cycle_note]
    # 회차 세션으로 마감까지 채웠으면 그 사실을 밝힌다 — 내용까지 지어낸 게 아님을 알 수 있게.
    extended = first_plan_adapter.coverage_extended_warning(
        state.get("coverage_extended", 0),
        outcome.horizon,
        max_weeks=state["max_plan_weeks"],
        target_date=start_day,
    )
    if extended and not placeholder_plan:
        warnings = [*warnings, extended]
    # 계획이 마감까지 안 닿으면 **왜 그런지** 알린다. 상한(만다라 유래면 2주, 아니면 4주)은
    # 의도된 설계인데, 말해주지 않으면 마감 전에 끝난 계획을 사용자가 버그로 읽는다.
    last_planned_day = max((to_kst(b.interval.start).date() for b in placed), default=None)
    coverage = first_plan_adapter.horizon_coverage_notice(
        outcome,
        last_planned_day=last_planned_day,
        target_date=start_day,
        max_weeks=state["max_plan_weeks"],
    )
    if coverage and not placeholder_plan:
        warnings = [*warnings, coverage]
    # 마감이 이미 지나 있으면 계획을 어떤 기준으로 잡았는지 밝힌다 — 인터뷰가 되묻지 못하고
    # 통과시킨 경우의 마지막 고지이자, 새 마감을 정하도록 이끄는 자리다 (#231).
    if overdue_deadline:
        overdue = first_plan_adapter.overdue_deadline_notice(
            outcome.horizon, start_day=start_day, last_planned_day=last_planned_day
        )
        if overdue:
            warnings = [*warnings, overdue]
    # 목표를 여러 개 말했는데 계획은 heaviest 하나만 다뤘다는 사실을 알린다. 한 번에 하나씩
    # 굴리는 건 의도된 설계지만, 말해주지 않으면 나머지 목표가 침묵 속에 사라진다(#187).
    deferred = first_plan_adapter.other_goals_deferred_notice(outcome)
    if deferred:
        warnings = [*warnings, deferred]
    # 하루 상한을 넘긴 날이 있으면 밝힌다. 상한은 1차 배치에서만 지켜지고, 마감이 임박하거나
    # 일이 많으면 2차가 '배치할 수 있으면 배치' 로 상한을 넘긴다(#fill-available). 의도된
    # 동작이지만 말해주지 않으면 사용자는 4시간짜리 하루를 이유 없이 마주친다(#190).
    overload = first_plan_adapter.daily_overload_notice(
        placed,
        committed_min_by_day=first_plan_adapter.committed_minutes_by_day(existing_busy),
        cap_min=daily_cap,
        # 마감이 없으면 마감을 이유로 대지 않는다 — 없는 마감을 지어내던 문구 봉합.
        horizon=outcome.horizon,
    )
    if overload:
        warnings = [*warnings, overload]
    # 사용자가 고른 빈도('매일'·'주 3회')를 못 지켰으면 그 사실과 이유를 밝힌다. 조용히
    # 넘어가면 사용자는 자기 케이던스가 반토막 난 걸 화면에서 직접 세어봐야 안다.
    cadence = first_plan_adapter.cadence_shortfall_notice(
        outcome,
        placed,
        start_day=start_day,
        committed_min_by_day=first_plan_adapter.committed_minutes_by_day(existing_busy),
    )
    if cadence:
        warnings = [*warnings, cadence]
    # 고른 시간대에 못 넣었으면 이유를 밝힌다 — '심야' 라고 답했는데 09:00 에 잡히면
    # 사용자는 자기 답이 왜 무시됐는지 알 방법이 없다.
    preferred = first_plan_adapter.preferred_window_missed_notice(outcome, placed)
    if preferred:
        warnings = [*warnings, preferred]
    return {**state, "scheduled_blocks": blocks, "schedule_warnings": warnings}


def _review_variables(state: FirstPlanState) -> dict[str, str]:
    """`planning/plan_quality` 프롬프트 변수 계약 (PR #44).

    goal_plan(분해 결과) + planning_context(요약) + policy_violations(충돌)를 프롬프트가
    요구하는 4종 문자열로 평탄화한다. 누락 시 render 실패 → 룰 fallback 으로 빠지므로
    리뷰 LLM 이 실제로 돌게 하려면 4종 모두 채워야 한다.
    """
    prompt_vars = state["planning_context"].get("prompt_vars", {})
    time_policy_summary = str(prompt_vars.get("time_policy_summary", ""))
    # 검토기가 세션 길이를 **사용자 값 기준**으로 보게 한다(v2). v1 은 `estimated_minutes ≤ 60`
    # 고정 상한을 들고 있었는데, 그건 goals.session_length 슬롯이 생기기 전 규칙이다. 실측:
    # 사용자가 120분이라 답해 분해가 120분 세션을 만들면 검토가 3/3 반려했고, 재분해는 2/2
    # 60분으로 줄였다 — 사용자가 명시한 값이 조용히 절반이 됐다(계획 총량도 반토막).
    session_length = str(prompt_vars.get("session_length", "(미입력)"))
    # 검토기가 **평균(session_length)과 상한(focus_capacity)을 구분**하게 한다 (ADR-0009 D2).
    # 길이가 작업 성격을 따라가면 개별 세션은 평균에서 벗어나는 게 정상이라, 평균 하나만 주면
    # 검토기가 정상적인 편차를 이탈로 읽고 반려한다 — 재분해가 다시 길이를 한 점으로 모은다.
    #
    # ⚠️ 위 120분 사고의 **구조적 원인이 2026-08-31 에 확정됐다** (L1-7B 선행 조사).
    # `focus_capacity` 는 `first_plan_adapter.session_min_for` 이고, ③층
    # `normalize_action_minutes` 의 클램프 상한이 **같은 함수**다. 그래서 검토기가 계획을
    # 볼 시점에는 상한 초과 항목이 존재할 수 없다 — 슬롯 조합 1,620건 전수에서 **0건**.
    # 즉 `plan_quality` 체크리스트 1번("세션 길이 상한")은 참인 반려가 원리적으로 불가능하고,
    # 그 항목의 모든 반려는 정의상 오탐이다. 2번("15분 하한")도 90/1,620 에서만 남는데
    # 전부 룰이 **의도적으로** 하한을 낮춘 조합이다(주당 시간 ÷ 빈도 < 15분).
    #   근거: docs/experiments/rubric-first-plan-v1.md §1
    #   핀:   tests/test_first_plan_verifier_invariants.py (불변식이 깨지면 거기서 먼저 터진다)
    #
    # ⚠️ **그렇다고 지금 v3 의 1·2번을 지우지 마라.** 그 두 항목이 있는 v3 가 L1-7B 의
    # **측정 기준선**이다. 먼저 지우면 "검토기 층이 순이득인가"(M33)를 비교할 대상이 사라진다.
    # 삭제는 v4 에서 하고, v4 는 `focus_capacity` 변수를 **아예 넘겨받지 않는다** — 변수를
    # 주고 "보지 마라"고 쓰는 것으로는 지킨다는 보장이 없기 때문이다(루브릭 §1.2).
    focus_capacity = str(prompt_vars.get("focus_capacity", session_length))
    gp = state["goal_plan"]
    if gp is None:
        return {
            "goal_nodes_json": "[]",
            "action_items_json": "[]",
            "session_length": session_length,
            "focus_capacity": focus_capacity,
            "time_policy_summary": time_policy_summary,
            "conflict_report": "분해 결과 없음",
        }
    # 정책 위반(분해) + 룰 스케줄러 배치 실패(schedule_blocks)를 함께 검토 대상으로 전달.
    conflict_parts = [f"{v.node_id}: {v.reason}" for v in gp.policy_violations]
    conflict_parts.extend(state["schedule_warnings"])
    conflicts = "; ".join(conflict_parts) if conflict_parts else "충돌 없음"
    # ⚠️ **'이어가기' 확장 구간은 검토기에게 아예 넘기지 않는다** (#436).
    #
    # `extend_action_plan_to_horizon` 이 마감까지 채우려고 덧붙이는 카드는 제목이
    # "{목표} 21회차" 로 서로 같고 first_step 이 "지난 회차에서 이어서 5분만 시작하기" 로
    # 여덟 장 모두 동일하다 — **규칙이 의도적으로 그렇게 만든다.** 내용을 지금 지어내면
    # 사용자가 안 정한 걸 정해 버리기 때문이다(루브릭 §D1: "반려 금지").
    #
    # M33 3-arm 실측: 반려 9건이 **전부** 확장 구간을 가진 계획에서 나왔고, 확장이 없는
    # 40케이스 120회에서는 0건이었다. 그중 8건은 피드백이 이 구간을 명시적으로 지목했다
    # ("이어하기로 추가된 회차별 … 제목과 첫 시작 단계가 다소 추상적이에요").
    # 검토기는 정체를 **알아보고도** 결함이라 말한다 — 혼동이 아니라 계약 충돌이다.
    #
    # ⚠️ **프롬프트에 "지적하지 마라"로 쓰지 않는 이유**는 루브릭 §1.2 다:
    #
    #     "프롬프트에 '검사하지 마라'가 아니라 **변수 자체를 넘기지 않는** 방식으로
    #      강제한다 — 변수가 있으면 지시를 어긴다(실측 다수)."
    #
    # v4 가 `focus_capacity` 에 이미 쓴 수법과 같다. 면제 조항 자체는
    # `plan_quality_eval.v4.md` 에 글자 그대로 있지만, 그건 오프라인 평가용 프롬프트라
    # 프로덕션 경로(`planning/plan_quality` → v3)에는 없다.
    #
    # 분해가 통째로 비어 확장이 계획 전부를 채운 경우에는 검토기가 **빈 계획**을 보고
    # 반려한다 — 그게 맞다. 그건 확장의 문제가 아니라 분해 실패다.
    nodes = [n for n in gp.goal_nodes if not first_plan_adapter.is_continuation_node(n.node_id)]
    items = [a for a in gp.action_items if not first_plan_adapter.is_continuation_node(a.node_id)]
    return {
        "goal_nodes_json": json.dumps([n.model_dump() for n in nodes], ensure_ascii=False),
        "action_items_json": json.dumps([a.model_dump() for a in items], ensure_ascii=False),
        "session_length": session_length,
        "focus_capacity": focus_capacity,
        "time_policy_summary": time_policy_summary,
        "conflict_report": conflicts,
    }


async def review_plan(state: FirstPlanState, config: RunnableConfig) -> FirstPlanState:
    """REVIEWING (LLM ④) — 플랜 품질 독립 검토. 미승인 시 재계획 cycle.

    `planning/plan_quality` 변수 계약을 `_review_variables` 로 채운다 → 리뷰 LLM 실제 실행
    (과거 `variables={}` 는 render 실패로 항상 룰 승인 fallback 이었다).
    """
    settings = get_settings()
    result = await aiClient.run(
        module="planning",
        schema=PlanReview,
        # ⚠️ **버전을 명시한다 — `latest()` 에 맡기지 않는다.**
        # 버전을 생략하면 registry 가 최신 활성 버전으로 해석한다(`prompts/registry.py`
        # 모듈 docstring). 그러면 누군가 `plan_quality.v4.md` 를 **파일로 만들기만 해도**
        # 프로덕션 ④층이 그 순간 조용히 갈아탄다 — 평가용으로 쓴 프롬프트가 배포 없이
        # 사용자에게 나가는 것이다. 오프라인 v4 후보를 `plan_quality_eval.v4.md` 라는
        # **다른 이름**으로 둔 것도 같은 위험을 피하려던 것이고, 이 핀은 그 우회를
        # 이름 규칙이 아니라 구조로 만든다. 승격은 이 줄을 고치는 **의도된 변경**이어야
        # 한다. `tests/test_plan_quality_version_pin.py` 가 이 계약을 지킨다.
        prompt_id="planning/plan_quality@v3",
        fallback=lambda: _rule_review(state),
        timeout=settings.llm_planning_timeout_seconds,
        variables=_review_variables(state),
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
        thinking_budget=settings.llm_planning_thinking_budget,
    )
    return {
        **state,
        "review": result.value,
        "replan_count": state["replan_count"] + 1,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Conditional edge — 순수 함수.
# ─────────────────────────────────────────────────────────────────────────────


def should_replan(state: FirstPlanState) -> Literal["replan", "approve"]:
    """리뷰 미승인 + 재계획 한도(2회) 미도달이면 재계획, 아니면 HITL 로 진행."""
    review = state["review"]
    if review is not None and not review.approved and state["replan_count"] < MAX_REPLAN:
        return "replan"
    return "approve"


def _past_review_deadline(state: FirstPlanState) -> bool:
    started = state.get("started_at")
    if started is None:
        return False
    return _time.monotonic() - started > _REVIEW_DEADLINE_SECONDS


def after_schedule(state: FirstPlanState) -> Literal["review", "done"]:
    """배치 뒤 검토를 **시작할지** — 그래프 엣지 전용(오프라인 하네스는 노드를 직접 부른다).

    안 하는 두 경우 (planB-4):
    - 분해가 다시 불러도 같은 이유로 폴백했다(`_RETRY_FUTILE_FALLBACKS`). 자리표시자를 검토에
      넘기면 반려 → 재분해로 같은 실패를 또 겪는다 — 타임아웃이면 스피너가 몇 분 더 돈다.
    - 이미 `_REVIEW_DEADLINE_SECONDS` 를 넘겼다.
    검토를 건너뛰는 건 검토 폴백(`_rule_review`, 그대로 승인)과 같은 결과다 — HITL 이 최종 게이트.
    """
    if state.get("decompose_fallback_reason") in _RETRY_FUTILE_FALLBACKS:
        return "done"
    # 교재 카드 계획은 결정적이다 — 반려돼 다시 분해해도 같은 계획이 나온다.
    if state.get("curriculum_plan"):
        return "done"
    if _past_review_deadline(state):
        return "done"
    return "review"


def after_review(state: FirstPlanState) -> Literal["replan", "approve"]:
    """검토 뒤 재분해 여부 — `should_replan` 에 시간 상한만 더한다(그래프 엣지 전용).

    `should_replan` 자체는 순수 판정으로 둔다 — M33 하네스(`scripts/l1_7b_m33_run.py`)가
    반려 여부를 그걸로 재는데, 벽시계가 끼면 같은 원자료가 실행 속도에 따라 달리 채점된다.
    """
    if should_replan(state) == "replan" and not _past_review_deadline(state):
        return "replan"
    return "approve"


def plan_ai_source(final: Mapping[str, Any]) -> Literal["llm", "rule"]:
    """응답 `aiSource` — **보여 주는 계획을 누가 만들었는가**(마지막 분해 기준).

    예전엔 `used_fallback`(어느 LLM 노드든 폴백하면 참)을 그대로 썼다. 그래서 분해는 LLM 이
    다 했는데 검토만 타임아웃나도 '룰 기반' 으로 표시됐다(planB-5). 검토 폴백은 '그대로
    승인' 이라 계획 내용과 무관하다. `used_fallback` 은 M33 하네스가 검토 폴백 감지에 쓰므로
    뜻을 바꾸지 않고 둔다.
    """
    return "rule" if final.get("decompose_fallback_reason") is not None else "llm"


def build_first_plan_graph() -> CompiledStateGraph[
    FirstPlanState, Any, FirstPlanState, FirstPlanState
]:
    """Sequential + 룰 fallback StateGraph 컴파일."""
    graph = StateGraph(FirstPlanState)
    graph.add_node("validate_inputs", validate_inputs)
    graph.add_node("decompose_goal", decompose_goal)
    graph.add_node("schedule_blocks", schedule_blocks)
    graph.add_node("review_plan", review_plan)

    graph.set_entry_point("validate_inputs")
    graph.add_edge("validate_inputs", "decompose_goal")
    graph.add_edge("decompose_goal", "schedule_blocks")
    graph.add_conditional_edges(
        "schedule_blocks",
        after_schedule,
        {"review": "review_plan", "done": END},
    )
    graph.add_conditional_edges(
        "review_plan",
        after_review,
        {"replan": "decompose_goal", "approve": END},
    )
    return graph.compile()
