"""자료 상세 확정 — 목차(best-effort)/분량/진도 (ADR-0010 §1 ③).

LLM 을 호출하지 않는다. 도서는 알라딘(페이지 수, 필수) + 국중 seoji(목차, best-effort)를
합치고, 영상은 YouTube 재생목록 상세(커리큘럼+분량, 필수) 하나만 부른다.

`compute_book_pace` 가 "목차를 가져와서 하루에 얼마나 볼지 페이지로 알려주는" 실질이다 —
`page_count` 만으로 **항상**(L0 10/10) 세션당 권장 페이지를 계산한다. 목차 전 챕터에
페이지 체크포인트가 있으면(`BookChapter.end_page`, L0 1/10) 균등 분할 대신 **챕터 경계를
존중한** 세션 배정으로 바뀐다(`_chapter_session_plan`) — 세션이 챕터 중간에서 끊기지
않는다. 목차가 없어도 기능 자체는 죽지 않는다(그게 이 계산을 page_count 기반으로 설계한
이유 — 균등 분할이 항상 쓸 수 있는 폴백이다).

`spec_slot_value` 가 만드는 dict 는 기존 `goals.materials` 슬롯에
`{"type": "spec", "items": [...]}` 로 얹힌다 — `items` 는 1~2개(책 1개·영상 1개까지,
`MaterialsSpecConfirmRequest` 가 강제). `interview_adapter._materials_note` 가 각 항목을
텍스트로 풀어 이어붙인다(ADR-0010 §5). 새 테이블도 마이그레이션도 없다 — 기존 슬롯
인프라를 그대로 쓴다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from typing import Any, Literal

from reaction_backend.config import Settings
from reaction_backend.domain import slot_value as sv
from reaction_backend.integrations.aladin import client as aladin_client
from reaction_backend.integrations.nl_seoji import client as seoji_client
from reaction_backend.integrations.youtube import client as youtube_client
from reaction_backend.orchestrator import first_plan_adapter
from reaction_backend.schemas.interview import InterviewOutcome
from reaction_backend.schemas.materials_spec import (
    MAX_TOC_ENTRIES,
    BookChapter,
    BookDetailResponse,
    BookPace,
    BookSpecDetail,
    ChapterPace,
    VideoDetailResponse,
    VideoSpecDetail,
    VideoSpecItem,
)

_ALADIN_LOOKUP_NOTICE: dict[str, str] = {
    aladin_client.REASON_NO_KEY: "지금은 도서 상세 조회를 쓸 수 없어요.",
    aladin_client.REASON_TIMEOUT: "도서 정보를 가져오는 데 시간이 걸리고 있어요. 다시 시도해 주세요.",
    aladin_client.REASON_UNAVAILABLE: "지금은 도서 정보를 가져올 수 없어요. 잠시 후 다시 시도해 주세요.",
    aladin_client.REASON_NOT_FOUND: "이 도서 정보를 찾지 못했어요.",
}
_YOUTUBE_DETAIL_NOTICE: dict[str, str] = {
    youtube_client.REASON_NO_KEY: "지금은 영상 상세 조회를 쓸 수 없어요.",
    youtube_client.REASON_TIMEOUT: "재생목록 정보를 가져오는 데 시간이 걸리고 있어요. 다시 시도해 주세요.",
    youtube_client.REASON_QUOTA: "오늘 쓸 수 있는 영상 조회를 다 썼어요. 내일 다시 시도해 주세요.",
    youtube_client.REASON_UNAVAILABLE: "지금은 재생목록 정보를 가져올 수 없어요. 잠시 후 다시 시도해 주세요.",
    youtube_client.REASON_NOT_FOUND: "이 재생목록을 찾지 못했어요. 비공개로 전환됐을 수 있어요.",
}
_FALLBACK_NOTICE = "지금은 정보를 가져올 수 없어요. 잠시 후 다시 시도해 주세요."
_NO_TOC_NOTICE = "이 책은 목차 정보가 없어요. 페이지 수만으로 계획에 반영돼요."


async def book_detail(isbn13: str, *, settings: Settings) -> BookDetailResponse:
    """도서 상세 — 페이지 수는 필수, 목차(+페이지 체크포인트)는 best-effort(L0 실측
    10권 중 1권). 진도(`BookPace`)는 여기서 계산하지 않는다 — 목표의 시간 예산이 있어야
    하는데 이 endpoint 는 목표 맥락을 안 받는다(`confirm_spec` 이 계산한다)."""
    lookup = await aladin_client.lookup_book(isbn13, key=settings.aladin_ttb_key)
    if not lookup.ok:
        return BookDetailResponse(
            notice=_ALADIN_LOOKUP_NOTICE.get(lookup.reason or "", _FALLBACK_NOTICE)
        )
    assert lookup.lookup is not None  # `ok` 가 보장

    toc_result = await seoji_client.lookup_toc(isbn13, key=settings.nl_seoji_key)
    chapters: list[BookChapter] = []
    toc_source: Literal["seoji"] | None = None
    notice: str | None = None
    if toc_result.ok:
        assert toc_result.lookup is not None
        # 스키마 상한(`MAX_TOC_ENTRIES`)까지만 — 하루 한 단원짜리 단어책은 목차 항목이 100개를
        # 쉽게 넘는다. 넘긴 채로 응답을 만들면 검증에서 터져 도서 상세 전체가 500 이 됐다
        # (inbox-5). 잘린 뒤쪽 분량은 진도 계산의 "목차 이후 나머지 분량" 항목이 받는다.
        chapters = [
            BookChapter(title=c.title, end_page=c.end_page)
            for c in toc_result.lookup.chapters[:MAX_TOC_ENTRIES]
        ]
        toc_source = "seoji"
    elif toc_result.reason != seoji_client.REASON_NO_TOC:
        # `REASON_NO_TOC` 는 정상 경로(90%) 라 안내하지 않는다. 그 외(키 없음·타임아웃 등)만
        # "목차를 못 가져왔다" 는 사실을 알린다 — 페이지 수는 있으니 책 자체는 쓸 수 있다.
        notice = _NO_TOC_NOTICE

    return BookDetailResponse(
        detail=BookSpecDetail(
            title=lookup.lookup.title,
            author=lookup.lookup.author,
            isbn13=isbn13,
            page_count=lookup.lookup.page_count,
            chapters=chapters,
            toc_source=toc_source,
        ),
        notice=notice,
    )


_PACE_DENSITY = "standard"
"""진도 추정에 쓰는 density — 이 값은 preview 라 실제 계획 생성 요청의 density 와
다를 수 있다(그건 나중에 사용자가 고른다). `summary` 문구가 이걸 "추정치" 로 명시한다."""

_TOC_TAIL_LABEL = "(목차 이후 나머지 분량)"
"""목차가 커버하는 마지막 페이지(`chapters[-1].end_page`)가 `page_count` 보다 작을 때(부록·
연습문제 등, seoji 목차가 다 못 담는 경우가 흔하다) 그 차이를 담는 항목의 제목. 실제
챕터명을 모르니 지어내지 않는다 — "여기부터는 목차 밖" 이라고 정직하게 표시한다."""


def _chapter_session_plan(
    chapters: Sequence[BookChapter], *, page_count: int, target_pages_per_session: int
) -> list[ChapterPace]:
    """챕터 전부가 `end_page` 를 가졌을 때만 배정한다(하나라도 없으면 빈 리스트 — 호출부가
    균등 분할로 폴백한다) — **세션이 챕터 경계를 걸치지 않도록** 챕터마다 정수 개의 세션을
    배정한다(예: 41쪽짜리 챕터를 `target_pages_per_session=20` 기준 2세션으로).

    목차가 커버하는 마지막 페이지가 `page_count` 보다 작으면(흔하다 — 부록·연습문제 등은
    목차에 항목별로 안 잡힌다) 그 차이를 `_TOC_TAIL_LABEL` 이름 없는 항목 하나로 마저
    배정한다 — 존재하는 페이지를 없는 셈 치지 않는다.
    """
    if not chapters or any(c.end_page is None for c in chapters):
        return []
    plan: list[ChapterPace] = []
    prev_end = 0
    for chapter in chapters:
        assert chapter.end_page is not None  # 위에서 전부 확인됨
        pages = max(1, chapter.end_page - prev_end)
        sessions = max(1, round(pages / target_pages_per_session))
        plan.append(ChapterPace(title=chapter.title, end_page=chapter.end_page, sessions=sessions))
        prev_end = chapter.end_page
    leftover = page_count - prev_end
    if leftover > 0:
        sessions = max(1, round(leftover / target_pages_per_session))
        plan.append(ChapterPace(title=_TOC_TAIL_LABEL, end_page=page_count, sessions=sessions))
    return plan


def compute_book_pace(
    *,
    page_count: int,
    chapters: Sequence[BookChapter] = (),
    outcome: InterviewOutcome,
    target_date: date,
) -> BookPace | None:
    """책 전체를 마감까지 다 보려면 세션당 몇 쪽인지 — `page_count` 만으로 **항상** 계산
    가능하다(목차 유무 무관, L0 10/10). 마감(`goals.deadlines`)이 없으면 나눌 기간이
    없으므로 `None` — 지어내지 않는다.

    세션 수는 `first_plan_adapter.target_sessions_per_week` 를 그대로 재사용한다 — 빈도
    (goals.frequency) vs 주당 시간(goals.weekly_time) 의 우선순위 규칙을 여기서 다시
    만들면 두 계산이 갈라질 위험이 있다(실제 계획 생성이 쓰는 것과 같은 함수여야 한다).

    `chapters` 에 페이지 체크포인트가 다 있으면(best-effort, L0 1/10) 그 시간 예산을
    **목표 세션당 쪽수**로만 쓰고, 실제 `total_sessions`/`pages_per_session` 은
    `_chapter_session_plan` 이 챕터 경계를 존중해 다시 계산한다 — 균등 분할로 챕터
    중간에 세션이 끊기지 않게 하기 위해서다.
    """
    if page_count <= 0:
        return None
    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    if not heaviest.deadline:
        return None
    try:
        deadline_date = date.fromisoformat(heaviest.deadline)
    except ValueError:
        return None
    days = (deadline_date - target_date).days
    if days <= 0:
        return None

    sessions_per_week = first_plan_adapter.target_sessions_per_week(outcome, _PACE_DENSITY)
    uniform_total_sessions = max(1, round(sessions_per_week * (days / 7)))
    target_pages_per_session = math.ceil(page_count / uniform_total_sessions)

    chapter_plan = _chapter_session_plan(
        chapters, page_count=page_count, target_pages_per_session=target_pages_per_session
    )
    if chapter_plan:
        total_sessions = sum(c.sessions for c in chapter_plan)
        pages_per_session = math.ceil(page_count / total_sessions)
        summary = (
            f"목차 챕터 경계에 맞춰 {total_sessions}세션(세션당 평균 약 {pages_per_session}쪽)"
            f"이면 마감({deadline_date.isoformat()})까지 {page_count}쪽을 다 봐요."
        )
    else:
        total_sessions = uniform_total_sessions
        pages_per_session = target_pages_per_session
        summary = (
            f"세션당 약 {pages_per_session}쪽씩 {total_sessions}번이면 "
            f"마감({deadline_date.isoformat()})까지 {page_count}쪽을 다 봐요."
        )

    return BookPace(
        pages_per_session=pages_per_session,
        total_sessions=total_sessions,
        days_until_deadline=days,
        summary=summary,
        chapters=chapter_plan,
    )


async def video_detail(playlist_id: str, *, settings: Settings) -> VideoDetailResponse:
    """영상 상세 — 커리큘럼+분량이 이 소스의 핵심이라(L0 4/4) 못 가져오면 후보 자체가 못 쓴다."""
    result = await youtube_client.get_playlist_detail(playlist_id, key=settings.youtube_api_key)
    if not result.ok:
        return VideoDetailResponse(
            notice=_YOUTUBE_DETAIL_NOTICE.get(result.reason or "", _FALLBACK_NOTICE)
        )
    assert result.detail is not None
    d = result.detail
    return VideoDetailResponse(
        detail=VideoSpecDetail(
            title=d.title,
            channel_title=d.channel_title,
            playlist_id=playlist_id,
            playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
            video_count=d.video_count,
            total_minutes=d.total_seconds // 60,
            curriculum=[
                VideoSpecItem(title=c.title, minutes=c.seconds // 60) for c in d.curriculum
            ],
            truncated=d.truncated,
        )
    )


def _spec_item_dict(
    detail: BookSpecDetail | VideoSpecDetail, *, book_pace: BookPace | None
) -> dict[str, Any]:
    """항목 하나 → dict. 명시적으로 조립한다 — `model_dump` 의 alias 여부에 슬롯 스키마가
    흔들리지 않게(내부 저장 키는 항상 snake_case).

    `book_pace` 는 **서버가 방금 계산한 값만** 받는다 — 클라이언트가 `BookSpecDetail` 에
    진도를 실어 보내도(애초에 그 모델엔 그런 필드가 없다) 여기서 무시되고 항상 새로
    계산된 값으로 덮인다. 계획의 분량 산술에 영향을 주는 숫자라 클라이언트를 신뢰하지
    않는다.

    `book_pace.chapters` 가 있으면(챕터 경계를 존중한 배정이 성공했을 때) `chapters` 를
    그 결과로 **통째로 교체**한다 — 목차 커버리지 밖 나머지 분량 항목까지 포함해서다.
    실패했으면(목차 없음/일부만 있음) `detail.chapters` 그대로(세션 정보 없이)."""
    if isinstance(detail, BookSpecDetail):
        chapters_payload = (
            [
                {"title": c.title, "end_page": c.end_page, "sessions": c.sessions}
                for c in book_pace.chapters
            ]
            if book_pace is not None and book_pace.chapters
            else [{"title": c.title, "end_page": c.end_page} for c in detail.chapters]
        )
        item: dict[str, Any] = {
            "kind": "book",
            "title": detail.title,
            "author": detail.author,
            "isbn13": detail.isbn13,
            "page_count": detail.page_count,
            "chapters": chapters_payload,
            "toc_source": detail.toc_source,
        }
        if book_pace is not None:
            item["pages_per_session"] = book_pace.pages_per_session
            item["total_sessions"] = book_pace.total_sessions
        return item
    return {
        "kind": "video",
        "title": detail.title,
        "channel_title": detail.channel_title,
        "playlist_id": detail.playlist_id,
        "playlist_url": detail.playlist_url,
        "video_count": detail.video_count,
        "total_minutes": detail.total_minutes,
        "curriculum": [{"title": c.title, "minutes": c.minutes} for c in detail.curriculum],
        "truncated": detail.truncated,
    }


def spec_slot_value(
    details: Sequence[BookSpecDetail | VideoSpecDetail], *, book_pace: BookPace | None = None
) -> dict[str, Any]:
    """`goals.materials` 슬롯에 쓸 dict — 1~2건(책 1개·영상 1개까지)을 `items` 로 담는다.
    `book_pace` 는 책 항목에만 붙는다(영상 항목은 무시)."""
    return sv.dump(sv.SpecAnswer(tuple(_spec_item_dict(d, book_pace=book_pace) for d in details)))


__all__ = ["book_detail", "compute_book_pace", "spec_slot_value", "video_detail"]
