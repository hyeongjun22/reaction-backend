"""`interview._decide_storage` 표 단위 테스트 — 저장 결정 순수 함수 (LLM/async 없음).

validate_answer 의 분기(스킵/핵심/pending/제약/clarity)를 LLM stub 없이 표로 고정한다.
서명: _decide_storage(slot_key, answer_type, last_answer, normalized, clarity, attempts)
       -> (stored: dict|None, filled_now: bool)
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from reaction_backend.orchestrator import interview, interview_catalog
from reaction_backend.orchestrator.interview import _decide_storage, _pending


class _AnySkip:
    """스킵이면 사유와 무관하게 같다 — 이 표는 '스킵으로 닫는가' 를 본다. 사유는
    `test_interview_skip_semantics.test_skip_records_why` 가 고정한다."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, dict) and other.get("type") == "skip"

    def __repr__(self) -> str:
        return "<any skip>"


_SKIP = _AnySkip()

_TEXT = {"type": "text", "raw": "x"}  # 대표 자유서술 raw (내용은 normalized/clarity 로 제어)


@pytest.mark.parametrize(
    ("slot_key", "answer_type", "last_answer", "normalized", "clarity", "attempts", "expected"),
    [
        # 답 미주입(배치 그래프) → 저장·충족 없음
        ("identity.role", "chip", None, None, 0.9, 1, (None, False)),
        # chip: LLM 정규화값 → chip 구조로 저장, 충족
        (
            "identity.role",
            "chip",
            {"type": "text", "raw": "컴공 3학년"},
            "3학년",
            0.2,
            1,
            ({"type": "chip", "values": ["3학년"]}, True),
        ),
        # text 고clarity → 저장·충족
        (
            "goals.success_image",
            "text",
            {"type": "text", "raw": "발표 잘 마치기"},
            "발표 잘 마치기",
            0.9,
            1,
            ({"type": "text", "raw": "발표 잘 마치기"}, True),
        ),
        # text 저clarity·비스킵·비핵심·상한 전 → 재질문(pending)
        (
            "goals.success_image",
            "text",
            {"type": "text", "raw": "음 그냥"},
            None,
            0.1,
            1,
            (_pending(1), False),
        ),
        # text 스킵 의사(비핵심) → 스킵 저장·진행
        (
            "goals.success_image",
            "text",
            {"type": "text", "raw": "없어"},
            None,
            0.0,
            1,
            (_SKIP, True),
        ),
        # 제약 슬롯(chip) LLM 매핑 실패·비스킵·비핵심 → 스킵으로 진행(무루프).
        # ⚠️ 이건 `has_chip_options=False` 일 때의 표다 — 카탈로그에 고정 보기가 있는 칩 슬롯은
        # 라우터가 그 플래그를 켜서 보내고, 그때는 스킵 대신 보기를 들고 다시 묻는다
        # (`tests/test_interview_harvest_and_chips.py`).
        (
            "recovery.tone",
            "chip",
            {"type": "text", "raw": "빨간색으로"},
            None,
            0.1,
            1,
            (_SKIP, True),
        ),
        # 핵심(goals.list) 스킵 신호·상한 전 → 스킵 거부, 재질문(pending)
        ("goals.list", "text", {"type": "text", "raw": "없어"}, "", 0.1, 1, (_pending(1), False)),
        # 핵심(goals.list) 상한 도달·비지 않은 답 → best-effort 채택·진행
        (
            "goals.list",
            "text",
            {"type": "text", "raw": "그냥 뭐라도"},
            "",
            0.1,
            3,
            ({"type": "text", "raw": "그냥 뭐라도"}, True),
        ),
        # 핵심(goals.heaviest) LLM 못 고름·상한 전 → 재질문(pending)
        (
            "goals.heaviest",
            "select",
            {"type": "text", "raw": "이것저것"},
            None,
            0.2,
            2,
            (_pending(2), False),
        ),
        # ── 빈 답 = 명시적 '넘기기' (회귀: "없으면 넘겨도 돼요" 슬롯이 빈 답을 3회 재질문) ──
        # 비핵심 text 빈 답 → 첫 시도에 곧장 스킵 저장·진행
        (
            "goals.materials",
            "text",
            {"type": "text", "raw": ""},
            None,
            0.0,
            1,
            (_SKIP, True),
        ),
        # 공백뿐인 답도 빈 답이다
        (
            "goals.approach",
            "text",
            {"type": "text", "raw": "   "},
            None,
            0.0,
            1,
            (_SKIP, True),
        ),
        # 빈 답에서 LLM 이 값을 '추출'했다 주장해도(has_real 경로) 믿지 않는다 —
        # 사용자는 아무것도 입력하지 않았으므로 스킵이 이긴다.
        (
            "goals.frequency",
            "chip",
            {"type": "text", "raw": ""},
            "주 3회",
            0.9,
            1,
            (_SKIP, True),
        ),
        # 핵심 슬롯의 빈 답은 스킵 불가 — 상한 전에는 기존 재질문(pending) 경로 유지
        ("goals.list", "text", {"type": "text", "raw": ""}, None, 0.0, 1, (_pending(1), False)),
        # 상한 바로 전(2 < MAX_SLOT_ATTEMPTS=3)도 여전히 재질문 — 상한 도달과 혼동 방지
        ("goals.list", "text", {"type": "text", "raw": ""}, None, 0.0, 2, (_pending(2), False)),
        # ── #79 회귀: 핵심 슬롯 + 빈 답 + 상한 도달 → 무한 재질문하지 말고 스킵으로 진행 ──
        # `and answer_text.strip()` 이 핵심 슬롯 분기의 유일한 탈출 조건이었을 때, 빈 답이면
        # attempts 가 아무리 커져도(3이든 30이든) 계속 _pending 만 돌려줘 인터뷰가 절대
        # 끝나지 않았다. 비핵심 슬롯이 상한에서 스킵으로 진행하는 것과 같은 탈출구가 필요하다.
        ("goals.list", "text", {"type": "text", "raw": ""}, None, 0.0, 3, (_SKIP, True)),
        ("goals.list", "text", {"type": "text", "raw": "   "}, None, 0.0, 3, (_SKIP, True)),
        (
            "goals.heaviest",
            "select",
            {"type": "text", "raw": ""},
            None,
            0.0,
            3,
            (_SKIP, True),
        ),
        # 상한을 넘겨도(레이스 등으로 4가 들어와도) 여전히 스킵으로 진행 — 다시 갇히지 않는다
        ("goals.list", "text", {"type": "text", "raw": ""}, None, 0.0, 4, (_SKIP, True)),
        # 상한 도달 + 비지 않은 답은 기존 best-effort 그대로 (회귀 없음 확인)
        (
            "goals.list",
            "text",
            {"type": "text", "raw": "그냥 뭐라도"},
            "",
            0.1,
            3,
            ({"type": "text", "raw": "그냥 뭐라도"}, True),
        ),
        # ── interview-11: 상한에서 '모르겠어요' 류는 목표로 채택하지 않는다 ──
        # 고치기 전엔 '음 잘 모르겠어요' 가 Focus 목표 제목으로 영속됐다.
        (
            "goals.list",
            "text",
            {"type": "text", "raw": "음 잘 모르겠어요"},
            "",
            0.1,
            3,
            (_SKIP, True),
        ),
        (
            "goals.heaviest",
            "select",
            {"type": "text", "raw": "모르겠어요"},
            None,
            0.1,
            3,
            (_SKIP, True),
        ),
    ],
)
def test_decide_storage(
    slot_key: str,
    answer_type: str,
    last_answer: dict[str, Any] | None,
    normalized: Any,
    clarity: float,
    attempts: int,
    expected: tuple[dict[str, Any] | None, bool],
) -> None:
    assert (
        _decide_storage(slot_key, answer_type, last_answer, normalized, clarity, attempts)
        == expected
    )


