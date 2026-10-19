"""교재 단원 커리큘럼 — 단계 나누기·세션 배정이 책의 단원을 빠짐없이, 순서대로 따르는가.

카드 JSON 이 아니라 여기서 만든 합성 교재로 잰다 — 실제 카드 데이터가 바뀌어도 규칙의
불변식(빠짐없이 · 순서대로 · 고르게)은 그대로여야 하기 때문이다. 카드 파일 자체의 모양은
맨 아래 테스트들이 따로 지킨다.
"""

from __future__ import annotations

import pytest

from reaction_backend.domain import curriculum as cur


def _book(
    book_id: str,
    short: str,
    n: int,
    *,
    role: cur.BookRole = "core",
    minutes: int = 30,
    label: str = "{i:02d}강",
    tests: int = 0,
    test_minutes: int = 120,
) -> cur.Book:
    units = [
        cur.Unit(
            id=f"U{i:02d}",
            label=label.format(i=i),
            section="본문",
            topic=f"주제{i}",
            kind="lesson",
            minutes=minutes,
        )
        for i in range(1, n + 1)
    ]
    units += [
        cur.Unit(
            id=f"T{i}",
            label=f"TEST {i}",
            section="실전",
            topic="모의고사",
            kind="practice_test",
            minutes=test_minutes,
        )
        for i in range(1, tests + 1)
    ]
    units.append(
        cur.Unit(id="ANS", label="정답", section="부록", topic="정답", kind="appendix", minutes=1)
    )
    return cur.Book(
        id=book_id,
        title=f"{short} 합성 교재",
        short=short,
        publisher="합성",
        isbn13="0000000000000",
        page_count=300,
        role=role,
        exam="toeic",
        score_band=(0, 990),
        structure="합성",
        units=tuple(units),
    )


VOCA = _book("voca", "VOCA", 30, role="vocab", minutes=20, label="DAY {i:02d}")
CORE = _book("core", "기본서", 24, minutes=40)
TESTS = _book("tests", "실전", 0, role="practice", tests=4)


def _ids(slots: list[cur.Slot] | tuple[cur.Slot, ...]) -> list[tuple[str, str]]:
    return [(s.book.id, s.unit.id) for s in slots]


# ─────────────────────────────── phases ───────────────────────────────


def test_phases_split_each_book_evenly_and_keep_both_books_moving_together() -> None:
    ps = cur.phases([VOCA, CORE], n_phases=3)

    assert len(ps) == 3
    # 책마다 10·8 단원씩 — 한 책을 다 끝내고 다음 책으로 넘어가지 않는다.
    for p in ps:
        assert sum(1 for s in p.slots if s.book.id == "voca") == 10
        assert sum(1 for s in p.slots if s.book.id == "core") == 8
    # 모든 공부 단원이 정확히 한 번, 책 순서대로 — 부록(정답)은 빠진다.
    all_slots = [s for p in ps for s in p.slots]
    assert [u for b, u in _ids(all_slots) if b == "voca"] == [u.id for u in VOCA.studyable()]
    assert [u for b, u in _ids(all_slots) if b == "core"] == [u.id for u in CORE.studyable()]
    assert all(s.unit.kind != "appendix" for s in all_slots)


def test_full_practice_tests_go_to_a_final_phase_not_between_lessons() -> None:
    ps = cur.phases([CORE, TESTS], n_phases=3)

    assert [p.is_test_phase for p in ps] == [False, False, False, True]
    assert [s.unit.id for s in ps[-1].slots] == ["T1", "T2", "T3", "T4"]
    assert ps[-1].title() == "실전 모의고사로 마무리"


def test_short_practice_tests_stay_in_book_order() -> None:
    """VOCA 의 '실전 문제' 처럼 짧은 시험은 그 자리에서 푼다 — 마지막으로 모으지 않는다."""
    voca = _book("v", "VOCA", 6, role="vocab", tests=1, test_minutes=15)

    ps = cur.phases([voca], n_phases=2)

    assert not any(p.is_test_phase for p in ps)
    assert [s.unit.id for s in ps[-1].slots][-1] == "T1"


def test_phase_title_states_unit_ranges_per_book() -> None:
    ps = cur.phases([VOCA, CORE], n_phases=3)

    assert ps[0].title() == "1단계 · VOCA DAY 01–10 · 기본서 01강–08강"
    assert ps[2].title() == "3단계 · VOCA DAY 21–30 · 기본서 17강–24강"


