"""First Plan — 마일스톤 생성 (Stage A, #milestones Phase 2).

목표를 3~5개 중간 목표(마일스톤)로 나눠 사용자에게 확인받는 단계. 세부 세션 분해(Stage B,
`first_plan.decompose_goal`) 전에, 사용자가 계획의 **뼈대**를 수정·확정하게 한다.

- LLM 1콜(`planning/plan_milestones`) + 실패 시 룰 폴백(빈 계획 방지).
- 그래프를 돌지 않는 가벼운 단독 호출 — 라우트(`POST /plans/milestones`)가 직접 부른다.
- 모든 LLM 은 `aiClient.run` 단일 게이트만 (AGENTS §2).
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.config import get_settings
from reaction_backend.llm import aiClient
from reaction_backend.orchestrator import curriculum_plan, materials_resolver
from reaction_backend.orchestrator.first_plan_adapter import (
    context_from_outcome,
    full_horizon_weeks,
    heaviest_goal,
    heaviest_goal_or_none,
    restore_user_phrases_in_milestones,
)
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.interview import InterviewOutcome
from reaction_backend.schemas.planning import MilestoneDraft, MilestonePlan


def curriculum_milestones(
    outcome: InterviewOutcome, *, cycle_weeks: int, target_date: date | None = None
) -> list[MilestoneDraft] | None:
    """목표에 교재 카드가 확정돼 있으면 그 책의 단원 단계를 마일스톤으로 — LLM 0회.

    카드가 없으면 `None` — 호출자가 `generate_milestones`(LLM)로 간다. 라우트가 이걸 먼저
    부르는 이유는 `aiSource` 를 정직하게 적기 위해서다: 카드 마일스톤은 폴백이 아니지만 LLM
    산출물도 아니라 `"rule"` 이다(`generate_milestones` 의 `fell_back` 과 뜻이 다르다).
    """
    books = curriculum_plan.books_for(heaviest_goal_or_none(outcome))
    if not books:
        return None
    return curriculum_plan.milestones(
        books,
        full_weeks=full_horizon_weeks(target_date or now_kst().date(), outcome.horizon),
        cycle_weeks=cycle_weeks,
    )


def _rule_milestones(outcome: InterviewOutcome) -> MilestonePlan:
    """LLM 실패 시 룰 폴백 — heaviest 목표를 준비→진행→마무리 3단계로 환원(빈 응답 방지)."""
    heaviest = heaviest_goal(outcome)
    title = heaviest.title
    return MilestonePlan(
        milestones=[
            MilestoneDraft(title=f"{title} 준비·기초", summary="필요한 기초와 준비를 갖춘다"),
            MilestoneDraft(title=f"{title} 핵심 진행", summary="핵심 내용을 실제로 진행한다"),
            MilestoneDraft(
                title=f"{title} 마무리·점검",
                summary=heaviest.success_image or "완료 상태로 마무리한다",
            ),
        ]
    )


async def generate_milestones(
    *,
    outcome: InterviewOutcome,
    session: AsyncSession | None = None,
    tone_mode: str | None = None,
    user_id: UUID | None = None,
) -> tuple[list[MilestoneDraft], bool]:
    """목표 컨텍스트 → 중간 목표 3~5개. 반환: (마일스톤 목록, 룰 폴백 여부).

    decompose 와 같은 prompt_vars(현재수준·성공이미지·접근·자료 등)를 재사용해 방향을 잡는다.

    ⚠️ **분량 프리셋(density)을 받지 않는다 — 마일스톤은 density 로 크기가 정해지지 않는다.**
    이 프롬프트가 쓰는 변수 10개 중 크기에 관한 것은 `total_capacity`(주당 가용 시간 ×
    마감까지 주 수, ADR-0007 §11)와 `session_length` 뿐이고, 둘 다 인터뷰 답에서 나온다.
    density 에서 파생되는 값들(`sessions_per_week`/`total_minutes`/`total_sessions`/
    `session_count_rule`)은 decompose 전용이라 이 템플릿에 아예 등장하지 않는다.

    예전에는 라우터가 `density=body.density` 를 넘겼는데, 그 값은 `context_from_outcome`
    까지만 가고 렌더 결과에는 한 글자도 영향을 주지 못했다(`tests/prompts` 가 이제 그
    사실을 고정한다). 받아 두면 "마일스톤도 분량을 따라간다" 는 **틀린 기대**가 생긴다 —
    실제로 이 파라미터를 근거로 "뼈대는 큰데 세션만 가벼워진다" 는 없는 결함을 읽어낸
    적이 있다. 실패가 쌓였을 때 **뼈대까지** 줄일지는 배선이 아니라 제품 결정이다(그건
    목표 재협상에 가깝다 — 근거 대장 §5.2 의 L3).

    참고 자료가 링크뿐이면 `validate_inputs` 와 **같은 방식으로** 열어서 넣는다 (#226).
    계획의 뼈대를 정하는 건 이 단계라, 여기서 자료가 빠지면 사용자가 강의계획서를 붙여도
    일반론 마일스톤이 나오고 Stage B 는 그 위에 "추가·삭제·병합·개명 금지" 로 묶인다 —
    자료를 준 사용자에게 링크를 무시한 계획이 나가는 경로였다. 실패하면 예전처럼
    '(없음)' 으로 내려가므로 회귀 위험은 없다. `/plans/generate` 와 fetch 를 공유하진
    않는다 — 별도 사용자 액션이고, 공유하려면 저장소가 필요하다(#226 step 2).
    """
    settings = get_settings()
    heaviest = heaviest_goal_or_none(outcome)
    materials = await materials_resolver.resolve(heaviest.materials_note if heaviest else None)
    # `target_date` 를 넘기지 않으면 마감까지 남은 기간을 계산할 기준이 없어
    # `total_capacity`(ADR-0007 §11)가 "마감 없음" 으로 읽힌다 — 마일스톤 크기를 재는
    # 프롬프트에 그건 치명적이라, 계획 시작일 기본값(오늘 KST)을 명시적으로 넘긴다.
    prompt_vars = context_from_outcome(
        outcome,
        target_date=now_kst().date(),
        fetched_materials=materials.text,
    )["prompt_vars"]
    result = await aiClient.run(
        module="planning",
        schema=MilestonePlan,
        prompt_id="planning/plan_milestones",
        fallback=lambda: _rule_milestones(outcome),
        timeout=settings.llm_planning_timeout_seconds,
        variables=prompt_vars,
        user_id=user_id,
        session=session,
        tone_mode=tone_mode,
        thinking_budget=settings.llm_planning_thinking_budget,
    )
    milestones = list(result.value.milestones)
    if heaviest is not None:
        # 금지어 치환이 사용자 목표 제목·완료 기준을 옮겨 쓴 자리까지 바꿔 놓았으면 되돌린다
        # (planB-3) — 룰 폴백은 '{제목} 준비·기초' 와 완료 기준을 그대로 싣는다. 여기서 확정된
        # 제목은 승인 때 목표의 뼈대로 영속되므로 깨진 문장이 오래 남는다.
        milestones = restore_user_phrases_in_milestones(
            milestones, [heaviest.title, heaviest.success_image]
        )
    return milestones, result.fell_back