def test_critical_slots_are_goal_defining() -> None:
    """핵심 슬롯 정의가 '핵심 목표'(goals.list/heaviest)인지 — 스킵 거부 대상."""
    assert (
        frozenset({"goals.list", "goals.heaviest"}) == interview_catalog.PLAN_CATALOG.critical_slots
    )


# ── #231 지난 마감 되묻기 ────────────────────────────────────────────────────
#
# 회귀 배경(코너 배터리 실측, 실 LLM): "마감이 지났는데 아직 못 냈어요" + 마감 2026-08-01
# (11일 전) 을 인터뷰가 **아무 확인 없이** 통과시켰다. 마감은 date_picker →
# `_CONSTRAINED_TYPES` 라 clarity 게이트를 통째로 건너뛰기 때문. 그 결과 계획 배치 창이
# 오늘 하루로 붕괴해 3세션 중 1개만 배치되고 나머지는 "가용 시간을 찾지 못했어요" 경고로 남았다.

_TODAY = date(2026, 8, 12)
_DEADLINE_PICKED = {"type": "text", "raw": "2026-08-01"}


def test_past_deadline_is_asked_again_instead_of_stored() -> None:
    """지난 마감은 저장하지 않고 되묻는다 — pending 에 사유를 실어 다음 질문이 이유를 안다."""
    stored, filled = _decide_storage(
        "goals.deadlines", "date_picker", _DEADLINE_PICKED, "2026-08-01", 0.9, 1, _TODAY
    )
    assert filled is False, "지난 마감이 그대로 충족 처리되면 안 된다"
    assert stored == _pending(1, interview._RETRY_PAST_DEADLINE)


