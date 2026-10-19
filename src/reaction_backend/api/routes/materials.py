"""자료 검색 3단계 HITL 흐름 (#259 §5 1차 범위).

    ① POST /plans/materials/search-query   검색어 제안 — 외부 호출 0회 · LLM 0회
    ② POST /plans/materials/search         검색 실행 — 그라운딩 1건, 저장 없음(Draft)
    ③ POST /plans/materials/confirm        사용자 확정 — goals.materials 슬롯에 기록

**③ 이후는 새 배선이 없다.** 확정된 자료는 `goals.materials` 슬롯에 들어가고, 그 뒤는
사용자가 붙여넣은 것과 **완전히 같은 경로**로 흐른다:

    goals.materials → build_outcome → materials_note
                    → materials_for_prompt(울타리 무력화, #260)
                    → planning/goal_decompose

그래서 검색 전용 저장소도, 분해 쪽 분기도 없다(#259 ⑪ "1차는 저장 없음" — 새 테이블·
마이그레이션이 없다는 뜻이고, 사용자가 확정한 자료를 기존 슬롯에 쓰는 건 붙여넣기와 같다).

라이브 실측(2026-08-23)이 이 흐름의 안내 문구를 결정했다: 인프런 강의 커리큘럼은 4/4 로
찾아지지만 상업 교재 목차는 provider 가 저작권으로 막는다(3/4). 그래서 `blocked_copyright`
를 따로 두고 "다시 시도" 가 아니라 "직접 붙여넣어 주세요" 로 안내한다.

**ADR-0010 이 별도 파이프라인을 추가한다** — 위 ①②③ 은 그대로 두고(교체하지 않는다,
ADR-0010 §4), 이미 정해진 자료의 목차를 그라운딩으로 "확인" 하는 대신 무엇을 찾을지부터
API 로 실제 검색한다:

    POST /plans/materials/study-method   추천 방식 + 검색어 2종 — LLM 구조화 호출 1회
    POST /plans/materials/catalog        알라딘/YouTube 후보 검색 — API 만, LLM 0회
    POST /plans/materials/book-detail    후보 도서 1건 → 페이지 수 + 목차(best-effort)
    POST /plans/materials/video-detail   후보 재생목록 1건 → 커리큘럼 + 분량
    POST /plans/materials/spec-confirm   "이 자료 맞아요" → goals.materials 슬롯에 저장

**spec-confirm 은 계획 생성에 반영된다(ADR-0010 §5)** — `interview_adapter._materials_note`
가 `type == "spec"` 값을 텍스트로 풀어 `materials_note` 에 싣고, 그 뒤로는 붙여넣기
텍스트와 완전히 같은 경로(`materials_for_prompt` → `goal_decompose`)를 탄다. 새 프롬프트
변수도 새 인젝션 방어도 만들지 않았다 — 기존 울타리를 그대로 통과시킨다.
"""

from __future__ import annotations

import logging
import re
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.agents import study_method_agent
from reaction_backend.api.deps import CurrentUser
from reaction_backend.config import get_settings
from reaction_backend.db.models.interview_session import InterviewSession
from reaction_backend.db.session import get_db
from reaction_backend.domain import curriculum as cur
from reaction_backend.domain import slot_value as sv
from reaction_backend.llm import aiClient
from reaction_backend.llm.tool_executor import GroundedResult
from reaction_backend.orchestrator import curriculum_plan, materials_catalog, materials_spec
from reaction_backend.orchestrator._common import user_agent_lock
from reaction_backend.orchestrator.interview_adapter import is_placeholder_goal
from reaction_backend.orchestrator.interview_projection import project_session_outcome
from reaction_backend.repositories.interview_repo import InterviewRepo, get_interview_repo
from reaction_backend.safety.llm_budget import check_grounding
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.interview import GoalCandidate, InterviewOutcome
from reaction_backend.schemas.materials import (
    MaterialsConfirmRequest,
    MaterialsConfirmResponse,
    MaterialSource,
    MaterialsQueryRequest,
    MaterialsQueryResponse,
    MaterialsSearchRequest,
    MaterialsSearchResponse,
    MaterialsSearchStatus,
)
from reaction_backend.schemas.materials_catalog import (
    MaterialsCatalogRequest,
    MaterialsCatalogResponse,
)
from reaction_backend.schemas.materials_spec import (
    BookDetailRequest,
    BookDetailResponse,
    BookSpecDetail,
    MaterialsSpecConfirmRequest,
    MaterialsSpecConfirmResponse,
    VideoDetailRequest,
    VideoDetailResponse,
)
from reaction_backend.schemas.study_method import (
    CuratedBook,
    CurriculumConfirmRequest,
    CurriculumConfirmResponse,
    StudyMethodRequest,
    StudyMethodResponse,
)

