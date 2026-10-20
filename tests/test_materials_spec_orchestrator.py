"""materials_spec 오케스트레이터 (ADR-0010 §1 ③) — 도서(필수+best-effort 합성)·영상(필수)
상세 조합, 진도 계산(`compute_book_pace`), `spec_slot_value` 직렬화.

클라이언트는 stub 한다(각 클라이언트 자체는 `test_seoji_client.py`·
`test_youtube_client.py` 가 이미 검증했다).
"""

from __future__ import annotations

from datetime import date

import pytest

from reaction_backend.config import get_settings
from reaction_backend.integrations.nl_seoji import client as seoji_client
from reaction_backend.integrations.youtube import client as youtube_client
from reaction_backend.orchestrator import materials_spec
from reaction_backend.schemas.materials_spec import (
    MAX_TOC_ENTRIES,
    BookChapter,
    BookSpecDetail,
    VideoSpecDetail,
    VideoSpecItem,
)
from tests.test_interview_adapter_materials_spec import _outcome_with_one_goal


async def _ok_lookup(
    *,
    title: str = "책 제목",
    author: str = "저자",
    pages: int = 500,
    chapters: list[seoji_client.TocChapter] | None = None,
) -> seoji_client.LookupResult:
    return seoji_client.LookupResult(
        lookup=seoji_client.BookLookup(
            title=title,
            author=author,
            publisher="출판사",
            isbn13="9788994492049",
            page_count=pages,
            chapters=chapters or [],
        )
    )


_TWO_CHAPTERS = [
    seoji_client.TocChapter(title="Chapter 1. 서론", end_page=30),
    seoji_client.TocChapter(title="Chapter 2. 본론", end_page=80),
]


async def _ok_playlist_detail(
    *, title: str = "재생목록", channel: str = "채널", minutes: int = 60
) -> youtube_client.DetailResult:
    return youtube_client.DetailResult(
        detail=youtube_client.PlaylistDetail(
            title=title,
            channel_title=channel,
            video_count=1,
            total_seconds=minutes * 60,
            curriculum=[youtube_client.CurriculumItem(title="1강", seconds=minutes * 60)],
        )
    )


# ─────────────────────────── book_detail ───────────────────────────


async def test_book_detail_takes_pages_and_toc_from_one_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        seoji_client,
        "lookup_book",
        lambda *a, **k: _ok_lookup(pages=1000, chapters=_TWO_CHAPTERS),
    )

    result = await materials_spec.book_detail("9788994492049", settings=get_settings())

    assert result.detail is not None
    assert result.detail.page_count == 1000
    assert result.detail.toc_source == "seoji"
    assert [c.title for c in result.detail.chapters] == ["Chapter 1. 서론", "Chapter 2. 본론"]
    assert [c.end_page for c in result.detail.chapters] == [30, 80]
    assert result.notice is None


async def test_book_detail_caps_a_very_long_toc_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """하루 한 단원짜리 단어책은 목차가 100개를 넘는다 — 넘친 채로 응답을 만들면 검증에서
    터져 도서 상세 전체가 500 이 됐다(inbox-5). 상한까지만 담고 나머지는 진도 계산의
    '목차 이후 나머지 분량' 이 받는다."""
    long_toc = [
        seoji_client.TocChapter(title=f"DAY {i:03d}", end_page=i * 2) for i in range(1, 151)
    ]
    monkeypatch.setattr(
        seoji_client, "lookup_book", lambda *a, **k: _ok_lookup(pages=320, chapters=long_toc)
    )

    result = await materials_spec.book_detail("9788994492049", settings=get_settings())

    assert result.detail is not None
    assert len(result.detail.chapters) == MAX_TOC_ENTRIES
    assert result.detail.chapters[-1].title == "DAY 100"
    assert result.detail.page_count == 320


async def test_book_detail_missing_toc_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """L0 실측: 10권 중 9권이 이 경로다 — 쪽수만으로도 `detail` 은 채워지고 안내도 없다."""
    monkeypatch.setattr(seoji_client, "lookup_book", lambda *a, **k: _ok_lookup(pages=816))

    result = await materials_spec.book_detail("9788965424765", settings=get_settings())

    assert result.detail is not None
    assert result.detail.page_count == 816
    assert result.detail.chapters == []
    assert result.detail.toc_source is None
    assert result.notice is None