def test_future_and_today_deadlines_store_normally() -> None:
    """오늘·미래 마감은 기존대로 한 번에 저장 — 되묻기가 정상 답까지 잡으면 안 된다."""
    for raw in ("2026-08-12", "2026-12-31"):
        stored, filled = _decide_storage(
            "goals.deadlines", "date_picker", {"type": "text", "raw": raw}, raw, 0.9, 1, _TODAY
        )
        assert (stored, filled) == ({"type": "text", "raw": raw}, True), raw


def test_past_deadline_is_accepted_at_the_attempt_cap() -> None:
    """상한(MAX_SLOT_ATTEMPTS)에 닿으면 사용자 뜻으로 보고 받는다 — 무한 재질문 금지.

    여기서부터는 플래닝 백스톱(`first_plan_adapter.is_overdue_deadline`)이 배치 창 붕괴를 막는다.
    """
    stored, filled = _decide_storage(
        "goals.deadlines",
        "date_picker",
        _DEADLINE_PICKED,
        "2026-08-01",
        0.9,
        interview.MAX_SLOT_ATTEMPTS,
        _TODAY,
    )
    assert (stored, filled) == (_DEADLINE_PICKED, True)


def test_deadline_skip_is_not_treated_as_past() -> None:
    """'마감 없어요'(빈 답 → 스킵 마커)는 지난 마감이 아니다 — 되묻기에 걸리면 안 된다."""
    stored, filled = _decide_storage(
        "goals.deadlines", "date_picker", {"type": "text", "raw": ""}, None, 0.0, 1, _TODAY
    )
    assert (stored, filled) == (_SKIP, True)


def test_past_date_in_other_slots_is_untouched() -> None:
    """마감 슬롯이 아닌 곳의 과거 날짜 문자열은 건드리지 않는다 — 판정 범위를 좁게 유지."""
    stored, filled = _decide_storage(
        "goals.success_image",
        "text",
        {"type": "text", "raw": "2026-08-01"},
        "2026-08-01",
        0.9,
        1,
        _TODAY,
    )
    assert filled is True


def test_retry_hint_tells_the_question_why_the_date_was_rejected() -> None:
    """되묻기 힌트가 '모호함' 이 아니라 '지난 날짜' 를 짚어야 한다 (프롬프트 문구 회귀 핀).

    사유 없이 기존 힌트가 나가면 LLM 이 "조금 더 구체적으로 말해달라" 는 엉뚱한 재질문을
    만든다 — 사용자가 준 날짜는 모호하지 않았다.
    """
    hint = interview._retry_hint("goals.deadlines", 1, interview._RETRY_PAST_DEADLINE)
    assert "이미 지난 날짜" in hint
    assert "언제까지 끝내고 싶은지" in hint
    assert "다그치지 말고" in hint  # 톤 잠금: "on your side, not on your case"
    # 사유가 없으면 기존(모호함) 힌트 그대로 — 다른 슬롯 재질문 문구를 바꾸지 않았다.
    assert "지난" not in interview._retry_hint("goals.deadlines", 1)


# ── #232 부연 설명이 유령 목표로 분리되는 문제 ─────────────────────────────
#
# 회귀 배경(코너 배터리 실측, 실 LLM): goals.list "전공책 3권을 완독하고 싶어요.
# 각 권당 10챕터 정도예요." 가 목표 **2개**로 정규화돼 '각 권당 10챕터 학습' 이라는 유령
# 목표가 생겼다. 그 유령이 heaviest 선택 chip 에 보기로 뜨고, "'각 권당 10챕터 학습'는 이번
# 계획에 넣지 않았어요" 헛경고를 만들고, proposed 목표로 영속돼 목표 목록에 남았다.


def _prune(items: list[str]) -> list[str]:
    stored = {"type": "text", "raw": ", ".join(items), "normalized": items}
    return interview._prune_goal_glosses("goals.list", stored)["normalized"]


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        # 실측 케이스 — LLM 이 부연 설명을 목표꼴로 다듬어 올려도 걷어낸다
        (["전공책 3권 완독", "각 권당 10챕터 학습"], ["전공책 3권 완독"]),
        (["전공책 3권 완독", "각 권당 10챕터 정도예요"], ["전공책 3권 완독"]),
        (["강의 완강", "총 20강이에요"], ["강의 완강", "총 20강이에요"]),  # 약한 신호는 프롬프트 몫
        (["러닝 습관 만들기", "회당 30분"], ["러닝 습관 만들기"]),
        (["토익 800점", "지금은 600점쯤"], ["토익 800점"]),
        # 진짜 목표 여러 개는 절대 건드리지 않는다 (오탐 방지가 최우선)
        (["운동", "토익 준비", "캡스톤"], ["운동", "토익 준비", "캡스톤"]),
        (["매일 30분 러닝", "전공책 3권 완독"], ["매일 30분 러닝", "전공책 3권 완독"]),
        # 항목이 하나뿐이면 손대지 않는다
        (["각 권당 10챕터 정도예요"], ["각 권당 10챕터 정도예요"]),
    ],
)
def test_prune_goal_glosses(items: list[str], expected: list[str]) -> None:
    assert _prune(items) == expected


