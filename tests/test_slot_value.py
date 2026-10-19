"""슬롯 답 값(`domain/slot_value`) — 저장 모양과 타입 있는 값 사이의 단일 경계.

저장 모양(JSONB·인터뷰 상태)은 chip/text/range/spec/pending 이 예전과 **바이트 단위로 같고**,
스킵만 새 모양(`{"type":"skip","reason":…}`)이다. 예전 스킵(빈 텍스트)도 스킵으로 읽는다.
"""

from __future__ import annotations

from typing import Any

import pytest

from reaction_backend.domain import slot_value as sv
from reaction_backend.domain.slot_value import (
    ChipAnswer,
    Pending,
    RangeAnswer,
    Skipped,
    SpecAnswer,
    TextAnswer,
)

_STORED_SHAPES: list[dict[str, Any]] = [
    {"type": "chip", "values": ["저녁", "심야"]},
    {"type": "text", "raw": "캡스톤"},
    {"type": "text", "raw": "토익, 캡스톤", "normalized": ["토익", "캡스톤"]},
    {"type": "range", "start": "09:00", "end": "23:00"},
    {"type": "spec", "items": [{"kind": "book", "title": "책"}]},
    {"type": "pending", "attempts": 2},
    {"type": "pending", "attempts": 1, "reason": "off_catalog"},
    {"type": "skip", "reason": "declined"},
]


@pytest.mark.parametrize("stored", _STORED_SHAPES)
def test_parse_and_dump_round_trip_keeps_the_stored_shape(stored: dict[str, Any]) -> None:
    """읽고 다시 쓰면 같은 JSON 이다 — 저장된 데이터·테스트 리터럴이 그대로 유효하다."""
    assert sv.dump(sv.parse(stored)) == stored


def test_skip_has_its_own_shape() -> None:
    """스킵은 빈 텍스트가 아니다 — 그 모양이 '답했다' 로 읽혀 #499 가 생겼다."""
    assert sv.dump(Skipped("empty")) == {"type": "skip", "reason": "empty"}
    assert sv.parse({"type": "skip", "reason": "off_options"}) == Skipped("off_options")


@pytest.mark.parametrize(
    "legacy", [{"type": "text", "raw": ""}, {"type": "text", "raw": "  ", "normalized": []}]
)
def test_legacy_empty_text_reads_as_skip(legacy: dict[str, Any]) -> None:
    """예전 스킵 마커(빈 텍스트)로 저장된 행도 스킵으로 읽는다 — 마이그레이션 없이."""
    assert sv.parse(legacy) == Skipped("legacy")


def test_parse_builds_typed_values() -> None:
    assert sv.parse({"type": "chip", "values": ["3학년"]}) == ChipAnswer(("3학년",))
    assert sv.parse({"type": "text", "raw": "a, b", "normalized": ["a", "b"]}) == TextAnswer(
        "a, b", ("a", "b")
    )
    assert sv.parse({"type": "range", "start": "08:00", "end": "22:00"}) == RangeAnswer(
        "08:00", "22:00"
    )
    assert sv.parse({"type": "spec", "items": []}) == SpecAnswer(())
    assert sv.parse({"type": "pending", "attempts": 3, "reason": "x"}) == Pending(3, "x")
    assert sv.parse(None) is None
    assert sv.parse({}) is None
    assert sv.parse({"type": "unknown"}) is None


@pytest.mark.parametrize(
    ("value", "settled", "answered"),
    [
        (None, False, False),
        ({}, False, False),
        ({"type": "pending", "attempts": 1}, False, False),
        ({"type": "skip", "reason": "declined"}, True, False),
        ({"type": "text", "raw": ""}, True, False),  # 예전 스킵
        ({"type": "chip", "values": ["저녁"]}, True, True),
        ({"type": "chip", "values": []}, True, False),  # 빈 칩은 값이 아니다
        ({"type": "text", "raw": "캡스톤"}, True, True),
        ({"type": "text", "raw": "", "normalized": ["토익"]}, True, True),  # 항목이 있으면 답
        ({"type": "range", "start": "09:00", "end": "23:00"}, True, True),
        ({"type": "spec", "items": [{"kind": "book"}]}, True, True),
    ],
)
def test_settled_and_answered_are_two_questions(
    value: dict[str, Any] | None, settled: bool, answered: bool
) -> None:
    """'다시 물어야 하나'(FSM·진행바)와 '사용자가 값을 줬나'(outcome·프로필·이월)는 다르다.

    스킵은 다시 묻지 않지만(무한 재질문 방지, #79) 사용자의 답도 아니다(#499).
    """
    assert sv.is_settled(value) is settled
    assert sv.has_answer(value) is answered


def test_readers() -> None:
    chip = {"type": "chip", "values": ["저녁", "심야"]}
    text = {"type": "text", "raw": "토익, 캡스톤", "normalized": ["토익", " ", "캡스톤"]}
    skip = {"type": "skip", "reason": "declined"}
    assert sv.chips(chip) == ["저녁", "심야"]
    assert sv.first_chip(chip) == "저녁"
    assert sv.chips(text) == [] and sv.first_chip(skip) is None
    assert sv.text(text) == "토익, 캡스톤"
    assert sv.text({"type": "text", "raw": "  "}) is None
    assert sv.items(text) == ["토익", "캡스톤"]
    assert sv.items({"type": "text", "raw": "캡스톤"}) == ["캡스톤"]
    assert sv.items({"type": "text", "raw": "", "normalized": ["토익", "캡스톤"]}) == [
        "토익",
        "캡스톤",
    ]
    assert sv.text({"type": "text", "raw": "", "normalized": ["토익"]}) is None
    assert sv.time_range({"type": "range", "start": "08:00", "end": "22:00"}) == (
        "08:00",
        "22:00",
    )
    assert sv.display_text(chip) == "저녁, 심야"
    assert sv.display_text({"type": "range", "start": "08:00", "end": "22:00"}) == "08:00~22:00"
    for value in (skip, None, {"type": "pending", "attempts": 1}):
        assert sv.display_text(value) == ""
        assert sv.items(value) == [] and sv.text(value) is None
