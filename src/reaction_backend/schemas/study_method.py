"""자료 조사 파이프라인 1단계 — Method Agent 산출물 스키마 (ADR-0010).

L0 스파이크(`docs/experiments/l0-materials-source-results.md`)가 확인한 사실 위에 얹는다:
도서 목차는 API 로 안정적으로 못 얻고(알라딘 0/10, seoji 1/10), 영상 강의는 재생목록
검색으로 커리큘럼+분량이 통째로 온다. 그래서 이 산출물은 "이 목표에 어떤 방식이 맞는지"
와 "그 방식에 맞는 자료를 찾을 검색어" 를 낸다 — 도서 검색어와 영상 검색어를 **따로**
낸다(ADR-0010 §3). 뒤 단계가 두 소스를 병행 검색해 사용자에게 후보로 보여주기 때문이다.

`material_mix` — 책·영상 중 무엇을 확정하는 게 좋은지도 Method Agent 가 함께 판단한다
(v2, 실사용 문의로 추가). 예: 목차 구조가 뚜렷한 단일 교재로 충분한 목표는 `book`, 설명이
핵심인 스킬은 `video`, 이론(책)과 실전 트레이닝(영상)이 둘 다 필요한 목표는 `both`. 이건
검색 대상을 좁히지 않는다 — 카탈로그는 어차피 둘 다 검색한다(§ 위 문단) — **확정 단계에서
사용자에게 몇 개를 고르라고 권할지**의 근거일 뿐이라 최종 결정은 여전히 사용자 몫이다.

그라운딩을 쓰지 않는다(#259 의 자료 검색과 다르다) — 이 산출물은 특정 자료의 원문을
인용하는 게 아니라 일반적 학습 전략 조언이라 RECITATION 리스크가 없다(ADR-0010 §2).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, DraftMixin

MAX_FOCUS_POINTS = 5

MaterialMix = Literal["book", "video", "both"]


class StudyMethodPlan(CamelModel):
    """`planning/study_method` LLM Structured Output — 추천 방식 + 검색어 2종 + 자료 조합."""

    approach: str = Field(min_length=1, max_length=200)
    """1~2문장. "왜 이 방식인지" 가 아니라 "무엇을 우선할지" 를 사용자가 바로 알아채게."""
    focus_points: list[str] = Field(default_factory=list, max_length=MAX_FOCUS_POINTS)
    """approach 를 뒷받침하는 우선순위 항목(예: "RC 파트5 문법", "LC 딕테이션")."""
    book_query: str = Field(min_length=1, max_length=100)
    """도서 검색(알라딘)에 쓸 질의."""
    video_query: str = Field(min_length=1, max_length=100)
    """영상 강의 검색(YouTube)에 쓸 질의."""
    material_mix: MaterialMix = "both"
    """이 목표엔 책·영상 중 뭘 확정하는 게 좋은지. `spec-confirm` 이 몇 건까지 받을지의
    권장값일 뿐 — 사용자가 그와 다르게 골라도 막지 않는다."""


class StudyMethodRequest(CamelModel):
    """POST /plans/materials/study-method."""

    interview_session_id: str | None = None
    """미지정이면 가장 최근 '정상 종료' 인터뷰에서 가장 무거운 목표를 쓴다."""


class StudyMethodResponse(DraftMixin, StudyMethodPlan):
    """POST /plans/materials/study-method 응답 — API 경계에서 `StudyMethodPlan` 을 감싼다.

    LLM 산출물이라 `DraftMixin`(is_draft/ai_source) 을 붙인다 — `MandalaSubgoalsResponse`
    와 같은 패턴. 이 단계는 아직 아무것도 외부로 나가지 않는다(그라운딩도 검색도 없다) —
    사용자가 검색어를 확인·편집한 뒤에야 `/plans/materials/catalog` 가 실제로 나간다
    (#259 §4.1 ① 결정과 같은 원칙).
    """

    goal_title: str
    notice: str
    curated_books: list[CuratedBook] = Field(default_factory=list)
    """목표가 교재 카드가 있는 시험(지금은 토익)이면 추천 교재 카드. 없으면 빈 배열 —
    그때는 예전처럼 검색어로 알라딘/YouTube 를 찾는다. 카드를 고르면
    `/plans/materials/curriculum-confirm` 으로 확정하고, 계획이 그 책의 단원 순서로 짜인다."""


class CuratedBook(CamelModel):
    """추천 교재 카드 한 장 — 구조 요약과 추천 이유. 단원 목록 자체는 싣지 않는다(카드에만 있다)."""

    id: str
    title: str
    publisher: str
    role: Literal["vocab", "starter", "core", "all_in_one", "practice"]
    structure: str
    """한 문장 구성 설명 — "30 DAY 단어 학습 + 10일마다 실전 문제"."""
    unit_count: int
    page_count: int
    reason: str
    """이 사용자에게 왜 이 책인지(현재 점수·목표 점수 기준)."""
    recommended: bool
    """기본으로 골라 둘 책인가 — `False` 는 대안·선택 사항(사용자가 직접 고르면 쓴다)."""


class CurriculumConfirmRequest(CamelModel):
    """POST /plans/materials/curriculum-confirm — "이 교재들로 할게요".

    카드 id 만 받는다 — 제목·단원은 서버의 카드에서 꺼낸다(클라이언트가 보낸 단원을 믿지 않는다).
    """

    book_ids: list[str] = Field(min_length=1, max_length=4)
    interview_session_id: str | None = None


class CurriculumConfirmResponse(CamelModel):
    goal_title: str
    book_ids: list[str]
    notice: str
