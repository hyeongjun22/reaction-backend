"""만다라 축 → 다음 2주 계획 시드 (U14, ADR-0008 §3·§8 "G") — LLM 0콜, DB 무관 순수 함수.

만다라트를 그려 놓고 축을 승격까지 해도, 지금까지는 그 축으로 계획이 서지 않았다.
`POST /plans/generate` 의 시드는 **계획 인터뷰 outcome** 이고 그 안의 `is_heaviest` 는
인터뷰 당시 사용자가 고른 목표라, 만다라트를 다시 세워 축이 바뀌어도 계획은 여전히 옛
목표를 분해한다. 축 제목이 `goals.heaviest` **보기**로 들어가는 배선(ADR-0008 §8 "B")은
있지만 그건 *새 인터뷰를 다시 할 때* 얘기다.

이 모듈이 그 사이를 잇는다 — 축의 칸 8개를 계획 뼈대(마일스톤)로 넘기고, 계획 인터뷰를 아직
안 한 사용자를 위해 온보딩 프로필을 2순위 시드로 만든다. **지어내지 않는다** — 활동 시간대를
어디서도 모르면 호출자가 422 로 안내한다(v2.01 의 교훈: 지어낸 답으로 슬롯을 닫느니 열어
두고 묻는 게 낫다).

⚠️ **`core_goals` 를 갈아끼우는 부분은 여기 없다** — [`goal_cycle`](goal_cycle.py) 로 옮겼다.
축(`GoalNode`)은 #373 에서 `why_text` 읽기가 빠진 뒤로 그 규칙에 **아무 값도 기여하지 않고**
승격된 `Goal` 만 쓰이고 있었다. 즉 처음부터 "축" 이 아니라 "목표" 단위 규칙이었고, 주간
리포트의 다음 주기 제안(#398)도 같은 것을 필요로 한다. 한 벌만 둔다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from typing import Any
from uuid import UUID

from reaction_backend.db.models.behavioral_profile import BehavioralProfile
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.interaction_style import InteractionStyle
from reaction_backend.domain import slot_value as sv
from reaction_backend.orchestrator import profile_memory
from reaction_backend.schemas.planning import MilestoneDraft


def cells_as_milestones(
    cells: Sequence[GoalNode], *, exclude_ids: Set[UUID] = frozenset()
) -> list[MilestoneDraft]:
    """축의 칸(depth=2) → 마일스톤 뼈대. 만다라트가 계획을 실제로 이끄는 지점이다.

    칸 제목이 곧 마일스톤 제목이다 — AI 가 다시 지어내게 두면 사용자가 만다라트에서 확정한
    분해를 계획이 무시하게 된다. `summary` 는 비운다 — 칸에는 요약에 해당하는 필드가 없고,
    없는 문장을 지어내지 않는다. 빼는 칸:
    - 완료 표시된 칸 — 이미 끝낸 것을 다시 계획하지 않는다.
    - 반복형 칸(`exclude_ids` — 호출자가 습관 링크로 넘긴다) — "1일 1문제" 처럼 끝이 없는 칸은
      계획(카드)으로 내려보내지 않고 습관 횟수로만 센다(ADR-0008 §1). 예전엔 마일스톤이 됐다.
    - 규칙이 채운 자리표시 칸(`source="rule"`, 예: "건강 1단계") — AI 가 못 채워 규칙이 넣은
      이름이라, 계획의 뼈대로 쓰면 "만다라트에서 가져온 단계: 건강 1단계" 가 뜬다. 사용자가
      고친 칸은 `source="user"` 라 그대로 들어간다.

    다 빠져서 비면 호출자가 자동 분해로 넘어간다(`or None`).

    ADR-0007 §1 커서 모델이 이 목록 중 **앞쪽 일부만** 이번 2주에 담는다(나머지는 다음
    주기가 이어받는다) — 여기서 미리 자르지 않는다.
    """
    return [
        MilestoneDraft(title=c.title, summary="")
        for c in sorted(cells, key=lambda c: c.order_index)
        if c.completed_at is None and c.id not in exclude_ids and c.source != "rule"
    ]


def slots_from_profile(
    *,
    behavioral: BehavioralProfile | None,
    interaction: InteractionStyle | None,
    focus_mode_prefs: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """온보딩 프로필 → `interview_adapter.build_outcome` 입력 슬롯.

    계획 인터뷰를 아직 안 한 사용자가 만다라 축으로 주기를 열 때의 **2순위 시드**다. 값을
    지어내는 게 아니라 사용자가 온보딩·설정에서 **직접 넣은 값**을 되돌린다 —
    `persist_profile_from_outcome` 이 인터뷰 답을 프로필에 저장하고 설정 화면
    (`routes/settings.py`)이 그걸 편집하므로, 프로필은 이 값들의 최신 진실이다.

    `profile_memory.seed_slots_from_profile`(재인터뷰 시드)를 그대로 쓰고 **활동창만 더한다**.
    그쪽이 활동창을 안 만드는 이유는 그 결과가 `interview_slot_answers` 에 UPSERT 되기 때문인데
    (옵션에 없는 표기가 '사용자의 답'으로 남는 사고, v2.01), 여기 결과는 이 계획 한 번을 위한
    임시 값이라 저장되지 않는다 — 그래서 안전하게 포함할 수 있다.

    채우지 못한 슬롯은 그냥 비운다. `build_outcome` 이 문서화된 기본값으로 채우고 그 키를
    `unresolved_slots` 에 남기므로, 무엇을 추정했는지가 응답에 그대로 드러난다.
    """
    slots = profile_memory.seed_slots_from_profile(
        behavioral=behavioral, interaction=interaction, focus_mode_prefs=focus_mode_prefs
    )
    if behavioral is not None:
        start, end = behavioral.preferred_start_time, behavioral.preferred_end_time
        if start is not None and end is not None:
            slots["time.activity_window"] = sv.dump(
                sv.RangeAnswer(start.strftime("%H:%M"), end.strftime("%H:%M"))
            )
    return slots


def has_usable_profile(behavioral: BehavioralProfile | None) -> bool:
    """이 프로필로 계획을 배치할 수 있는가 — **활동 시간대를 아는가**가 유일한 기준.

    피크 시간·집중 길이는 없으면 기본값으로 굴러가지만, 활동창은 "언제 배치해도 되는가" 라
    모르면 배치 자체가 추측이 된다. 그래서 여기가 비면 호출자가 422 로 인터뷰를 안내한다.
    """
    return (
        behavioral is not None
        and behavioral.preferred_start_time is not None
        and behavioral.preferred_end_time is not None
    )


__all__ = [
    "cells_as_milestones",
    "has_usable_profile",
    "slots_from_profile",
]
