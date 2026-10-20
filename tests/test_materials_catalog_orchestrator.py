"""materials_catalog 오케스트레이터 (ADR-0010 §1 ②) — 두 소스 조합·부분 실패 처리.

라우터 테스트는 이 모듈 자체를 stub 하므로, reason→notice 매핑과 "한쪽만 죽어도 다른 쪽은
그대로 낸다" 는 여기서 직접 검증한다. 클라이언트는 stub 한다(`test_seoji_client.py`·
`test_youtube_client.py` 가 클라이언트 자체는 이미 검증했다).
"""

from __future__ import annotations

import pytest

from reaction_backend.integrations.nl_seoji import client as seoji_client
from reaction_backend.integrations.youtube import client as youtube_client
from reaction_backend.orchestrator import materials_catalog


async def test_only_the_requested_sources_are_queried(monkeypatch: pytest.MonkeyPatch) -> None:
    """검색어가 없는 소스는 아예 부르지 않는다 — 쿼터를 아낀다."""

    async def _explode_book(*a: object, **k: object) -> object:
        raise AssertionError("bookQuery 없이 도서 검색을 부르면 안 된다")

    monkeypatch.setattr(seoji_client, "search_books", _explode_book)
    monkeypatch.setattr(youtube_client, "search_playlists", lambda *a, **k: _ok_playlists())

    result = await materials_catalog.search(book_query=None, video_query="토익 강의")

    assert result.books == []
    assert result.book_notice is None
    assert len(result.videos) == 1


async def _ok_books() -> seoji_client.SearchResult:
    return seoji_client.SearchResult(
        books=[
            seoji_client.BookHit(
                title="해커스 토익",
                author="A",
                publisher="P",
                isbn13="9788965422785",
                page_count=556,
                cover_url="",
            )
        ]
    )


async def _ok_playlists() -> youtube_client.SearchResult:
    return youtube_client.SearchResult(
        playlists=[
            youtube_client.PlaylistResult(
                playlist_id="PL1",
                title="토익 강의",
                channel_title="C",
                thumbnail_url="",
                playlist_url="",
            )
        ]
    )


async def test_book_failure_does_not_lose_video_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fail(*a: object, **k: object) -> seoji_client.SearchResult:
        return seoji_client.SearchResult(reason=seoji_client.REASON_EMPTY)

    monkeypatch.setattr(seoji_client, "search_books", _fail)
    monkeypatch.setattr(youtube_client, "search_playlists", lambda *a, **k: _ok_playlists())

    result = await materials_catalog.search(book_query="존재안함", video_query="토익 강의")

    assert result.books == []
    assert result.book_notice == (
        "이 검색어로는 도서를 찾지 못했어요. 책 제목에 들어가는 낱말로 바꿔 보세요."
    )
    assert len(result.videos) == 1
    assert result.video_notice is None


async def test_video_quota_failure_does_not_lose_book_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _quota(*a: object, **k: object) -> youtube_client.SearchResult:
        return youtube_client.SearchResult(reason=youtube_client.REASON_QUOTA)

    monkeypatch.setattr(seoji_client, "search_books", lambda *a, **k: _ok_books())
    monkeypatch.setattr(youtube_client, "search_playlists", _quota)

    result = await materials_catalog.search(book_query="토익", video_query="토익 강의")

    assert len(result.books) == 1
    assert result.videos == []
    assert "다 썼어요" in (result.video_notice or "")


async def test_unmapped_reason_falls_back_to_a_generic_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _weird(*a: object, **k: object) -> seoji_client.SearchResult:
        return seoji_client.SearchResult(reason="something_new")

    monkeypatch.setattr(seoji_client, "search_books", _weird)

    result = await materials_catalog.search(book_query="토익", video_query=None)

    assert result.book_notice == materials_catalog._FALLBACK_NOTICE


async def test_book_candidates_carry_the_page_count_from_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """이 소스는 후보 단계에서 쪽수를 이미 안다 — 버리지 않고 싣는다(모르면 null)."""
    monkeypatch.setattr(seoji_client, "search_books", lambda *a, **k: _ok_books())

    result = await materials_catalog.search(book_query="토익", video_query=None)

    assert [(b.isbn13, b.page_count, b.link_url) for b in result.books] == [
        ("9788965422785", 556, "")
    ]