def test_phases_drop_empty_phases_when_a_book_is_shorter_than_the_phase_count() -> None:
    tiny = _book("t", "짧은책", 2)

    ps = cur.phases([tiny], n_phases=3)

    assert [p.index for p in ps] == [0, 1]
    assert all(p.slots for p in ps)


# ─────────────────────────────── spread ───────────────────────────────


def test_spread_assigns_every_unit_once_in_order_across_exactly_n_sessions() -> None:
    window = cur.phases([VOCA, CORE], n_phases=3)[:1]  # 1단계: VOCA 10 + 기본서 8

    sp = cur.spread(window, n_sessions=10, session_minutes=60)

    assert len(sp.sessions) == 10
    assigned = [s for sess in sp.sessions for s in sess.slots]
    assert len(assigned) == len(window[0].slots)
    assert sorted(_ids(assigned)) == sorted(_ids(window[0].slots))
    # 순서: 각 책의 단원이 세션 번호를 따라 앞에서 뒤로 간다.
    for book_id in ("voca", "core"):
        order = [s.unit.id for sess in sp.sessions for s in sess.slots if s.book.id == book_id]
        assert order == [s.unit.id for s in window[0].slots if s.book.id == book_id]
    # 고르게: VOCA 10일이 10세션에 하나씩.
    assert all(sum(1 for s in sess.slots if s.book.id == "voca") == 1 for sess in sp.sessions)


def test_spread_gives_more_sessions_to_heavier_phases() -> None:
    light = cur.Phase(index=0, slots=tuple(cur.Slot(CORE, u) for u in CORE.studyable()[:2]))
    heavy = cur.Phase(index=1, slots=tuple(cur.Slot(CORE, u) for u in CORE.studyable()[2:10]))

    sp = cur.spread([light, heavy], n_sessions=10, session_minutes=60)

    per_phase = [
        sum(
            1
            for sess in sp.sessions
            if sess.slots and sess.slots[0].unit in {s.unit for s in p.slots}
        )
        for p in (light, heavy)
    ]
    assert per_phase[0] < per_phase[1]
    assert len(sp.sessions) == 10


def test_spread_fills_spare_sessions_with_reviews_instead_of_leaving_them_empty() -> None:
    window = [cur.Phase(index=0, slots=tuple(cur.Slot(CORE, u) for u in CORE.studyable()[:3]))]

    sp = cur.spread(window, n_sessions=6, session_minutes=60)

    assert len(sp.sessions) == 6
    reviews = [s for s in sp.sessions if not s.slots]
    assert len(reviews) == 3
    assert all(r.review_of for r in reviews)
    assert all(r.title().startswith("복습 · ") for r in reviews)


def test_spread_puts_one_full_test_per_session_in_the_test_phase() -> None:
    ps = cur.phases([CORE, TESTS], n_phases=3)

    sp = cur.spread(ps[-1:], n_sessions=4, session_minutes=120)

    assert [[s.unit.id for s in sess.slots] for sess in sp.sessions] == [
        ["T1"],
        ["T2"],
        ["T3"],
        ["T4"],
    ]


def test_spread_reports_overload_instead_of_dropping_units() -> None:
    """세션이 모자라도 단원을 버리지 않는다 — 대신 세션당 필요한 시간을 알린다."""
    window = cur.phases([VOCA, CORE], n_phases=1)

    sp = cur.spread(window, n_sessions=6, session_minutes=60)

    assert sum(len(s.slots) for s in sp.sessions) == len(window[0].slots)
    assert sp.minutes_needed > 60
    assert sp.overloaded


def test_spread_with_fewer_sessions_than_phases_keeps_every_phase() -> None:
    """세션이 단계보다 적어도 남는 단계의 단원을 버리지 않는다 — 마지막 세션에 얹고 고지한다."""
    ps = cur.phases([VOCA, CORE], n_phases=3)

    sp = cur.spread(ps, n_sessions=2, session_minutes=60)

    assert len(sp.sessions) == 2
    assigned = [s for sess in sp.sessions for s in sess.slots]
    assert sorted(_ids(assigned)) == sorted(_ids([s for p in ps for s in p.slots]))
    assert sp.overloaded


