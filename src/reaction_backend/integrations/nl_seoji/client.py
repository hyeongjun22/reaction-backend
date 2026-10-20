"""국립중앙도서관 ISBN 서지정보 API — 도서 검색 · 쪽수 · 목차(best-effort) (ADR-0010 §1 ②③).

**알라딘 Open API 를 대신한다.** 알라딘은 2026-09-04 에 신규 키 발급을 끝냈고 2026-10-30 에
서비스를 종료한다(알라딘 고객센터 공지). 그 API 가 하던 두 일 — 후보 검색과 ISBN → 쪽수 —
을 이 API 한 키로 한다(`search_books` · `lookup_book`). 같은 응답에 목차(`BOOK_TB_CNT`)도
실려 오므로 예전처럼 쪽수(알라딘)·목차(seoji)를 두 번 부르지 않는다.

검색에서 알아 둘 것(2026-10-21 실측):
- 표제(`title`) 검색은 **모든 낱말이 맞아야** 걸린다 — "해커스 토익 기출 VOCA" 는 0건인데
  "해커스 토익 기출 보카" 는 걸린다. 그래서 0건이면 뒤 낱말을 하나씩 떼고 다시 찾는다.
- 판매 순위가 없다. 대신 **최신 발행순**(종이책만)으로 받는다 — 수험서는 최신판이 맞는 책이다.
- 표지 이미지(`TITLE_URL`)는 대개 비어 있고, 쪽수(`PAGE`) 표기가 제각각이다("556 p.",
  "464", "약 446페이지 내외", "186, 307 p.").

아래는 목차 파싱에 관한 원래 설명이다 — 목차는 best-effort 다(ADR-0010 §1 ③).


L0 스파이크(`docs/experiments/l0-materials-source-results.md` §3.1)가 확인한 사실:
`BOOK_TB_CNT` 필드가 목차를 담고 있지만 **판본에 따라 10권 중 1권만 채워져 있다.** 같은
저자·시리즈의 "기초편" 은 있고 "3rd Edition" 은 없는 식이라 판별 규칙이 없다 — 그래서
"안 되는 게 정상" 이고, 실패는 오류가 아니라 이 소스의 기본 동작이다.

**필드명을 추측하지 않는다.** 알라딘 `OptResult=Toc` 를 문서만 믿고 가정했다가 0/10 으로
틀린 실수를 반복하지 않기 위해, 응답으로 온 모든 필드를 펼쳐 목차처럼 생긴 값(길고,
챕터 패턴이 잡히는 값)을 찾는다 — `BOOK_TB_CNT` 가 표준이지만 고정하지 않는다.

목차 형식도 알라딘과 다르다 — `<br>` 구분 없이 점선(···)+페이지번호로 항목을 이어붙인
한 덩어리 문자열이다(실측: "Java의 정석 : 기초편" 59,105자, 개행 0건). 그래서 줄 시작
앵커에 기대지 않는 챕터 패턴만 쓴다 — "Chapter"/"장"/"PART"/"DAY" 류는 목차 밖 본문에
흔치 않아 앵커 없이도 안전하다.

**목차가 있을 때는 챕터별 종료 페이지도 뽑는다**(`TocChapter.end_page`) — 소단원 항목의
점선 뒤 페이지 번호를 챕터 경계마다 마지막 것만 취한다(실측: 15챕터에서 41→67→95→...→700
으로 단조증가, 오염 없이 깨끗했다). 단조증가가 깨지면 파싱이 틀렸다는 뜻이라 **전부
버린다** — 페이지 체크포인트가 없어도 챕터 제목 목록 자체는 남는다.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

import requests

logger = logging.getLogger(__name__)

# ⚠️ 공식 안내(국립중앙도서관 Open API > 활용방법 > ISBN 서지정보)의 주소는
# `https://www.nl.go.kr/seoji/SearchApi.do` 인데, 2026-10-21 실측에서 `requests` 로는 그 호스트에
# **연결 자체가 안 됐다**(ConnectTimeout 10초 × 3/3). 같은 키·같은 파라미터로 이 주소는
# 0.6~0.8초에 응답한다. 안내 주소로 "고치지" 말 것 — 바꾸려면 서버에서 실제로 재 보고 바꾼다.
_SEARCH_URL: Final = "https://seoji.nl.go.kr/landingPage/SearchApi.do"
_CONNECT_TIMEOUT: Final = 3.0
_READ_TIMEOUT: Final = 5.0
_HARD_TIMEOUT: Final = 8.0

REASON_NO_KEY: Final = "no_key"
REASON_TIMEOUT: Final = "timeout"
REASON_UNAVAILABLE: Final = "unavailable"
REASON_NOT_FOUND: Final = "not_found"
REASON_EMPTY: Final = "empty"
"""검색 결과가 없다 — 낱말을 줄여 다시 찾아도 0건."""

# 목차로 볼 만한 값의 최소 길이 — 제목·저자 같은 짧은 필드를 목차로 오인하지 않게.
_TOC_MIN_CHARS = 80
# 목차 안에서 "이게 챕터 헤더다" 로 볼 패턴 — 줄 구분자가 없는 문자열에도 안전하도록
# 문맥 키워드에 기댄다(페이지 번호 같은 순수 숫자 나열과는 안 겹친다).
_CHAPTER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:Chapter|CHAPTER|chapter)\s*\.?\s*\d+"),
    re.compile(r"(?:제\s*)?\d+\s*(?:장|과|강|부|주차|일차)\b"),
    re.compile(r"(?:PART|Part|part)\s*\.?\s*[\dIVX]+"),
    re.compile(r"(?:DAY|Day)\s*\.?\s*\d+"),
)


_HEADER_RE = re.compile("|".join(p.pattern for p in _CHAPTER_PATTERNS))


def _split_toc_entries(raw_toc: str) -> list[str]:
    """목차 원문에서 챕터 헤더를 경계로 항목을 끊는다. 못 끊으면 원문 그대로 1개.

    헤더는 **겹치지 않게** 왼쪽부터 찾는다(`finditer`). 예전엔 모든 위치에서 앞을 내다보고
    끊어서(`(?=...)` 로 split) "제10장" 하나가 '제'·'1'·'0장' 위치마다 잘렸다 — "제",
    "1" 같은 가짜 챕터가 끼어 챕터 수가 두세 배로 부풀었다(inbox-5).
    """
    starts = [m.start() for m in _HEADER_RE.finditer(raw_toc)]
    bounds = [0, *starts, len(raw_toc)]
    parts = [raw_toc[a:b].strip() for a, b in itertools.pairwise(bounds)]
    parts = [p for p in parts if p]
    return parts or ([raw_toc.strip()] if raw_toc.strip() else [])


# 점선(···) 뒤에 붙는 페이지 번호. 개행이 없는 원문이라 소단원 항목끼리 바로 이어붙어서
# (예: "···20" 다음에 곧장 "02 자바의 역사") 점선 직후 숫자만 잘라내도 다음 소단원의 앞자리
# 번호까지 붙어 나온다("2002"). 그런데 **한 챕터 블록의 마지막 매치**는 다르다 — 다음
# 챕터 헤더가 "Chapter"/"제N장" 처럼 문자로 시작해서 숫자가 이어지지 않는다. 그래서 항목
# 하나에서 **마지막 매치만** 신뢰한다(실측: "Java의 정석 : 기초편" 15챕터에서 41→67→95→
# 125→...→700 으로 단조증가 — 오염 없이 깨끗했다).
_PAGE_MARKER_RE = re.compile(r"[·.]{3,}(\d+)")


def _chapter_end_page(entry: str) -> int | None:
    """항목(챕터 1개) 원문에서 그 챕터가 끝나는 페이지(추정) — 마지막 소단원의 페이지."""
    matches = _PAGE_MARKER_RE.findall(entry)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except ValueError:
        return None


def _chapter_end_pages(entries: list[str]) -> list[int | None]:
    """항목별 종료 페이지. **단조증가가 깨지면 전부 버린다** — 개별 항목은 그럴싸해도
    파싱이 실제로는 틀렸다는 뜻이라, 잘못된 페이지 번호를 사실인 척 내보내는 것보다
    아예 없는 게 낫다(이 파이프라인 전체의 원칙 — L0 §3 "안 되는 게 정상")."""
    pages = [_chapter_end_page(e) for e in entries]
    known = [p for p in pages if p is not None]
    if len(known) < 2:
        return [None] * len(entries)
    if any(a > b for a, b in zip(known, known[1:], strict=False)):
        return [None] * len(entries)
    return pages


# 소단원 번호("01 ", "02 " ...) — 챕터 항목 원문에서 제목 뒤에 곧장 이어붙는 첫 소단원의
# 시작점. 개행이 없어 챕터 제목과 첫 소단원이 공백 없이 붙는다("...전에01 자바(Java)란?").
_SUBITEM_RE = re.compile(r"\d{1,2}\s")


def _chapter_title(entry: str) -> str:
    """항목 원문에서 챕터 제목만 — 소단원 나열(점선·페이지 번호)은 잘라낸다.

    실측(2026-09-04, "Java의 정석 : 기초편" 15챕터 전수)으로 검증한 휴리스틱: 소단원
    번호 또는 점선 중 **먼저 나오는 지점**에서 자른다. "Chapter 10. 날짜와 시간 & 형식화"
    처럼 제목 자체에 숫자가 있어도 오탐하지 않는다 — `\\d{1,2}\\s` 는 숫자 바로 뒤에
    공백이 와야 하는데 "10." 은 마침표가 끼어 있어 매치되지 않는다.
    """
    sub_match = _SUBITEM_RE.search(entry)
    page_match = _PAGE_MARKER_RE.search(entry)
    cuts = [m.start() for m in (sub_match, page_match) if m]
    return entry[: min(cuts)].strip() if cuts else entry.strip()


def _count_chapters(raw_toc: str) -> int:
    best = 0
    for pat in _CHAPTER_PATTERNS:
        n = len(pat.findall(raw_toc))
        if n > best:
            best = n
    return best


def _find_toc_field(fields: dict[str, str]) -> str | None:
    """모든 필드를 훑어 챕터 패턴이 가장 많이 잡히는 값을 목차로 고른다."""
    best_chapters, best_value = 0, None
    for value in fields.values():
        if len(value) < _TOC_MIN_CHARS:
            continue
        n = _count_chapters(value)
        if n > best_chapters:
            best_chapters, best_value = n, value
    return best_value


@dataclass(frozen=True, slots=True)
class TocChapter:
    """목차 항목 1개 — 소단원 나열은 걷어낸 챕터 제목."""

    title: str
    end_page: int | None
    """이 챕터가 끝나는 페이지(추정) — `None` 이면 이 판본에선 페이지 체크포인트를
    못 만든다(챕터 목록 자체는 여전히 쓸 수 있다)."""


# 한 번에 받아 걸러 낼 후보 수 — 전자책·ISBN 없는 행·같은 책 다른 쇄를 걸러도 `limit` 개가
# 남도록 넉넉히 받는다(실측: "해커스 토익" 최신순 10건 중 같은 제목 중복 2건).
_SEARCH_FETCH_SIZE = 30
# 0건일 때 뒤 낱말을 떼며 다시 찾는 최대 횟수 — 한 번에 0.2초 안팎이라 3번까지 둔다.
_MAX_QUERY_SHORTENINGS = 3
_ISBN13_RE = re.compile(r"^97[89]\d{10}$")
_NUM_RE = re.compile(r"\d+")
# 이보다 작은 쪽수는 세트 표기("1")·낱장 등이라 쪽수로 믿지 않는다.
_MIN_PLAUSIBLE_PAGES = 20


def parse_page_count(raw: str | None) -> int:
    """`PAGE` 표기 → 쪽수. 모르면 0.

    "556 p." · "464" · "약 446페이지 내외" 는 그 숫자, "186, 307 p." 처럼 쉼표로 나열된 건
    여러 권이라 합한다. 20 미만("1" — 세트 행)은 믿지 않는다.
    """
    if not raw:
        return 0
    nums = [int(n) for n in _NUM_RE.findall(raw) if int(n) >= _MIN_PLAUSIBLE_PAGES]
    if not nums:
        return 0
    return sum(nums) if "," in raw else max(nums)


@dataclass(frozen=True, slots=True)
class BookHit:
    """검색 후보 1건 — 목차는 싣지 않는다(고른 뒤 `lookup_book` 몫)."""

    title: str
    author: str
    publisher: str
    isbn13: str
    page_count: int  # 0 = 모름
    cover_url: str  # 대개 빈 문자열(표지 이미지를 안 채운 행이 많다)


@dataclass(slots=True)
class SearchResult:
    """가져온 후보 목록, 또는 못 가져온 이유. `books` 가 비어 있으면 `reason` 이 채워진다."""

    books: list[BookHit] = field(default_factory=list)
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.books)


@dataclass(frozen=True, slots=True)
class BookLookup:
    """ISBN 조회 결과 — 서지 + 쪽수 + 목차(best-effort, 없으면 빈 목록이 정상)."""

    title: str
    author: str
    publisher: str
    isbn13: str
    page_count: int  # 0 = 모름
    chapters: list[TocChapter] = field(default_factory=list)

    @property
    def has_page_checkpoints(self) -> bool:
        return any(c.end_page is not None for c in self.chapters)


@dataclass(slots=True)
class LookupResult:
    lookup: BookLookup | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.lookup is not None


def _fetch_sync(params: dict[str, str], key: str) -> list[dict[str, Any]] | str:
    """한 번 호출 → `docs` 목록, 또는 실패 사유 문자열."""
    try:
        response = requests.get(
            _SEARCH_URL,
            params={"cert_key": key, "result_style": "json", "page_no": "1", **params},
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        response.raise_for_status()
    except requests.Timeout:
        return REASON_TIMEOUT
    except requests.RequestException as e:
        # 예외 문자열엔 요청 URL 전체(`cert_key` 쿼리 포함)가 들어 있다 — 유형·상태 코드만
        # 남긴다(inbox-7). `exc_info=True` 로 남기면 서버 로그에 API 키가 평문으로 쌓인다.
        status = e.response.status_code if e.response is not None else None
        logger.warning("seoji request failed: %s status=%s", type(e).__name__, status)
        return REASON_UNAVAILABLE

    try:
        body: dict[str, Any] = response.json()
    except ValueError:
        return REASON_UNAVAILABLE

    if str(body.get("RESULT", "")).upper() == "ERROR":
        logger.warning("seoji returned RESULT=ERROR: %s", body.get("ERR_MESSAGE"))
        return REASON_UNAVAILABLE
    docs = body.get("docs") or []
    return [d for d in docs if isinstance(d, dict)]


def _text(doc: dict[str, Any], name: str) -> str:
    value = doc.get(name)
    return value.strip() if isinstance(value, str) else ""


def _search_sync(query: str, key: str, limit: int, today: date) -> SearchResult:
    words = query.split()
    attempts = [" ".join(words[: len(words) - i]) for i in range(_MAX_QUERY_SHORTENINGS + 1)]
    for attempt in [a for a in dict.fromkeys(attempts) if a]:
        docs = _fetch_sync(
            {
                "page_size": str(_SEARCH_FETCH_SIZE),
                "title": attempt,
                "ebook_yn": "N",
                "sort": "PUBLISH_PREDATE",
                "order_by": "DESC",
                # 발행예정일이 미래로 잘못 들어간 행("2070-11-05")이 최신순 맨 위를 차지한다.
                "end_publish_date": today.strftime("%Y%m%d"),
            },
            key,
        )
        if isinstance(docs, str):
            return SearchResult(reason=docs)
        books: list[BookHit] = []
        seen: set[tuple[str, str]] = set()
        for d in docs:
            isbn = _text(d, "EA_ISBN")
            title = _text(d, "TITLE")
            if not _ISBN13_RE.match(isbn) or not title or _text(d, "FORM") not in ("", "종이책"):
                continue
            dedupe = (title.replace(" ", ""), _text(d, "PUBLISHER"))
            if dedupe in seen:  # 최신순이라 처음 본 것이 최신판이다
                continue
            seen.add(dedupe)
            books.append(
                BookHit(
                    title=title,
                    author=_text(d, "AUTHOR"),
                    publisher=_text(d, "PUBLISHER"),
                    isbn13=isbn,
                    page_count=parse_page_count(_text(d, "PAGE")),
                    cover_url=_text(d, "TITLE_URL"),
                )
            )
            if len(books) >= limit:
                break
        if books:
            return SearchResult(books=books)
    return SearchResult(reason=REASON_EMPTY)


def _lookup_sync(isbn13: str, key: str) -> LookupResult:
    docs = _fetch_sync({"page_size": "1", "isbn": isbn13}, key)
    if isinstance(docs, str):
        return LookupResult(reason=docs)
    if not docs:
        return LookupResult(reason=REASON_NOT_FOUND)
    doc = docs[0]
    fields = {str(k): str(v) for k, v in doc.items() if isinstance(v, str)}
    chapters: list[TocChapter] = []
    raw_toc = _find_toc_field(fields)
    if raw_toc is not None:
        entries = _split_toc_entries(raw_toc)
        end_pages = _chapter_end_pages(entries)
        chapters = [
            TocChapter(title=_chapter_title(e), end_page=p)
            for e, p in zip(entries, end_pages, strict=True)
        ]
    return LookupResult(
        lookup=BookLookup(
            title=_text(doc, "TITLE"),
            author=_text(doc, "AUTHOR"),
            publisher=_text(doc, "PUBLISHER"),
            isbn13=_text(doc, "EA_ISBN") or isbn13,
            page_count=parse_page_count(_text(doc, "PAGE")),
            chapters=chapters,
        )
    )


async def search_books(
    query: str, *, key: str, limit: int = 5, today: date | None = None
) -> SearchResult:
    """표제 검색 → 종이책 후보(최신 발행순). 0건이면 뒤 낱말을 떼며 다시 찾는다."""
    if not key:
        return SearchResult(reason=REASON_NO_KEY)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_search_sync, query, key, limit, today or date.today()),
            timeout=_HARD_TIMEOUT,
        )
    except TimeoutError:
        return SearchResult(reason=REASON_TIMEOUT)
    except Exception:  # noqa: BLE001 — 검색 실패가 화면을 깨면 안 된다
        logger.warning("seoji search_books failed", exc_info=True)
        return SearchResult(reason=REASON_UNAVAILABLE)


async def lookup_book(isbn13: str, *, key: str) -> LookupResult:
    """ISBN13 → 서지 + 쪽수 + 목차. 목차가 비어 있는 건 정상이다(판본 대부분)."""
    if not key:
        return LookupResult(reason=REASON_NO_KEY)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_lookup_sync, isbn13, key), timeout=_HARD_TIMEOUT
        )
    except TimeoutError:
        return LookupResult(reason=REASON_TIMEOUT)
    except Exception:  # noqa: BLE001 — 도서 조회 실패가 계획 생성을 막으면 안 된다
        logger.warning("seoji lookup_book failed", exc_info=True)
        return LookupResult(reason=REASON_UNAVAILABLE)