async def test_book_detail_without_a_page_count_is_usable_but_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """쪽수 표기가 없거나 못 읽으면(세트 행 "1" 등) 진도는 못 내지만 책은 확정할 수 있다."""
    monkeypatch.setattr(seoji_client, "lookup_book", lambda *a, **k: _ok_lookup(pages=0))

    result = await materials_spec.book_detail("9788965424765", settings=get_settings())

    assert result.detail is not None and result.detail.page_count == 0
    assert result.notice is not None and "쪽수" in result.notice


async def test_book_detail_lookup_failure_yields_no_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fail(*a: object, **k: object) -> seoji_client.LookupResult:
        return seoji_client.LookupResult(reason=seoji_client.REASON_NOT_FOUND)

    monkeypatch.setattr(seoji_client, "lookup_book", _fail)

    result = await materials_spec.book_detail("0000000000000", settings=get_settings())

    assert result.detail is None
    assert result.notice == "이 도서 정보를 찾지 못했어요."


# ─────────────────────────── video_detail ───────────────────────────


async def test_video_detail_returns_curriculum_and_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        youtube_client, "get_playlist_detail", lambda *a, **k: _ok_playlist_detail(minutes=90)
    )

    result = await materials_spec.video_detail("PL1", settings=get_settings())

    assert result.detail is not None
    assert result.detail.total_minutes == 90
    assert result.detail.curriculum[0].minutes == 90
    assert result.notice is None


async def test_video_detail_quota_exceeded_yields_no_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _quota(*a: object, **k: object) -> youtube_client.DetailResult:
        return youtube_client.DetailResult(reason=youtube_client.REASON_QUOTA)

    monkeypatch.setattr(youtube_client, "get_playlist_detail", _quota)

    result = await materials_spec.video_detail("PL1", settings=get_settings())

    assert result.detail is None
    assert "다 썼어요" in (result.notice or "")


# ─────────────────────────── compute_book_pace ───────────────────────────
# "목차를 가져와서 하루에 얼마나 볼지 페이지로 알려주는" 실질 — page_count 만으로 항상
# 계산된다(목차 유무 무관, L0 10/10).


def test_pace_is_none_without_a_deadline() -> None:
    outcome = _outcome_with_one_goal(weekly_hours=5, session_length_min=60, deadline=None)
    pace = materials_spec.compute_book_pace(
        page_count=420, outcome=outcome, target_date=date(2026, 9, 4)
    )
    assert pace is None


def test_pace_is_none_for_a_past_deadline() -> None:
    outcome = _outcome_with_one_goal(weekly_hours=5, session_length_min=60, deadline="2026-01-01")
    pace = materials_spec.compute_book_pace(
        page_count=420, outcome=outcome, target_date=date(2026, 9, 4)
    )
    assert pace is None


def test_pace_is_none_when_page_count_is_unknown() -> None:
    outcome = _outcome_with_one_goal(weekly_hours=5, session_length_min=60, deadline="2026-12-01")
    pace = materials_spec.compute_book_pace(
        page_count=0, outcome=outcome, target_date=date(2026, 9, 4)
    )
    assert pace is None


def test_pace_computes_pages_per_session_from_time_budget() -> None:
    """실측(2026-09-04) 시나리오와 같은 입력 — 주 5시간·세션 60분·마감 약 12주 뒤."""
    outcome = _outcome_with_one_goal(weekly_hours=5, session_length_min=60, deadline="2026-12-01")
    pace = materials_spec.compute_book_pace(
        page_count=420, outcome=outcome, target_date=date(2026, 9, 4)
    )
    assert pace is not None
    assert pace.total_sessions > 0
    # 세션당 페이지 × 세션 수는 총 페이지를 밑돌지 않아야 한다(다 못 보고 끝나면 안 된다).
    assert pace.pages_per_session * pace.total_sessions >= 420
    assert "420쪽" in pace.summary
    assert "2026-12-01" in pace.summary


def test_pace_scales_with_weekly_hours() -> None:
    """시간이 적으면(주 1시간) 세션 수가 줄어 세션당 페이지가 늘어야 한다."""
    light = _outcome_with_one_goal(weekly_hours=1, session_length_min=60, deadline="2026-12-01")
    heavy = _outcome_with_one_goal(weekly_hours=10, session_length_min=60, deadline="2026-12-01")

    pace_light = materials_spec.compute_book_pace(
        page_count=420, outcome=light, target_date=date(2026, 9, 4)
    )
    pace_heavy = materials_spec.compute_book_pace(
        page_count=420, outcome=heavy, target_date=date(2026, 9, 4)
    )
    assert pace_light is not None
    assert pace_heavy is not None
    assert pace_light.total_sessions < pace_heavy.total_sessions
    assert pace_light.pages_per_session > pace_heavy.pages_per_session


