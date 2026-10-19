"""Ultimate Goal Outcome 어댑터 (#6-B) — 궁극목표 인터뷰 경계 계약.

`build_ultimate_outcome` 은 Interview 그래프 터미널(`kind="ultimate"`)에서 호출되는
**순수 함수**다 (`interview_adapter.build_outcome` 과 대칭):
- slot_answers(인터뷰가 누적한 정규화 답) → `UltimateGoalOutcome` 결정적 투영.
- LLM 호출 0회 / DB 무관 → 경계에서 8s timeout·rate limit 실패 표면이 없다.

`InterviewOutcome` 을 확장하지 않고 별도 스키마(`schemas/ultimate_goal.py`)를 쓰는 이유:
1. `core_goals: list[GoalCandidate] = Field(min_length=1)` 때문에 궁극목표 세션도
   GoalCandidate 를 1개 만들어야 해 `PLACEHOLDER_GOAL_TITLE`("(미입력 목표)") 유령 목표가
   부활한다(`interview_adapter.is_placeholder_goal` 이 정확 일치 판정이라 우회 불가).
2. `availability`/`preferences` 도 필수라 묻지도 않은 활동창을 `_DEFAULT_ACTIVITY`(09:00~23:00)
   으로 지어내게 된다.
3. `schema_version: Literal["1.0"]` 를 bump 하면 `plan_drafts.payload` JSONB 에 스냅샷된
   기존 outcome 역직렬화가 깨진다(72h TTL 이라도 무중단 배포 창에 걸릴 수 있다).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import date
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.goal import Goal
from reaction_backend.domain import slot_value as sv
from reaction_backend.orchestrator import interview_adapter
from reaction_backend.orchestrator.goal_policy import clip_goal_title
from reaction_backend.orchestrator.interview_catalog import ULTIMATE_REQUIRED_SLOT_KEYS
from reaction_backend.repositories.interview_repo import InterviewRepo
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.interview import InterviewEndReason
from reaction_backend.schemas.ultimate_goal import UltimateGoalOutcome

# 궁극목표 → 계획 인터뷰로 이월하는 슬롯 — ultimate.* 는 몇 년에 한 번 바뀌는 값이라 **전량**
# 이월 대상이다. 계획 인터뷰의 `interview_adapter.CARRY_OVER_SLOT_KEYS`(제외 목록이 아니라
# "이 슬롯들만 이어받는다"는 허용 목록)와 방향이 반대라 별도 상수로 둔다 — 기존 상수 주석이
# "매 주기 바뀌는 goals.* 제외" 라는 정반대 전제로 쓰였으므로 재사용하면 의미가 뒤집힌다.
ULTIMATE_CARRY_OVER_SLOT_KEYS: frozenset[str] = frozenset(
    f"ultimate.{name}"
    for name in (
        "statement",
        "domain",
        "horizon",
        "measure",
        "success_image",
        "identity",
        "current_position",
        "pillars_hint",
        "constraints",
        "values",
        "assets",
        "role_model",
    )
)

_NOT_SET = "아직 정하지 않음"


_HORIZON_YEARS: dict[str, int | None] = {
    "3년": 3,
    "5년": 5,
    "7년": 7,
    "10년": 10,
    "10년 이상": 10,
    "기한 없음": None,
}


def _horizon_years(value: Mapping[str, Any] | None) -> int | None:
    chip = sv.first_chip(value)
    return _HORIZON_YEARS.get(chip) if chip else None


def summary_variables(slot_answers: Mapping[str, Mapping[str, Any] | None]) -> dict[str, str]:
    """P3(`interview/ultimate_summary`) 프롬프트 변수 + 룰 폴백 공용 — 슬롯에서 사람이 읽을

    문자열로 추출(룰). `interview._summary_variables` 의 궁극목표판.
    """
    return {
        "statement": sv.text(slot_answers.get("ultimate.statement")) or _NOT_SET,
        "measure": sv.text(slot_answers.get("ultimate.measure")) or _NOT_SET,
        "horizon": sv.first_chip(slot_answers.get("ultimate.horizon")) or _NOT_SET,
        "identity": sv.text(slot_answers.get("ultimate.identity")) or _NOT_SET,
        "current_position": sv.text(slot_answers.get("ultimate.current_position")) or _NOT_SET,
        "constraints": ", ".join(sv.items(slot_answers.get("ultimate.constraints"))) or _NOT_SET,
    }


def build_ultimate_outcome(
    *,
    session_id: str,
    slot_answers: Mapping[str, Mapping[str, Any] | None],
    ambiguity_final: float,
    end_reason: InterviewEndReason,
    analysis_source: Literal["llm", "rule"],
) -> UltimateGoalOutcome:
    """slot_answers → UltimateGoalOutcome. LLM 0회·순수함수.

    빈 필수 슬롯은 빈 문자열/빈 리스트로 두고 `unresolved_slots` 에 키를 남긴다 — `GoalCandidate`
    처럼 `min_length=1` 계약이 없어 placeholder sentinel 이 필요 없다(설계서 §5.4 근거 1).
    스킵으로 닫힌 슬롯도 남긴다(#499).
    """
    # 세는 규칙은 계획 인터뷰와 **같은 함수** — ultimate.* 9개엔 지금 유도 슬롯이 없어
    # 결과가 같지만, 판정이 갈릴 자리를 애초에 만들지 않는다(#weekly_time 이 그렇게 샜다).
    unresolved = interview_adapter.unresolved_required_keys(
        ULTIMATE_REQUIRED_SLOT_KEYS, slot_answers
    )
    return UltimateGoalOutcome(
        session_id=session_id,
        generated_at=now_kst(),
        end_reason=end_reason,
        ambiguity_final=ambiguity_final,
        analysis_source=analysis_source,
        statement=sv.text(slot_answers.get("ultimate.statement")) or "",
        domain=sv.first_chip(slot_answers.get("ultimate.domain")) or "",
        horizon_years=_horizon_years(slot_answers.get("ultimate.horizon")),
        measure=sv.text(slot_answers.get("ultimate.measure")) or "",
        success_image=sv.text(slot_answers.get("ultimate.success_image")) or "",
        identity_note=sv.text(slot_answers.get("ultimate.identity")) or "",
        current_position=sv.text(slot_answers.get("ultimate.current_position")) or "",
        constraints=sv.items(slot_answers.get("ultimate.constraints")),
        values=sv.chips(slot_answers.get("ultimate.values")),
        assets=sv.text(slot_answers.get("ultimate.assets")),
        pillars_hint=sv.items(slot_answers.get("ultimate.pillars_hint")),
        unresolved_slots=unresolved,
    )


async def resolve_outcome(
    repo: InterviewRepo, user_id: uuid.UUID, *, inline: UltimateGoalOutcome | None = None
) -> UltimateGoalOutcome | None:
    """`POST /goals/ultimate`(U1) · 만다라 생성(U2/U3/U5) 공용 outcome 확정.

    우선순위: ① 인라인 `outcome`(인터뷰 종료 턴이 이미 돌려준 값을 FE 가 그대로 실어 보냄,
    LLM 0회) → ② 최근 '정상 종료' `kind="ultimate"` 세션에서 slot_answers 를 결정적으로
    재투영. `routes/planning.py::_resolve_outcome`(계획 인터뷰)와 같은 패턴이다 — FE 가
    새로고침 등으로 값을 잃어도 서버가 복구할 수 있게, 그리고 만다라 재생성처럼 인터뷰
    직후가 아닌 시점에도 같은 소스로 재현 가능하게 한다. 완료된 세션이 없으면 None
    (호출자가 422 로 안내).
    """
    if inline is not None:
        return inline
    row = await repo.get_latest_finished(user_id, kind="ultimate")
    if row is None:
        return None
    slot_rows = await repo.list_slot_answers(row.id)
    slot_answers = {r.slot_key: r.value for r in slot_rows if r.value is not None}
    return build_ultimate_outcome(
        session_id=str(row.id),
        slot_answers=slot_answers,
        ambiguity_final=(float(row.ambiguity_final) if row.ambiguity_final is not None else 0.0),
        end_reason=cast(InterviewEndReason, row.end_reason or "completed"),
        analysis_source="rule" if row.used_fallback else "llm",
    )


def _deadline_from_horizon(horizon_years: int | None) -> date | None:
    """`ultimate.horizon`(3/5/7/10/10+ 년) → 실제 마감일(ADR-0008 §2).

    지금까지 `horizon_years` 는 `mandala_adapter.context_from_ultimate` 의 프롬프트
    문자열로만 쓰였다 — 실제 날짜가 된 적이 없어 계획 지평 계산(`_horizon_weeks`)이 만다라
    유래 목표를 늘 "마감 없음"으로 봤다. "기한 없음"(`None`)이면 그대로 `None`.

    2/29 + N년 뒤가 평년이면 `date.replace` 가 `ValueError` — 2/28 로 보정한다.
    """
    if horizon_years is None:
        return None
    today = now_kst().date()
    try:
        return today.replace(year=today.year + horizon_years)
    except ValueError:
        return today.replace(month=2, day=28, year=today.year + horizon_years)


async def materialize_ultimate_goal(
    session: AsyncSession, *, user_id: uuid.UUID, outcome: UltimateGoalOutcome
) -> Goal:
    """`UltimateGoalOutcome` → 영속 `Goal`(U1). 이미 있으면 **같은 행을 갱신**(신규 생성 X).

    `status="active"`, `goal_tier="parked"` 를 생성 시점부터 고정하고 `proposed` 를 경유하지
    않는다(§3.2) — Focus≤3/Maintain≤5 한도(`goal_repo.count_by_tier`)는 tier 별로 세므로
    parked 는 애초에 계산 대상이 아니고, `expire_stale_proposed`/`supersede_proposed_goals`
    같은 잠정-목표 정리 경로(`status=='proposed'` 전제)를 아예 타지 않는다.

    `category` 는 항상 `"other"` 로 둔다 — `ultimate.domain`(8축 렌즈: 역량/기술·방법/체력·
    컨디션/멘탈·루틴/환경·도구/사람·피드백/점검·기록/운·기회)은 `GOAL_CATEGORY_VALUES`(study/
    health/... 9종)와 taxonomy 자체가 달라 억지로 매핑하면 의미 없는 카테고리가 붙는다.
    궁극목표는 여러 카테고리를 가로지르는 게 정상이라 애초에 하나로 분류될 대상이 아니다.

    "같은 행" 판별은 `Goal.is_ultimate`(사용자당 최대 1개, 부분 유니크 인덱스로 DB 도 보장)
    이다 — `status='active' AND goal_tier='parked'` 만으로는 일반 목표와 구분이 안 된다
    (`POST /goals` 가 `goal_tier` 를 그대로 받으므로).
    """
    stmt = select(Goal).where(
        Goal.user_id == user_id, Goal.is_ultimate.is_(True), Goal.archived_at.is_(None)
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    # 인터뷰 문장은 길이 제한이 없는데 `goals.title` 은 200자다 — 그대로 넣으면 긴 문장이
    # 500 을 내고, 서버 fallback 도 같은 문장을 다시 투영해 재시도가 영영 실패했다. 제목만
    # 줄이고 원문은 인터뷰 기록(slot_answers)에 그대로 남는다.
    title = clip_goal_title(outcome.statement)
    if existing is not None:
        existing.title = title
        existing.why_now = outcome.success_image
        existing.deadline = _deadline_from_horizon(outcome.horizon_years)
        await session.flush()
        return existing

    goal = Goal()
    goal.user_id = user_id
    goal.title = title
    goal.category = "other"
    goal.goal_tier = "parked"
    goal.status = "active"
    goal.is_ultimate = True
    goal.why_now = outcome.success_image
    goal.deadline = _deadline_from_horizon(outcome.horizon_years)
    # DB server_default 를 믿지 않고 명시한다 — `schemas.goals.Goal.priority_level` 이
    # 필수(int, Optional 아님)라, refresh 없이 이 값을 바로 응답에 실어도 안전해야 한다.
    goal.priority_level = 3
    session.add(goal)
    await session.flush()
    return goal


__all__ = [
    "ULTIMATE_CARRY_OVER_SLOT_KEYS",
    "build_ultimate_outcome",
    "materialize_ultimate_goal",
    "resolve_outcome",
    "summary_variables",
]
