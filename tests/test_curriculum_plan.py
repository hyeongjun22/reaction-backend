"""교재 카드 계획 — 카드를 확정한 목표는 마일스톤·세션이 LLM 없이 책 단원 순서로 나온다.

지키는 것:
  ① 단계 수는 주기 수의 배수 — 주기마다 책을 고르게 나눠 받는다.
  ② 마일스톤과 단계는 **위치로** 짝짓는다 — 2주기에 영속된 마일스톤으로 같은 단계를 다시 만든다.
  ③ 사용자가 마일스톤을 추가·삭제했으면 카드 계획을 포기하고 LLM 분해로 넘어간다.
  ④ 분해 그래프에서 카드 경로는 LLM 을 부르지 않고, 검토 단계도 건너뛴다.
  ⑤ 인터뷰 확정본(`goals.materials` spec)의 `catalog_id` 가 목표의 `curriculum` 으로 실린다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from reaction_backend.domain import curriculum as cur
from reaction_backend.domain import slot_value as sv
from reaction_backend.llm import aiClient
from reaction_backend.orchestrator import (
    curriculum_plan,
    first_plan,
    first_plan_adapter,
    first_plan_milestones,
    interview_adapter,
)
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    MaterialItem,
    MaterialItemUnit,
    PreferenceProfile,
    TimeRange,
)
from reaction_backend.schemas.planning import MilestoneDraft
from tests.test_curriculum import _book

KST = timezone(timedelta(hours=9))
VOCA = _book("voca", "VOCA", 30, role="vocab", minutes=20, label="DAY {i:02d}")
CORE = _book("core", "기본서", 24, minutes=35)
TESTS = _book("tests", "실전", 0, role="practice", tests=4)
TARGET = date(2026, 10, 19)  # 월요일


@pytest.fixture
def cards(monkeypatch: pytest.MonkeyPatch) -> None:
    """카드 목록을 합성 교재로 갈아 끼운다 — 실제 카드 데이터와 무관하게 규칙을 잰다."""
    by_id = {b.id: b for b in (VOCA, CORE, TESTS)}
    monkeypatch.setattr(cur, "book", lambda book_id: by_id.get(book_id))


def _outcome(
    *,
    curriculum: list[str],
    weeks: int = 8,
    freq: int = 4,
    material_items: list[MaterialItem] | None = None,
) -> InterviewOutcome:
    deadline = (TARGET + timedelta(weeks=weeks)).isoformat()
    goal = GoalCandidate(
        title="토익 700점",
        category="study",
        is_heaviest=True,
        tentative_tier="focus",
        confidence=1.0,
        deadline=deadline,
        frequency_per_week=freq,
        session_length_min=60,
        curriculum=curriculum,
        material_items=material_items or [],
    )
    return InterviewOutcome(
        session_id="t",
        generated_at=datetime(2026, 10, 19, 9, tzinfo=KST),
        end_reason="completed",
        ambiguity_final=0.0,
        analysis_source="llm",
        identity=IdentityContext(role="3학년", season="학기 중"),
        core_goals=[goal],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"), peak_window=["저녁"]
        ),
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True, downscope_unit_min=10),
        horizon=deadline,
    )


def _state(
    outcome: InterviewOutcome, milestones: list[MilestoneDraft] | None = None, *, cursor: int = 0
) -> first_plan.FirstPlanState:
    state = first_plan.initial_state(
        user_id=uuid4(),
        outcome=outcome,
        target_date=TARGET.isoformat(),
        milestones=milestones,
        milestone_cursor=cursor,
    )
    state["planning_context"] = first_plan_adapter.context_from_outcome(outcome, target_date=TARGET)
    return state


@pytest.fixture
def no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(**kwargs: Any) -> Any:
        raise AssertionError(f"교재 카드 계획이 LLM 을 불렀다: {kwargs.get('prompt_id')}")

    monkeypatch.setattr(aiClient, "run", never)


# ─────────────────────────── ① 단계 수 ───────────────────────────


@pytest.mark.parametrize(
    ("full_weeks", "expected"),
    [(None, 3), (3, 3), (4, 3), (8, 4), (12, 3), (16, 4), (20, 5), (52, 5)],
)
def test_phase_count_is_a_multiple_of_the_cycle_count(
    full_weeks: int | None, expected: int
) -> None:
    assert curriculum_plan.phase_count_for(full_weeks=full_weeks, cycle_weeks=4) == expected


def test_milestones_are_book_phases_with_unit_ranges() -> None:
    ms = curriculum_plan.milestones([VOCA, CORE, TESTS], full_weeks=8, cycle_weeks=4)

    assert [m.title for m in ms] == [
        "1단계 · VOCA DAY 01–08 · 기본서 01강–06강",
        "2단계 · VOCA DAY 09–16 · 기본서 07강–12강",
        "3단계 · VOCA DAY 17–23 · 기본서 13강–18강",
        "4단계 · VOCA DAY 24–30 · 기본서 19강–24강",
        "실전 모의고사로 마무리",
    ]


def test_curriculum_milestones_need_a_card(cards: None) -> None:
    assert (
        first_plan_milestones.curriculum_milestones(_outcome(curriculum=[]), cycle_weeks=4) is None
    )
    ms = first_plan_milestones.curriculum_milestones(
        _outcome(curriculum=["voca", "core"]), cycle_weeks=4, target_date=TARGET
    )
    assert ms is not None and ms[0].title.startswith("1단계 · VOCA DAY 01")


# ─────────────────────────── ② ③ 분해 ───────────────────────────


def _decompose(
    milestones: list[MilestoneDraft], window: list[MilestoneDraft], n: int = 16
) -> curriculum_plan.CurriculumPlan | None:
    return curriculum_plan.decomposition(
        goal_title="토익 700점",
        books=[VOCA, CORE],
        all_milestones=milestones,
        window=window,
        n_sessions=n,
        session_minutes=60,
    )


def test_decomposition_puts_each_window_phase_under_its_milestone_branch() -> None:
    ms = curriculum_plan.milestones([VOCA, CORE], full_weeks=8, cycle_weeks=4)

    plan = _decompose(ms, ms[:2])

    assert plan is not None
    d = plan.decomposition
    branches = [n for n in d.goal_nodes if n.node_type == "branch"]
    assert [b.title for b in branches] == [ms[0].title, ms[1].title]
    assert len(d.action_items) == 16
    # 모든 세션이 책 단원을 가리킨다 — 기준선은 0/16 이었다.
    assert all("VOCA DAY" in a.title or a.title.startswith("복습") for a in d.action_items)
    # 1단계 세션은 1단계 branch 아래, 2단계 세션은 2단계 branch 아래.
    parent = {n.node_id: n.parent_id for n in d.goal_nodes}
    first, last = d.action_items[0], d.action_items[-1]
    assert parent[first.node_id] == branches[0].node_id and "DAY 01" in first.title
    assert parent[last.node_id] == branches[1].node_id and "DAY 16" in last.title


def test_second_cycle_continues_from_the_cursor_with_the_same_phases() -> None:
    """2주기는 영속된 마일스톤(같은 목록)과 커서로 3·4단계를 받는다 — 진도 저장소 없이."""
    ms = curriculum_plan.milestones([VOCA, CORE], full_weeks=8, cycle_weeks=4)

    plan = _decompose(ms, ms[2:4])

    assert plan is not None
    assert "DAY 17" in plan.decomposition.action_items[0].title
    assert "DAY 30" in plan.decomposition.action_items[-1].title


def test_user_edited_milestone_titles_are_kept_as_branches() -> None:
    ms = curriculum_plan.milestones([VOCA, CORE], full_weeks=8, cycle_weeks=4)
    edited = [MilestoneDraft(title="단어 먼저 잡기", summary=""), *ms[1:]]

    plan = _decompose(edited, edited[:2])

    assert plan is not None
    branches = [n.title for n in plan.decomposition.goal_nodes if n.node_type == "branch"]
    assert branches[0] == "단어 먼저 잡기"
    assert "DAY 01" in plan.decomposition.action_items[0].title


def test_added_or_removed_milestones_fall_back_to_the_llm_path() -> None:
    ms = curriculum_plan.milestones([VOCA, CORE], full_weeks=8, cycle_weeks=4)
    added = [*ms, MilestoneDraft(title="영어 일기 쓰기", summary="")]
    removed = ms[:-1]

    assert _decompose(added, added[:2]) is None
    assert _decompose(removed, removed[:2]) is None


def test_too_few_sessions_keep_every_unit_and_say_how_long_sessions_need_to_be() -> None:
    ms = curriculum_plan.milestones([VOCA, CORE], full_weeks=4, cycle_weeks=4)

    plan = _decompose(ms, ms, n=8)

    assert plan is not None
    titles = " ".join(a.title for a in plan.decomposition.action_items)
    # 세션당 단원이 셋 이상이면 제목이 책별 범위로 줄어든다("VOCA DAY 26–30").
    assert titles.startswith("VOCA DAY 01") and "DAY 26–30" in titles and "21강–24강" in titles
    assert plan.notice is not None and "세션당 평균" in plan.notice


# ─────────────────────────── ④ 그래프 배선 ───────────────────────────


async def test_decompose_goal_uses_the_curriculum_without_calling_the_llm(
    cards: None, no_llm: None
) -> None:
    outcome = _outcome(curriculum=["voca", "core"])
    ms = first_plan_milestones.curriculum_milestones(outcome, cycle_weeks=4, target_date=TARGET)
    assert ms is not None

    state = await first_plan.decompose_goal(_state(outcome, ms), {"configurable": {}})

    plan = state["goal_plan"]
    assert plan is not None and state["curriculum_plan"] is True
    assert state["decompose_fallback_reason"] is None
    # 주 4회 × 4주 = 16 세션 — LLM 경로가 보충까지 거친 개수와 같다.
    assert len(plan.action_items) == 16
    assert "VOCA DAY 01" in plan.action_items[0].title
    assert first_plan.after_schedule(state) == "done"


async def test_decompose_goal_without_confirmed_milestones_still_follows_the_book(
    cards: None, no_llm: None
) -> None:
    state = await first_plan.decompose_goal(
        _state(_outcome(curriculum=["voca", "core"])), {"configurable": {}}
    )

    plan = state["goal_plan"]
    assert plan is not None and "VOCA DAY 01" in plan.action_items[0].title
    # 확정한 적 없는 마일스톤을 상태에 남기지 않는다 — '확정하신 중간 목표' 고지가 거짓말이 된다.
    assert state["milestones"] is None


async def test_goal_without_a_card_takes_the_llm_path(
    cards: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []

    async def fake_run(**kwargs: Any) -> Any:
        called.append(kwargs["prompt_id"])
        return await first_plan_adapter_fallback(kwargs)

    async def first_plan_adapter_fallback(kwargs: dict[str, Any]) -> Any:
        from reaction_backend.llm import RunResult

        return RunResult(
            value=kwargs["fallback"](),
            fell_back=True,
            reason="timeout",
            prompt_id=kwargs["prompt_id"],
            prompt_version="v3",
        )

    monkeypatch.setattr(aiClient, "run", fake_run)

    state = await first_plan.decompose_goal(_state(_outcome(curriculum=[])), {"configurable": {}})

    assert called == ["planning/goal_decompose@v3"]
    assert state["curriculum_plan"] is False


# ─────────────────────────── ⑤ 인터뷰 확정본 → 목표 ───────────────────────────


def test_outcome_carries_catalog_ids_from_the_confirmed_materials_on_the_heaviest_goal() -> None:
    slot = sv.dump(
        sv.SpecAnswer(
            (
                {"kind": "book", "title": "VOCA", "catalog_id": "voca", "page_count": 500},
                {"kind": "book", "title": "기본서", "catalog_id": "core", "page_count": 400},
                {"kind": "video", "title": "강의"},
            )
        )
    )
    outcome = interview_adapter.build_outcome(
        session_id="s",
        slot_answers={
            "goals.list": sv.dump(sv.TextAnswer("토익, 운동", ("토익", "운동"))),
            "goals.heaviest": sv.dump(sv.ChipAnswer(("토익",))),
            "goals.materials": slot,
        },
        ambiguity_final=0.0,
        end_reason="completed",
        analysis_source="llm",
    )

    by_title = {g.title: g for g in outcome.core_goals}
    assert by_title["토익"].curriculum == ["voca", "core"]
    assert by_title["운동"].curriculum == []


def test_searched_or_pasted_materials_carry_no_curriculum() -> None:
    pasted = sv.dump(sv.TextAnswer("1주차 ... 2주차 ..."))
    searched = sv.dump(sv.SpecAnswer(({"kind": "book", "title": "알라딘 책", "page_count": 300},)))

    assert interview_adapter._curriculum_ids(pasted) == []  # noqa: SLF001
    assert interview_adapter._curriculum_ids(searched) == []  # noqa: SLF001


# ─────────────────────────── ⑥ 목차 있는 도서 · 재생목록 ───────────────────────────

# 실측 재생목록(구자연의 자연스러운 문법)과 같은 모양 — 모든 제목에 같은 머리말이 붙고 대괄호에 주제.
PLAYLIST = MaterialItem(
    kind="video",
    title="구자연의 자연스러운 문법🌱 (토익은 역시 1위 해커스토익)",
    source="해커스토익",
    units=[
        MaterialItemUnit(title=f"토익 독학러 문법 정복 프로젝트 [{i}일차 {t}]", minutes=m)
        for i, (t, m) in enumerate(
            [("시제", 54), ("수동태&수일치", 67), ("to부정사&동명사", 56), ("분사", 130)], start=1
        )
    ],
)
# Java의 정석 기초편 seoji 목차 모양 — 15장, 체크포인트 쪽수, 목차 밖 나머지 분량 항목.
JAVA_ENDS = [41, 67, 95, 125, 154, 207, 262, 290, 330, 360, 420, 480, 560, 620, 700]
JAVA = MaterialItem(
    kind="book",
    title="java의 정석 기초편",
    source="남궁성",
    page_count=756,
    pages_per_session=42,
    units=[
        *(
            MaterialItemUnit(title=f"Chapter {i}. 주제 {i}", end_page=end)
            for i, end in enumerate(JAVA_ENDS, start=1)
        ),
        MaterialItemUnit(title="(목차 이후 나머지 분량)", end_page=756),
    ],
)


def test_outcome_keeps_only_materials_with_a_unit_structure_on_the_heaviest_goal() -> None:
    chapters = [{"title": f"{i}. 장 {i}", "end_page": 30 * i} for i in range(1, 5)]
    videos = [{"title": f"강의 {i}", "minutes": 40} for i in range(1, 4)]
    slot = sv.dump(
        sv.SpecAnswer(
            (
                {"kind": "book", "title": "카드", "catalog_id": "voca", "page_count": 500},
                {
                    "kind": "book",
                    "title": "목차 책",
                    "author": "저자",
                    "page_count": 120,
                    "pages_per_session": 30,
                    "chapters": chapters,
                },
                {"kind": "book", "title": "목차 없는 책", "page_count": 300},
                {
                    "kind": "video",
                    "title": "재생목록",
                    "channel_title": "채널",
                    "curriculum": videos,
                },
                {"kind": "video", "title": "두 편짜리", "curriculum": videos[:2]},
            )
        )
    )
    outcome = interview_adapter.build_outcome(
        session_id="s",
        slot_answers={
            "goals.list": sv.dump(sv.TextAnswer("코딩, 운동", ("코딩", "운동"))),
            "goals.heaviest": sv.dump(sv.ChipAnswer(("코딩",))),
            "goals.materials": slot,
        },
        ambiguity_final=0.0,
        end_reason="completed",
        analysis_source="llm",
    )

    by_title = {g.title: g for g in outcome.core_goals}
    items = by_title["코딩"].material_items
    # 카드는 `curriculum` 몫, 목차 없는 책·단원 셋 미만 재생목록은 예전처럼 LLM 분해 몫이다.
    assert [(i.kind, i.title, i.source) for i in items] == [
        ("book", "목차 책", "저자"),
        ("video", "재생목록", "채널"),
    ]
    assert [u.end_page for u in items[0].units] == [30, 60, 90, 120]
    assert items[0].pages_per_session == 30
    assert [u.minutes for u in items[1].units] == [40, 40, 40]
    assert by_title["운동"].material_items == []


def test_playlist_units_take_each_titles_own_part_and_split_long_videos() -> None:
    (book,) = curriculum_plan.books_from_materials([PLAYLIST], session_minutes=50)

    assert book.role == "video" and book.short == "구자연의 자연스러운 문법"
    assert [(u.label, u.topic, u.minutes) for u in book.units] == [
        ("1강", "1일차 시제", 54),
        ("2강", "2일차 수동태&수일치", 67),
        ("3강", "3일차 to부정사&동명사", 56),
        # 130분 영상은 50분 세션 하나에 안 들어가니 셋으로 — 안 나누면 그 세션에 2시간이 필요하다.
        ("4강 (1/3)", "4일차 분사", 44),
        ("4강 (2/3)", "4일차 분사", 44),
        ("4강 (3/3)", "4일차 분사", 44),
    ]


def test_channel_tags_on_every_title_are_not_taken_as_the_topic() -> None:
    item = MaterialItem(
        kind="video",
        title="[해커스] 토익 문법",
        units=[
            MaterialItemUnit(title=f"[해커스] 토익 {i}강 - {t}", minutes=30)
            for i, t in enumerate(["시제", "수동태", "분사"], start=1)
        ],
    )

    (book,) = curriculum_plan.books_from_materials([item])

    assert [(u.label, u.topic) for u in book.units] == [
        ("1강", "시제"),
        ("2강", "수동태"),
        ("3강", "분사"),
    ]


def test_one_odd_video_at_the_end_does_not_keep_the_series_prefix_on_every_topic() -> None:
    """실측 재생목록: 9편 시리즈 뒤에 '총정리' 1편 — 전체 공통 머리는 "토익 " 뿐이다."""
    series = [f"토익 독학러 문법 정복 프로젝트 [{i}일차 주제{i}]" for i in range(1, 4)]
    item = MaterialItem(
        kind="video",
        title="문법",
        units=[
            MaterialItemUnit(title=t, minutes=40)
            for t in [*series, "토익 문법, 이 영상 하나로 완성❕ 빠짐없이 모두 정리"]
        ],
    )

    (book,) = curriculum_plan.books_from_materials([item])

    assert [u.topic for u in book.units] == [
        "1일차 주제1",
        "2일차 주제2",
        "3일차 주제3",
        "문법, 이 영상 하나로 완성 빠짐없이 모두",  # 이모지는 떼고 24자에서 자른다
    ]


def test_book_chapters_keep_their_labels_and_weigh_by_pages() -> None:
    (book,) = curriculum_plan.books_from_materials([JAVA], session_minutes=50)

    labels = [u.label for u in book.units]
    assert labels[:3] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert labels[-1] == "나머지" and book.units[-1].topic == "목차 밖 분량"
    # 41쪽 × (50분 / 42쪽) ≈ 49분, 26쪽 ≈ 31분 — 확정 화면의 세션당 쪽수를 그대로 따른다.
    assert [u.minutes for u in book.units[:2]] == [49, 31]
    assert book.role == "book" and book.publisher == "남궁성"


@pytest.mark.parametrize(
    ("title", "label", "topic"),
    [
        ("제2장 변수", "제2장", "변수"),
        ("3. 연산자", "3장", "연산자"),
        ("PART 1 기초", "PART 1", "기초"),
        ("들어가며", "4장", "들어가며"),
    ],
)
def test_chapter_labels_follow_the_toc_numbering(title: str, label: str, topic: str) -> None:
    item = MaterialItem(
        kind="book",
        title="책",
        page_count=100,
        units=[MaterialItemUnit(title=t) for t in ["가", "나", "다", title]],
    )

    (book,) = curriculum_plan.books_from_materials([item])

    assert (book.units[-1].label, book.units[-1].topic) == (label, topic)


async def test_toc_book_plan_covers_every_chapter_without_the_llm(no_llm: None) -> None:
    """기준선(LLM 분해)은 장 경계를 세션 수에 맞춰 제멋대로 쪼개고 합쳤다(7·11장을 둘로, 15·16장을
    한 세션으로) — 마일스톤과 세션이 따로 놀 수 있다. 규칙 배정은 목차 순서를 그대로 지킨다."""
    outcome = _outcome(curriculum=[], weeks=4, material_items=[JAVA])
    ms = first_plan_milestones.curriculum_milestones(outcome, cycle_weeks=4, target_date=TARGET)
    assert ms is not None

    state = await first_plan.decompose_goal(_state(outcome, ms), {"configurable": {}})

    plan = state["goal_plan"]
    assert plan is not None and state["curriculum_plan"] is True
    assert len(plan.action_items) == 16
    labels: list[str] = []
    for a in plan.action_items:
        for part in a.title.split(" + "):
            label = part.removeprefix("java의 정석 기초편 ").split(" · ")[0].split(" (")[0]
            if not labels or labels[-1] != label:  # 나눠 보는 긴 장("Chapter 13 (1/2)")은 한 번
                labels.append(label)
    # 모든 장이 책 순서대로 — 빠진 장도 지어낸 장도 없다.
    assert labels == [f"Chapter {i}" for i in range(1, 16)] + ["나머지"]
    assert (
        plan.action_items[0].first_step == "java의 정석 기초편 Chapter 1 펴고 첫 두 쪽만 읽어 보기"
    )


async def test_playlist_plan_goes_video_by_video_without_the_llm(no_llm: None) -> None:
    outcome = _outcome(curriculum=[], weeks=4, freq=2, material_items=[PLAYLIST])

    state = await first_plan.decompose_goal(_state(outcome), {"configurable": {}})

    plan = state["goal_plan"]
    assert plan is not None and state["curriculum_plan"] is True
    titles = [a.title for a in plan.action_items]
    # 주 2회 × 4주 = 8세션 — 130분 영상은 60분 세션 둘로 나눠 영상 5조각이 순서대로, 나머지는 복습.
    assert titles[0] == "구자연의 자연스러운 문법 1강 · 1일차 시제"
    assert [t for t in titles if not t.startswith("복습")][-1].endswith("4강 (2/2) · 4일차 분사")
    assert plan.action_items[0].first_step.endswith("재생하고 첫 5분만 따라 보기")


def test_cards_and_structured_materials_share_one_plan(cards: None) -> None:
    ms = first_plan_milestones.curriculum_milestones(
        _outcome(curriculum=["voca"], material_items=[PLAYLIST]), cycle_weeks=4, target_date=TARGET
    )

    assert ms is not None
    assert "VOCA DAY 01" in ms[0].title and "구자연의 자연스러운 문법 1강" in ms[0].title