# ─────────────────── compute_book_pace — 챕터 경계 배정 ───────────────────
# "목차가 있으면 챕터별 실제 페이지 체크포인트로 진행하는가?" — 그렇다: 균등 분할이 아니라
# 각 챕터가 정확히 세션 경계에서 끝나도록(세션이 챕터 중간에서 안 끊기게) 세션 수를
# 챕터마다 정수로 배정한다.


def test_pace_respects_chapter_boundaries_when_full_toc_is_available() -> None:
    """주 5시간·세션 60분·마감 7일 뒤(2026-09-04 → 2026-09-11) → 균등 분할 기준
    `target_pages_per_session=ceil(100/5)=20`. 챕터가 41/26/28쪽 + 목차 밖 나머지 5쪽이면
    그 기준으로 챕터마다 반올림한 세션 수(2/1/1/1)를 배정하고, 합계가 `total_sessions` 가
    된다 — 균등하게 20쪽씩 자르는 게 아니다."""
    outcome = _outcome_with_one_goal(weekly_hours=5, session_length_min=60, deadline="2026-09-11")
    chapters = [
        BookChapter(title="Chapter 1. 서론", end_page=41),
        BookChapter(title="Chapter 2. 본론", end_page=67),
        BookChapter(title="Chapter 3. 결론", end_page=95),
    ]

    pace = materials_spec.compute_book_pace(
        page_count=100, chapters=chapters, outcome=outcome, target_date=date(2026, 9, 4)
    )

    assert pace is not None
    assert [c.title for c in pace.chapters] == [
        "Chapter 1. 서론",
        "Chapter 2. 본론",
        "Chapter 3. 결론",
        materials_spec._TOC_TAIL_LABEL,
    ]
    assert [c.sessions for c in pace.chapters] == [2, 1, 1, 1]
    # 목차 밖 나머지 5쪽(95→100)도 없는 셈 치지 않는다 — 이름 없는 항목으로 살아남는다.
    assert pace.chapters[-1].end_page == 100
    assert pace.total_sessions == sum(c.sessions for c in pace.chapters) == 5


def test_pace_leftover_below_half_a_session_still_gets_one() -> None:
    """목차 밖 나머지가 목표 세션당 쪽수의 절반도 안 되면(반올림하면 0) 그래도 최소 1세션은
    배정한다 — 존재하는 페이지를 없는 셈 치지 않는다."""
    outcome = _outcome_with_one_goal(weekly_hours=5, session_length_min=60, deadline="2026-09-11")
    chapters = [BookChapter(title="Chapter 1", end_page=95)]  # target_pages_per_session=20

    pace = materials_spec.compute_book_pace(
        page_count=100, chapters=chapters, outcome=outcome, target_date=date(2026, 9, 4)
    )

    assert pace is not None
    assert pace.chapters[-1].title == materials_spec._TOC_TAIL_LABEL
    assert pace.chapters[-1].sessions == 1  # round(5/20)=0 이었다면 여기서 최소 1로 강제됨


def test_pace_falls_back_to_uniform_when_any_chapter_is_missing_a_page() -> None:
    """일부 챕터만 페이지를 알면(중간에 `None` 하나라도 있으면) 챕터 배정 자체를 포기하고
    균등 분할로 돌아간다 — 절반만 믿을 수 있는 배정을 내느니 기존 폴백이 낫다."""
    outcome = _outcome_with_one_goal(weekly_hours=5, session_length_min=60, deadline="2026-09-11")
    chapters = [
        BookChapter(title="Chapter 1", end_page=41),
        BookChapter(title="Chapter 2", end_page=None),
        BookChapter(title="Chapter 3", end_page=95),
    ]

    pace = materials_spec.compute_book_pace(
        page_count=100, chapters=chapters, outcome=outcome, target_date=date(2026, 9, 4)
    )

    assert pace is not None
    assert pace.chapters == []
    assert pace.total_sessions == 5  # 균등 분할(ceil(100/5)=20쪽 기준)로 그대로 폴백
    assert pace.pages_per_session == 20


def test_pace_without_any_chapters_stays_uniform() -> None:
    """목차 자체가 없는 10권 중 9권 경로 — `chapters=()` 기본값으로도 그대로 동작해야 한다."""
    outcome = _outcome_with_one_goal(weekly_hours=5, session_length_min=60, deadline="2026-09-11")

    pace = materials_spec.compute_book_pace(
        page_count=100, outcome=outcome, target_date=date(2026, 9, 4)
    )

    assert pace is not None
    assert pace.chapters == []
    assert pace.total_sessions == 5


