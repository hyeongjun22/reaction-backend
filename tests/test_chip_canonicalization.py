"""칩 답 정규화 — 저장 경계의 단일 관문 (#validate-chip-answers).

칩 값이 슬롯 옵션으로 검증되지 않아 사고가 두 번 났다. 둘 다 파서가 예상 못 한 문자열을
만나 숫자를 잘못 읽은 것이다:

- v2.00: `"2시간 이상"` → **2분** (세션 길이 상한이 2분이 돼 계획 전체가 붕괴)
- v2.01: `"30분"` 이 주당 시간 슬롯에 → **30시간**(주 1800분)

파서를 하나씩 고치는 대신 저장 경계에서 어휘를 좁힌다. 이 파일은 그 관문이 실제로
닫혀 있는지 지킨다.
"""

from __future__ import annotations

from typing import Any

from reaction_backend.orchestrator.interview import _coerce_normalized
from reaction_backend.orchestrator.interview_catalog import (
    PLAN_CATALOG,
    ULTIMATE_CATALOG,
    canonical_chip,
    canonical_chip_values,
)
from reaction_backend.orchestrator.interview_runner import coerce_client_answer

FOCUS = PLAN_CATALOG.by_key["energy.focus_duration"]
WEEKLY = PLAN_CATALOG.by_key["goals.weekly_time"]
PEAK = PLAN_CATALOG.by_key["time.peak_window"]


# ───────────────────────── canonical_chip ─────────────────────────


def test_exact_option_passes_through() -> None:
    for option in FOCUS.options:
        assert canonical_chip(FOCUS, option) == option


def test_whitespace_variants_snap_to_the_option() -> None:
    """LLM 이 `"2시간이상"` 처럼 붙여 쓰거나 앞뒤 공백을 남겨도 옵션 표기로 맞춘다."""
    assert canonical_chip(FOCUS, "2시간이상") == "2시간 이상"
    assert canonical_chip(FOCUS, "  50분  ") == "50분"


def test_same_duration_different_notation_snaps() -> None:
    """`"120분"` ↔ `"2시간 이상"` — 표기는 달라도 같은 시간이면 옵션 표기로.

    `profile_memory.seed_slots_from_profile` 이 프로필의 분 값을 `f"{n}분"` 으로 되돌리는데,
    그 표기가 옵션에 없어 그대로 저장돼 왔다(v2.01 시드 루프).
    """
    assert canonical_chip(FOCUS, "120분") == "2시간 이상"
    assert canonical_chip(FOCUS, "2시간") == "2시간 이상"


def test_values_outside_the_option_set_are_rejected() -> None:
    """옵션에 없는 값은 None — 이게 두 사고의 문을 닫는 지점이다."""
    assert canonical_chip(FOCUS, "30분") is None  # 이 슬롯의 보기가 아니다
    assert canonical_chip(FOCUS, "아무 문자열") is None
    assert canonical_chip(FOCUS, "") is None
    # 주당 시간 슬롯에 분 단위 답이 오면 거부 — 예전엔 "30분" → 30시간(주 1800분)이 됐다.
    assert canonical_chip(WEEKLY, "30분") is None
    assert canonical_chip(WEEKLY, "1시간 30분") is None


def test_categorical_slot_matches_only_exactly() -> None:
    """시간이 아닌 칩(오전/오후/…)은 시간 대조가 없으니 정확·공백 일치만."""
    assert canonical_chip(PEAK, "저녁") == "저녁"
    assert canonical_chip(PEAK, "밤") is None


def test_slot_without_options_is_passed_through() -> None:
    """런타임에 보기를 만드는 슬롯(`goals.heaviest`)은 대조할 대상이 없다 — 다듬기만."""
    heaviest = PLAN_CATALOG.by_key["goals.heaviest"]
    assert heaviest.options == ()
    assert canonical_chip(heaviest, "  캡스톤  ") == "캡스톤"


def test_ultimate_catalog_slots_are_covered_too() -> None:
    """궁극목표 인터뷰도 같은 관문을 쓴다 — 카탈로그가 둘이라 한쪽만 지키면 반쪽이다."""
    horizon = ULTIMATE_CATALOG.by_key["ultimate.horizon"]
    assert horizon.options
    for option in horizon.options:
        assert canonical_chip(horizon, option) == option
    assert canonical_chip(horizon, "100년") is None


# ───────────────────────── 저장 경계 세 곳 ─────────────────────────


def test_harvest_output_is_rejected_when_off_option() -> None:
    """harvest LLM 출력은 **버린다** — 슬롯이 열린 채 남아 실제 보기로 다시 묻는다.

    `None` 을 돌려주는 게 핵심이다: 호출부가 그걸 '미리 채우지 않음' 으로 읽는다.
    """
    assert _coerce_normalized("chip", "2시간 이상", slot=FOCUS) == {
        "type": "chip",
        "values": ["2시간 이상"],
    }
    assert _coerce_normalized("chip", "120분", slot=FOCUS) == {
        "type": "chip",
        "values": ["2시간 이상"],
    }
    # 사고의 입구였던 값들
    assert _coerce_normalized("chip", "30분", slot=FOCUS) is None
    assert _coerce_normalized("chip", 120, slot=FOCUS) is None  # 단위 없는 숫자
    assert _coerce_normalized("chip", "30분", slot=WEEKLY) is None


def test_harvest_without_slot_keeps_old_behavior() -> None:
    """슬롯을 모르면(카탈로그에 없는 키) 대조할 대상이 없으므로 다듬기만 한다."""
    assert _coerce_normalized("chip", "아무거나", slot=None) == {
        "type": "chip",
        "values": ["아무거나"],
    }


def test_client_answer_is_normalized_but_never_dropped() -> None:
    """사용자가 방금 누른 답은 **버리지 않는다** — 버리면 같은 질문을 또 하는 루프가 된다.

    카탈로그 옵션은 시간이 지나며 바뀐다(주당 시간 척도가 한 번 개편됐다). 옛 척도를 든
    클라이언트를 그 루프에 빠뜨릴 수 없어, 여기서는 표기 정규화만 얻고 거부는 하지 않는다.
    """
    assert coerce_client_answer(["2시간이상"], slot=FOCUS) == {
        "type": "chip",
        "values": ["2시간 이상"],
    }
    # 옛 척도 값이 와도 살아남는다(거부하면 인터뷰가 멈춘다).
    legacy: Any = {"type": "chip", "values": ["6시간"]}
    assert coerce_client_answer(legacy, slot=WEEKLY) == {"type": "chip", "values": ["6시간"]}


def test_client_answer_without_slot_is_unchanged() -> None:
    assert coerce_client_answer(["아무거나"], slot=None) == {"type": "chip", "values": ["아무거나"]}
    assert coerce_client_answer("자유 서술", slot=FOCUS) == {"type": "text", "raw": "자유 서술"}


def test_duplicate_chips_collapse_after_normalization() -> None:
    """`"2시간 이상"` 과 `"120분"` 은 같은 답 — 정규화 후 중복이 남으면 안 된다."""
    assert canonical_chip_values(FOCUS, ["2시간 이상", "120분", "2시간"]) == ["2시간 이상"]
