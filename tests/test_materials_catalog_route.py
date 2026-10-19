"""ADR-0010 자료 검색 파이프라인 — `/plans/materials/study-method` · `/plans/materials/catalog`.

기존 ①②③(#259) 과 독립된 새 엔드포인트다(ADR-0010 §4 — 교체가 아니라 병행). 이 파일이
지키는 것:

  ① **study-method 는 아직 아무것도 검색하지 않는다** — LLM 구조화 호출 1회로 검색어를
     "제안" 할 뿐, catalog 를 사용자가 따로 눌러야 실제로 나간다(#259 §4.1 ① 결정과 같은
     원칙 — 여기 적용하면 다음 원칙: "확인 전엔 안 나간다").
  ② **catalog 는 두 소스가 독립적으로 실패할 수 있다** — 한쪽이 죽어도 다른 쪽 후보는
     그대로 온다.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from reaction_backend.api.routes import materials
from reaction_backend.llm import RunResult
from reaction_backend.schemas.materials_catalog import (
    BookCandidate,
    MaterialsCatalogResponse,
    VideoCandidate,
)
from reaction_backend.schemas.study_method import StudyMethodPlan
from tests.conftest import FakeInterviewRepo, _FakeSession
from tests.test_materials_routes import _GOAL, _seed_finished_interview, _use_session

_PLAN = StudyMethodPlan(
    approach="RC 문법과 LC 딕테이션을 집중하는 게 효율적이에요.",
    focus_points=["RC 파트5 문법", "LC 딕테이션"],
    book_query="해커스 토익 RC 문법 기본서",
    video_query="토익 RC 문법 강의",
    material_mix="both",
)


def _stub_study_method(*, fell_back: bool = False, plan: StudyMethodPlan = _PLAN) -> Any:
    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        value = kwargs["fallback"]() if fell_back else plan
        return RunResult(
            value=value,
            fell_back=fell_back,
            reason="timeout" if fell_back else None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    return stub_run


# ─────────────────────── study-method ───────────────────────


def test_study_method_returns_approach_and_two_queries(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_session(client, _FakeSession())
    monkeypatch.setattr(materials.aiClient, "run", _stub_study_method())
    client.portal.call(_seed_finished_interview, fake_interview_repo)  # type: ignore[attr-defined]

    res = client.post("/plans/materials/study-method", json={})

    assert res.status_code == 200
    body = res.json()
    assert body["goalTitle"] == _GOAL
    assert body["bookQuery"] == "해커스 토익 RC 문법 기본서"
    assert body["videoQuery"] == "토익 RC 문법 강의"
    assert body["materialMix"] == "both"
    assert body["focusPoints"] == ["RC 파트5 문법", "LC 딕테이션"]
    assert body["isDraft"] is True
    assert body["aiSource"] == "llm"
    # 아직 검색으로 나간 게 아니라는 사실을 사용자가 알아야 편집할 이유가 생긴다(원칙 ①).
    assert "검색" in body["notice"]


@pytest.mark.parametrize("mix", ["book", "video"])
def test_study_method_passes_the_agents_material_mix_through(
    client: TestClient,
    fake_interview_repo: FakeInterviewRepo,
    monkeypatch: pytest.MonkeyPatch,
    mix: str,
) -> None:
    """에이전트가 고른 조합이 응답에 그대로 실린다 — 기본값("both")과 다른 값으로 확인한다.

    응답을 만들며 이 필드를 빠뜨려, 에이전트가 무엇을 골랐든 클라이언트는 늘 기본값 "both"
    를 받았다. 위 테스트는 에이전트도 "both" 를 고르는 경우라 그 누락을 볼 수 없었다.
    """
    _use_session(client, _FakeSession())
    plan = _PLAN.model_copy(update={"material_mix": mix})
    monkeypatch.setattr(materials.aiClient, "run", _stub_study_method(plan=plan))
    client.portal.call(_seed_finished_interview, fake_interview_repo)  # type: ignore[attr-defined]

    res = client.post("/plans/materials/study-method", json={})

    assert res.status_code == 200
    assert res.json()["materialMix"] == mix


def test_study_method_falls_back_to_rule_and_reports_ai_source(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_session(client, _FakeSession())
    monkeypatch.setattr(materials.aiClient, "run", _stub_study_method(fell_back=True))
    client.portal.call(_seed_finished_interview, fake_interview_repo)  # type: ignore[attr-defined]

    res = client.post("/plans/materials/study-method", json={})

    assert res.status_code == 200
    body = res.json()
    assert body["aiSource"] == "rule"
    # 룰 폴백도 유효한 질의를 내야 한다 — 빈 문자열이면 catalog 가 422 를 낸다.
    assert body["bookQuery"]
    assert body["videoQuery"]


def test_study_method_without_an_interview_is_a_clear_422(client: TestClient) -> None:
    assert client.post("/plans/materials/study-method", json={}).status_code == 422


# ─────────────────────────── catalog ───────────────────────────


def test_catalog_returns_candidates_from_both_sources(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _stub_search(*, book_query: str | None, video_query: str | None) -> Any:
        assert book_query == "해커스 토익 RC 문법 기본서"
        assert video_query == "토익 RC 문법 강의"
        return MaterialsCatalogResponse(
            books=[
                BookCandidate(
                    title="해커스 토익 RC 리딩",
                    author="David Cho",
                    publisher="해커스어학연구소",
                    isbn13="9788965422389",
                    cover_url="https://image.aladin.co.kr/cover.jpg",
                    link_url="https://www.aladin.co.kr/shop/wproduct.aspx?ItemId=1",
                )
            ],
            videos=[
                VideoCandidate(
                    playlist_id="PL1",
                    title="토익 RC 문법",
                    channel_title="해커스토익",
                    thumbnail_url="https://i.ytimg.com/vi/x/mqdefault.jpg",
                    playlist_url="https://www.youtube.com/playlist?list=PL1",
                )
            ],
        )

    monkeypatch.setattr(materials.materials_catalog, "search", _stub_search)

    res = client.post(
        "/plans/materials/catalog",
        json={"bookQuery": "해커스 토익 RC 문법 기본서", "videoQuery": "토익 RC 문법 강의"},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["books"][0]["title"] == "해커스 토익 RC 리딩"
    assert body["videos"][0]["title"] == "토익 RC 문법"
    assert body["bookNotice"] is None
    assert body["videoNotice"] is None


def test_catalog_one_source_failing_does_not_lose_the_other(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """두 소스는 독립적으로 실패한다 — 영상 쿼터가 다 찼다고 도서 후보까지 잃으면 안 된다."""

    async def _stub_search(*, book_query: str | None, video_query: str | None) -> Any:
        return MaterialsCatalogResponse(
            books=[
                BookCandidate(
                    title="해커스 토익 RC 리딩",
                    author="David Cho",
                    publisher="해커스어학연구소",
                    isbn13="9788965422389",
                    cover_url="",
                    link_url="",
                )
            ],
            videos=[],
            video_notice="오늘 쓸 수 있는 영상 검색을 다 썼어요. 내일 다시 시도해 주세요.",
        )

    monkeypatch.setattr(materials.materials_catalog, "search", _stub_search)

    res = client.post(
        "/plans/materials/catalog",
        json={"bookQuery": "해커스 토익 RC 문법 기본서", "videoQuery": "토익 RC 문법 강의"},
    )

    assert res.status_code == 200
    body = res.json()
    assert len(body["books"]) == 1
    assert body["videos"] == []
    assert "다 썼어요" in body["videoNotice"]


def test_catalog_requires_at_least_one_query(client: TestClient) -> None:
    assert client.post("/plans/materials/catalog", json={}).status_code == 422


def test_catalog_requires_auth(unauthed_client: TestClient) -> None:
    res = unauthed_client.post("/plans/materials/catalog", json={"bookQuery": "토익"})
    assert res.status_code == 401