# ─────────────────────────── spec_slot_value ───────────────────────────


def test_spec_slot_value_for_book() -> None:
    detail = BookSpecDetail(
        title="책",
        author="저자",
        isbn13="9788994492049",
        page_count=1000,
        chapters=[],
        toc_source=None,
    )
    value = materials_spec.spec_slot_value([detail])
    assert value == {
        "type": "spec",
        "items": [
            {
                "kind": "book",
                "title": "책",
                "author": "저자",
                "isbn13": "9788994492049",
                "page_count": 1000,
                "chapters": [],
                "toc_source": None,
            }
        ],
    }


def test_spec_slot_value_attaches_book_pace_only_from_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """클라이언트가 보낸 `BookSpecDetail` 에는 애초에 진도 필드가 없다 — `book_pace` 인자로만
    실린다는 걸 확인한다(계획 산술에 영향을 주는 숫자를 클라이언트에서 신뢰하지 않는다)."""
    from reaction_backend.schemas.materials_spec import BookPace

    detail = BookSpecDetail(
        title="책", author="저자", isbn13="1", page_count=420, chapters=[], toc_source=None
    )
    pace = BookPace(
        pages_per_session=21, total_sessions=20, days_until_deadline=140, summary="요약"
    )
    value = materials_spec.spec_slot_value([detail], book_pace=pace)
    assert value["items"][0]["pages_per_session"] == 21
    assert value["items"][0]["total_sessions"] == 20


def test_spec_slot_value_replaces_chapters_with_chapter_pace_when_available() -> None:
    """`book_pace.chapters` 가 있으면(챕터 경계 배정 성공) 슬롯의 `chapters` 는 원본
    `detail.chapters` 가 아니라 그 배정 결과(세션 수 포함, 나머지 항목 포함)로 나가야
    한다 — `_materials_note` 가 이 슬롯 값을 그대로 텍스트로 풀기 때문이다."""
    from reaction_backend.schemas.materials_spec import BookPace, ChapterPace

    detail = BookSpecDetail(
        title="책",
        author="저자",
        isbn13="1",
        page_count=100,
        chapters=[BookChapter(title="Chapter 1", end_page=41)],  # 서버가 계산 전 원본
        toc_source="seoji",
    )
    pace = BookPace(
        pages_per_session=20,
        total_sessions=5,
        days_until_deadline=7,
        summary="요약",
        chapters=[
            ChapterPace(title="Chapter 1", end_page=41, sessions=2),
            ChapterPace(title=materials_spec._TOC_TAIL_LABEL, end_page=100, sessions=3),
        ],
    )
    value = materials_spec.spec_slot_value([detail], book_pace=pace)
    assert value["items"][0]["chapters"] == [
        {"title": "Chapter 1", "end_page": 41, "sessions": 2},
        {"title": materials_spec._TOC_TAIL_LABEL, "end_page": 100, "sessions": 3},
    ]


def test_spec_slot_value_for_video() -> None:
    detail = VideoSpecDetail(
        title="재생목록",
        channel_title="채널",
        playlist_id="PL1",
        playlist_url="https://www.youtube.com/playlist?list=PL1",
        video_count=1,
        total_minutes=54,
        curriculum=[VideoSpecItem(title="1강", minutes=54)],
        truncated=False,
    )
    value = materials_spec.spec_slot_value([detail])
    assert value["type"] == "spec"
    assert len(value["items"]) == 1
    assert value["items"][0]["kind"] == "video"
    assert value["items"][0]["curriculum"] == [{"title": "1강", "minutes": 54}]
    assert value["items"][0]["truncated"] is False


def test_spec_slot_value_combines_book_and_video() -> None:
    """materialMix="both" 경로 — 책 1개 + 영상 1개를 같이 담는다."""
    book = BookSpecDetail(
        title="책", author="저자", isbn13="1", page_count=100, chapters=[], toc_source=None
    )
    video = VideoSpecDetail(
        title="영상",
        channel_title="채널",
        playlist_id="PL1",
        playlist_url="https://www.youtube.com/playlist?list=PL1",
        video_count=1,
        total_minutes=10,
        curriculum=[VideoSpecItem(title="1강", minutes=10)],
    )
    value = materials_spec.spec_slot_value([book, video])
    assert [item["kind"] for item in value["items"]] == ["book", "video"]