def test_spread_never_drops_tests_when_there_are_more_tests_than_sessions() -> None:
    """모의고사가 세션보다 많으면 한 세션에 둘 이상 — 예전엔 세션 수를 넘는 시험을 버렸다."""
    ps = cur.phases([TESTS], n_phases=1)

    sp = cur.spread(ps, n_sessions=2, session_minutes=120)

    assert [[s.unit.id for s in sess.slots] for sess in sp.sessions] == [["T1", "T2"], ["T3", "T4"]]


def test_tracks_in_one_book_get_their_own_ranges_and_labels() -> None:
    """한 권짜리(LC·RC·VOCA 병행)는 갈래마다 번호가 따로 간다."""

    def u(track: str, i: int) -> cur.Unit:
        return cur.Unit(f"{track}{i}", f"DAY {i:02d}", track, "주제", "lesson", 30, track)

    aio = cur.Book(
        "aio",
        "합성 700+",
        "700+",
        "합성",
        "0" * 13,
        500,
        "all_in_one",
        "toeic",
        (0, 990),
        "합성",
        tuple(u(t, i) for i in range(1, 5) for t in ("LC", "RC", "VOCA")),
    )
    (p,) = cur.phases([aio], n_phases=1)
    sp = cur.spread([p], n_sessions=4, session_minutes=90)

    assert p.title() == "1단계 · 700+ LC·RC·VOCA DAY 01–04"
    assert sp.sessions[0].title() == "700+ LC·RC·VOCA DAY 01"
    assert cur.Slot(aio, aio.units[0]).label == "700+ LC DAY 01"


def test_spread_of_nothing_is_empty() -> None:
    assert cur.spread([], n_sessions=5, session_minutes=60).sessions == ()
    assert cur.spread(cur.phases([CORE]), n_sessions=0, session_minutes=60).sessions == ()


def test_session_title_and_first_step_name_the_actual_units() -> None:
    sp = cur.spread(cur.phases([VOCA, CORE], n_phases=3)[:1], n_sessions=10, session_minutes=60)

    first = sp.sessions[0]
    assert first.title() == "VOCA DAY 01 · 주제1 + 기본서 01강 · 주제1"
    assert first.first_step().startswith("VOCA DAY 01 단어 목록")


@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [
        ("DAY 01", "DAY 10", "DAY 01–10"),
        ("DAY 07", "DAY 07", "DAY 07"),
        ("01강", "08강", "01강–08강"),
        ("Part 1 Unit 1", "Part 2 Unit 4", "Part 1 Unit 1–Part 2 Unit 4"),
        ("TEST 1", "TEST 4", "TEST 1–4"),
    ],
)
def test_compact_range(first: str, last: str, expected: str) -> None:
    assert cur._compact_range(first, last) == expected  # noqa: SLF001


def test_phase_range_is_written_with_lesson_units_not_a_trailing_check_test() -> None:
    voca = _book("v", "VOCA", 10, role="vocab", label="DAY {i:02d}", tests=1, test_minutes=15)

    (only,) = cur.phases([voca], n_phases=1)

    assert only.title() == "1단계 · VOCA DAY 01–10"


# ─────────────────────────── 카드 파일 ───────────────────────────


def test_every_card_file_loads_with_unique_ids_and_known_kinds() -> None:
    """`curriculum_cards/*.json` 의 모양 — 카드가 늘어도 깨지면 바로 여기서 잡는다."""
    books = cur.books()

    assert books, "카드가 하나도 없다"
    assert len({b.id for b in books}) == len(books)
    for b in books:
        assert b.short and b.title and len(b.isbn13) == 13 and b.page_count > 0
        assert b.score_band[0] <= b.score_band[1]
        ids = [u.id for u in b.units]
        assert len(set(ids)) == len(ids), f"{b.id}: 단원 id 중복"
        assert b.studyable(), f"{b.id}: 공부할 단원이 없다"
        for u in b.units:
            assert u.kind in {"lesson", "practice_test", "review", "appendix"}
            assert u.label and u.topic and u.section
            assert u.kind == "appendix" or u.minutes > 0, f"{b.id}/{u.id}: 분량 없음"


def test_card_topics_stay_short() -> None:
    """주제어는 분야 이름만 — 목차 문장을 통째로 옮기지 않는다(저작권 경계, 카드 파일 _note)."""
    for b in cur.books():
        for u in b.units:
            assert len(u.topic) <= 24, f"{b.id}/{u.id}: 주제어가 길다 {u.topic!r}"