def test_first_item_is_never_pruned() -> None:
    """첫 항목은 정의상 gloss 가 아니다 — 목표가 통째로 사라지는 최악을 구조로 막는다."""
    assert _prune(["각 권당 10챕터 정도예요", "각 권당 10챕터 정도예요"]) == [
        "각 권당 10챕터 정도예요"
    ]


def test_prune_only_touches_goal_list() -> None:
    """다른 슬롯의 항목 리스트는 건드리지 않는다 — 판정 범위를 좁게 유지."""
    stored = {"type": "text", "raw": "x", "normalized": ["목표", "각 권당 10챕터"]}
    assert interview._prune_goal_glosses("goals.materials", stored) == stored


def test_prune_shrinks_machine_joined_raw_but_keeps_user_text() -> None:
    """raw 가 항목을 이어붙여 만든 것이면 같이 줄이고, 사용자 원문이면 보존한다."""
    joined = {
        "type": "text",
        "raw": "전공책 3권 완독, 각 권당 10챕터 학습",
        "normalized": ["전공책 3권 완독", "각 권당 10챕터 학습"],
    }
    assert interview._prune_goal_glosses("goals.list", joined)["raw"] == "전공책 3권 완독"

    original = {
        "type": "text",
        "raw": "전공책 3권을 완독하고 싶어요. 각 권당 10챕터 정도예요.",
        "normalized": ["전공책 3권 완독", "각 권당 10챕터 학습"],
    }
    pruned = interview._prune_goal_glosses("goals.list", original)
    assert pruned["raw"] == original["raw"], "사용자 원문은 보존"
    assert pruned["normalized"] == ["전공책 3권 완독"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 산문형 — 쪼개면 조각이 목표가 된다 → 통째로 하나
        (
            "대학원 지원을 다 끝냈고, 이제 합격 발표를 기다리는 중이에요.",
            ["대학원 지원을 다 끝냈고, 이제 합격 발표를 기다리는 중이에요."],
        ),
        (
            "토익 900점 따야 하고, 캡스톤도 마무리해야 해요",
            ["토익 900점 따야 하고, 캡스톤도 마무리해야 해요"],
        ),
        # 짧은 명사구 나열 — 진짜 다중 목표라 그대로 나눈다
        ("토익, 캡스톤, 운동", ["토익", "캡스톤", "운동"]),
        ("전공책 3권 완독, 매일 30분 러닝", ["전공책 3권 완독", "매일 30분 러닝"]),
        # 항목이 하나면 분기 자체가 없다
        ("대학원 합격", ["대학원 합격"]),
    ],
)
def test_goal_list_prose_is_not_comma_split(raw: str, expected: list[str]) -> None:
    """룰 폴백(LLM 정규화 실패)에서 산문형 goals.list 를 쉼표로 쪼개지 않는다 (#232).

    회귀 배경(코너 배터리 재점검, 실 LLM): ambiguity_score 가 normalized_value 를 못 주면
    이 경로가 도는데, "대학원 지원을 다 끝냈고, 이제 합격 발표를 기다리는 중이에요." 가
    조각 2개로 쪼개져 **둘 다 목표로 영속**됐다(heaviest chip 에도 조각이 보기로 떴다).
    """
    out = interview._normalize_for_store("goals.list", {"type": "text", "raw": raw})
    assert out["normalized"] == expected


def test_prose_split_guard_is_scoped_to_goal_list() -> None:
    """다른 text 슬롯의 쉼표 분리는 그대로 — 목표 목록만의 규칙이다."""
    out = interview._normalize_for_store(
        "goals.materials", {"type": "text", "raw": "1장은 읽었고, 2장은 아직이에요"}
    )
    assert out["normalized"] == ["1장은 읽었고", "2장은 아직이에요"]


def test_goal_list_pruning_runs_through_decide_storage() -> None:
    """`_decide_storage` 경로(LLM 정규화 배열)에서도 유령 목표가 걸러진다."""
    stored, filled = _decide_storage(
        "goals.list",
        "text",
        {"type": "text", "raw": "전공책 3권을 완독하고 싶어요. 각 권당 10챕터 정도예요."},
        ["전공책 3권 완독", "각 권당 10챕터 학습"],
        0.9,
        1,
    )
    assert filled is True
    assert stored is not None and stored["normalized"] == ["전공책 3권 완독"]
