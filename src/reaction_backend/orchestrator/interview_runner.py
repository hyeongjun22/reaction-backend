"""Interview turn driver — FastAPI 라우터 ↔ Interview FSM 브리지 (ADR-0005 §7.3).

딥 인터뷰는 사용자 답이 매 HTTP 요청으로 외부에서 들어오는 **턴 단위** 흐름이라,
그래프 전체를 한 번에 `ainvoke` 하지 않는다(그러면 답을 기다릴 수 없다). 대신 라우터는
이 모듈의 함수를 호출하고, 각 함수는 `interview.py` 의 노드(일반 async 함수)를 직접 엮어
"질문 1개 응답" 또는 "요약 + InterviewOutcome" 을 돌려준다.

상태(`InterviewState`)는 직렬화 가능하므로 라우터가 요청 사이에 보관한다
(권장: `interview_sessions` 스칼라 + `interview_slot_answers` 행으로 영속, 매 요청 복원).
세션(AsyncSession)은 state 가 아니라 `config["configurable"]["session"]` 로만 전달한다.

Envelope-less: 반환은 도메인 객체(`NextQuestionSchema` / `InterviewOutcome`) 그대로.
8s timeout / rate limit 은 각 노드의 룰 fallback 이 흡수하므로 이 레이어는 실패하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.domain import slot_value as sv
from reaction_backend.orchestrator import interview
from reaction_backend.orchestrator.interview import InterviewState
from reaction_backend.orchestrator.interview_catalog import (
    CATALOGS,
    InterviewSlot,
    canonical_chip_values,
)
from reaction_backend.schemas.interview import (
    InterviewOutcome,
    InterviewSummary,
    NextQuestionSchema,
)
from reaction_backend.schemas.ultimate_goal import UltimateGoalOutcome

__all__ = [
    "TurnResult",
    "UnsupportedAnswerError",
    "coerce_client_answer",
    "finish_early",
    "resume",
    "start_interview",
    "submit_and_advance",
]


@dataclass(slots=True)
class TurnResult:
    """한 턴의 결과. `done=False` 면 `question` 으로 계속, `done=True` 면 `outcome` 확정.

    `state` 는 라우터가 다음 턴까지 보관(영속)할 직렬화 가능한 인터뷰 상태다.
    """

    state: InterviewState
    done: bool
    question: NextQuestionSchema | None = None
    summary: InterviewSummary | None = None  # 요약 확인 카드 (done=True 일 때)
    outcome: InterviewOutcome | None = None  # 경계 계약 (done=True, kind="plan" 일 때)
    ultimate_outcome: UltimateGoalOutcome | None = None  # 경계 계약 (kind="ultimate" 일 때)
    end_reason: str | None = None
    harvested: list[str] = field(default_factory=list)  # 이번 턴에 미리 채운 슬롯키들


def _config(
    session: AsyncSession | None,
    tone_mode: str | None = None,
    *,
    answer_type: str | None = None,
    options: list[str] | None = None,
    slot_meta: dict[str, dict[str, Any]] | None = None,
) -> RunnableConfig:
    """노드가 예산 가드·llm_runs 기록에 쓰는 세션 채널 (ADR-0005 §7.1) + 톤(#23-D).

    answer_type/options 는 직전 답 슬롯 메타(라우터가 카탈로그에서 주입) — validate_answer
    가 자유서술을 슬롯 형식대로 구조화(normalized_value)하는 데 쓴다.
    slot_meta 는 슬롯키→{label,answer_type,options} 전체 맵 — ask_question 이 이번에 물을
    슬롯의 라벨·형식·보기를 질문 프롬프트에 실어 정확한 질문을 만드는 데 쓴다.
    """
    return {
        "configurable": {
            "session": session,
            "tone_mode": tone_mode,
            "answer_type": answer_type,
            "options": options or [],
            "slot_meta": slot_meta or {},
        }
    }


class UnsupportedAnswerError(ValueError):
    """클라이언트가 보낸 답이 사용자 답 모양(칩·텍스트·구간)이 아니다 — 라우트가 422 로 바꾼다."""


def coerce_client_answer(value: Any, *, slot: InterviewSlot | None = None) -> dict[str, Any]:
    """라우터가 받은 JsonValue 를 slot_answers value 형식으로 환원.

    클라이언트가 보낼 수 있는 건 **사용자 답**뿐이다 — 칩·텍스트·구간 dict, 그리고 그 약식(목록 =
    칩, `{start, end}` = 구간, 나머지 스칼라 = 텍스트). 대기(pending)·스킵(skip)·자료 확정(spec)은
    서버만 쓰는 표시라 받지 않는다(`UnsupportedAnswerError`). 예전엔 `{"type": ...}` 가 붙은
    dict 를 그대로 믿어, 클라이언트가 `pending` 으로 재질문 횟수를 되돌리거나 `skip` 으로 핵심
    슬롯을 닫을 수 있었다.

    칩은 **표기만** 카탈로그 옵션으로 정규화한다(`drop_unknown=False`) — `"2시간이상"` 처럼
    공백이 빠졌거나 `"120분"` 처럼 다른 표기로 온 값을 옵션 표기로 맞춘다. 여기서는 **거부하지
    않는다**: 사용자가 방금 누른 답을 버리면 "칩을 눌렀는데 서버가 미응답으로 보고 같은 질문을
    또 하는" 루프가 된다. 카탈로그 옵션은 시간이 지나며 바뀌므로(주당 시간 척도가 한 번
    개편됐다) 옛 척도를 든 클라이언트를 그 루프에 빠뜨릴 수 없다.
    신뢰할 수 없는 출처(harvest LLM · 프로필 시드)는 반대로 **버리는** 정책을 쓴다.
    """
    if isinstance(value, list):
        return sv.dump(sv.ChipAnswer(tuple(canonical_chip_values(slot, value, drop_unknown=False))))
    if not isinstance(value, dict):
        return sv.dump(sv.TextAnswer(str(value)))
    kind = value.get("type")
    if kind is None and "start" in value and "end" in value:
        kind = "range"
    if kind == "chip" and isinstance(value.get("values"), list):
        chips = canonical_chip_values(slot, value["values"], drop_unknown=False)
        return sv.dump(sv.ChipAnswer(tuple(chips)))
    if (
        kind == "range"
        and isinstance(value.get("start"), str)
        and isinstance(value.get("end"), str)
    ):
        return sv.dump(sv.RangeAnswer(value["start"], value["end"]))
    if kind == "text" and isinstance(value.get("raw"), str):
        normalized = value.get("normalized")
        text_items = tuple(str(v) for v in normalized) if isinstance(normalized, list) else None
        return sv.dump(sv.TextAnswer(value["raw"], text_items))
    raise UnsupportedAnswerError(f"unsupported answer shape: type={kind!r}")


async def start_interview(
    *,
    session_id: UUID,
    user_id: UUID,
    kind: str = "plan",
    session: AsyncSession | None = None,
    tone_mode: str | None = None,
    slot_meta: dict[str, dict[str, Any]] | None = None,
    seed_answers: dict[str, dict[str, Any]] | None = None,
) -> TurnResult:
    """세션 시작 → FSM 이 고른 첫 필수 슬롯 질문 1개를 만들어 반환.

    `kind` 기본값이 `"plan"` 이라 기존 호출부는 변경 없이 그대로 안전하다.
    seed_answers 가 있으면(재인터뷰 시 지난 인터뷰의 지속형 슬롯) 그 슬롯들은 이미 채워진
    것으로 두어 FSM 이 건너뛰고 첫 '미충족' 슬롯(보통 목표)부터 묻는다(#reduce-reask).
    """
    config = _config(session, tone_mode, slot_meta=slot_meta)
    state = interview.initial_state(session_id=session_id, user_id=user_id, kind=kind)
    if seed_answers:
        state["slot_answers"] = dict(seed_answers)
    # 시드만으로 필수 슬롯이 다 찼으면 물을 게 없다 — 빈 질문으로 시작하면 FE 는 답할 칸도
    # 끝낼 신호도 없는 화면에 갇힌다(두 번째 궁극목표 인터뷰가 그랬다). 곧바로 마감한다.
    if interview.should_continue(state) == "finish":
        return await _finalize(state, config)
    state = await interview.ask_question(state, config)
    return TurnResult(state=state, done=False, question=state["next_question"])


async def submit_and_advance(
    *,
    state: InterviewState,
    slot_key: str,
    answer_value: Any,
    session: AsyncSession | None = None,
    tone_mode: str | None = None,
    answer_type: str | None = None,
    options: list[str] | None = None,
    slot_meta: dict[str, dict[str, Any]] | None = None,
) -> TurnResult:
    """답 1개 주입 → 채점/정규화/저장 → 종료면 요약+outcome, 아니면 다음 질문.

    이게 `POST /interview/sessions/{id}/answers` 가 호출하는 핵심 진입점이다.
    answer_type/options 는 답한 슬롯 메타(정규화용), slot_meta 는 다음 질문 슬롯 메타용.
    """
    config = _config(
        session, tone_mode, answer_type=answer_type, options=options, slot_meta=slot_meta
    )
    coerced = coerce_client_answer(answer_value, slot=CATALOGS[state["kind"]].by_key.get(slot_key))
    state = {**state, "last_answer": coerced, "last_slot_key": slot_key}

    state = await interview.receive_answer(state, config)
    state = await interview.validate_answer(state, config)

    # 같은 답에 섞여 들어온 다른 미충족 슬롯은 **`validate_answer` 가 같은 호출에서** 뽑는다.
    # 예전엔 여기서 `harvest_slots` 를 따로 불러 자유서술 턴이 LLM 3콜이 됐다 — 수확 여부는
    # LLM 을 부르기 전에 이미 정해지므로(자유서술인가 · 20자 이상인가 · 열린 슬롯이 있는가)
    # 프롬프트를 골라 한 번에 처리할 수 있다. 실측 −753 요청 / 토큰 −5%.
    harvested = list(state.get("harvested", []))

    if interview.should_continue(state) == "finish":
        return await _finalize(state, config, harvested=harvested)

    state = await interview.ask_question(state, config)
    return TurnResult(state=state, done=False, question=state["next_question"], harvested=harvested)


async def resume(
    *,
    state: InterviewState,
    session: AsyncSession | None = None,
    tone_mode: str | None = None,
    slot_meta: dict[str, dict[str, Any]] | None = None,
) -> TurnResult:
    """중단된 세션 재개(`next-question`) — 지금 미해결 슬롯의 질문을 다시 만든다.

    필수 슬롯이 **이미** 다 찼으면 질문 대신 마감한다. 답 제출(`submit_and_advance`) 밖에서
    슬롯이 채워지는 경로가 있다 — 자료 확정(`POST /plans/materials/spec-confirm`)이
    `goals.materials` 를 직접 쓴다. 재인터뷰에서 그게 마지막 빈 슬롯이면, 재개가 물을 슬롯
    없이 빈 질문만 돌려줘 인터뷰가 영영 끝나지 않았다.

    시작·답 제출과 **같은 config**(톤 포함)로 노드를 부른다 — 라우트가 노드를 직접 부르던
    시절 재개 질문만 tone_mode 없이 만들어졌다.
    """
    config = _config(session, tone_mode, slot_meta=slot_meta)
    if interview.should_continue(state) == "finish":
        return await _finalize(state, config)
    state = await interview.ask_question(state, config)
    return TurnResult(state=state, done=False, question=state["next_question"])


async def finish_early(
    *,
    state: InterviewState,
    session: AsyncSession | None = None,
    tone_mode: str | None = None,
) -> TurnResult:
    """[충분해요] — 남은 슬롯이 있어도 즉시 마감(end_reason=early_user).

    빈 필수 슬롯은 `interview_adapter` 가 안전 default 로 채우고 unresolved_slots 에 남긴다.
    """
    config = _config(session, tone_mode)
    state = {**state, "early_finish": True}
    return await _finalize(state, config)


async def _finalize(
    state: InterviewState, config: RunnableConfig, *, harvested: list[str] | None = None
) -> TurnResult:
    state = await interview.summarize_interview(state, config)
    state = await interview.finalize_outcome(state, config)
    return TurnResult(
        state=state,
        done=True,
        summary=state["summary"],
        outcome=state["outcome"],
        ultimate_outcome=state["ultimate_outcome"],
        end_reason=state["end_reason"],
        harvested=harvested or [],
    )
