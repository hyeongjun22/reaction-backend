"""국립중앙도서관 ISBN 서지정보 클라이언트 — 도서 검색 · ISBN 조회(쪽수 + 목차 best-effort).

알라딘(2026-10-30 종료)을 대신한다. 목차는 best-effort 다 — 비어 있는 건 실패가 아니라 이
소스의 기본 동작이다(L0 실측: 도서 10권 중 9권). 네트워크는 타지 않는다 — `requests.get` 을
대체한다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
import requests

from reaction_backend.integrations.nl_seoji import client

# 실측(2026-09-03, "Java의 정석 : 기초편") 형식 그대로 — 개행 없이 점선(···)+페이지번호로
# 항목을 이어붙인 한 덩어리 문자열.
_REAL_TOC = (
    "Chapter 1. 자바를 시작하기 전에01 자바(Java)란? ····20"
    "02 자바의 역사 ····30"
    "Chapter 2. 변수01 변수란? ····52"
    "Chapter 3. 연산자01 연산자와 피연산자 ····98"
)


class _FakeResponse:
    def __init__(self, *, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status_code = status
        self._body = body or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._body


def _docs_body(**fields: str) -> dict[str, Any]:
    return {"docs": [fields]}


def _row(
    title: str, isbn: str, *, form: str = "종이책", page: str = "300 p.", **extra: str
) -> dict[str, str]:
    return {
        "TITLE": title,
        "EA_ISBN": isbn,
        "FORM": form,
        "PAGE": page,
        "AUTHOR": "저자",
        "PUBLISHER": extra.pop("PUBLISHER", "출판사"),
        **extra,
    }


# ─────────────────────────────── ISBN 조회 ───────────────────────────────


async def test_no_key_fails_without_a_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(*a: Any, **k: Any) -> Any:
        raise AssertionError("key 가 없으면 네트워크를 타면 안 된다")

    monkeypatch.setattr(client.requests, "get", _explode)
    assert (await client.lookup_book("9788994492049", key="")).reason == client.REASON_NO_KEY
    assert (await client.search_books("자바", key="")).reason == client.REASON_NO_KEY


async def test_lookup_returns_pages_and_toc_in_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def _get(*a: Any, **k: Any) -> Any:
        calls.append(k["params"])
        return _FakeResponse(
            body=_docs_body(
                TITLE="Java의 정석 : 기초편",
                AUTHOR="남궁성",
                PUBLISHER="도우출판",
                EA_ISBN="9788994492049",
                PAGE="756 p.",
                BOOK_TB_CNT=_REAL_TOC,
            )
        )

    monkeypatch.setattr(client.requests, "get", _get)

    result = await client.lookup_book("9788994492049", key="testkey")

    assert len(calls) == 1  # 예전엔 쪽수(알라딘)·목차(seoji)를 두 번 불렀다
    assert result.ok and result.lookup is not None
    book = result.lookup
    assert (book.title, book.publisher, book.page_count) == (
        "Java의 정석 : 기초편",
        "도우출판",
        756,
    )
    assert [c.title for c in book.chapters] == [
        "Chapter 1. 자바를 시작하기 전에",
        "Chapter 2. 변수",
        "Chapter 3. 연산자",
    ]
    assert [c.end_page for c in book.chapters] == [30, 52, 98]
    assert book.has_page_checkpoints is True


async def test_non_monotonic_page_markers_are_dropped_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """페이지 번호가 거꾸로 가면 파싱이 실제로는 틀렸다는 뜻이다 — 잘못된 숫자를 사실인
    척 내보내는 것보다 전부 버리는 게 낫다(챕터 제목 목록 자체는 그래도 남는다)."""
    broken_toc = (
        "Chapter 1. 서론 부분입니다01 시작하며 배경을 설명합니다 ····50"
        "Chapter 2. 본론 부분입니다01 전개를 자세히 다룹니다 ····10"  # 역행 — 파싱 오류를 흉내
        "Chapter 3. 결론 부분입니다01 마무리를 정리합니다 ····80"
    )
    assert len(broken_toc) >= 80  # _TOC_MIN_CHARS 게이트를 통과해야 챕터 파싱까지 간다
    body = _docs_body(TITLE="어떤 책", BOOK_TB_CNT=broken_toc)
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=body))

    result = await client.lookup_book("9780000000001", key="testkey")

    assert result.lookup is not None
    assert result.lookup.has_page_checkpoints is False
    assert len(result.lookup.chapters) == 3  # 챕터 목록 자체는 살아남는다


async def test_does_not_guess_the_field_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """목차가 `BOOK_TB_CNT` 가 아닌 다른 필드에 와도 챕터 패턴으로 찾아낸다."""
    body = _docs_body(TITLE="어떤 책", SOME_OTHER_FIELD=_REAL_TOC)
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=body))

    result = await client.lookup_book("9780000000000", key="testkey")

    assert result.lookup is not None and len(result.lookup.chapters) == 3


@pytest.mark.parametrize(
    "fields",
    [
        {"TITLE": "Chapter 1 이 들어간 책 제목", "BOOK_TB_CNT": "", "PAGE": "556 p."},
        {"TITLE": "해커스 토익", "BOOK_TB_CNT": "", "PAGE": "556 p."},
    ],
)
async def test_missing_toc_is_still_a_found_book(
    monkeypatch: pytest.MonkeyPatch, fields: dict[str, str]
) -> None:
    """목차가 없거나(대부분의 판본) 짧은 필드에 챕터 낱말이 우연히 있어도 — 책은 찾은 것이다.
    쪽수만으로 계획에 쓸 수 있으므로 실패로 돌려주면 안 된다."""
    monkeypatch.setattr(
        client.requests, "get", lambda *a, **k: _FakeResponse(body=_docs_body(**fields))
    )

    result = await client.lookup_book("9788965422785", key="testkey")

    assert result.ok and result.lookup is not None
    assert result.lookup.chapters == []
    assert result.lookup.page_count == 556


async def test_not_found_when_docs_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body={"docs": []}))
    result = await client.lookup_book("0000000000000", key="testkey")
    assert result.reason == client.REASON_NOT_FOUND


async def test_result_error_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"RESULT": "ERROR", "ERR_CODE": "010", "ERR_MESSAGE": "인증키 정보가 없습니다."}
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=body))
    assert (
        await client.lookup_book("9788965424765", key="bad")
    ).reason == client.REASON_UNAVAILABLE
    assert (await client.search_books("토익", key="bad")).reason == client.REASON_UNAVAILABLE


async def test_timeout_is_reported_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(*a: Any, **k: Any) -> Any:
        raise requests.Timeout()

    monkeypatch.setattr(client.requests, "get", _timeout)
    assert (await client.lookup_book("9788994492049", key="k")).reason == client.REASON_TIMEOUT
    assert (await client.search_books("자바", key="k")).reason == client.REASON_TIMEOUT


@pytest.mark.parametrize(
    ("raw", "pages"),
    [
        ("556 p.", 556),
        ("464", 464),
        ("약 446페이지 내외", 446),
        ("186, 307 p.", 493),  # 여러 권 — 합한다
        ("413 p. (필수문제)", 413),
        ("1", 0),  # 세트 행 표기 — 쪽수로 믿지 않는다
        ("", 0),
        (None, 0),
    ],
)
def test_page_count_parsing(raw: str | None, pages: int) -> None:
    assert client.parse_page_count(raw) == pages


# ─────────────────────────────── 검색 ───────────────────────────────


async def test_search_keeps_paper_books_with_isbn13_newest_first_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []
    rows = [
        _row("해커스 토익 실전 LC+RC", "9788965427483", page="256 p."),
        _row("해커스 토익 실전 LC+RC", "9788965420000"),  # 같은 책 옛 판 — 최신순이라 뒤에 온다
        _row("해커스 토익 전자책", "9788965421111", form="전자책"),
        _row("해커스 토익 옛 ISBN", "8965421111"),  # ISBN10 — 상세 조회를 못 한다
        _row("해커스 토익 VOCA", "9788965422785", page="약 556페이지 내외"),
    ]

    def _get(*a: Any, **k: Any) -> Any:
        seen.append(k["params"])
        return _FakeResponse(body={"docs": rows})

    monkeypatch.setattr(client.requests, "get", _get)

    result = await client.search_books("해커스 토익", key="k", limit=5, today=date(2026, 10, 21))

    assert [(b.title, b.isbn13, b.page_count) for b in result.books] == [
        ("해커스 토익 실전 LC+RC", "9788965427483", 256),
        ("해커스 토익 VOCA", "9788965422785", 556),
    ]
    params = seen[0]
    assert params["title"] == "해커스 토익"
    assert params["ebook_yn"] == "N"
    assert (params["sort"], params["order_by"]) == ("PUBLISH_PREDATE", "DESC")
    # 발행예정일이 미래로 잘못 들어간 행이 최신순 맨 위를 차지하지 않게 오늘까지로 자른다.
    assert params["end_publish_date"] == "20261021"


async def test_search_drops_trailing_words_until_something_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """표제 검색은 모든 낱말이 맞아야 걸린다 — LLM 이 만든 "… 문법 기본서" 같은 설명형
    검색어가 0건으로 끝나지 않게 뒤 낱말을 떼며 다시 찾는다."""
    queries: list[str] = []

    def _get(*a: Any, **k: Any) -> Any:
        queries.append(k["params"]["title"])
        docs = [_row("해커스 토익 RC", "9788965424765")] if len(queries) == 3 else []
        return _FakeResponse(body={"docs": docs})

    monkeypatch.setattr(client.requests, "get", _get)

    result = await client.search_books("해커스 토익 RC 문법 기본서", key="k")

    assert queries == ["해커스 토익 RC 문법 기본서", "해커스 토익 RC 문법", "해커스 토익 RC"]
    assert [b.isbn13 for b in result.books] == ["9788965424765"]


async def test_search_gives_up_after_a_bounded_number_of_shortenings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []

    def _get(*a: Any, **k: Any) -> Any:
        queries.append(k["params"]["title"])
        return _FakeResponse(body={"docs": []})

    monkeypatch.setattr(client.requests, "get", _get)

    result = await client.search_books("아주 긴 설명형 검색어 여섯 낱말", key="k")

    assert result.reason == client.REASON_EMPTY
    assert len(queries) == 4  # 원문 + 3번 줄이기
    assert queries[-1] == "아주 긴 설명형"


async def test_search_respects_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_row(f"책 {i}", f"97889654{i:05d}") for i in range(10)]
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body={"docs": rows}))

    result = await client.search_books("책", key="k", limit=3)

    assert len(result.books) == 3


def test_korean_chapter_headers_are_not_split_in_the_middle() -> None:
    """ "제10장" 하나가 '제'·'1'·'0장' 으로 쪼개져 가짜 챕터가 끼던 회귀 (inbox-5)."""
    entries = client._split_toc_entries("제1장 A···5 제2장 B···10 제10장 C···50")
    assert [client._chapter_title(e) for e in entries] == ["제1장 A", "제2장 B", "제10장 C"]
    assert client._chapter_end_pages(entries) == [5, 10, 50]


def test_text_before_the_first_header_is_kept_as_its_own_entry() -> None:
    """머리말처럼 첫 헤더 앞에 오는 부분은 예전처럼 따로 한 항목으로 남는다."""
    entries = client._split_toc_entries("머리말···3 DAY 01 인사···10 DAY 02 날씨···20")
    assert entries == ["머리말···3", "DAY 01 인사···10", "DAY 02 날씨···20"]


@pytest.mark.parametrize("kind", ["connection", "http"])
async def test_failure_log_does_not_leak_the_api_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    """`requests` 예외 문자열엔 요청 URL 전체(`cert_key` 포함)가 담긴다 — 로그에 남기면
    API 키가 평문으로 쌓인다(inbox-7)."""
    secret = "SECRET123"
    leaky_url = f"https://seoji.nl.go.kr/landingPage/SearchApi.do?cert_key={secret}&isbn=1"

    class _HttpErrorResponse(_FakeResponse):
        def raise_for_status(self) -> None:
            raise requests.HTTPError(f"503 Server Error for url: {leaky_url}", response=self)

    def _get(*a: Any, **k: Any) -> Any:
        if kind == "connection":
            raise requests.ConnectionError(f"Max retries exceeded with url: {leaky_url}")
        return _HttpErrorResponse(status=503)

    monkeypatch.setattr(client.requests, "get", _get)
    with caplog.at_level("DEBUG"):
        lookup = await client.lookup_book("9788994492049", key=secret)
        search = await client.search_books("자바", key=secret)
    assert lookup.reason == client.REASON_UNAVAILABLE
    assert search.reason == client.REASON_UNAVAILABLE
    assert "seoji request failed" in caplog.text
    assert secret not in caplog.text
