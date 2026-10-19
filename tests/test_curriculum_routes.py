"""교재 카드 추천·확정 — `study-method` 의 `curatedBooks` 와 `POST /plans/materials/curriculum-confirm`.

지키는 것:
  ① 토익 목표에만 카드를 추천한다 — 현재·목표 점수로 기본 선택과 대안을 가른다.
  ② 확정은 **카드 id 만** 받고 항목은 서버 카드로 만든다 — `catalog_id` 가 슬롯에 실려야
     다음 계획이 카드 경로를 탄다.
  ③ 모르는 id 는 422(field=bookIds) — 고른 책을 조용히 빼지 않는다.
  ④ 우리가 쓴 추천 문구·안내는 금지어 필터를 통과한다.
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import Any

import pytest
from fastapi.testclient import TestClient

from reaction_backend.api.routes import materials
from reaction_backend.domain import curriculum as cur
from reaction_backend.domain import slot_value as sv
from reaction_backend.orchestrator import curriculum_plan, interview_adapter
from reaction_backend.safety.banned_words import scan
from reaction_backend.schemas.interview import GoalCandidate
from tests.conftest import FakeInterviewRepo, _FakeSession
from tests.test_curriculum import _book
from tests.test_materials_catalog_route import _stub_study_method
from tests.test_materials_routes import _seed_finished_interview, _use_session

VOCA = _book("voca", "기출 VOCA", 30, role="vocab", minutes=20, label="DAY {i:02d}")
STARTER = _book("starter-rc", "왕기초 RC", 20, role="starter")
AIO_LOW = replace(_book("aio-600", "600+", 24, role="all_in_one"), score_band=(0, 549))
AIO_MID = replace(_book("aio-700", "700+", 24, role="all_in_one"), score_band=(550, 699))
CORE_RC = _book("core-rc", "RC 기본서", 30, role="core")
CORE_LC = _book("core-lc", "LC 기본서", 30, role="core")
PRACTICE = _book("ets-rc", "ETS RC", 0, role="practice", tests=10, test_minutes=75)
ALL = (VOCA, STARTER, AIO_LOW, AIO_MID, CORE_RC, CORE_LC, PRACTICE)


@pytest.fixture(autouse=True)
def cards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cur, "books", lambda: ALL)


def _goal(title: str, level: str | None) -> GoalCandidate:
    return GoalCandidate(
        title=title, category="study", is_heaviest=True, confidence=1.0, current_level=level
    )


def _picked(goal: GoalCandidate) -> tuple[list[str], list[str]]:
    recs = curriculum_plan.recommend(goal)
    return [r.book.id for r in recs if r.recommended], [
        r.book.id for r in recs if not r.recommended
    ]


# ─────────────────────────── ① 추천 규칙 ───────────────────────────


def test_mid_level_to_700_gets_the_matching_all_in_one_and_vocab_becomes_optional() -> None:
    chosen, optional = _picked(_goal("토익 700점", "작년 모의고사 550점 정도"))

    # 한 권짜리엔 단어 DAY 가 있다 — 단어장까지 기본으로 고르면 같은 일을 두 번 한다.
    assert chosen == ["aio-700"]
    # 단어장·기본서는 대안, 모의고사집은 선택 — 기본으로 고르지 않는다.
    assert optional == ["voca", "core-rc", "core-lc", "ets-rc"]


def test_beginner_gets_starter_books() -> None:
    chosen, _ = _picked(_goal("TOEIC 600 넘기기", "토익은 처음이에요"))

    assert chosen == ["voca", "starter-rc"]


def test_high_target_gets_part_by_part_core_books() -> None:
    chosen, optional = _picked(_goal("토익 850점", "지난달 정기시험 680점"))

    assert chosen == ["voca", "core-rc", "core-lc"]
    assert "aio-700" in optional


def test_non_toeic_goal_gets_no_cards() -> None:
    assert curriculum_plan.recommend(_goal("정보처리기사 실기", "필기 합격")) == []
    assert curriculum_plan.recommend(None) == []


def test_score_parsing_ignores_numbers_outside_the_toeic_range() -> None:
    assert curriculum_plan._score("2026년 12월까지 750점") == 750  # noqa: SLF001
    assert curriculum_plan._score("주 3회 30분") is None  # noqa: SLF001


# ─────────────────────────── ② ③ 라우트 ───────────────────────────


def test_study_method_returns_curated_books_for_a_toeic_goal(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_session(client, _FakeSession())
    monkeypatch.setattr(materials.aiClient, "run", _stub_study_method())
    client.portal.call(partial(_seed_finished_interview, goal="토익 700점"), fake_interview_repo)  # type: ignore[attr-defined]

    res = client.post("/plans/materials/study-method", json={})

    assert res.status_code == 200
    books = res.json()["curatedBooks"]
    # 현재 점수를 모르면(시드 인터뷰엔 수준 답이 없다) 구간에 맞는 처음 한 권짜리를 고른다.
    assert [b["id"] for b in books if b["recommended"]] == ["aio-600"]
    voca = next(b for b in books if b["id"] == "voca")
    assert voca["unitCount"] == 30 and voca["role"] == "vocab" and not voca["recommended"]
    assert all(b["reason"] for b in books)


def test_study_method_has_no_curated_books_for_other_goals(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_session(client, _FakeSession())
    monkeypatch.setattr(materials.aiClient, "run", _stub_study_method())
    client.portal.call(_seed_finished_interview, fake_interview_repo)  # type: ignore[attr-defined]

    res = client.post("/plans/materials/study-method", json={})

    assert res.status_code == 200
    assert res.json()["curatedBooks"] == []


def test_curriculum_confirm_writes_card_items_with_catalog_ids(
    client: TestClient, fake_interview_repo: FakeInterviewRepo
) -> None:
    _use_session(client, _FakeSession())
    session_id = client.portal.call(  # type: ignore[attr-defined]
        partial(_seed_finished_interview, goal="토익 700점"), fake_interview_repo
    )

    res = client.post(
        "/plans/materials/curriculum-confirm", json={"bookIds": ["voca", "aio-700", "voca"]}
    )

    assert res.status_code == 200
    body = res.json()
    assert body["goalTitle"] == "토익 700점"
    assert body["bookIds"] == ["voca", "aio-700"]  # 중복은 한 번만
    rows = client.portal.call(fake_interview_repo.list_slot_answers, session_id)  # type: ignore[attr-defined]
    value = next(r.value for r in rows if r.slot_key == "goals.materials")
    assert isinstance(sv.parse(value), sv.SpecAnswer)
    items = value["items"]
    assert [i["catalog_id"] for i in items] == ["voca", "aio-700"]
    # 단원은 서버 카드에서 — LLM 경로로 떨어져도 목차가 텍스트로 남는다.
    assert items[0]["chapters"][0]["title"] == "DAY 01 · 주제1"
    # 이 슬롯으로 만든 outcome 의 heaviest 목표가 카드 경로를 탄다.
    assert interview_adapter._curriculum_ids(value) == ["voca", "aio-700"]  # noqa: SLF001


def test_curriculum_confirm_rejects_unknown_book_ids(
    client: TestClient, fake_interview_repo: FakeInterviewRepo
) -> None:
    _use_session(client, _FakeSession())
    client.portal.call(partial(_seed_finished_interview, goal="토익 700점"), fake_interview_repo)  # type: ignore[attr-defined]

    res = client.post(
        "/plans/materials/curriculum-confirm", json={"bookIds": ["voca", "no-such-book"]}
    )

    assert res.status_code == 422
    assert res.json()["field"] == "bookIds"


def test_curriculum_confirm_without_an_interview_is_a_clear_422(client: TestClient) -> None:
    res = client.post("/plans/materials/curriculum-confirm", json={"bookIds": ["voca"]})

    assert res.status_code == 422


# ─────────────────────────── ④ 문구 ───────────────────────────


@pytest.mark.parametrize(
    ("title", "level"),
    [
        ("토익 700점", "550점"),
        ("토익 600", "처음이에요"),
        ("토익 900점", "780점"),
        ("토익 목표", None),
    ],
)
def test_recommendation_reasons_pass_the_banned_word_filter(title: str, level: str | None) -> None:
    for r in curriculum_plan.recommend(_goal(title, level)):
        assert scan(r.reason) == (), f"금지어가 들어간 문구: {r.reason!r}"


def test_overload_notice_passes_the_banned_word_filter() -> None:
    sp = cur.spread(cur.phases([VOCA, CORE_RC], n_phases=1), n_sessions=4, session_minutes=30)
    notice: Any = curriculum_plan._overload_notice(sp)  # noqa: SLF001

    assert scan(notice) == ()
