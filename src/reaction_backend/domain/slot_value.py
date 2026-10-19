"""인터뷰 슬롯 답 값 — 저장 모양(JSON)과 타입 있는 값 사이의 단일 경계.

`interview_slot_answers.value`(JSONB)와 인터뷰 상태의 `slot_answers` 는 JSON dict 로 남는다
(상태는 직렬화돼야 한다 — ADR-0005 §7.1). 그 dict 를 **읽는 곳은 전부 이 모듈의 함수를,
쓰는 곳은 전부 `dump` 를** 지난다. 예전엔 같은 읽기 함수가 4벌(interview · interview_adapter ·
ultimate_adapter · routes/interview)이었고 조금씩 달랐다.

저장 모양은 chip/text/range/spec/pending 이 예전과 **같고**, 스킵만 새 모양이다:

- chip    → `{"type":"chip","values":[...]}`
- text    → `{"type":"text","raw":"...","normalized":[...]}` (`normalized` 는 있을 때만)
- range   → `{"type":"range","start":"HH:MM","end":"HH:MM"}`
- spec    → `{"type":"spec","items":[...]}` (자료 확정 — ADR-0010, 내용은 `materials_spec`)
- pending → `{"type":"pending","attempts":n,"reason"?:...}` (재질문 대기 — 시도 횟수를 턴 사이에 나른다)
- skip    → `{"type":"skip","reason":...}`

## 스킵은 다시 묻지 않지만 답도 아니다 (#499)

예전 스킵 마커는 빈 텍스트(`{"type":"text","raw":""}`)였다. 그래서 "이 슬롯을 다시 물어야
하나" 와 "사용자가 값을 줬나" 가 한 술어로 판정됐고, 스킵한 슬롯이 `unresolved_slots` 에서
빠져 서버 기본값이 사용자의 답으로 프로필에 굳었다. 이제 두 질문은 다른 함수다:

- `is_settled` — FSM 다음 슬롯·진행바. 스킵은 **닫힌** 슬롯이다(다시 물으면 무한 재질문, #79).
- `has_answer` — outcome 의 `unresolved_slots`·프로필 영속·재인터뷰 이월. 스킵은 **답이 아니다**.

예전 빈 텍스트 스킵으로 저장된 행도 `Skipped("legacy")` 로 읽는다 — 마이그레이션은 없다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

__all__ = [
    "Answer",
    "ChipAnswer",
    "Pending",
    "RangeAnswer",
    "SkipReason",
    "Skipped",
    "SlotValue",
    "SpecAnswer",
    "TextAnswer",
    "chips",
    "display_text",
    "dump",
    "first_chip",
    "has_answer",
    "is_settled",
    "items",
    "parse",
    "text",
    "time_range",
]

# 스킵 사유 — **기록만** 다르고 동작은 같다(#499 은 "명시적 없음" 과 "상한 포기" 의 구분을 범위
# 밖으로 뒀다). 분석·디버깅에서 "왜 이 칸이 비었나" 를 답하려고 남긴다.
#   empty       빈 답(=넘기기)
#   declined    "없어요·모르겠어요" — 사용자가 없다고 했다
#   llm         채점 LLM 이 '없음' 신호(빈 문자열)를 냈다
#   unreadable  형식이 정해진 슬롯(날짜·시간대)인데 값을 읽지 못했다
#   gave_up     재질문 상한에 닿아 포기했다
#   off_options 보기 중에서 골라야 하는데 상한까지 보기 밖 답이 왔다
#   legacy      예전 빈 텍스트 스킵 마커로 저장된 행
SkipReason = Literal["empty", "declined", "llm", "unreadable", "gave_up", "off_options", "legacy"]
_SKIP_REASONS: frozenset[str] = frozenset(get_args(SkipReason))


@dataclass(frozen=True, slots=True)
class ChipAnswer:
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TextAnswer:
    raw: str
    items: tuple[str, ...] | None = None  # 저장 키 `normalized` — 정리된 항목들


@dataclass(frozen=True, slots=True)
class RangeAnswer:
    start: str
    end: str


@dataclass(frozen=True, slots=True)
class SpecAnswer:
    items: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class Pending:
    attempts: int
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Skipped:
    reason: SkipReason


Answer = ChipAnswer | TextAnswer | RangeAnswer | SpecAnswer
SlotValue = Answer | Pending | Skipped


def _str_list(raw: Any) -> list[str] | None:
    """JSON 배열이면 문자열 목록, 아니면 None (문자열 자체는 배열로 치지 않는다)."""
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        return [str(v) for v in raw]
    return None


def parse(raw: Mapping[str, Any] | None) -> SlotValue | None:
    """저장 모양 → 타입 있는 값. 비었거나 모르는 모양이면 None.

    관대하게 읽는다 — 저장된 데이터는 여러 시절의 코드가 썼다. 예전 스킵(빈 텍스트)은
    `Skipped("legacy")`, 칩 값이 배열이 아니면 빈 칩이다(예전 읽기 함수와 같은 결과).
    """
    if not raw:
        return None
    kind = raw.get("type")
    if kind == "chip":
        return ChipAnswer(tuple(_str_list(raw.get("values")) or ()))
    if kind == "text":
        text_raw = raw.get("raw")
        value = text_raw if isinstance(text_raw, str) else ""
        normalized = _str_list(raw.get("normalized"))
        # 원문도 정리된 항목도 비었을 때만 예전 스킵이다 — 원문이 비어도 항목이 있으면 답이다
        # (예전 읽기 함수들은 원문과 무관하게 `normalized` 를 읽었다).
        if not value.strip() and not any(v.strip() for v in normalized or ()):
            return Skipped("legacy")
        return TextAnswer(value, tuple(normalized) if normalized is not None else None)
    if kind == "range":
        start, end = raw.get("start"), raw.get("end")
        if isinstance(start, str) and isinstance(end, str):
            return RangeAnswer(start, end)
        return None
    if kind == "spec":
        spec_items = raw.get("items")
        if isinstance(spec_items, Sequence) and not isinstance(spec_items, str):
            return SpecAnswer(tuple(i for i in spec_items if isinstance(i, Mapping)))
        return SpecAnswer(())
    if kind == "pending":
        attempts = raw.get("attempts", 0)
        reason = raw.get("reason")
        return Pending(
            attempts if isinstance(attempts, int) else 0,
            reason if isinstance(reason, str) and reason else None,
        )
    if kind == "skip":
        reason = raw.get("reason")
        return Skipped(reason if reason in _SKIP_REASONS else "legacy")
    return None


def dump(value: SlotValue) -> dict[str, Any]:
    """타입 있는 값 → 저장 모양. `parse` 의 역(저장 모양을 바꾸지 않는다)."""
    match value:
        case ChipAnswer(values=values):
            return {"type": "chip", "values": list(values)}
        case TextAnswer(raw=raw, items=None):
            return {"type": "text", "raw": raw}
        case TextAnswer(raw=raw, items=text_items):
            return {"type": "text", "raw": raw, "normalized": list(text_items or ())}
        case RangeAnswer(start=start, end=end):
            return {"type": "range", "start": start, "end": end}
        case SpecAnswer(items=spec_items):
            return {"type": "spec", "items": [dict(i) for i in spec_items]}
        case Pending(attempts=attempts, reason=None):
            return {"type": "pending", "attempts": attempts}
        case Pending(attempts=attempts, reason=reason):
            return {"type": "pending", "attempts": attempts, "reason": reason}
        case Skipped(reason=reason):
            return {"type": "skip", "reason": reason}


def is_settled(raw: Mapping[str, Any] | None) -> bool:
    """이 슬롯을 **다시 물을 필요가 없는가** — FSM 다음 슬롯·진행바(`ambiguityScore`).

    답이든 스킵이든 닫힌 것이다. 재질문 대기(pending)와 빈 값만 열려 있다. 모르는 모양도
    닫힌 것으로 본다 — 열려 있다고 보면 같은 질문을 끝없이 반복한다.
    """
    if not raw:
        return False
    return raw.get("type") != "pending"


def has_answer(raw: Mapping[str, Any] | None) -> bool:
    """사용자가 **실제 값을 줬는가** — `unresolved_slots`·프로필 영속·재인터뷰 이월.

    스킵·대기·빈 칩은 답이 아니다. 그 슬롯에 outcome 이 채우는 값은 서버 기본값이라,
    사용자의 답으로 다루면 안 된다(#499).
    """
    match parse(raw):
        case ChipAnswer(values=values):
            return bool(values)
        case TextAnswer():
            return True  # 원문·항목이 다 빈 텍스트는 parse 가 이미 스킵으로 읽는다
        case RangeAnswer(start=start, end=end):
            return bool(start and end)
        case SpecAnswer(items=spec_items):
            return bool(spec_items)
        case _:
            return False


def chips(raw: Mapping[str, Any] | None) -> list[str]:
    """칩 값들. 칩이 아니면 빈 목록."""
    value = parse(raw)
    return list(value.values) if isinstance(value, ChipAnswer) else []


def first_chip(raw: Mapping[str, Any] | None) -> str | None:
    values = chips(raw)
    return values[0] if values else None


def text(raw: Mapping[str, Any] | None) -> str | None:
    """텍스트 답 원문(공백뿐이면 None). 텍스트가 아니면 None."""
    value = parse(raw)
    return value.raw if isinstance(value, TextAnswer) and value.raw.strip() else None


def items(raw: Mapping[str, Any] | None) -> list[str]:
    """텍스트 답의 항목들 — 정리된 목록(`normalized`)이 있으면 그것, 없으면 원문 1개."""
    value = parse(raw)
    if not isinstance(value, TextAnswer):
        return []
    if value.items is not None:
        return [v for v in value.items if v.strip()]
    return [value.raw] if value.raw.strip() else []


def time_range(raw: Mapping[str, Any] | None) -> tuple[str, str] | None:
    value = parse(raw)
    return (value.start, value.end) if isinstance(value, RangeAnswer) else None


def display_text(raw: Mapping[str, Any] | None) -> str:
    """사람이 읽는 한 줄 — 프롬프트 맥락·스킵 감지용. 스킵·대기·자료 확정은 빈 문자열."""
    match parse(raw):
        case TextAnswer(raw=value):
            return value
        case ChipAnswer(values=values):
            return ", ".join(values)
        case RangeAnswer(start=start, end=end):
            return f"{start}~{end}"
        case _:
            return ""