_log = logging.getLogger(__name__)

RepoDep = Annotated[InterviewRepo, Depends(get_interview_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]

router = APIRouter(prefix="/plans/materials", tags=["planning"])

_LOCK_AGENT = "materials_search"
_MATERIALS_SLOT = "goals.materials"

# 목표 제목 끝에 붙는 '행위' 표현 — 검색어에서 떼어낸다. "김영한의 실전 자바 완강" 을 그대로
# 검색하면 강의가 아니라 완강 후기가 먼저 잡힌다. 우리가 찾는 건 **자료 자체**다.
_GOAL_VERB_TAIL = re.compile(
    r"(?:완강|완독|정복|마스터|끝내기|끝내자|마치기|떼기|공부하기|시작하기|도전|하기|완료)\s*$"
)


# ─────────────────────────────────────────────────────────────────────────────
# 공통
# ─────────────────────────────────────────────────────────────────────────────


def _no_goal() -> ApiError:
    return ApiError(
        ErrorCode.COMMON_VALIDATION_ERROR,
        "자료를 붙일 목표가 없어요. 인터뷰에서 목표를 먼저 정해 주세요.",
        http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
    )


def _no_interview() -> ApiError:
    return ApiError(
        ErrorCode.COMMON_VALIDATION_ERROR,
        "완료된 인터뷰가 없어요. 인터뷰를 먼저 진행해 주세요.",
        http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
    )


async def _resolve_session(
    repo: InterviewRepo, user_id: UUID, session_id: str | None
) -> InterviewSession:
    """자료를 붙일 인터뷰 세션 — 지정이 없으면 가장 최근 정상 종료 세션."""
    if session_id:
        try:
            parsed = UUID(session_id)
        except ValueError as exc:
            raise _no_interview() from exc
        row = await repo.get_active(user_id, parsed)
        if row is None:
            raise _no_interview()
        return row
    latest = await repo.get_latest_finished(user_id)
    if latest is None:
        raise _no_interview()
    return latest


def _heaviest(outcome: InterviewOutcome) -> GoalCandidate:
    """분해가 실제로 쓰는 목표 — 자료도 이 목표에만 붙는다(`materials_note` 계약).

    placeholder(#88)를 거르고 `is_heaviest` 를 고르는 것은 `first_plan_adapter` 와 **같은
    규칙**이다. 다르면 사용자가 A 목표의 자료를 확정했는데 계획은 B 목표로 짜인다.
    """
    real = [g for g in outcome.core_goals if not is_placeholder_goal(g)]
    if not real:
        raise _no_goal()
    return next((g for g in real if g.is_heaviest), real[0])


def suggest_query(goal: GoalCandidate) -> str:
    """목표에서 검색어를 만든다 — **규칙만. LLM 도 외부 호출도 없다.**

    이 단계가 아무것도 내보내지 않는 것이 ① 프라이버시 결정의 핵심이다. 사용자가 이
    문자열을 보고 고친 뒤에야 2단계에서 외부로 나간다.

    제목 끝의 행위 표현("완강", "끝내기")을 떼는 이유: 그대로 검색하면 자료가 아니라 후기·
    회고가 먼저 잡힌다. 우리가 찾는 건 자료 자체다.
    """
    title = _GOAL_VERB_TAIL.sub("", goal.title.strip()).strip(" -–—·")
    return f"{title} 목차 커리큘럼".strip() if title else "목차 커리큘럼"


# ─────────────────────────────────────────────────────────────────────────────
# ① 검색어 제안
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/search-query")
async def propose_search_query(
    body: MaterialsQueryRequest,
    user: CurrentUser,
    repo: RepoDep,
) -> MaterialsQueryResponse:
    """검색어를 제안한다. **아직 아무것도 검색하지 않는다.**

    외부 호출이 0회라 과금도 예산 차감도 없다 — 사용자가 몇 번을 다시 열어봐도 무료다.
    """
    session_row = await _resolve_session(repo, user.id, body.interview_session_id)
    outcome = await project_session_outcome(session_row, repo)
    goal = _heaviest(outcome)
    return MaterialsQueryResponse(
        suggested_query=suggest_query(goal),
        goal_title=goal.title,
        notice=(
            "이 검색어가 그대로 웹 검색에 쓰여요. 빼고 싶은 내용이 있으면 고쳐 주세요. "
            "고치기 전까지는 아무것도 나가지 않아요."
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# ② 검색 실행
# ─────────────────────────────────────────────────────────────────────────────

# 폐기 사유 → (상태, 사용자에게 할 말). **다음에 할 행동이 다르면 문구도 달라야 한다.**
# 금지어 필터(DevBaseline §4.2)를 통과하는 표현으로 쓴다 — 이 문구들은 우리가 쓴
# 사용자 노출 문자열이다. 회귀는 `test_notices_pass_the_banned_word_filter` 가 잡는다.
_DISCARD_NOTICE: dict[str, tuple[MaterialsSearchStatus, str]] = {
    "ungrounded": (
        "not_found",
        "검색으로는 이 자료의 목차가 확인되지 않았어요. "
        "검색어를 바꿔 보거나, 목차를 직접 붙여넣어 주시면 그대로 반영할게요.",
    ),
    "empty": (
        "not_found",
        "검색 결과에서 읽을 만한 내용을 찾지 않았어요. "
        "목차를 직접 붙여넣어 주시면 그대로 반영할게요.",
    ),
    "recitation": (
        "blocked_copyright",
        "이 자료는 저작권 보호 때문에 검색으로 가져올 수 없어요. "
        "다시 시도해도 같으니, 목차를 직접 붙여넣어 주시면 그대로 반영할게요.",
    ),
    "grounding_budget": (
        "quota_exceeded",
        "오늘 쓸 수 있는 자료 검색을 다 썼어요. 내일 다시 쓸 수 있고, "
        "목차를 직접 붙여넣으면 지금 바로 반영돼요.",
    ),
    "budget": (
        "quota_exceeded",
        "오늘 쓸 수 있는 AI 사용량을 다 썼어요. 내일 다시 쓸 수 있고, "
        "목차를 직접 붙여넣으면 지금 바로 반영돼요.",
    ),
}
_UNAVAILABLE_NOTICE = (
    "지금은 검색이 잘 되지 않네요. 잠시 후 다시 시도하거나, "
    "목차를 직접 붙여넣어 주시면 그대로 반영할게요."
)
_FOUND_NOTICE = "이 자료를 참고했어요. 내용이 맞는지 확인하고, 다르면 고친 뒤에 확정해 주세요."


def _to_response(result: GroundedResult, *, remaining: int | None) -> MaterialsSearchResponse:
    """`GroundedResult` → API 응답. 폐기 사유를 사용자 행동으로 번역하는 곳."""
    sources = [MaterialSource(title=s.title, uri=s.uri) for s in result.sources]
    if result.usable:
        return MaterialsSearchResponse(
            status="found",
            text=result.text,
            sources=sources,
            search_queries=list(result.search_queries),
            notice=_FOUND_NOTICE,
            remaining_today=remaining,
            ai_source="llm",
        )
    status, notice = _DISCARD_NOTICE.get(result.reason or "", ("unavailable", _UNAVAILABLE_NOTICE))
    return MaterialsSearchResponse(
        status=status,
        text=None,
        sources=sources,
        search_queries=list(result.search_queries),
        notice=notice,
        remaining_today=remaining,
        ai_source="llm",
    )


@router.post("/search")
async def search_materials(
    body: MaterialsSearchRequest,
    user: CurrentUser,
    session: SessionDep,
) -> MaterialsSearchResponse:
    """사용자가 확정한 검색어로 자료를 찾는다. 결과는 **Draft — 저장하지 않는다.**

    동시 요청을 락으로 직렬화하는 이유는 성능이 아니라 **예산**이다. 그라운딩은 건당
    과금인데, 두 요청이 나란히 들어오면 둘 다 잔량 검사를 통과한 뒤 둘 다 호출해
    상한을 넘길 수 있다(TOCTOU). 락 안에서 검사→호출→기록이 한 트랜잭션으로 끝난다.
    """
    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        result = await aiClient.run_grounded(
            "planning",
            "planning/materials_search",
            variables={"query": body.query},
            user_id=user.id,
            session=session,
        )
        remaining = await _remaining_today(session, user.id)
        await session.commit()

    _log.info(
        "materials_search",
        extra={
            "user_id": str(user.id),
            "reason": result.reason,
            "sources": len(result.sources),
            "grounding_requests": result.grounding_requests,
        },
    )
    return _to_response(result, remaining=remaining)


async def _remaining_today(session: SessionDep, user_id: UUID) -> int | None:
    """오늘 남은 검색 횟수. 한도 0(무제한)이면 None — 화면에 "남은 횟수"를 띄우지 않게."""
    if get_settings().llm_daily_grounding_budget <= 0:
        return None
    try:
        status = await check_grounding(session, user_id=user_id, projected_requests=0)
    except Exception:  # noqa: BLE001 - 잔량 표시는 부가 정보라 실패해도 본 응답을 막지 않는다
        return None
    return max(0, status.limit - status.used)


# ─────────────────────────────────────────────────────────────────────────────
# ③ 사용자 확정
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/confirm")
async def confirm_materials(
    body: MaterialsConfirmRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> MaterialsConfirmResponse:
    """ "이 자료 맞아요" — 확정본을 `goals.materials` 슬롯에 쓴다 (② HITL 결정).

    여기서부터는 **붙여넣기와 구분되지 않는다.** 다음 계획 생성이 기존 경로로 이 값을
    집어가고, 자료 텍스트는 `materials_for_prompt` 의 울타리 안에 들어간다(#260).

    사용자가 고친 텍스트를 그대로 받는다 — 검색이 다른 판을 가져왔거나 일부만 맞을 때
    지우고 붙일 수 있어야 HITL 이다.
    """
    session_row = await _resolve_session(repo, user.id, body.interview_session_id)
    outcome = await project_session_outcome(session_row, repo)
    goal = _heaviest(outcome)

    text = body.text.strip()
    await repo.upsert_slot_answer(
        session_row.id,
        _MATERIALS_SLOT,
        sv.dump(sv.TextAnswer(text)),
        is_required=True,
    )
    await session.commit()

    return MaterialsConfirmResponse(
        goal_title=goal.title,
        saved_chars=len(text),
        notice="자료를 반영했어요. 다음 계획 생성부터 이 목차를 뼈대로 삼아요.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# ADR-0010 — 자료 검색 파이프라인 (①②③ 과 독립. 배선 병행, 교체 아님 — ADR-0010 §4)
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/study-method")
async def propose_study_method(
    body: StudyMethodRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> StudyMethodResponse:
    """목표에 맞는 학습 방식 + 도서/영상 검색어 2종을 추천한다.

    LLM 구조화 호출 1회뿐 — 그라운딩도 검색도 아직 없다(ADR-0010 §2). 아무것도 외부로
    나가지 않으므로 `/plans/materials/catalog` 이전에 사용자가 검색어를 확인·편집할 수
    있다(#259 §4.1 ① 결정과 같은 원칙).
    """
    session_row = await _resolve_session(repo, user.id, body.interview_session_id)
    outcome = await project_session_outcome(session_row, repo)
    goal = _heaviest(outcome)

    plan, fell_back = await study_method_agent.run(goal=goal, session=session, user_id=user.id)
    await session.commit()
    curated = [
        CuratedBook(
            id=r.book.id,
            title=r.book.title,
            publisher=r.book.publisher,
            role=r.book.role,
            structure=r.book.structure,
            unit_count=len(r.book.studyable()),
            page_count=r.book.page_count,
            reason=r.reason,
            recommended=r.recommended,
        )
        for r in curriculum_plan.recommend(goal)
    ]

    return StudyMethodResponse(
        curated_books=curated,
        approach=plan.approach,
        focus_points=plan.focus_points,
        book_query=plan.book_query,
        video_query=plan.video_query,
        material_mix=plan.material_mix,
        goal_title=goal.title,
        notice=(
            "이 검색어들이 그대로 도서/영상 검색에 쓰여요. 빼고 싶은 내용이 있으면 고쳐 "
            "주세요. 고치기 전까지는 아무것도 나가지 않아요."
        ),
        ai_source="rule" if fell_back else "llm",
    )


@router.post("/catalog")
async def search_catalog(
    body: MaterialsCatalogRequest, user: CurrentUser
) -> MaterialsCatalogResponse:
    """사용자가 확인·편집한 검색어로 알라딘/YouTube 후보를 찾는다. 저장하지 않는다.

    LLM 을 부르지 않으므로(ADR-0010 §1 ②) `llm_runs`/그라운딩 예산과 무관하다 — 대신
    쿼터는 각 provider 가 관리한다. YouTube `search.list` 는 1회에 100유닛이라 앱 전체
    일일 쿼터(10,000유닛)로 하루 ~100회가 상한이다(`config.youtube_api_key` 참고) — 지금은
    사용자별 상한이 없고, 쿼터를 넘기면 `videos` 가 빈 배열로 오고 `videoNotice` 에 그
    사유(다시 시도 안내)가 채워질 뿐이다(500 이 아니다).
    """
    return await materials_catalog.search(book_query=body.book_query, video_query=body.video_query)


@router.post("/book-detail")
async def get_book_detail(body: BookDetailRequest, user: CurrentUser) -> BookDetailResponse:
    """후보 도서 1건의 페이지 수 + 목차(best-effort)를 조회한다. 저장하지 않는다.

    페이지 수는 알라딘에서 안정적으로 온다(L0 실측 10/10). 목차는 국중 seoji 에서
    best-effort 로만 온다(L0 실측 10권 중 1권, 판본마다 다르다) — 못 가져와도 `detail`
    은 채워진다(페이지 수만으로 계획에 반영할 수 있으므로). `detail` 자체가 없으면 알라딘
    조회가 실패한 것이다(`notice` 에 사유).
    """
    settings = get_settings()
    return await materials_spec.book_detail(body.isbn13, settings=settings)


@router.post("/video-detail")
async def get_video_detail(body: VideoDetailRequest, user: CurrentUser) -> VideoDetailResponse:
    """후보 재생목록 1건의 커리큘럼(영상 제목) + 분량(재생시간)을 조회한다. 저장하지 않는다.

    이 소스는 커리큘럼·분량이 핵심이라(L0 실측 4/4) 못 가져오면 `detail` 자체가 없다
    (`notice` 에 사유 — 쿼터 초과 포함).
    """
    settings = get_settings()
    return await materials_spec.video_detail(body.playlist_id, settings=settings)


@router.post("/spec-confirm")
async def confirm_spec(
    body: MaterialsSpecConfirmRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> MaterialsSpecConfirmResponse:
    """ "이 자료 맞아요" — book/video-detail 이 돌려준 `details`(1~2건)를 그대로 되받아
    저장한다. 몇 건이 좋을지는 `study-method` 의 `materialMix` 가 권장하지만 강제하지
    않는다 — 사용자가 책만, 영상만, 또는 둘 다 보낼 수 있다.

    재조회하지 않는다 — 사용자가 화면에서 이미 확인한 값이고(②→③ 과 같은 HITL 왕복), 같은
    isbn/재생목록을 또 부르면 API 호출만 늘어난다. `goals.materials` 슬롯에 새
    `{"type": "spec", "items": [...]}` 로 쓰므로 기존 텍스트 확정(`/confirm`)이나 이전
    spec 확정이 있었다면 그걸 대체한다(마지막 확정이 이긴다 — `/confirm` 과 같은 규칙).

    다음 계획 생성부터 반영된다 — 이 파일 상단 독스트링 참고(`interview_adapter.
    _materials_note` 가 각 항목을 텍스트로 풀어 이어붙여 기존 `materials_for_prompt`
    경로를 그대로 탄다). 책이 있으면 **여기서** 세션당 권장 페이지(`book_pace`)를 계산해
    응답에 싣고, 같은 값을 슬롯에도 저장해 분해 프롬프트에도 실린다 — 클라이언트가 보낸
    값이 아니라 서버가 지금 막 계산한 값이다(`materials_spec._spec_item_dict` 참고). 목차
    전 챕터에 페이지가 있으면(best-effort) 균등 분할이 아니라 **챕터 경계를 존중한** 세션
    배정으로 계산된다(`materials_spec._chapter_session_plan`) — 세션이 챕터 중간에서 안
    끊긴다.
    """
    session_row = await _resolve_session(repo, user.id, body.interview_session_id)
    outcome = await project_session_outcome(session_row, repo)
    goal = _heaviest(outcome)

    book_detail_item = next((d for d in body.details if isinstance(d, BookSpecDetail)), None)
    book_pace = (
        materials_spec.compute_book_pace(
            page_count=book_detail_item.page_count,
            chapters=book_detail_item.chapters,
            outcome=outcome,
            target_date=now_kst().date(),
        )
        if book_detail_item is not None
        else None
    )

    await repo.upsert_slot_answer(
        session_row.id,
        _MATERIALS_SLOT,
        materials_spec.spec_slot_value(body.details, book_pace=book_pace),
        is_required=True,
    )
    await session.commit()

    return MaterialsSpecConfirmResponse(
        goal_title=goal.title,
        kinds=[d.kind for d in body.details],
        notice="자료를 반영했어요. 다음 계획 생성부터 이 목차를 뼈대로 삼아요.",
        book_pace=book_pace,
    )


@router.post("/curriculum-confirm")
async def confirm_curriculum(
    body: CurriculumConfirmRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> CurriculumConfirmResponse:
    """ "이 교재들로 할게요" — 교재 카드를 `goals.materials` 에 확정한다.

    `spec-confirm` 과 같은 슬롯·같은 규칙(마지막 확정이 이긴다)을 쓰되, 항목을 클라이언트가
    보낸 값이 아니라 **서버의 카드**로 만든다 — 단원 목록이 계획의 뼈대가 되므로 신뢰할 수
    없는 입력으로 받지 않는다. 항목에 `catalog_id` 가 실려, 다음 마일스톤·계획 생성이 LLM
    대신 이 책들의 단원 순서로 짜인다(`orchestrator/curriculum_plan.py`).

    카드에 없는 id 는 422 — 모르는 책을 조용히 빼면 사용자는 고른 책이 계획에 없는 이유를 모른다.
    """
    session_row = await _resolve_session(repo, user.id, body.interview_session_id)
    outcome = await project_session_outcome(session_row, repo)
    goal = _heaviest(outcome)

    ids = list(dict.fromkeys(body.book_ids))
    books = [cur.book(i) for i in ids]
    if any(b is None for b in books):
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "고른 교재를 찾지 못했어요. 목록을 다시 불러와 주세요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="bookIds",
        )

    await repo.upsert_slot_answer(
        session_row.id,
        _MATERIALS_SLOT,
        sv.dump(
            sv.SpecAnswer(tuple(curriculum_plan.card_spec_item(b) for b in books if b is not None))
        ),
        is_required=True,
    )
    await session.commit()

    return CurriculumConfirmResponse(
        goal_title=goal.title,
        book_ids=ids,
        notice="교재를 반영했어요. 다음 계획부터 이 책들의 단원 순서대로 세션을 짜요.",
    )
