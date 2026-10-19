"""교재 단원 커리큘럼 — 책의 단원을 단계(마일스톤)로 나누고 세션에 배정하는 순수 규칙.

## 왜 있나

"토익 700, 해커스 VOCA 로 할게요" 라고 해도 계획은 책의 단원을 따르지 않았다. 해커스 교재의
목차는 API 로 얻을 수 없어(알라딘 0/10 · 국중 seoji 해커스 0/3 —
`docs/experiments/l0-materials-source-results.md`) 분해 프롬프트엔 "세션당 약 18쪽" 만 갔고,
LLM 은 책에 없는 "Voca 1~4세션" 같은 단위를 지어냈다(기준선 `scripts/toeic_baseline.py`:
책 단원을 가리키는 세션 0/16).

그래서 **자주 쓰는 교재는 단원 구조를 카드로 들고 있고**(`curriculum_cards/*.json`, 공개
목차에서 구조·짧은 주제어만 옮김), 계획은 LLM 이 아니라 이 모듈이 단원을 세션에 배정한다.
LLM 순응에 기대지 않으니 "세션이 책 단원을 따른다" 가 항상 참이다.

## 두 층

1. `phases` — 단원을 3~4 단계로 나눈다. 단계가 곧 **마일스톤**이다. 계획 파이프라인은 이미
   마일스톤 단위로 주기를 잇는다(ADR-0007 커서) — 그래서 다음 주기가 어디서부터 책을 이어야
   하는지 따로 저장하지 않아도 된다. 단계는 **단원 개수로** 나누므로 같은 교재 묶음이면 언제
   다시 계산해도 같은 결과다(주기마다 남은 세션 수가 달라도 단계 경계는 안 흔들린다).
2. `spread` — 한 주기에 받은 세션 수에 그 단계들의 단원을 **고르게** 펼친다. 앞 세션에 몰아
   담지 않는다 — 몰아 담으면 마감 전에 일찍 끝나고 뒤가 빈다.

## 하지 않는 것

- 분량이 세션 길이를 넘어도 단원을 **잘라내지 않는다.** 책 중간을 말없이 빼는 것보다, 세션당
  얼마가 필요한지(`Spread.minutes_needed`)를 알려 사용자가 고르게 하는 게 맞다.
- 진도(어디까지 실제로 했는지)는 모른다 — 그건 단계 완료(마일스톤 커서)까지만 안다.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "Book",
    "Phase",
    "Session",
    "Slot",
    "Spread",
    "Unit",
    "UnitKind",
    "book",
    "books",
    "has_test_phase",
    "phases",
    "spread",
]

UnitKind = Literal["lesson", "practice_test", "review", "appendix"]
BookRole = Literal["vocab", "starter", "core", "all_in_one", "practice"]

# 단계 수 — 마일스톤 3~5개(plan_milestones 프롬프트 계약)에 맞춘다. 실전 모의고사 단계가
# 따로 붙으면 하나 더 는다.
DEFAULT_PHASES = 3

# 한 세션 전부를 차지하는 "통짜" 실전 모의고사로 볼 길이(분). VOCA 의 '실전 문제' 처럼 짧은
# 시험은 책 순서대로 단원 사이에 끼우고, 이보다 긴 것만 마지막 단계로 모은다.
FULL_TEST_MIN_MINUTES = 90

_CARDS_DIR = Path(__file__).resolve().parent / "curriculum_cards"


@dataclass(frozen=True, slots=True)
class Unit:
    """책의 단원 하나 — 세션에 배정되는 최소 단위."""

    id: str  # 카드 안에서 고유 — "DAY03", "LC-P2-U04", "TEST1"
    label: str  # 세션 제목에 그대로 쓰는 짧은 표기 — "DAY 03", "Part 2 Unit 4"
    section: str  # 상위 묶음 — "VOCA", "LC Part 2", "실전"
    topic: str  # 짧은 주제어 — "일반사무", "의문사 의문문"
    kind: UnitKind
    minutes: int  # 이 단원을 한 번 공부하는 데 드는 추정 시간
    # 한 책 안의 병행 갈래 — "LC"·"RC"·"VOCA". 한 권짜리 교재는 갈래마다 DAY 번호가 따로 가고
    # 하루에 갈래별로 하나씩 나란히 본다(카드의 단원 순서가 그 병행 순서다). 갈래가 없으면 "".
    track: str = ""


@dataclass(frozen=True, slots=True)
class Book:
    """교재 카드 — 구조와 메타데이터만 담는다(본문·설명 문구는 담지 않는다)."""

    id: str
    title: str
    short: str  # 세션 제목 머리말 — "기출 VOCA", "700+"
    publisher: str
    isbn13: str
    page_count: int
    role: BookRole
    exam: str  # "toeic"
    score_band: tuple[int, int]  # 이 책이 맞는 **현재** 점수 구간(포함)
    structure: str  # 한 문장 구성 설명 — 추천 이유·자료 노트에 쓴다
    units: tuple[Unit, ...]

    def studyable(self) -> tuple[Unit, ...]:
        """계획에 올릴 단원 — 부록(정답·인덱스)은 따로 공부하는 단위가 아니다."""
        return tuple(u for u in self.units if u.kind != "appendix")


@dataclass(frozen=True, slots=True)
class Slot:
    """세션에 배정된 단원 하나."""

    book: Book
    unit: Unit

    @property
    def label(self) -> str:
        track = f"{self.unit.track} " if self.unit.track else ""
        return f"{self.book.short} {track}{self.unit.label}"


@dataclass(frozen=True, slots=True)
class Phase:
    """단원 묶음 하나 = 마일스톤 하나."""

    index: int  # 0부터
    slots: tuple[Slot, ...]
    is_test_phase: bool = False

    @property
    def minutes(self) -> int:
        return sum(s.unit.minutes for s in self.slots)

    def title(self) -> str:
        """마일스톤 제목 — 책마다 단원 범위를 적는다("기출 VOCA DAY 01–10 · 700+ LC Part 1–2")."""
        if self.is_test_phase:
            return "실전 모의고사로 마무리"
        parts = [_range_label(book, units) for book, units in _group_by_book(self.slots)]
        return f"{self.index + 1}단계 · " + " · ".join(parts)

    def summary(self) -> str:
        sections = list(dict.fromkeys(s.unit.section for s in self.slots))
        if self.is_test_phase:
            return f"실전 모의고사 {len(self.slots)}회로 시간 배분과 약한 파트를 점검해요"
        return f"{', '.join(sections[:4])} 단원을 순서대로 끝내요 (단원 {len(self.slots)}개)"


@dataclass(frozen=True, slots=True)
class Session:
    """세션 하나에 배정된 단원들. 비어 있으면 복습 세션이다(`review_of`)."""

    index: int
    phase: int  # 이 세션이 속한 단계(`Phase.index`) — 마일스톤 branch 를 고른다
    slots: tuple[Slot, ...]
    review_of: tuple[Slot, ...] = ()

    @property
    def minutes(self) -> int:
        if self.slots:
            return sum(s.unit.minutes for s in self.slots)
        return 0

    def title(self) -> str:
        """세션 제목 — 단원이 둘 이하면 주제까지, 셋 이상이면 책별 범위만(제목이 길어지지 않게)."""
        if len(self.slots) > 2:
            return " + ".join(
                _range_label(book, units) for book, units in _group_by_book(self.slots)
            )
        if self.slots:
            return " + ".join(f"{s.label} · {s.unit.topic}" for s in self.slots)
        if self.review_of:
            return "복습 · " + " · ".join(
                _range_label(book, units) for book, units in _group_by_book(self.review_of)
            )
        return "복습 · 지난 단원 다시 보기"

    def first_step(self) -> str:
        """5분 안에 시작할 수 있는 첫 행동 — 단원 종류에 따라 다르다."""
        if not self.slots:
            return "지난 세션에서 틀린 문제·모르는 단어를 5분만 다시 보기"
        first = self.slots[0]
        if first.unit.kind == "practice_test":
            return f"{first.label} 펴고 타이머를 맞춘 뒤 첫 문항만 풀기"
        if first.book.role == "vocab":
            return f"{first.label} 단어 목록을 펴고 첫 10개를 소리 내어 읽기"
        return f"{first.label} 첫 페이지를 펴고 예제 한 문제만 풀어 보기"


@dataclass(frozen=True, slots=True)
class Spread:
    """한 주기의 세션 배정 결과."""

    sessions: tuple[Session, ...]
    session_minutes: int  # 사용자가 답한 한 번 집중 길이

    @property
    def minutes_needed(self) -> int:
        """이 배정을 다 하려면 세션당 평균 몇 분이 필요한지 — 세션 길이와 비교해 고지한다."""
        busy = [s.minutes for s in self.sessions if s.slots]
        return math.ceil(sum(busy) / len(busy)) if busy else 0

    @property
    def overloaded(self) -> bool:
        """평균 필요 시간이 세션 길이를 30% 넘게 웃돈다 — 마감까지 다 보기엔 시간이 모자라다."""
        return self.minutes_needed > self.session_minutes * 1.3


# ─────────────────────────────────────────────────────────────────────────────
# 카드 로드
# ─────────────────────────────────────────────────────────────────────────────


def _unit(raw: dict[str, Any], minutes_by_kind: dict[str, int]) -> Unit:
    kind: UnitKind = raw["kind"]
    return Unit(
        id=str(raw["id"]),
        label=str(raw["label"]),
        section=str(raw["section"]),
        topic=str(raw["topic"]),
        kind=kind,
        minutes=int(raw.get("minutes") or minutes_by_kind[kind]),
        track=str(raw.get("track") or ""),
    )


def _book(raw: dict[str, Any]) -> Book:
    minutes_by_kind = {k: int(v) for k, v in raw["unit_minutes"].items()}
    lo, hi = raw["score_band"]
    return Book(
        id=str(raw["id"]),
        title=str(raw["title"]),
        short=str(raw["short"]),
        publisher=str(raw["publisher"]),
        isbn13=str(raw["isbn13"]),
        page_count=int(raw["page_count"]),
        role=raw["role"],
        exam=str(raw["exam"]),
        score_band=(int(lo), int(hi)),
        structure=str(raw["structure"]),
        units=tuple(_unit(u, minutes_by_kind) for u in raw["units"]),
    )


@cache
def books() -> tuple[Book, ...]:
    """모든 교재 카드 — `curriculum_cards/*.json` 의 `books` 배열을 이어 붙인다."""
    out: list[Book] = []
    for path in sorted(_CARDS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.extend(_book(b) for b in data["books"])
    return tuple(out)


def book(book_id: str) -> Book | None:
    return next((b for b in books() if b.id == book_id), None)


# ─────────────────────────────────────────────────────────────────────────────
# 단계 나누기 · 세션 배정
# ─────────────────────────────────────────────────────────────────────────────


def _is_full_test(book: Book, unit: Unit) -> bool:
    """마지막 단계로 모을 시험인가 — 모의고사 전용 교재의 회차는 전부, 다른 교재는 긴 시험만."""
    if book.role == "practice":
        return True
    return unit.kind == "practice_test" and unit.minutes >= FULL_TEST_MIN_MINUTES


def has_test_phase(selected: Sequence[Book]) -> bool:
    """`phases` 가 마지막에 실전 모의고사 단계를 붙이는가 — 마일스톤 수에서 단계 수를 역산할 때."""
    return any(_is_full_test(b, u) for b in selected for u in b.studyable())


def _chunk_bounds(n: int, parts: int) -> list[tuple[int, int]]:
    """n 개를 parts 묶음으로 — 앞 묶음부터 하나씩 더 받는다(9 → 3·3·3, 10 → 4·3·3)."""
    base, extra = divmod(n, parts)
    bounds: list[tuple[int, int]] = []
    start = 0
    for i in range(parts):
        size = base + (1 if i < extra else 0)
        bounds.append((start, start + size))
        start += size
    return bounds


def phases(selected: Sequence[Book], *, n_phases: int = DEFAULT_PHASES) -> list[Phase]:
    """교재 묶음 → 단계 목록. 책마다 단원을 `n_phases` 등분해 같은 번호끼리 묶는다.

    VOCA 30일과 기본서 24강을 3단계로 나누면 1단계 = DAY 01–10 + 1–8강 — 두 책이 나란히
    나아간다. 한 책을 다 끝내고 다음 책으로 넘어가는 계획은 어휘처럼 매일 해야 하는 것을
    뒤로 미룬다.

    통짜 실전 모의고사(`FULL_TEST_MIN_MINUTES` 이상)는 책 순서에서 빼 **마지막 단계**로 모은다
    — 기초 단원 사이에 끼면 아직 배우지 않은 파트를 시험 보게 된다.
    """
    if n_phases < 1:
        raise ValueError("n_phases must be >= 1")
    tracks: list[list[Slot]] = []
    tests: list[Slot] = []
    for b in selected:
        track: list[Slot] = []
        for u in b.studyable():
            (tests if _is_full_test(b, u) else track).append(Slot(b, u))
        if track:
            tracks.append(track)
    # 단원이 단계 수보다 적은 책은 앞 단계에만 실린다(빈 단계는 아래에서 버린다).
    per_phase: list[list[Slot]] = [[] for _ in range(n_phases)]
    for track in tracks:
        for i, (lo, hi) in enumerate(_chunk_bounds(len(track), n_phases)):
            per_phase[i].extend(track[lo:hi])
    out = [Phase(index=i, slots=tuple(s)) for i, s in enumerate(per_phase) if s]
    out = [Phase(index=i, slots=p.slots) for i, p in enumerate(out)]
    if tests:
        out.append(Phase(index=len(out), slots=tuple(tests), is_test_phase=True))
    return out


def spread(window: Sequence[Phase], *, n_sessions: int, session_minutes: int) -> Spread:
    """이번 주기 단계들의 단원을 `n_sessions` 세션에 고르게 펼친다.

    세션은 단계 분량(분)에 비례해 나눈다 — 단원이 많은 단계가 세션도 더 받는다. 단계 안에서는
    **책마다 따로** 고르게 펼친 뒤 같은 세션 번호끼리 합친다: 단원 j(전체 k개)는 세션
    ⌊j·m/k⌋ 로 간다(m = 그 단계 세션 수). 그래서 VOCA 10일·기본서 8강을 10세션에 펴면 매
    세션 VOCA 한 DAY 가 있고 기본서는 거의 매 세션 한 강씩이다.

    실전 모의고사 단계도 같은 규칙이다 — RC·LC 모의고사집을 둘 다 고르면 같은 번호끼리 한
    세션에 모여 LC+RC 한 회가 된다. 시험이 세션보다 많으면 한 세션에 둘 이상이 들어가고
    (`overloaded` 로 고지), **세션 수를 넘는 시험을 버리지 않는다.**

    단원보다 세션이 많아 빈 세션이 생기면 **복습 세션**으로 채운다 — 바로 앞 세션의 단원을
    다시 본다. 비워 두면 스케줄러가 그 칸을 다른 일로 채우거나 계획이 짧아 보인다.

    세션이 단계 수보다 적으면 남는 단계의 단원은 **마지막 세션에 얹는다** — 버리면 그 마일스톤이
    계획에 없는 채 다음 주기로 넘어간다.
    """
    if n_sessions <= 0 or not window:
        return Spread(sessions=(), session_minutes=session_minutes)
    counts = _allocate(window, n_sessions)
    sessions: list[Session] = []
    for phase, m in zip(window, counts, strict=True):
        if m <= 0:
            continue
        buckets: list[list[Slot]] = [[] for _ in range(m)]
        for _, units in _group_by_book(phase.slots):
            k = len(units)
            for j, slot in enumerate(units):
                buckets[j * m // k].append(slot)
        for bucket in buckets:
            sessions.append(Session(index=len(sessions), phase=phase.index, slots=tuple(bucket)))
    leftover = [s for p, m in zip(window, counts, strict=True) if m <= 0 for s in p.slots]
    if leftover:
        last = sessions[-1]
        sessions[-1] = Session(index=last.index, phase=last.phase, slots=(*last.slots, *leftover))
    return Spread(sessions=tuple(_fill_reviews(sessions)), session_minutes=session_minutes)


def _allocate(window: Sequence[Phase], n_sessions: int) -> list[int]:
    """세션 수를 단계 분량(분)에 비례해 나눈다 — 각 단계 최소 1(세션이 모자라면 앞부터)."""
    if n_sessions < len(window):
        return [1 if i < n_sessions else 0 for i in range(len(window))]
    total = sum(p.minutes for p in window) or len(window)
    raw = [n_sessions * (p.minutes or 1) / total for p in window]
    counts = [max(1, math.floor(r)) for r in raw]
    # 내림으로 남은 세션은 소수부가 큰 단계부터 하나씩 — 합이 정확히 n_sessions 가 되게.
    order = sorted(range(len(window)), key=lambda i: raw[i] - math.floor(raw[i]), reverse=True)
    i = 0
    while sum(counts) < n_sessions:
        counts[order[i % len(order)]] += 1
        i += 1
    while sum(counts) > n_sessions:  # 최소 1 보정으로 넘쳤으면 가장 큰 단계에서 뺀다
        counts[counts.index(max(counts))] -= 1
    return counts


def _fill_reviews(sessions: list[Session]) -> list[Session]:
    out: list[Session] = []
    last: tuple[Slot, ...] = ()
    for s in sessions:
        if s.slots:
            last = s.slots
            out.append(s)
        else:
            out.append(Session(index=s.index, phase=s.phase, slots=(), review_of=last))
    return out


def _group_by_book(slots: Sequence[Slot]) -> list[tuple[Book, list[Slot]]]:
    """슬롯을 책별로 묶는다 — 처음 나온 책 순서를 지킨다."""
    groups: dict[str, tuple[Book, list[Slot]]] = {}
    for s in slots:
        groups.setdefault(s.book.id, (s.book, []))[1].append(s)
    return list(groups.values())


def _range_label(book: Book, slots: Sequence[Slot]) -> str:
    """ "기출 VOCA DAY 01–10" — 첫·끝 단원 표기로 범위를 적는다."""
    if not slots:
        return book.short
    # 범위는 학습 단원으로 적는다 — 끝에 짧은 점검 시험이 붙으면 "DAY 24–실전 문제 3" 이 된다.
    lessons = [s for s in slots if s.unit.kind == "lesson"] or list(slots)
    # 갈래(LC·RC·VOCA)가 있는 책은 갈래마다 번호가 따로 간다 — 갈래별 범위가 같으면 하나로
    # 묶고("700+ LC·RC·VOCA DAY 01–07"), 다르면 갈래마다 적는다("ETS 750+ LC Unit 01–05 · RC
    # Unit 01–07"). 갈래를 무시하고 첫·끝만 보면 "LC DAY 01–VOCA DAY 07" 같은 범위가 된다.
    by_track: dict[str, list[Slot]] = {}
    for s in lessons:
        by_track.setdefault(s.unit.track, []).append(s)
    ranges = {
        track: _compact_range(units[0].unit.label, units[-1].unit.label)
        for track, units in by_track.items()
    }
    if len(ranges) == 1:
        ((track, rng),) = ranges.items()
        return f"{book.short} {track + ' ' if track else ''}{rng}"
    if len(set(ranges.values())) == 1:
        return f"{book.short} {'·'.join(ranges)} {next(iter(ranges.values()))}"
    return f"{book.short} " + " · ".join(f"{t} {r}".strip() for t, r in ranges.items())


def _compact_range(first: str, last: str) -> str:
    """ "DAY 01"·"DAY 10" → "DAY 01–10". 끝 표기가 숫자만 다를 때만 줄인다 — "Part 1 Unit 1"·
    "Part 2 Unit 4" 를 "Part 1 Unit 1–2 Unit 4" 로 줄이면 오히려 읽기 어렵다."""
    if first == last:
        return first
    i = 0
    while i < min(len(first), len(last)) and first[i] == last[i]:
        i += 1
    while i > 0 and first[i - 1].isdigit():  # 숫자 중간에서 자르지 않는다("DAY 1" | "0")
        i -= 1
    tail = last[i:]
    return f"{first}–{tail}" if tail.isdigit() and i > 0 else f"{first}–{last}"
