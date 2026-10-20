"""L0 자료조사 스파이크 — 알라딘/YouTube API 가 **목차와 분량**을 실제로 주는가.

⚠️ 기록용 하네스다 — 알라딘 Open API 가 2026-10-30 종료돼 알라딘 부분은 더 재현되지 않는다.
앱의 도서 검색은 국립중앙도서관 ISBN 서지정보로 바뀌었다(`integrations/nl_seoji`, api v2.38).

`docs/decisions/` 로 설계를 굳히기 전에 돌리는 실측 하네스다. 이 레포의 관행(`llm/
provider.py` 주석 전체가 실측 기반)을 따라, **문서가 아니라 응답을 진실로 삼는다.**

배경 — 왜 이 스파이크가 필요한가
================================
기존 자료 검색(`api/routes/materials.py`)은 Gemini 검색 그라운딩 1회가 전부인데,
상업 교재는 `finish_reason=RECITATION` 으로 막힌다(`llm/provider.py:46` 실측). 토익·
공무원 수험서처럼 **목차가 가장 필요한 자료가 정확히 막히는 쪽**이라 기능이 사실상 죽어
있다. 그래서 "LLM 이 읊게" 하지 말고 "우리가 API 로 가져오는" 쪽으로 뒤집으려 한다.

크롤링 폴백은 이미 판정났다 (2026-09-01 실측, 이 파일 `--crawl` 로 재현 가능):

    소스      검색 페이지                    상세 페이지          목차
    알라딘    열림 (raw 307KB, ItemId 46건)  열림 (raw 381KB)     **0건 — AJAX 후행 로드**
    교보      열림 (raw 578KB, ID 20건)      **빈 응답 0 chars**  불가 (봇 차단)
    YES24     열림 (raw 17KB 추출)           미확인               —

즉 **크롤링으로 도서 목차는 확보할 수 없다.** JS 실행(Playwright)이 필요한데 새 무거운
의존성 + EC2 메모리라 AGENTS §8 대상이고, 사이트 개편마다 깨진다. 알라딘 `OptResult=Toc`
가 유일한 현실적 경로다 — 그게 정말 오는지를 이 스파이크가 확인한다.

무엇을 재는가
=============
    M1  목차 확보율      4개 주제 × 상위 3건 중 목차가 온 비율
    M2  목차 파싱 가능성  챕터 경계를 기계적으로 끊을 수 있는가 (계획 분해의 전제)
    M3  분량 확보율      페이지 수(도서) / 총 재생시간(영상) 이 오는가
    M4  응답 지연        계획 생성 예산(#179) 안에 들어오는가

M3 이 이 설계의 실질이다. 지금은 목차가 2000자로 잘린 텍스트 덩어리라 LLM 이 "적당히"
나누지만, `{total: 20, unit: "chapter"}` 가 있으면 룰 스케줄러가 산술로 세션당 진도를
계산할 수 있다. 그게 "보편적 계획" 과 "이 자료 기반 계획" 의 차이다.

측정 결과 (2026-09-01, 4주제 · 도서 10건 · 재생목록 4건)
========================================================
**설계 가정이 틀렸다. 알라딘 API 에는 목차가 없다.** 대신 국중 seoji 가 판본에 따라
간헐적으로 준다(10%) — best-effort 로만 쓸 수 있는 수준이다.

    알라딘   M1 목차   0/10   ← `OptResult=Toc` 는 실재하지 않는다
             M3 페이지 10/10   M4 중앙 117ms
    seoji    M1+M2    1/10   ← 판본별로 다르다. 되면 15챕터·516소단원·정확한 페이지
    YouTube  M1+M2    4/4     M3 4/4     M4 중앙 ~1.8s

`OptResult` 기구 자체는 동작한다 — `packing`·`ebookList`·`ratingInfo`·`bestSellerRank`
는 요청하면 `subInfo` 에 나타난다. 그런데 `Toc`/`TOC`/`toc`/`tableOfContents`/`contents`
/`Story` 는 **어떤 표기로도 반환되지 않는다.** 상품 페이지도 목차를 `ajax.aspx` 로 후행
로드해 raw HTML 에 `"목차"` 가 0건이고, 교보 상세는 봇에 빈 응답(0 chars)을 준다.
Google Books 는 한국 수험서를 아예 모른다(ISBN 3건 전부 0건).

**국중 seoji 는 판본에 따라 간헐적으로 목차를 준다.** `BOOK_TB_CNT` 필드가 실재하고,
채워지면 매우 상세하다 — "Java의 정석 : 기초편" 에서 15챕터·516소단원·정확한 페이지
번호(59,105자)가 나왔다. 그런데 **같은 책의 다른 판**("3rd Edition"·"2nd Edition"·구판
"java의 정석")은 전부 비어 있다(`len == 0`). 10권 전체를 훑어도 1/10 만 채워져 있어,
출판사가 납본할 때 선택적으로 채우는 필드로 보인다. **표본을 주제당 1권으로 줄이면 이
차이를 놓친다** — 처음에 그렇게 줄였다가 우연히 빈 판만 걸려 "seoji 에도 없다" 로 오판할
뻔했다(하네스는 후보 전체를 조회하도록 고쳤다).

**YouTube 는 도서 목차보다 나은 것을 준다.** 영상 제목이 곧 커리큘럼인데, 책 목차와
달리 **단원마다 정확한 분량이 붙어 온다**:

    책 목차   "Chapter 3 DFS & BFS"          ← 몇 시간짜리인지 모른다
    강의      "3. DFS & BFS [58분]"          ← 세션 배치가 산술이 된다

실측 예: 토익 "LC 1강 PART 1 UNIT 01-02 [27분]" — 교재 단원과 강의 진도가 이미 매핑돼
있다. 코딩테스트·정보처리기사는 **책과 같은 교재의 강의**가 검색으로 잡혔다(이코테 604쪽
책 ↔ 이코테 15편 10시간 강의).

그래서 설계를 뒤집는다: **영상 강의를 1급 시민으로, 도서는 분량(페이지) 기반 보조 +
seoji 목차는 best-effort 덤(10% 확률).** 안 되면 사용자 붙여넣기(기존 HITL)로 남긴다 —
못 얻는 걸 얻는 척하지 않는다.

실행
====
    export ALADIN_TTB_KEY=ttb...        # https://www.aladin.co.kr/ttb/wblog_manage.aspx
    export YOUTUBE_API_KEY=AIza...      # GCP 콘솔 → YouTube Data API v3 활성화
    export NL_SEOJI_KEY=...             # https://seoji.nl.go.kr/landingPage → 오픈API (선택)

    uv run python -m scripts.l0_materials_probe              # 전체
    uv run python -m scripts.l0_materials_probe --crawl      # 크롤링 폴백 재현만 (키 불필요)
    uv run python -m scripts.l0_materials_probe --only 토익

쿼터: 알라딘 5,000/일, YouTube 10,000/일. 전체 1회 실행은 알라딘 ~16회, YouTube ~
12유닛(search 는 100유닛/회라 주제당 1회로 묶는다). 하루 수십 번 돌려도 여유가 있다.

키가 없으면 그 파트를 SKIP 으로 보고하고 나머지를 돌린다 — 한쪽 키만 먼저 받아도
진행할 수 있게. **결과는 `docs/experiments/` 에 남기고 그 위에서 ADR 을 쓴다.**
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _key(name: str) -> str:
    """환경변수 → 없으면 `.env` 에서 읽는다. **새 의존성 없이.**

    `python-dotenv` 을 쓰지 않는 이유는 `integrations/web_fetch/extract.py` 가 PyYAML 을
    피한 것과 같다 — 우리 직접 의존이 아니라 pydantic-settings 의 transitive 라, 상류가
    끊으면 조용히 깨진다. 6줄이면 되는 일에 그 위험을 지지 않는다.

    앱 본체는 `config.py` 가 pydantic-settings 로 같은 파일을 읽는다. 여기서 굳이 다시
    파싱하는 건 스파이크가 `Settings` 를 거치지 않기 때문 — 이 키들은 아직 프로덕션
    설정이 아니고, 정식 편입은 API 를 쓰기로 확정한 뒤 PR2 에서 한다.
    """
    value = os.environ.get(name, "").strip()
    if value or not _ENV_FILE.exists():
        return value
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        found, _, raw = line.partition("=")
        if found.strip() == name:
            return raw.strip().strip("'\"")
    return ""


# 스파이크 주제 — "목차가 필요한 자료" 의 스펙트럼을 덮는다.
#   토익/정보처리기사 = 상업 수험서 (RECITATION 으로 막히던 바로 그 부류)
#   자바/코딩테스트   = 기술서 + 영상 강의가 둘 다 있는 부류
SUBJECTS: dict[str, dict[str, str]] = {
    "토익": {"book": "해커스 토익", "video": "토익 인강 강의"},
    "자바": {"book": "자바의 정석", "video": "자바 기초 강의"},
    "코딩테스트": {"book": "이것이 코딩 테스트다", "video": "코딩테스트 파이썬 강의"},
    "정보처리기사": {"book": "정보처리기사 실기", "video": "정보처리기사 실기 강의"},
}

_UA = "reaction-backend/1.0 (+https://github.com/hanium-reaction)"
_HDR = {"User-Agent": _UA, "Accept-Language": "ko,en;q=0.8"}
_TIMEOUT = (3.0, 10.0)

_ALADIN_BASE = "https://www.aladin.co.kr/ttb/api"
_YOUTUBE_BASE = "https://www.googleapis.com/youtube/v3"


# ─────────────────────────────────────────────────────────────────────────────
# 목차 파싱 (M2) — 챕터 경계를 기계적으로 끊을 수 있는가
# ─────────────────────────────────────────────────────────────────────────────

# 알라딘 Toc 는 HTML 조각(`<br>` 구분 → 줄 단위)으로 온다. seoji `BOOK_TB_CNT` 는 정반대로
# 개행이 **아예 없이** 점선(···)+페이지번호로 항목을 이어붙인 한 덩어리 문자열이다(실측
# 2026-09-01, 자바의 정석 기초편 59,105자 1건). `^`(줄 시작) 앵커를 쓰면 seoji 형식에서
# 문자열 전체가 한 줄이라 챕터가 무조건 0~1 로 나온다 — 그래서 앵커 유무로 두 그룹을
# 나눈다: "Chapter/장/PART/DAY" 류는 목차 밖 본문에 흔치 않아 앵커 없이도 안전하지만,
# 숫자만으로 된 "1. 들어가며" 류는 앵커가 없으면 seoji 의 페이지 번호("···20")를 챕터로
# 오탐한다. 그래서 이것만 줄 시작을 요구해 알라딘(줄 단위) 형식에만 적용되게 남긴다.
_CHAPTER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:Chapter|CHAPTER|chapter)\s*\.?\s*\d+"),
    re.compile(r"(?:제\s*)?\d+\s*(?:장|과|강|부|주차|일차)\b"),
    re.compile(r"(?:PART|Part|part)\s*\.?\s*[\dIVX]+"),
    re.compile(r"(?:DAY|Day)\s*\.?\s*\d+"),
    re.compile(r"^\s*\d+\.\s+\S", re.M),
)


def _toc_to_lines(raw_toc: str) -> list[str]:
    """알라딘 Toc HTML 조각 → 줄 리스트."""
    text = re.sub(r"<br\s*/?>", "\n", raw_toc, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def count_chapters(raw_toc: str) -> tuple[int, str | None]:
    """목차에서 챕터 수를 센다. → (개수, 매칭된 패턴 이름)

    계획 분해가 "20챕터 ÷ 40세션 = 2세션당 1챕터" 를 계산하려면 이 숫자가 필요하다.
    가장 많이 잡히는 패턴 하나를 고른다 — 목차엔 대·중·소 단위가 섞여 있고, 우리가
    원하는 건 **한 세션에 담을 만한 단위** 라서 최빈 단위가 대개 맞다.
    """
    lines = _toc_to_lines(raw_toc)
    joined = "\n".join(lines)
    best, best_name = 0, None
    for pat in _CHAPTER_PATTERNS:
        n = len(pat.findall(joined))
        if n > best:
            best, best_name = n, pat.pattern[:28]
    return best, best_name


def parse_iso8601_duration(value: str) -> int:
    """`PT1H23M45S` → 초. YouTube `contentDetails.duration` 형식."""
    m = re.fullmatch(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not m:
        return 0
    d, h, mi, s = (int(g or 0) for g in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


# ─────────────────────────────────────────────────────────────────────────────
# 알라딘
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BookProbe:
    subject: str
    title: str = ""
    item_id: str = ""
    isbn: str = ""
    """seoji 조회의 열쇠 — 알라딘이 ISBN13 을 주므로 국중 API 로 넘길 수 있다."""
    page_count: int = 0
    toc_chars: int = 0
    chapters: int = 0
    pattern: str | None = None
    latency_ms: int = 0
    error: str | None = None

    @property
    def has_toc(self) -> bool:
        return self.toc_chars > 0

    @property
    def usable(self) -> bool:
        """계획 분해가 쓸 수 있는가 — 목차가 있고 챕터를 끊을 수 있어야 한다."""
        return self.has_toc and self.chapters >= 3


def _aladin_get(path: str, params: dict[str, Any], key: str) -> Any:
    params = {**params, "ttbkey": key, "output": "js", "Version": "20131101"}
    r = requests.get(f"{_ALADIN_BASE}/{path}", params=params, headers=_HDR, timeout=_TIMEOUT)
    r.raise_for_status()
    # 알라딘은 Content-Type 이 text/html 이어도 본문은 JSON 이다. 끝에 `;` 가 붙어 오는
    # 경우가 있어 그대로 json() 하면 깨진다.
    body = r.text.strip().rstrip(";")
    import json  # noqa: PLC0415

    return json.loads(body)


def probe_aladin(subject: str, query: str, key: str, top: int) -> list[BookProbe]:
    """ItemSearch 로 후보를 찾고, 각각 ItemLookUp(OptResult=Toc) 로 목차를 받는다."""
    started = time.monotonic()
    try:
        search = _aladin_get(
            "ItemSearch.aspx",
            {
                "Query": query,
                "QueryType": "Keyword",
                "SearchTarget": "Book",
                "MaxResults": top,
                "start": 1,
                "Sort": "SalesPoint",
            },
            key,
        )
    except Exception as exc:  # noqa: BLE001 — 스파이크는 실패도 데이터다
        return [BookProbe(subject, error=f"ItemSearch: {type(exc).__name__}: {exc}")]

    items = search.get("item") or []
    if not items:
        return [
            BookProbe(
                subject, error=f"ItemSearch 결과 0건 (errorMessage={search.get('errorMessage')})"
            )
        ]

    out: list[BookProbe] = []
    for item in items[:top]:
        probe = BookProbe(
            subject=subject,
            title=str(item.get("title", ""))[:60],
            item_id=str(item.get("itemId", "")),
            isbn=str(item.get("isbn13") or item.get("isbn") or ""),
        )
        t0 = time.monotonic()
        try:
            look = _aladin_get(
                "ItemLookUp.aspx",
                {
                    "itemIdType": "ItemId",
                    "ItemId": probe.item_id,
                    "OptResult": "Toc,packing,cardReviewImgList",
                },
                key,
            )
            detail = (look.get("item") or [{}])[0]
            sub = detail.get("subInfo") or {}
            raw_toc = str(sub.get("toc") or "")
            probe.toc_chars = len(raw_toc)
            probe.page_count = int((sub.get("itemPage") or 0) or 0)
            probe.chapters, probe.pattern = count_chapters(raw_toc)
        except Exception as exc:  # noqa: BLE001
            probe.error = f"ItemLookUp: {type(exc).__name__}: {exc}"
        probe.latency_ms = int((time.monotonic() - t0) * 1000)
        out.append(probe)

    total = int((time.monotonic() - started) * 1000)
    print(f"    (알라딘 {subject}: {len(out)}건, 총 {total}ms)")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 국립중앙도서관 seoji — 도서 목차의 마지막 카드
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SeojiProbe:
    """seoji 응답에 목차가 있는가. **필드명을 추측하지 않고 스키마를 전수 조사한다.**

    알라딘에서 `OptResult=Toc` 를 문서만 믿고 가정했다가 0/10 으로 틀렸다. 같은 실수를
    반복하지 않으려면 "이 필드에 목차가 있을 것" 이라고 찍지 말고, **온 것을 전부 펼쳐
    보고 목차처럼 생긴 값을 찾아야** 한다. 그래서 `all_fields` 를 통째로 들고 있는다.
    """

    isbn: str
    title: str = ""
    all_fields: dict[str, str] = field(default_factory=dict)
    toc_field: str | None = None
    toc_chars: int = 0
    chapters: int = 0
    latency_ms: int = 0
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.toc_chars > 0 and self.chapters >= 3


# 목차로 볼 만한 값의 최소 길이 — 제목·저자 같은 짧은 필드를 목차로 오인하지 않게.
_TOC_MIN_CHARS = 80


def probe_seoji(isbn: str, key: str) -> SeojiProbe:
    """ISBN → 국중 서지정보. 목차 필드가 **어떤 이름으로든** 오는지 찾는다."""
    probe = SeojiProbe(isbn=isbn)
    t0 = time.monotonic()
    try:
        r = requests.get(
            "https://seoji.nl.go.kr/landingPage/SearchApi.do",
            params={
                "cert_key": key,
                "result_style": "json",
                "page_no": "1",
                "page_size": "1",
                "isbn": isbn,
            },
            headers=_HDR,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
        if str(body.get("RESULT", "")).upper() == "ERROR":
            probe.error = f"{body.get('ERR_CODE')}: {body.get('ERR_MESSAGE')}"
            return probe
        docs = body.get("docs") or []
        if not docs:
            probe.error = "결과 0건"
            return probe

        doc = docs[0]
        probe.title = str(doc.get("TITLE", ""))[:60]
        probe.all_fields = {k: str(v) for k, v in doc.items()}

        # 목차 후보: 값이 충분히 길고, 챕터 패턴이 가장 많이 잡히는 필드를 고른다.
        # 이름(TB_CNT 등)에 기대지 않는 이유는 위 docstring 과 같다.
        best_n = 0
        for name, value in probe.all_fields.items():
            if len(value) < _TOC_MIN_CHARS:
                continue
            n, _ = count_chapters(value)
            if n > best_n:
                best_n, probe.toc_field, probe.toc_chars = n, name, len(value)
        probe.chapters = best_n
    except Exception as exc:  # noqa: BLE001 — 스파이크는 실패도 데이터다
        probe.error = f"{type(exc).__name__}: {exc}"
    probe.latency_ms = int((time.monotonic() - t0) * 1000)
    return probe


# ─────────────────────────────────────────────────────────────────────────────
# YouTube
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class VideoProbe:
    subject: str
    title: str = ""
    playlist_id: str = ""
    video_count: int = 0
    total_seconds: int = 0
    latency_ms: int = 0
    error: str | None = None
    curriculum: list[tuple[str, int]] = field(default_factory=list)
    """(영상 제목, 초). **이게 목차다** — 2026-09-01 실측이 뒤집은 지점.

    도서 목차보다 낫다. 책 목차는 "Chapter 3 DFS & BFS" 까지만 알려주고 그게 몇 시간짜리
    인지는 말해주지 않는데, 강의는 "3. DFS & BFS [58분]" 로 **단원과 분량이 한 줄에** 온다.
    세션 배치가 LLM 의 어림짐작이 아니라 산술이 된다.
    """

    @property
    def usable(self) -> bool:
        """계획을 세울 수 있는가 — 커리큘럼(무엇을)과 분량(얼마나)이 둘 다 있어야 한다."""
        return self.video_count >= 3 and self.total_seconds > 0 and len(self.curriculum) >= 3

    @property
    def hours(self) -> float:
        return round(self.total_seconds / 3600, 1)


def _yt_get(path: str, params: dict[str, Any], key: str) -> Any:
    r = requests.get(
        f"{_YOUTUBE_BASE}/{path}", params={**params, "key": key}, headers=_HDR, timeout=_TIMEOUT
    )
    r.raise_for_status()
    return r.json()


def probe_youtube(subject: str, query: str, key: str) -> VideoProbe:
    """재생목록 1개를 찾아 **영상 수 + 총 재생시간**을 계산한다.

    search.list 는 100유닛이라 주제당 1회만 쓴다. playlistItems/videos 는 1유닛이다.
    """
    probe = VideoProbe(subject=subject)
    t0 = time.monotonic()
    try:
        search = _yt_get(
            "search",
            {
                "part": "snippet",
                "q": query,
                "type": "playlist",
                "maxResults": 3,
                "relevanceLanguage": "ko",
                "regionCode": "KR",
            },
            key,
        )
        items = search.get("items") or []
        if not items:
            probe.error = "재생목록 검색 결과 0건"
            return probe
        top = items[0]
        probe.playlist_id = top["id"]["playlistId"]
        probe.title = str(top["snippet"].get("title", ""))[:60]

        # 재생목록의 영상 ID + **제목** 전부 (페이지네이션).
        # `snippet` 을 함께 받는 이유: 제목이 곧 커리큘럼이다(`VideoProbe.curriculum`).
        # 순서도 의미가 있다 — 재생목록 순서가 곧 학습 순서라 그대로 보존한다.
        ordered: list[tuple[str, str]] = []  # (videoId, title)
        page: str | None = None
        while True:
            params: dict[str, Any] = {
                "part": "snippet,contentDetails",
                "playlistId": probe.playlist_id,
                "maxResults": 50,
            }
            if page:
                params["pageToken"] = page
            batch = _yt_get("playlistItems", params, key)
            for it in batch.get("items", []):
                vid = (it.get("contentDetails") or {}).get("videoId")
                if vid:
                    ordered.append((vid, str((it.get("snippet") or {}).get("title", ""))))
            page = batch.get("nextPageToken")
            if not page or len(ordered) >= 200:  # 상한 — 스파이크에 200개면 충분
                break
        probe.video_count = len(ordered)

        # 길이 합산 — videos.list 는 한 번에 50개까지
        seconds: dict[str, int] = {}
        for i in range(0, len(ordered), 50):
            chunk = [v for v, _ in ordered[i : i + 50]]
            detail = _yt_get("videos", {"part": "contentDetails", "id": ",".join(chunk)}, key)
            for it in detail.get("items", []):
                seconds[it["id"]] = parse_iso8601_duration(it["contentDetails"]["duration"])
        probe.total_seconds = sum(seconds.values())
        probe.curriculum = [(title, seconds.get(vid, 0)) for vid, title in ordered]
    except Exception as exc:  # noqa: BLE001
        probe.error = f"{type(exc).__name__}: {exc}"
    probe.latency_ms = int((time.monotonic() - t0) * 1000)
    return probe


# ─────────────────────────────────────────────────────────────────────────────
# 크롤링 폴백 재현 (키 불필요)
# ─────────────────────────────────────────────────────────────────────────────


async def probe_crawl() -> None:
    """2026-09-01 판정을 재현한다 — 커머스 사이트에서 목차를 못 얻는다는 사실."""
    from reaction_backend.integrations.web_fetch import fetcher  # noqa: PLC0415

    print("\n── 크롤링 폴백 (키 불필요) ─────────────────────────────")

    # 1) 알라딘 상세: raw 에 '목차' 가 있는가
    html = requests.get(
        "https://www.aladin.co.kr/search/wsearchresult.aspx?SearchWord=해커스+토익",
        headers=_HDR,
        timeout=_TIMEOUT,
    ).text
    ids = list(dict.fromkeys(re.findall(r"ItemId=(\d+)", html)))
    print(f"  알라딘 검색: raw {len(html):,} chars, ItemId 고유 {len(ids)}건")
    if ids:
        detail = requests.get(
            f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={ids[0]}",
            headers=_HDR,
            timeout=_TIMEOUT,
        ).text
        toc_hits = len(re.findall("목차", detail))
        print(f"  알라딘 상세: raw {len(detail):,} chars, '목차' {toc_hits}건 → ", end="")
        print("SSR 로 옴" if toc_hits else "**AJAX 후행 로드 — 크롤링 불가**")

    # 2) 교보 상세: 응답 자체가 오는가
    kyobo = requests.get(
        "https://search.kyobobook.co.kr/search?keyword=해커스%20토익",
        headers=_HDR,
        timeout=_TIMEOUT,
    ).text
    kids = list(dict.fromkeys(re.findall(r"/detail/(S\d+)", kyobo)))
    print(f"  교보 검색: raw {len(kyobo):,} chars, 상품ID {len(kids)}건")
    if kids:
        kd = requests.get(
            f"https://product.kyobobook.co.kr/detail/{kids[0]}", headers=_HDR, timeout=_TIMEOUT
        ).text
        print(f"  교보 상세: raw {len(kd):,} chars → ", end="")
        print(
            "**빈 응답 — 봇 차단**" if len(kd) == 0 else f"'목차' {len(re.findall('목차', kd))}건"
        )

    # 3) 우리 fetcher 로 열었을 때 2000자 clip 안에 내용이 들어오는가
    result = await fetcher.fetch_text(
        "https://www.aladin.co.kr/search/wsearchresult.aspx?SearchWord=해커스+토익"
    )
    if result.ok:
        text = result.text or ""
        pos = text.find("해커스")
        print(
            f"  fetcher 추출: {len(text):,} chars, 내용 최초 위치 {pos} "
            f"→ clip(2000) 안: {pos >= 0 and pos < 2000}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 리포트
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Report:
    books: list[BookProbe] = field(default_factory=list)
    videos: list[VideoProbe] = field(default_factory=list)
    seoji: list[SeojiProbe] = field(default_factory=list)

    def render(self) -> None:
        print("\n" + "=" * 72)
        print("L0 결과")
        print("=" * 72)

        if self.books:
            parsable = [b for b in self.books if b.usable]
            with_toc = [b for b in self.books if b.has_toc]
            pages = [b.page_count for b in self.books if b.page_count > 0]
            lat = [b.latency_ms for b in self.books if b.latency_ms]
            print(f"\n[알라딘] {len(self.books)}건 조회")
            print(f"  M1 목차 확보율   {len(with_toc)}/{len(self.books)}")
            print(f"  M2 챕터 파싱     {len(parsable)}/{len(self.books)} (3챕터 이상 끊김)")
            print(f"  M3 페이지수 확보 {len(pages)}/{len(self.books)}")
            if lat:
                print(
                    f"  M4 지연          중앙 {int(statistics.median(lat))}ms / 최대 {max(lat)}ms"
                )
            print()
            for b in self.books:
                if b.error:
                    print(f"   ✗ [{b.subject}] {b.error}")
                    continue
                mark = "✓" if b.usable else "△" if b.has_toc else "✗"
                print(
                    f"   {mark} [{b.subject}] {b.title[:34]:<34} "
                    f"목차 {b.toc_chars:>5}자 · 챕터 {b.chapters:>3} · {b.page_count:>4}p "
                    f"· {b.latency_ms}ms"
                )
                if b.pattern:
                    print(f"        └ 패턴 {b.pattern!r}")

        if self.seoji:
            found = [s for s in self.seoji if s.usable]
            print(f"\n[국중 seoji] {len(self.seoji)}건 조회 — 도서 목차의 마지막 카드")
            print(f"  M1+M2 목차       {len(found)}/{len(self.seoji)}")
            print()
            for s in self.seoji:
                if s.error:
                    print(f"   ✗ [{s.isbn}] {s.error}")
                    continue
                mark = "✓" if s.usable else "✗"
                print(f"   {mark} [{s.isbn}] {s.title[:38]:<38} · {s.latency_ms}ms")
                if s.toc_field:
                    print(
                        f"        └ 목차 후보 필드 {s.toc_field!r} "
                        f"({s.toc_chars}자 · 챕터 {s.chapters})"
                    )
                else:
                    # 목차가 없다는 판정의 근거 — 어떤 필드가 왔는지 그대로 보여준다.
                    print(f"        └ 받은 필드 {len(s.all_fields)}종: {list(s.all_fields)[:12]}")

        if self.videos:
            measured = [v for v in self.videos if v.usable]
            lat = [v.latency_ms for v in self.videos if v.latency_ms]
            print(f"\n[YouTube] {len(self.videos)}건 조회")
            print(f"  M1+M2 커리큘럼   {len(measured)}/{len(self.videos)} (영상 제목 = 목차)")
            print(f"  M3 분량 확보     {len(measured)}/{len(self.videos)}")
            if lat:
                print(
                    f"  M4 지연          중앙 {int(statistics.median(lat))}ms / 최대 {max(lat)}ms"
                )
            print()
            for v in self.videos:
                if v.error:
                    print(f"   ✗ [{v.subject}] {v.error}")
                    continue
                mark = "✓" if v.usable else "△"
                print(
                    f"   {mark} [{v.subject}] {v.title[:34]:<34} "
                    f"{v.video_count:>3}편 · {v.hours:>5}시간 · {v.latency_ms}ms"
                )
                # 커리큘럼 앞 3편 — "제목이 목차 구실을 한다" 는 주장의 증거를 눈으로 본다.
                for n, (title, secs) in enumerate(v.curriculum[:3], 1):
                    print(f"        {n}. [{secs // 60:>3}분] {title[:56]}")
                if len(v.curriculum) > 3:
                    print(f"        … 외 {len(v.curriculum) - 3}편")

        print("\n" + "-" * 72)
        print("2026-09-01 판정: 알라딘 API 목차 0/10 → 그 경로는 접었다.")
        print("                 국중 seoji 목차 1/10(10%, 판본별로 다름) → best-effort 보조.")
        print("                 YouTube 커리큘럼+분량 4/4 → 영상 강의를 1급 시민으로.")
        print("                 도서는 페이지 수(10/10) 기준, seoji 로 되면 목차도 덤.")
        print("-" * 72)


async def main() -> int:
    ap = argparse.ArgumentParser(description="L0 자료조사 스파이크")
    ap.add_argument("--only", help="특정 주제만 (예: 토익)")
    ap.add_argument("--top", type=int, default=3, help="주제당 도서 후보 수 (기본 3)")
    ap.add_argument("--crawl", action="store_true", help="크롤링 폴백 재현만 (키 불필요)")
    args = ap.parse_args()

    if args.crawl:
        await probe_crawl()
        return 0

    subjects = {k: v for k, v in SUBJECTS.items() if not args.only or args.only in k}
    if not subjects:
        print(f"주제 없음: {args.only} (가능: {', '.join(SUBJECTS)})")
        return 2

    aladin_key = _key("ALADIN_TTB_KEY")
    youtube_key = _key("YOUTUBE_API_KEY")
    report = Report()

    print(f"주제 {len(subjects)}개: {', '.join(subjects)}\n")

    if aladin_key:
        print("── 알라딘 ItemSearch → ItemLookUp(OptResult=Toc) ──")
        for subject, q in subjects.items():
            report.books += probe_aladin(subject, q["book"], aladin_key, args.top)
    else:
        print("── 알라딘 SKIP (ALADIN_TTB_KEY 없음) ──")
        print("   발급: https://www.aladin.co.kr/ttb/wblog_manage.aspx")

    seoji_key = _key("NL_SEOJI_KEY")
    if seoji_key and report.books:
        print("\n── 국중 seoji SearchApi (ISBN → 서지정보) ──")
        # **후보 전체**를 조회한다 — 주제당 1권으로 줄이려 했다가 틀렸다(2026-09-01 실측):
        # "Java의 정석" 은 판마다 다르다. 3rd/2nd Edition 은 BOOK_TB_CNT 가 비어 있고
        # "기초편" 만 15챕터·59,105자로 채워져 있었다. 대표 1권만 보면 이 차이를 놓치고
        # "seoji 에 목차가 없다" 로 오판할 뻔했다 — 값 채움이 판본 단위라 표본을 줄이면
        # 안 된다.
        for b in report.books:
            if b.isbn:
                report.seoji.append(probe_seoji(b.isbn, seoji_key))
    elif not seoji_key:
        print("\n── 국중 seoji SKIP (NL_SEOJI_KEY 없음) ──")
        print("   발급: https://seoji.nl.go.kr/landingPage → 오픈API → 인증키 신청 (무료)")

    if youtube_key:
        print("\n── YouTube search → playlistItems → videos(duration) ──")
        for subject, q in subjects.items():
            probe = probe_youtube(subject, q["video"], youtube_key)
            report.videos.append(probe)
            print(f"    ({subject}: {probe.video_count}편, {probe.latency_ms}ms)")
    else:
        print("\n── YouTube SKIP (YOUTUBE_API_KEY 없음) ──")
        print("   발급: GCP 콘솔 → API 및 서비스 → YouTube Data API v3 사용 설정")

    if not aladin_key and not youtube_key:
        print("\n키가 하나도 없어 API 파트를 건너뛰었다. 크롤링 폴백만 보려면 --crawl.")
        return 1

    report.render()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
