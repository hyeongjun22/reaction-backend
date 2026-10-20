"""자료 검색 카탈로그 — 도서(국립중앙도서관)/YouTube 후보 스키마 (ADR-0010 §1 ②).

LLM 산출물이 아니라 순수 검색 API 결과라 `DraftMixin` 을 쓰지 않는다 — 그 mixin 은 "AI 가
만들어낸 것이라 accept/edit/reject 이 필요한" 단일 산출물용이다(`MaterialsSearchResponse`
참고). 이건 사용자가 여러 후보 중 하나를 **고르는** 목록이지, 승인/거절할 산출물이 아니다.

후보에는 목차·분량이 없다(ADR-0010 §4 — 그건 후보를 고른 뒤의 별도 단계 몫). 검색 질의는
사용자가 확인·편집한 것만 받는다(#259 §4.1 ① 결정과 같은 원칙) — 목표 원문을 서버가
알아서 검색으로 내보내지 않는다.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from reaction_backend.schemas.common import CamelModel

MAX_CANDIDATES = 5
MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 100


class BookCandidate(CamelModel):
    """도서 검색 후보 1건 — 국립중앙도서관 ISBN 서지정보(알라딘 종료로 교체, v2.38)."""

    title: str
    author: str
    publisher: str
    isbn13: str
    cover_url: str
    """표지 이미지 — 이 소스는 대개 비워 두므로 빈 문자열이 흔하다."""
    link_url: str
    """서점 상품 페이지 — 이 소스엔 없어 빈 문자열이다(알라딘 시절 필드, 하위호환으로 남김)."""
    page_count: int | None = None
    """쪽수 — 후보 단계에서 이미 안다. 모르면 `null`(세트·낱장 표기 등)."""


class VideoCandidate(CamelModel):
    """YouTube 재생목록 검색 후보 1건."""

    playlist_id: str
    """후보 선택 시 `/plans/materials/video-detail` 로 그대로 넘긴다."""
    title: str
    channel_title: str
    thumbnail_url: str
    playlist_url: str


class MaterialsCatalogRequest(CamelModel):
    """POST /plans/materials/catalog — 사용자가 확인·편집한 검색어만 받는다.

    둘 다 생략할 수 없다(적어도 하나는 있어야 검색할 게 있다) — `study-method` 가 준
    `bookQuery`/`videoQuery` 를 그대로 쓰거나 사용자가 고친 값을 보낸다.
    """

    book_query: str | None = Field(
        default=None, min_length=MIN_QUERY_CHARS, max_length=MAX_QUERY_CHARS
    )
    video_query: str | None = Field(
        default=None, min_length=MIN_QUERY_CHARS, max_length=MAX_QUERY_CHARS
    )

    @model_validator(mode="after")
    def _at_least_one_query(self) -> MaterialsCatalogRequest:
        if not self.book_query and not self.video_query:
            raise ValueError("bookQuery 또는 videoQuery 중 하나는 있어야 해요.")
        return self


class MaterialsCatalogResponse(CamelModel):
    """검색 결과 — 두 소스는 독립적으로 실패할 수 있다(부분 성공을 실패로 뭉개지 않는다)."""

    books: list[BookCandidate] = Field(default_factory=list, max_length=MAX_CANDIDATES)
    book_notice: str | None = None
    """도서 후보가 비어 있을 때 이유(검색 실패·결과 없음). 요청에 `bookQuery` 가 없었으면 None."""
    videos: list[VideoCandidate] = Field(default_factory=list, max_length=MAX_CANDIDATES)
    video_notice: str | None = None
    """영상 후보가 비어 있을 때 이유. 요청에 `videoQuery` 가 없었으면 None."""
