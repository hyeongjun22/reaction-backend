"""Deep Interview Orchestrator (#6) — Rule-based Slot FSM + LLM Nodes.

흐름 (베이스라인 §6 "필수 슬롯 채우기 → 모호함 0 까지 cycle"):

    ask_question → receive_answer → validate_answer ─┐
         ▲                                           │ should_continue
         └──────────────── continue ─────────────────┤
                            finish → summarize_interview → finalize_outcome → END

설계 원칙 (요청 규칙 엄수):
- **Rule-based Slot FSM**: 다음에 물을 슬롯 선택·종료 판단은 LLM 0회의 순수 규칙
  (`_next_required_slot` / `_terminal_reason`). 룰이 흐름을 운전하고 LLM 은 문장 생성·
  채점에만 쓴다 — 8s timeout/rate limit 이 와도 인터뷰가 끊기지 않는다.
- **모든 LLM 호출은 `aiClient.run(...)` 단일 게이트만** (AGENTS.md §2). Gemini SDK 직접
  import 금지. 각 노드는 timeout=8.0 + 같은 schema 로 환원하는 룰 `fallback=` 을 넘긴다.
- **Envelope-less**: 터미널은 껍데기 없이 도메인 객체 `InterviewOutcome` 를 빌드(LLM 0회).
  요약 확인 카드(`InterviewSummary`)는 표현 계층으로 state 에만 싣는다.
- State 는 직렬화 가능해야 한다 → `AsyncSession` 은 넣지 않고
  `config["configurable"]["session"]` 채널로 전달 (ADR-0005 §7.1).

종료 조건 (FSM 완료):
  필수 슬롯 전부 충족(= 명료성 100%) / early_finish.

  ⚠️ float `ambiguity_score` 는 **종료를 운전하지 않는다**. FE 명료성 지표(= 남은 필수
  슬롯 수, API 의 `ambiguityScore`(int))와 진실 소스가 달라, float 임계로 조기 종료하면
  필수 슬롯이 다 차기 전에 끝나 명료성이 100%에 못 닿는다. 완료는 슬롯 충족(FSM)이 단독으로
  운전하고, float 값은 telemetry(`ambiguity_final`)로만 남긴다.

  루프 방지는 **슬롯별 시도 상한**(`_decide_storage`/`MAX_SLOT_ATTEMPTS`, pending 마커로
  영속)이 담당한다 — 상한에 닿으면 그 슬롯을 스킵/best-effort 로 채워 진행시켜, 같은 질문이
  무한 반복되지 않고 모든 슬롯이 결국 채워져 완료로 수렴한다(별도 turn_limit 불필요).

라우터는 보통 그래프를 한 번에 `ainvoke` 하지 않고 `interview_runner` 로 턴 단위 구동한다
(사용자 답이 HTTP 요청으로 외부에서 들어오기 때문 — `receive_answer` 가 no-op 인 이유).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Literal, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from reaction_backend.agents import ultimate_summary_agent
from reaction_backend.domain import slot_value as sv
from reaction_backend.llm import aiClient
from reaction_backend.orchestrator import datetime_parse, interview_adapter, ultimate_adapter
from reaction_backend.orchestrator.interview_catalog import (
    CATALOGS,
    GLOBAL_SCOPE_HINT,
    PLAN_CATALOG,
    InterviewSlot,
    canonical_chip_values,
    is_duration_slot,
    is_goal_scoped,
)
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.interview import (
    AmbiguityUpdate,
    AnswerIntake,
    HarvestedSlot,
    InterviewEndReason,
    InterviewOutcome,
    InterviewSummary,
    NextQuestionSchema,
)
from reaction_backend.schemas.ultimate_goal import UltimateGoalOutcome

__all__ = [
    "InterviewState",
    "ask_question",
    "build_interview_graph",
    "finalize_outcome",
    "initial_state",
    "receive_answer",
    "should_continue",
    "summarize_interview",
    "validate_answer",
]

STORE_CLARITY_MIN = 0.4  # clarity 가 이 미만이면 답을 채우지 않고 같은 슬롯 재질문


def _skip(reason: sv.SkipReason) -> dict[str, Any]:
    """스킵 — FSM 은 '닫힘' 으로 보아 다음 슬롯으로 진행하고(무한 재질문 방지, #79), outcome 은
    그 슬롯을 `unresolved_slots` 에 남긴다(사용자의 답이 아니다, #499). 사유는 기록용이다."""
    return sv.dump(sv.Skipped(reason))


_log = logging.getLogger(__name__)

# 한 슬롯에 허용하는 최대 시도 횟수(최초 1 + 재질문 2). 이후엔 어쩔 수 없이 진행:
# 핵심 슬롯은 마지막 비지 않은 답을 best-effort 로 채택, 비핵심은 스킵(default).
MAX_SLOT_ATTEMPTS = 3

# 하베스팅(slot_extraction) — 이 신뢰도 미만은 미리 채우지 않고 정식 질문으로 넘긴다.
# 잘못 채우면 사용자가 정정 기회를 잃어 재질문보다 나쁘므로 보수적으로.
HARVEST_MIN_CONFIDENCE = 0.7

# 이 길이 미만의 답에는 하베스팅을 **시도하지 않는다**.
#
# 근거(실측, 자유서술 답 173개): 중앙 길이가 **13자**고 78.6%가 20자 미만이다. 하베스팅은
# "3학년 방학이고 캡스톤 8월 마감" 처럼 한 답에 여러 슬롯이 섞여 나오는 상황을 위한 건데,
# 실제로는 인터뷰가 한 번에 한 질문씩(추천 답변 카드까지 붙여) 묻기 때문에 사용자가 물어본
# 것에만 답한다. 긴 답조차 대개 **한 슬롯의 내용이 풍부한 것**이지 여러 슬롯이 섞인 게 아니다.
#
# 그 결과 273회 호출해 9회 수확(3.3%)했고, 나머지 96.7%는 회당 약 1,010 토큰과 1초를
# 쓰고 빈 배열을 돌려받았다. 20자 게이트는 그 호출의 78.6%를 없애면서 수확 가능성이 있는
# 구간은 그대로 둔다.
#
# 20자인 이유: 한국어에서 서로 다른 두 사실을 한 문장에 담으려면 대략 이 길이가 필요하다.
# 게이트를 통과한 호출만 남으면 **적중률을 제대로 측정할 수 있다** — 지금 3.3% 는 짧은 답이
# 분모를 채운 값이라 기능의 실력인지 표본의 문제인지 구분되지 않는다.
HARVEST_MIN_ANSWER_CHARS = 20


# 재질문 사유 — pending 마커에 실어 다음 턴의 `_retry_hint` 가 **왜** 다시 묻는지 알게 한다.
# 사유가 없으면 '모호해서' 로 읽히는데, 지난 마감은 모호한 게 아니라 **또렷하게 지나 있는** 것이라
# 되묻는 문장이 달라야 한다(#231).
_RETRY_PAST_DEADLINE = "past_deadline"
# 보기가 정해진 칩 슬롯인데 답을 보기로 맞출 수 없었다 — 모호한 게 아니라 **보기 밖**이다.
# 되묻는 문장이 달라야 한다: 사용자는 또렷하게 답했으므로 "조금 모호했다" 고 하면 안 된다.
_RETRY_OFF_CATALOG = "off_catalog"


def _pending(attempts: int, reason: str | None = None) -> dict[str, Any]:
    """재질문 대기 마커 — 시도 횟수를 slot_answers 에 실어 턴 사이에 영속(스키마 변경 없이)."""
    return sv.dump(sv.Pending(attempts, reason or None))


def _pending_attempts(value: dict[str, Any] | None) -> int:
    """슬롯에 누적된 시도 횟수 (pending 마커면 그 값, 아니면 0)."""
    parsed = sv.parse(value)
    return parsed.attempts if isinstance(parsed, sv.Pending) else 0


def _pending_reason(value: dict[str, Any] | None) -> str | None:
    """pending 마커에 실린 재질문 사유 (없으면 None)."""
    parsed = sv.parse(value)
    return parsed.reason if isinstance(parsed, sv.Pending) else None


def _retry_hint(
    slot_key: str,
    attempts: int,
    reason: str | None = None,
    *,
    critical_slots: frozenset[str] = PLAN_CATALOG.critical_slots,
) -> str:
    """재질문 힌트 — 같은 질문 반복이 아니라 직전 답이 왜 부족했는지 짚고 더 구체적으로 묻게 한다.

    `critical_slots` 는 카탈로그(kind) 별로 다르다 — 기본값은 plan 카탈로그(하위호환).
    """
    if attempts <= 0:
        return ""
    if reason == _RETRY_PAST_DEADLINE:
        return (
            "재질문: 사용자가 고른 마감일이 **이미 지난 날짜**다. 모호해서가 아니라 날짜가 지나서 "
            "다시 묻는 것이니, 지났다는 사실을 담백하게 짚고 — 늦은 것을 지적하거나 다그치지 말고 — "
            "'이미 지난 마감을 수습하는 중이라면 실제로 언제까지 끝내고 싶은지' 를 물어라."
        )
    if reason == _RETRY_OFF_CATALOG:
        return (
            "재질문: 직전 답이 **보기 중에 없는 값**이다. 모호해서가 아니라 보기 밖이라 다시 묻는 "
            "것이니, 답이 이상하다고 하거나 사용자를 탓하지 말고 — 직전 답을 그대로 짧게 되짚은 뒤 "
            "보기를 전부 보여주고 그중 가장 가까운 걸 골라 달라고 부드럽게 물어라. "
            "**임의로 가까운 보기를 대신 고르지 마라.**"
        )
    if slot_key in critical_slots:
        return (
            "재질문: 직전 답으로는 이 항목을 정하기 어려웠다. 이건 계획의 핵심이라 건너뛸 수 없으니, "
            "직전 답을 짧게 되짚고 보기·예시를 들어 고르기 쉽게 다시 물어라."
        )
    return "재질문: 직전 답이 조금 모호했다. 같은 말 반복 말고 예시·보기를 들어 답하기 쉽게 물어라."


class InterviewState(TypedDict):
    """LangGraph 가 Node 간 전달하는 상태. DB(`interview_sessions`)와 별도 short-lived.

    직렬화 가능해야 하므로 비직렬화 객체(AsyncSession 등)는 넣지 않는다(ADR-0005 §7.1).
    """

    # 식별/진행
    session_id: UUID
    user_id: UUID
    kind: str  # "plan" | "ultimate" — CATALOGS[kind] 조회 키 (interview_catalog.py)
    ambiguity_score: float  # 0..1, 낮을수록 명확 (DB ambiguity_final 과 동일 척도)
    total_turns: int
    early_finish: bool  # [충분해요] 탭
    end_reason: InterviewEndReason | None

    # 턴 단위
    next_slot_key: str | None  # FSM 이 이번 턴에 물은 필수 슬롯
    last_slot_key: str | None  # 직전 답이 속한 슬롯 (라우터가 주입)
    last_answer: dict[str, Any] | None  # interview_slot_answers.value 형태
    next_question: NextQuestionSchema | None
    used_fallback: bool  # 어느 턴이든 룰 정규화면 True → outcome.analysis_source

    # 누적 슬롯 (DB slot_answers 의 in-memory 미러) {slot_key: value}
    slot_answers: dict[str, dict[str, Any] | None]

    # 이번 턴에 하베스팅으로 미리 채운 슬롯키들 (transient — 응답 표시용, 영속 대상 아님)
    harvested: list[str]

    # 터미널 산출물
    summary: InterviewSummary | None  # 요약 확인 카드 (표현 계층)
    outcome: InterviewOutcome | None  # 경계 계약 (First Plan 시드) — kind="plan" 전용
    ultimate_outcome: UltimateGoalOutcome | None  # 경계 계약 — kind="ultimate" 전용


def initial_state(*, session_id: UUID, user_id: UUID, kind: str = "plan") -> InterviewState:
    """라우터/테스트에서 그래프 진입 시 쓰는 초기 상태.

    `kind` 기본값이 `"plan"` 이라 기존 호출부(라우터·`interview_runner`·테스트) 전부
    변경 없이 그대로 안전하다.
    """
    return InterviewState(
        session_id=session_id,
        user_id=user_id,
        kind=kind,
        ambiguity_score=1.0,
        total_turns=0,
        early_finish=False,
        end_reason=None,
        next_slot_key=None,
        last_slot_key=None,
        last_answer=None,
        next_question=None,
        used_fallback=False,
        slot_answers={},
        harvested=[],
        summary=None,
        outcome=None,
        ultimate_outcome=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rule-based FSM helpers — 순수 함수 (LLM 호출 X). 흐름은 룰이 운전한다.
# ─────────────────────────────────────────────────────────────────────────────


def _next_required_slot(state: InterviewState) -> str | None:
    """아직 안 채운 첫 필수 슬롯 키. 모두 채웠으면 None (FSM 완료 신호).

    필수 슬롯 시퀀스는 `state["kind"]` 로 카탈로그를 조회해 얻는다(plan/ultimate 가 다름).
    다른 답에서 **유도되는** 슬롯은 건너뛴다 — 주당 시간은 세션 길이 × 빈도로 계산되므로
    둘을 답했으면 묻지 않는다(ultimate 슬롯 9개는 서로 독립이라 전부 True).

    판정은 직접 조립하지 않고 `interview_adapter.open_required_keys` 하나만 쓴다 —
    `unresolved_slots`(build_outcome)·FE 명료성 지표(`_remaining_required`)와 **같은
    함수**여야 한다. 손으로 각자 조립하던 시절 FE 지표만 유도 규칙을 빠뜨려, 유도로
    안 물은 슬롯이 영영 미해결로 남아 진행바가 100%에 못 닿았다.
    """
    answers = state["slot_answers"]
    required = CATALOGS[state["kind"]].required_keys
    return next(iter(interview_adapter.open_required_keys(required, answers)), None)


def _all_required_filled(state: InterviewState) -> bool:
    return _next_required_slot(state) is None


# ─────────────────────────────────────────────────────────────────────────────
# 룰 fallback (8s timeout / rate limit / schema 실패 시) — 같은 schema 로 환원.
# ─────────────────────────────────────────────────────────────────────────────


def _fill_goal(text: str, state: InterviewState) -> str:
    """기본 질문의 `{goal}` 자리에 대상 목표 이름을 넣는다 (#187).

    목표별 슬롯 질문이 "이 목표는 한 번에 어느 정도…" 처럼 **지시어로만** 물어서, 목표를
    여러 개 말한 사용자는 자기가 무엇에 답하는지 알 수 없었다(실측: 목표 3개 투입 시
    계획은 heaviest 하나만 다루는데 질문은 끝까지 '이 목표'). 프롬프트 규칙이 1차지만
    LLM 이 죽으면 이 룰 폴백이 그대로 사용자에게 나가므로 여기서도 이름을 넣는다.

    `str.replace` 를 쓰는 이유: `str.format` 은 질문에 다른 중괄호가 섞이면 터진다.
    """
    return text.replace("{goal}", _heaviest_goal_hint(state)) if "{goal}" in text else text


# LLM 이 실제 이름을 모를 때 채워 넣는 자리표시자 — "수험서 (ㅇㅇ 출판사)", "유튜브 ㅁㅁㅁ
# 채널", "○○ 강의", "OO대학교". 추천 답변 카드는 **탭 한 번이 곧 사용자의 답**이라, 이런
# 카드를 누르면 지어낸 틀이 그대로 슬롯에 저장되고 계획 프롬프트까지 흘러간다(2026-09-18
# 배포 미러에서 goals.materials 카드 4장 중 2장이 이 모양이었다).
_PLACEHOLDER_CARD = re.compile(r"[ㄱ-ㅎ]{2,}|[○◯△□×]{2,}|(?<![A-Za-z])(?:OO|XX|xx)(?![A-Za-z])")


def drop_placeholder_cards(cards: Sequence[str]) -> list[str]:
    """자리표시자가 섞인 추천 답변 카드를 버린다 — 고칠 수 없으니 통째로 뺀다.

    카드가 줄어드는 건 괜찮다(프롬프트도 "확신이 없으면 빈 배열"을 허용한다). 사용자는
    언제든 직접 입력할 수 있고, 지어낸 틀을 답으로 고르게 두는 것보다 낫다.
    """
    return [c for c in cards if not _PLACEHOLDER_CARD.search(c)]


def _rule_next_question(state: InterviewState, slot_key: str) -> NextQuestionSchema:
    """카탈로그 기본 질문으로 회귀 — LLM 죽어도 인터뷰가 끊기지 않는다."""
    catalog = CATALOGS[state["kind"]]
    return NextQuestionSchema(
        question=_fill_goal(
            catalog.default_questions.get(slot_key, "조금만 더 구체적으로 알려주실 수 있을까요?"),
            state,
        ),
        empathy_one_liner="천천히 알려주셔도 괜찮아요.",
    )


def _rule_ambiguity_update(state: InterviewState, slot_key: str) -> AmbiguityUpdate:
    """답이 있으면 모호함을 소폭 감소시키는 단순 휴리스틱."""
    answered = _has_answer_text(state)
    new_score = max(0.0, state["ambiguity_score"] - (0.15 if answered else 0.0))
    return AmbiguityUpdate(
        slot_key=slot_key,
        clarity_score=0.5 if answered else 0.0,
        new_ambiguity=new_score,
    )


def _rule_summary(state: InterviewState) -> InterviewSummary:
    """슬롯에서 결정적으로 빌드한 룰 요약 — LLM 실패 시 그대로 노출.

    LLM 요약과 같은 슬롯 소스를 쓰되, 값이 있는 항목(마감·성공 이미지·휴식·다운스코프)만
    골라 문장에 덧붙인다 — "아직 정하지 않음" 은 지어내지 않으려 생략.
    """
    v = _summary_variables(state)
    goals = v["goals"]

    goal_summary = f"가장 무겁게 느끼는 일은 '{v['heaviest']}' 이고, 정리한 목표는 {goals} 예요."
    if v["deadlines"] != _NOT_SET:
        goal_summary += f" 마감은 {v['deadlines']} 예요."
    if v["success_image"] != _NOT_SET:
        goal_summary += f" 다 이뤘을 때 '{v['success_image']}' 모습을 그리셨어요."

    time_summary = (
        f"활동 시간대는 {v['time_window']}, 집중은 {v['peak_window']} 가 좋다고 하셨어요."
    )
    # 계산된 주당 총량을 **되돌려 보여준다**(C안). 주당 시간을 직접 묻지 않게 된 대신,
    # 확인 카드에서 곱셈 결과를 확인하고 조정할 수 있어야 한다 — 그러지 않으면 사용자는
    # 자기가 주 14시간을 약속했다는 걸 계획이 나온 뒤에야 안다. LLM 요약이 죽어도 보이도록
    # 룰 폴백에도 싣는다(프롬프트의 {{weekly_load}} 와 같은 값).
    if v["weekly_load"] != _NOT_SET:
        time_summary += f" 이 목표에는 {v['weekly_load']} 정도 쓰게 돼요."

    # 톤을 답하지 않았으면(조기 종료) 선호라고 말하지 않는다 — 예전엔 기본값 '담백' 을
    # "담백한 회복 톤을 선호하시는군요" 처럼 사용자의 선호로 적었다.
    prefs: list[str] = []
    if v["tone"] != _NOT_SET:
        prefs.append(f"못 한 날엔 '{v['tone']}' 톤을 선호하세요.")
    if v["rest_ok"] != _NOT_SET:
        prefs.append(f"휴식 제안은 '{v['rest_ok']}'.")
    if v["downscope_unit"] != _NOT_SET:
        prefs.append(f"밀리면 {v['downscope_unit']} 단위로 줄여볼게요.")
    preference_summary = " ".join(prefs) or "회복 방식은 계획을 써 보면서 함께 맞춰 갈게요."

    return InterviewSummary(
        headline=f"{v['identity']} · 핵심 목표 {goals}",
        goal_summary=goal_summary,
        time_summary=time_summary,
        preference_summary=preference_summary,
        confirm_question="이대로 계획을 세워볼까요?",
    )


def _session(config: RunnableConfig) -> Any:
    """config["configurable"]["session"] 안전 추출 (없으면 None → 예산/로깅 skip)."""
    return config.get("configurable", {}).get("session")


def _tone_mode(config: RunnableConfig) -> str | None:
    """config["configurable"]["tone_mode"] 안전 추출 (#23-D). 없으면 None = 톤 prefix 없음."""
    raw = config.get("configurable", {}).get("tone_mode")
    return raw if isinstance(raw, str) else None


def _answer_type(config: RunnableConfig) -> str | None:
    """직전 답 슬롯의 answer_type (라우터가 카탈로그에서 주입). 정규화 추출 지시에 사용."""
    raw = config.get("configurable", {}).get("answer_type")
    return raw if isinstance(raw, str) else None


def _answer_options(config: RunnableConfig) -> list[str]:
    """직전 답 슬롯의 chip/select 보기 (라우터 주입). LLM 이 자유서술을 보기로 매핑하게 한다."""
    raw = config.get("configurable", {}).get("options")
    return [str(x) for x in raw] if isinstance(raw, list) else []


def _slot_meta(config: RunnableConfig) -> dict[str, dict[str, Any]]:
    """슬롯키→{label, answer_type, options} 맵 (라우터가 카탈로그에서 주입).

    ask_question 이 이번에 물을 슬롯의 사람용 라벨·형식·보기를 프롬프트에 실어, LLM 이
    슬롯 의도에 정확히 맞는 질문을 만들게 한다(없으면 키 문자열만 보고 추측하던 문제).
    """
    raw = config.get("configurable", {}).get("slot_meta")
    return raw if isinstance(raw, dict) else {}


# ─────────────────────────────────────────────────────────────────────────────
# Nodes — async def node(state, config). config 두 번째 인자 (ADR-0005 §7.1).
# ─────────────────────────────────────────────────────────────────────────────


async def ask_question(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """LLM ① — FSM 이 고른 다음 필수 슬롯에 대한 질문 1개 생성.

    슬롯 선택은 룰(`_next_required_slot`), 문장만 LLM. timeout 시 카탈로그 기본 질문.
    `prompt_id`/기본 질문은 `state["kind"]` 로 카탈로그를 조회해 정한다
    (`interview/next_question` | `interview/ultimate_next_question`).
    """
    catalog = CATALOGS[state["kind"]]
    slot_key = _next_required_slot(state) or ""
    meta = _slot_meta(config).get(slot_key) or {}
    meta_options = meta.get("options") or []
    pending = state["slot_answers"].get(slot_key)
    attempts = _pending_attempts(pending)  # 이 슬롯 재질문 횟수
    retry_reason = _pending_reason(pending)  # 왜 다시 묻는지 (#231 지난 마감 등)
    # 슬롯 의도(라벨)·형식·보기를 실어 LLM 이 정확한 질문을 만들게 한다. 카탈로그 라벨이
    # 없을 때만 기본 질문으로 대체 — 그 문자열에는 `{goal}` 자리가 있을 수 있으므로
    # 채워서 넘긴다(프롬프트에 자리표시자가 새지 않게. ultimate 기본 질문엔 `{goal}` 이
    # 없으므로 `_fill_goal` 은 그대로 통과시킨다).
    slot_label = _fill_goal(
        str(meta.get("label") or catalog.default_questions.get(slot_key, slot_key)), state
    )
    answer_type = str(meta.get("answer_type") or "text")
    options_text = ", ".join(str(o) for o in meta_options) or "(자유 입력)"
    retry = _retry_hint(slot_key, attempts, retry_reason, critical_slots=catalog.critical_slots)
    if state["kind"] == "ultimate":
        # goal_title(heaviest 목표 개념)이 없다 — 대신 궁극 목표 선언 자체를 그라운딩으로
        # 싣는다(P1). plan 프롬프트와 변수 집합이 달라도 되는 이유는 서로 다른 파일이라서다
        # (tests/prompts/test_interview_prompts.py 가 파일별 {{var}} 집합을 강제).
        variables = {
            "ambiguous_slot": slot_key,
            "slot_label": slot_label,
            "answer_type": answer_type,
            "options": options_text,
            "answered_context": _answered_context(state),
            "last_answer": _last_answer_text(state),
            "retry": retry,
            "statement": sv.display_text(state["slot_answers"].get("ultimate.statement")),
        }
    else:
        # ⚠️ **목표별 슬롯이 아니면 목표 이름을 아예 넘기지 않는다.**
        # 예전엔 슬롯 종류와 무관하게 늘 넘기고 프롬프트가 "goals. 로 시작하지 않으면 절대
        # 넣지 마라" 를 산문으로 가르쳤다 — 그 규칙 자체는 실측 회귀의 가드라 살아 있지만
        # (#187 과교정: 실 LLM 3회에 8건), 규칙은 **어길 수 있고 어겨도 조용하다.**
        # 이름을 안 주면 어길 이름이 없다. 룰 폴백은 이미 `default_questions` 의 `{goal}`
        # 자리로 같은 분기를 하고 있었다 — LLM 경로만 안 하고 있던 것이다.
        variables = {
            "goal_title": (
                _heaviest_goal_hint(state) if is_goal_scoped(slot_key) else GLOBAL_SCOPE_HINT
            ),
            "answered_context": _answered_context(state),
            "ambiguous_slot": slot_key,
            "slot_label": slot_label,
            "answer_type": answer_type,
            "options": options_text,
            "last_answer": _last_answer_text(state),
            "retry": retry,
        }
    result = await aiClient.run(
        module="interview",
        schema=NextQuestionSchema,
        prompt_id=catalog.prompt_next_question,
        fallback=lambda: _rule_next_question(state, slot_key),
        timeout=8.0,
        variables=variables,
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
    )
    return {
        **state,
        "next_question": result.value,
        "next_slot_key": slot_key,
        "total_turns": state["total_turns"] + 1,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


async def receive_answer(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """사용자 답 수신 노드 — 외부 트리거(POST .../answers)로 진입.

    실제 답 주입·DB UPSERT 는 라우터(`interview_runner.submit_and_advance`)가 한다.
    그래프 자체를 batch `ainvoke` 할 때는 답이 없으므로 no-op (state passthrough).
    """
    return state


async def validate_answer(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """LLM ② — 직전 답을 채점·정규화하고, **수확할 게 있으면 같은 호출에서** 함께 뽑는다.

    ## 왜 조건부인가 (실측)

    예전엔 채점(`ambiguity_score`)과 수확(`slot_extraction`)이 **별도 호출**이라 자유서술
    답 한 턴이 LLM 3콜이 됐다. 그런데 수확은 이미 두 게이트로 막혀 있어서 실제로는
    **전체 인터뷰 호출의 9.4%**(753/8031)만 발생했다.

    그래서 **무조건 합치면 손해다** — 수확하지 않는 turn 까지 수확 규칙(약 856토큰)을
    프롬프트에 짊어진다:

        현재            5,762,682 토큰 / 요청 4200
        무조건 합치기    7,807,455 토큰 (+35%) / 요청 3447
        조건부 합치기    5,501,391 토큰 (−5%)  / 요청 3447   ← 이 설계

    수확 여부는 `harvest_candidates` 가 **LLM 을 부르기 전에** 판정하므로(자유서술인가 ·
    20자 이상인가 · 열린 슬롯이 있는가) 프롬프트를 골라 쓸 수 있다.

    ---


    실제 저장 결정(무엇을 저장하고 채워졌다고 볼지)은 순수 함수 `_decide_storage` 가 맡는다
    (표로 단위 테스트 가능). 이 노드는 LLM 호출·상태 조립만 한다.

    ⚠️ chip 을 clarity 게이트에 태우면 안 되는 이유: 실 LLM 이 "1학년"·"담백" 같은 유효한
    단일 chip 선택을 0.3 정도로 낮게 채점해, 필수 chip 슬롯(13개 중 7개)이 영구 재질문에
    빠져 turn_limit 로 끝나고 명료성이 0% 에 갇힌다. 명확성 판단이 필요한 건 자유 서술뿐이다.
    """
    catalog = CATALOGS[state["kind"]]
    slot_key = state.get("last_slot_key") or state.get("next_slot_key") or ""
    answer_type = _answer_type(config)
    answer_text = _last_answer_text(state)

    # 수확 대상은 **호출 전에** 정해진다 — 있으면 합친 프롬프트, 없으면 채점 전용.
    last = state.get("last_answer") or {}
    open_slots = (
        harvest_candidates(state, config, answer_text=answer_text, answered_slot=slot_key)
        if catalog.prompt_intake and last.get("type") == "text"
        else []
    )
    intake_prompt = catalog.prompt_intake
    merged = bool(open_slots) and intake_prompt is not None
    variables = {
        "slot_key": slot_key,
        "answer": answer_text,
        "answer_type": answer_type or "text",
        "options": ", ".join(_answer_options(config)) or "(자유 입력)",
        "today": now_kst().date().isoformat(),
    }
    if merged:
        variables["open_slots"] = harvest_listing(open_slots, config, kind=state["kind"])

    result = await aiClient.run(
        module="interview",
        schema=AnswerIntake,
        prompt_id=(intake_prompt if merged and intake_prompt else catalog.prompt_ambiguity),
        fallback=lambda: AnswerIntake(
            **_rule_ambiguity_update(state, slot_key).model_dump(by_alias=True)
        ),
        timeout=8.0,
        variables=variables,
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
    )
    update = result.value

    # ── 날짜·시각은 **룰이 먼저다** (#432) ──────────────────────────────────
    # LLM 도 뽑지만 흔하고 확실한 표현에서까지 조용히 틀린다(연도 경계·자정). 파서가
    # 확실히 이기는 표현만 덮어쓰고, 못 뽑으면(`None`) LLM 값을 그대로 쓴다 —
    # "이번 학기 말" 같은 표현은 파서가 못 하고 LLM 이 해야 한다.
    #
    # ⚠️ **답한 슬롯에만** 적용한다. 수확된 슬롯은 한 답에 여러 날짜가 섞였을 때 어느 것이
    # 어느 슬롯인지 골라야 하는데, 그건 파서가 할 수 없는 판단이다.
    ruled = _rule_first_value(answer_type, answer_text, today=now_kst().date())
    slot = catalog.by_key.get(slot_key)
    # 답이 보기 글자 그대로거나 같은 시간 길이면 룰이 맞춘 보기 (아니면 None).
    #
    # 칩 답을 LLM 이 정규화하지 못했을 때(타임아웃·예산 소진·금지어 차단으로 룰 폴백)의
    # 안전망이기도 하다 — FE 는 칩을 탭해도 **문자열**("3학년")로 보내므로, 이 길이 없으면
    # 폴백 한 번에 칩 답이 전부 '없음'(스킵)으로 저장되고 — 이월 슬롯이라 — 다음 인터뷰에서도
    # 다시 묻지 않았다.
    options = _answer_options(config)
    rule_chips = _rule_chip_values(answer_type, answer_text, slot=slot, options=options)
    # 칩/선택 슬롯에서 LLM 값이 보기로 안 맞춰지면 LLM 이 판단하지 못한 것으로 본다 — 그러면
    # 사용자가 보기를 그대로 보낸 경우(칩 탭) 룰이 맞춘 보기가 이긴다.
    llm_value = (
        update.normalized_value
        if _fits_options(answer_type, update.normalized_value, slot=slot, options=options)
        else None
    )
    if ruled is not None:
        normalized = ruled
    elif _chip_rule_decides(slot, answer_type, state["last_answer"]):
        # 길이 슬롯에 **직접 입력**이 온 경우 — 값을 정하는 건 룰이다(`_chip_rule_decides`).
        normalized = rule_chips
    elif llm_value is not None:
        normalized = llm_value
    else:
        normalized = rule_chips

    slot_answers = dict(state["slot_answers"])
    attempts = _pending_attempts(slot_answers.get(slot_key)) + 1  # 이번 시도 포함
    stored, filled_now = _decide_storage(
        slot_key,
        answer_type,
        state["last_answer"],
        normalized,
        update.clarity_score,
        attempts,
        now_kst().date(),
        critical_slots=catalog.critical_slots,
        deadline_slot=catalog.deadline_slot,
        # 카탈로그에 고정 보기가 있는 칩/선택 슬롯만 — `goals.heaviest` 처럼 보기를 런타임에
        # 만드는 슬롯은 핵심 슬롯 경로가 이미 같은 재질문을 한다.
        has_chip_options=bool(slot and slot.options) and answer_type in {"chip", "select"},
        slot=slot,
        options=options,
    )
    if stored is not None:  # 실제 값·스킵·pending 모두 저장(영속) — pending 은 '미충족'으로 읽힘
        slot_answers[slot_key] = stored
    # 목표가 1개뿐이면 goals.heaviest 자동 채움 → 자명한 select 질문(직전 답 echo)을 건너뛴다.
    if filled_now and slot_key == "goals.list":
        _autofill_single_goal_heaviest(slot_answers)

    # 같은 호출에서 함께 뽑힌 다른 슬롯들 — 게이트·정규화·과거마감 규칙은 그대로다.
    harvested = (
        _apply_harvested(update.slots, slot_answers, open_slots, config, kind=state["kind"])
        if merged
        else []
    )

    return {
        **state,
        "harvested": harvested,
        "slot_answers": slot_answers,
        "ambiguity_score": update.new_ambiguity,  # telemetry(ambiguity_final) — 종료는 FSM 이 운전
        "last_answer": None,  # 소비 완료 — 다음 턴 답과 섞이지 않게
        "last_slot_key": None,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


def _harvest_slot_line(
    slot_key: str,
    meta: dict[str, Any],
    *,
    default_questions: Mapping[str, str] = PLAN_CATALOG.default_questions,
) -> str:
    """하베스팅 프롬프트에 실을 '미충족 슬롯' 한 줄 (key | 라벨 | 형식 | 보기)."""
    label = str(meta.get("label") or default_questions.get(slot_key, slot_key))
    answer_type = str(meta.get("answer_type") or "text")
    opts = meta.get("options") or []
    opts_str = ", ".join(str(o) for o in opts) or "(자유 입력)"
    return f"- {slot_key} | {label} | {answer_type} | {opts_str}"


def _apply_harvested(
    harvested: Sequence[HarvestedSlot],
    slot_answers: dict[str, Any],
    open_slots: Sequence[str],
    config: RunnableConfig,
    *,
    kind: str,
) -> list[str]:
    """수확된 값들을 게이트에 태워 `slot_answers` 에 **제자리로** 채운다. 채운 키를 돌려준다.

    별도 함수인 이유: 수확 호출이 채점과 **합쳐질 수도, 따로 갈 수도** 있어서다
    (`validate_answer` 의 조건부 배선 참고). 규칙이 두 벌이 되면 어느 경로로 들어왔느냐에
    따라 같은 값이 다르게 저장된다.
    """
    catalog = CATALOGS[kind]
    meta = _slot_meta(config)
    open_set = set(open_slots)
    prefilled: list[str] = []
    for h in harvested:
        if h.slot_key not in open_set or sv.is_settled(slot_answers.get(h.slot_key)):
            continue
        if h.confidence < HARVEST_MIN_CONFIDENCE:
            continue
        answer_type = (meta.get(h.slot_key) or {}).get("answer_type")
        stored = _coerce_normalized(
            answer_type if isinstance(answer_type, str) else None,
            h.normalized_value,
            slot=catalog.by_key.get(h.slot_key),
        )
        # 지난 마감은 **미리 채우지 않는다** — 하베스팅은 `_decide_storage` 를 안 거쳐서,
        # 여기서 채우면 슬롯이 '충족' 이 돼 되묻기(#231) 경로 자체가 열리지 않는다.
        # 건너뛰면 슬롯이 열린 채 남아 정식 질문에서 물어보고, 거기서 판정이 돈다.
        if _is_past_deadline(
            h.slot_key, stored, now_kst().date(), deadline_slot=catalog.deadline_slot
        ):
            continue
        if stored is not None:
            slot_answers[h.slot_key] = _prune_goal_glosses(h.slot_key, stored)
            prefilled.append(h.slot_key)
    return prefilled


def harvest_candidates(
    state: InterviewState,
    config: RunnableConfig,
    *,
    answer_text: str,
    answered_slot: str,
) -> list[str]:
    """이 답에서 **미리 채워볼 수 있는** 슬롯들. 비어 있으면 수확을 하지 않는다.

    ⚠️ **이 판정은 LLM 을 부르기 전에 끝난다** — 그래서 "수확할 때만 합친 프롬프트를
    쓰는" 조건부 배선이 가능하다. 무조건 합치면 수확하지 않는 turn 까지 수확 규칙을
    프롬프트에 짊어져 **토큰이 늘어난다**(실측 +35%). 조건부면 −5% 다.
    """
    catalog = CATALOGS[state["kind"]]
    if not catalog.harvest_enabled:
        return []
    open_slots = [
        k
        for k in catalog.required_keys
        if k != answered_slot
        and k not in catalog.harvest_exclude
        and not sv.is_settled(state["slot_answers"].get(k))
    ]
    if not _per_goal_harvest_allowed(state["slot_answers"]):
        open_slots = [k for k in open_slots if k not in catalog.per_goal_slots]
    stripped = answer_text.strip()
    if not open_slots or not stripped:
        return []
    if len(stripped) < HARVEST_MIN_ANSWER_CHARS:
        _log.info(
            "harvest_skipped_short_answer",
            extra={
                "answered_slot": answered_slot,
                "answer_chars": len(stripped),
                "open_slots": len(open_slots),
            },
        )
        return []
    return open_slots


def harvest_listing(open_slots: Sequence[str], config: RunnableConfig, *, kind: str) -> str:
    """수확 프롬프트에 실을 미충족 슬롯 목록."""
    meta = _slot_meta(config)
    catalog = CATALOGS[kind]
    return "\n".join(
        _harvest_slot_line(k, meta.get(k) or {}, default_questions=catalog.default_questions)
        for k in open_slots
    )


async def summarize_interview(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """LLM ③ — 모은 슬롯을 요약 확인 카드로. timeout 시 슬롯에서 룰 요약.

    `kind="ultimate"` 는 `agents/ultimate_summary_agent` 를 통한다(§2.2) — 이 레포에 실제로
    배선된 첫 `agents/` 모듈. `kind="plan"` 은 기존 인라인 호출 그대로(리팩터 범위 밖).
    """
    if state["kind"] == "ultimate":
        summary, fell_back = await ultimate_summary_agent.run(
            slot_answers=state["slot_answers"],
            session=_session(config),
            user_id=state["user_id"],
            tone_mode=_tone_mode(config),
        )
        return {
            **state,
            "summary": summary,
            "used_fallback": state["used_fallback"] or fell_back,
        }

    v = _summary_variables(state)
    result = await aiClient.run(
        module="interview",
        schema=InterviewSummary,
        prompt_id=CATALOGS[state["kind"]].prompt_summary,
        fallback=lambda: _rule_summary(state),
        timeout=8.0,
        variables=v,
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
    )
    return {
        **state,
        "summary": result.value,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


async def finalize_outcome(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """터미널 — LLM 0회로 경계 계약 빌드. `kind` 로 `InterviewOutcome`(First Plan 시드) 또는

    `UltimateGoalOutcome`(만다라 시드, §5.4)을 분기해서 만든다 — 하나의 core_goals
    min_length=1 계약에 억지로 맞추면 궁극목표 세션에도 PLACEHOLDER_GOAL_TITLE 유령 목표가
    생긴다(#88/#96 재발 지점).
    """
    reason: InterviewEndReason = _terminal_reason(state) or "completed"
    analysis_source: Literal["llm", "rule"] = "rule" if state["used_fallback"] else "llm"
    if state["kind"] == "ultimate":
        ultimate_outcome = ultimate_adapter.build_ultimate_outcome(
            session_id=str(state["session_id"]),
            slot_answers=state["slot_answers"],
            ambiguity_final=state["ambiguity_score"],
            end_reason=reason,
            analysis_source=analysis_source,
        )
        return {**state, "ultimate_outcome": ultimate_outcome, "end_reason": reason}

    outcome = interview_adapter.build_outcome(
        session_id=str(state["session_id"]),
        slot_answers=state["slot_answers"],
        ambiguity_final=state["ambiguity_score"],
        end_reason=reason,
        analysis_source=analysis_source,
    )
    return {**state, "outcome": outcome, "end_reason": reason}


# ─────────────────────────────────────────────────────────────────────────────
# Conditional edge — 순수 함수 (LLM 호출 X, ADR-0005 §2.4 패턴).
# ─────────────────────────────────────────────────────────────────────────────


def _terminal_reason(state: InterviewState) -> InterviewEndReason | None:
    """종료 조건 평가. 종료면 DB enum 사유, 아니면 None.

    완료는 **필수 슬롯 완료(FSM)가 단독으로 운전**한다 — 이때만 남은 필수 슬롯 0(= FE
    명료성 100%)이 보장된다. float `ambiguity_score` 가 낮아도 미해결 필수 슬롯이 남으면
    계속 묻는다. 재질문 폭주는 `_decide_storage` 의 슬롯별 시도 상한이 막아 모든 슬롯이 결국
    채워지므로, 별도 turn_limit 없이도 완료로 수렴한다.
    """
    if state["early_finish"]:
        return "early_user"
    if _all_required_filled(state):
        return "completed"
    return None


def should_continue(state: InterviewState) -> Literal["continue", "finish"]:
    """Cycle 종료 조건. 종료면 summarize_interview, 아니면 ask_question 재진입."""
    return "finish" if _terminal_reason(state) is not None else "continue"


# ─────────────────────────────────────────────────────────────────────────────
# 답 정규화 / 프롬프트 변수 보조
# ─────────────────────────────────────────────────────────────────────────────

_TEXT_SPLIT_RE = re.compile(r"[,、，\n]")

# '없음/모름/건너뛰기' 의사 표현 — LLM 이 normalized_value="" 신호를 놓쳐도(짧은 "없어" 등)
# 룰로 스킵 처리해 무한 재질문을 막는 백스톱.
_SKIP_RE = re.compile(
    r"없어|없음|없다|없습니다|모르|몰라|상관\s*없|딱히|건너|넘어갈|해당\s*없|스킵|skip",
    re.IGNORECASE,
)


def _looks_like_skip(text: str) -> bool:
    """항목 없음·건너뛰기 의사가 답의 거의 전부인지 (룰 백스톱).

    긴 답에 우연히 '없어'가 섞인 경우(예: "고정 시간 없어서 자유로워")는 제외하려고 길이 상한.
    """
    t = text.strip()
    return bool(t) and len(t) <= 20 and _SKIP_RE.search(t) is not None


# 문장 조각의 강한 신호 — 이 어미로 끝나는 조각은 **목표 제목이 될 수 없다** (#232).
#
# 진짜 다중 목표를 쉼표로 나열할 때 각 항목은 짧은 명사구다("토익", "캡스톤", "운동").
# 반면 산문형 답을 쉼표로 쪼개면 서술 조각이 나온다("대학원 지원을 다 끝냈고").
# 연결어미(고/며/는데/지만)와 종결어미(요/습니다)는 그 둘을 가르는 문법적 신호다.
_SENTENCE_TAIL_RE = re.compile(r"(고|며|면서|는데|지만|아서|어서|니까|요|습니다|이다)\s*[.!?]?\s*$")


def _normalize_for_store(slot_key: str, answer: dict[str, Any]) -> dict[str, Any]:
    """저장 직전 룰 정규화 — text 답은 항목 리스트(`normalized`)를 채워 어댑터가 쓰기 쉽게.

    chip/range 는 그대로 둔다. 이미 normalized 가 있으면 보존.

    **goals.list 는 산문형 답을 쉼표로 쪼개지 않는다** (#232). 이 경로는 LLM 정규화가
    실패했을 때만 도는데, 실측에서 "대학원 지원을 다 끝냈고, 이제 합격 발표를 기다리는
    중이에요." 가 조각 2개(`['대학원 지원을 다 끝냈고', '이제 합격 발표를 기다리는 중이에요.']`)
    로 쪼개져 **둘 다 목표로 영속**됐다. 쪼개는 건 추측이고, 통째로 하나로 두면 최악이
    '제목이 긴 목표 1개' 라 사용자가 고칠 수 있다 — 가짜 목표가 생기는 것보다 낫다.
    쉼표로 나열된 짧은 명사구(진짜 다중 목표)는 그대로 나눈다.
    """
    if answer.get("type") == "text" and "normalized" not in answer:
        raw = str(answer.get("raw", ""))
        parts = [p.strip() for p in _TEXT_SPLIT_RE.split(raw) if p.strip()]
        if (
            slot_key == "goals.list"
            and len(parts) > 1
            and any(_SENTENCE_TAIL_RE.search(p) for p in parts)
        ):
            _log.info("goal_list_prose_not_split", extra={"parts": len(parts)})
            return sv.dump(sv.TextAnswer(raw, (raw.strip(),)))
        if parts:
            return sv.dump(sv.TextAnswer(raw, tuple(parts)))
    return answer


# 구조화 슬롯 — 추출값만 있으면 clarity 게이트 없이 저장(선택/구간/날짜는 재질문 대상 아님).
_CONSTRAINED_TYPES = {"chip", "select", "time_range", "date_picker"}


def _rule_first_value(answer_type: str | None, text: str, *, today: date) -> Any | None:
    """날짜·시각 슬롯이면 룰 파서 값. 슬롯이 아니거나 못 뽑으면 `None`(= LLM 값을 쓴다).

    ⚠️ **`None` 은 "값이 없다" 가 아니라 "룰이 판단하지 않았다" 는 뜻**이다. 호출부가
    그때 LLM 값으로 떨어진다. 파서가 억지로 맞히면 사용자가 정정할 기회를 잃는다.
    """
    if not text.strip():
        return None
    if answer_type == "date_picker":
        return datetime_parse.parse_date(text, today=today)
    if answer_type == "time_range":
        return datetime_parse.parse_time_range(text)
    return None


def _rule_chip_values(
    answer_type: str | None,
    text: str,
    *,
    slot: InterviewSlot | None,
    options: Sequence[str],
) -> list[str] | None:
    """칩/선택 슬롯의 답이 **보기 글자 그대로**면 그 보기들. 아니면 `None`(= 판단하지 않음).

    쉼표로 여러 개를 고른 답("저녁, 심야")도 나눠서 맞춘다. 단 답 전체가 보기 하나와
    같으면 나누지 않는다 — `goals.heaviest` 의 보기는 사용자가 적은 목표 제목이라 쉼표가
    들어 있을 수 있다("토익, 오픽 준비").

    카탈로그 보기가 있으면 `canonical_chip_values`(공백·시간 표기 정규화)로, 없으면
    (`goals.heaviest` 처럼 런타임에 만든 보기) 라우터가 넘긴 보기와 **정확히 같을 때만**
    받는다. 보기에 없는 말은 버린다 — 지어낸 값으로 슬롯을 닫느니 다시 묻는다.
    """
    if answer_type not in {"chip", "select"}:
        return None
    whole = text.strip()
    if not whole:
        return None
    parts = [whole] if whole in options else [p.strip() for p in _TEXT_SPLIT_RE.split(whole)]
    parts = [p for p in parts if p]
    if slot is not None and slot.options:
        picked = canonical_chip_values(slot, parts, drop_unknown=True)
    else:
        allowed = set(options)
        picked = list(dict.fromkeys(p for p in parts if p in allowed))
    return picked or None


def _chip_rule_decides(
    slot: InterviewSlot | None, answer_type: str | None, last_answer: dict[str, Any] | None
) -> bool:
    """이 답은 **룰이** 보기를 정하는가 — 길이 칩 슬롯에 직접 입력으로 답한 경우.

    FE 는 칩 슬롯에도 직접 입력을 열어 둔다("직접 입력해도 돼요…"). 거기 적은 값을 LLM 이
    **가장 가까운 보기로 반올림**해 왔다: `goals.session_length` 에 "20분" 을 적으면 30분,
    "45분" 도 30분, "7분" 은 15분, "하루종일" 은 240분이 저장되고 슬롯은 '충족' 으로 닫혔다.
    세션 길이는 모든 블록의 길이이자 주당 분량의 기준이라, 계획 전체가 사용자가 말한 적 없는
    숫자 위에 세워졌다 — 그리고 사용자는 자기가 20분이라고 적은 걸 기억한다.

    길이 슬롯에서는 **숫자가 곧 답**이라 대조가 정확히 된다(`is_duration_slot`). 그래서 여기서만
    룰이 결정한다: 보기와 같은 길이면 그 보기로 맞추고("한 시간"·"90분" 같은 다른 표기 포함),
    아니면 값을 만들지 않는다 → 슬롯이 열린 채 남아 **보기를 들고 다시 묻는다**(v2.30-interview
    가 문서화한 의도). 의미 추론이 필요한 나머지 칩(학년·시간대·톤 — "지금 방학이에요" → '방학')은
    종전대로 LLM 이 맡는다. 거긴 반올림할 숫자가 없다.

    `last_answer` 가 이미 칩 구조면(옛 클라이언트가 `{"type":"chip"}` 로 보낸 경우) 관여하지
    않는다 — 사용자가 **누른** 답을 룰이 거부하면 같은 질문이 반복되는 루프가 된다
    (`interview_runner._coerce_answer` 의 `drop_unknown=False` 와 같은 이유).
    """
    return (
        answer_type in {"chip", "select"}
        and is_duration_slot(slot)
        and (last_answer or {}).get("type") == "text"
    )


def _match_options(
    values: Sequence[Any], *, slot: InterviewSlot | None, options: Sequence[str]
) -> list[str]:
    """칩 값들을 **실제로 보여 준 보기**로 맞춘다. 못 맞춘 값은 버린다.

    - 카탈로그 보기가 있는 슬롯 → `canonical_chip_values`(정확·공백 무시·같은 시간 길이).
    - 보기를 런타임에 만드는 슬롯(`goals.heaviest` — 사용자가 적은 목표 제목) → 라우터가 넘긴
      `options` 와 공백만 무시하고 대조한다. 비슷한 값("토익" ↔ "토익 900점")을 받아 주면
      `interview_adapter._build_goals` 가 목록에 없는 heaviest 를 새 목표로 덧붙인다.
    - 대조할 보기가 아예 없으면(배치 그래프·옛 호출부) 다듬기만 한다 — 종전 동작.
    """
    if slot is not None and slot.options:
        return canonical_chip_values(slot, values, drop_unknown=True)
    if options:
        by_squashed = {"".join(o.split()): o for o in options}
        picked = (by_squashed.get("".join(str(v).split())) for v in values)
        return list(dict.fromkeys(p for p in picked if p))
    return canonical_chip_values(None, values, drop_unknown=True)


def _fits_options(
    answer_type: str | None, value: Any, *, slot: InterviewSlot | None, options: Sequence[str]
) -> bool:
    """칩/선택 슬롯의 LLM 정규화 값이 보기로 맞춰지는가.

    칩/선택이 아니거나, 값이 없거나, '없음' 신호(빈 문자열)면 보기 검증 대상이 아니라 True.
    """
    if answer_type not in {"chip", "select"} or value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return _coerce_normalized(answer_type, value, slot=slot, options=options) is not None


def _coerce_normalized(
    answer_type: str | None,
    norm: Any,
    *,
    slot: InterviewSlot | None = None,
    options: Sequence[str] = (),
) -> dict[str, Any] | None:
    """LLM 이 뽑은 normalized_value 를 슬롯 형식대로 저장 형태(dict)로 환원. 불가면 None.

    ⚠️ **칩은 보기로 검증한다**(`_match_options`) — 보기에 없는 값은 버려 `None` 을 돌려준다
    (그러면 슬롯이 열린 채 남아 인터뷰가 실제 보기를 들고 정식으로 묻는다). 예전에는 LLM 이
    낸 문자열을 `str()` 해서 그대로 담았고, 그 구멍으로 파서 사고가 두 번 났다 — `"2시간 이상"`
    을 2분으로(v2.00), `"30분"` 을 주당 30시간으로(v2.01) 읽은 것이다. 파서를 하나씩 고치는
    대신 어휘를 좁힌다(`interview_catalog.canonical_chip`). 수확 경로뿐 아니라 **답한 슬롯
    자신의 값**도 이 검증을 지나야 한다 — 슬롯 없이 부르면 "주 1~2회" 가 그대로 저장돼
    `_chip_frequency` 가 주 12회로 읽었다.

    build_outcome 이 읽는 규약과 일치:
    - chip/select → {"type":"chip","values":[...]}
    - time_range  → {"type":"range","start":"HH:MM","end":"HH:MM"}
    - date_picker → {"type":"text","raw":"YYYY-MM-DD"}  (goals.deadlines 는 slot_value.text 로 읽음)
    - text/미지정 → {"type":"text","raw":..., "normalized":[...]}
    """
    if norm is None:
        return None
    if answer_type in {"chip", "select"}:
        vals = norm if isinstance(norm, list) else [norm]
        cleaned = _match_options(vals, slot=slot, options=options)
        return sv.dump(sv.ChipAnswer(tuple(cleaned))) if cleaned else None
    if answer_type == "time_range":
        if isinstance(norm, dict):
            start, end = norm.get("start"), norm.get("end")
            if isinstance(start, str) and start and isinstance(end, str) and end:
                return sv.dump(sv.RangeAnswer(start, end))
        return None
    if answer_type == "date_picker":
        if isinstance(norm, (dict, list)):
            return None
        s = str(norm).strip()
        return sv.dump(sv.TextAnswer(s)) if s else None
    # text 또는 answer_type 미지정 (graph/legacy) — 정리된 핵심값
    if isinstance(norm, list):
        items = [str(v).strip() for v in norm if str(v).strip()]
        return sv.dump(sv.TextAnswer(", ".join(items), tuple(items))) if items else None
    s = str(norm).strip()
    return sv.dump(sv.TextAnswer(s)) if s else None


# goals.list 항목 중 **목표가 아니라 직전 목표의 부연 설명**인 것의 강한 신호 (#232 백스톱).
#
# 보수적으로 '수량 단위(N당)·정도 표현' 만 잡는다. 넓은 판별은 ambiguity_score 프롬프트 규칙이
# 맡고(1차), 여기는 프롬프트가 놓친 명백한 것만 걷어낸다 — 진짜 목표를 지우는 오탐이 훨씬 나쁘다.
_GOAL_GLOSS_RE = re.compile(
    r"^\s*(각각|각\s|한\s*\S+당)"  # "각 권당 10챕터", "각각 3회씩"
    r"|[가-힣]당\s*\d"  # "권당 10챕터", "회당 30분"
    r"|(정도|쯤|가량)\s*(예요|이에요|에요|입니다|이다|야|임)?\s*$"  # "10챕터 정도예요"
)


def _prune_goal_glosses(slot_key: str, stored: dict[str, Any]) -> dict[str, Any]:
    """goals.list 에서 부연 설명 항목을 걷어낸다 — 유령 목표 방지 (#232).

    실측: "전공책 3권을 완독하고 싶어요. **각 권당 10챕터 정도예요.**" 가 목표 2개로 쪼개져
    '각 권당 10챕터 학습' 이라는 없는 목표가 생겼다. 그 유령이 (1) heaviest 선택 chip 에
    보기로 뜨고, (2) "'각 권당 10챕터 학습'는 이번 계획에 넣지 않았어요" 헛경고를 만들고,
    (3) proposed 목표로 영속돼 목표 목록 화면에 남았다.

    **첫 항목은 절대 걷어내지 않는다** — 부연 설명이려면 설명할 대상이 앞에 있어야 하므로,
    index 0 은 정의상 gloss 가 아니다. 이 구조 조건이 '목표가 통째로 사라지는' 최악을 막는다.

    goals.list 가 아니거나 항목 리스트가 없으면 그대로 반환.
    """
    if slot_key != "goals.list" or stored.get("type") != "text":
        return stored
    items = stored.get("normalized")
    if not isinstance(items, list) or len(items) < 2:
        return stored
    titles = [str(v) for v in items]
    kept = [t for i, t in enumerate(titles) if i == 0 or not _GOAL_GLOSS_RE.search(t)]
    if len(kept) == len(titles):
        return stored
    _log.info(
        "goal_gloss_pruned",
        extra={"kept": len(kept), "dropped": len(titles) - len(kept)},
    )
    pruned = {**stored, "normalized": kept}
    # raw 가 `_coerce_normalized` 가 항목을 이어붙여 만든 것이면 같이 줄인다(원문이면 보존).
    if stored.get("raw") == ", ".join(titles):
        pruned["raw"] = ", ".join(kept)
    return pruned


def _resolve_stored_value(
    slot_key: str,
    answer_type: str | None,
    last_answer: dict[str, Any] | None,
    normalized: Any,
    *,
    slot: InterviewSlot | None = None,
    options: Sequence[str] = (),
) -> tuple[dict[str, Any] | None, bool]:
    """저장할 값과 is_constrained 를 결정.

    우선순위: LLM 정규화값 → 이미 구조화된 raw(chip/range) → text raw.
    구조화 슬롯인데 어느 것도 못 얻으면 (None, True) → 저장 안 함(재질문). text 슬롯은
    원문 저장으로 폴백해 clarity 게이트가 판단하게 한다.

    goals.list 는 마지막에 `_prune_goal_glosses` 를 태운다 — LLM 경로와 룰 분리 경로
    (`_TEXT_SPLIT_RE`) 둘 다 부연 설명을 목표로 승격시킬 수 있어서다 (#232).
    """
    raw_type = last_answer.get("type") if last_answer else None
    raw_structured = raw_type in {"chip", "range"}
    is_constrained = answer_type in _CONSTRAINED_TYPES or raw_structured

    norm = _coerce_normalized(answer_type, normalized, slot=slot, options=options)
    if norm is not None:
        stored = norm
    elif (raw_structured or not is_constrained) and last_answer is not None:
        stored = _normalize_for_store(slot_key, last_answer)
    else:
        return None, is_constrained
    return _prune_goal_glosses(slot_key, stored), is_constrained


def _is_past_deadline(
    slot_key: str,
    stored: dict[str, Any] | None,
    today: date | None,
    *,
    deadline_slot: str | None = PLAN_CATALOG.deadline_slot,
) -> bool:
    """저장하려는 값이 마감 슬롯의 **지난 날짜** 인가 (#231).

    마감은 `date_picker` 라 `_CONSTRAINED_TYPES` 에 들어 있어 clarity 게이트를 통째로
    건너뛴다 — 즉 지금까지는 어떤 날짜든 무조건 한 번에 저장됐다. 지난 날짜는 모호한 게
    아니라 **상황이 달라졌다는 신호**("놓친 마감을 수습 중")라 되물어야 하고, 그냥 받으면
    계획 배치 창이 오늘 하루로 붕괴한다(#231 실측: 3세션 중 1개만 배치).

    `deadline_slot` 이 `None` 인 카탈로그(ultimate — 마감 개념 없음)는 항상 False.
    """
    if deadline_slot is None or slot_key != deadline_slot or today is None or not stored:
        return False
    raw = stored.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        return False  # 스킵 마커('마감 없음')는 지난 마감이 아니다
    try:
        return date.fromisoformat(raw.strip()) < today
    except ValueError:
        return False  # 날짜로 못 읽으면 기존 경로가 처리


def _decide_storage(
    slot_key: str,
    answer_type: str | None,
    last_answer: dict[str, Any] | None,
    normalized: Any,
    clarity: float,
    attempts: int,
    today: date | None = None,
    *,
    critical_slots: frozenset[str] = PLAN_CATALOG.critical_slots,
    deadline_slot: str | None = PLAN_CATALOG.deadline_slot,
    has_chip_options: bool = False,
    slot: InterviewSlot | None = None,
    options: Sequence[str] = (),
) -> tuple[dict[str, Any] | None, bool]:
    """직전 답을 어떻게 저장할지 결정하는 **순수 함수** — `(stored, filled_now)`.

    LLM 없이 표로 단위 테스트할 수 있도록 validate_answer 의 분기를 여기로 모은다.
    - 답 미주입(배치 그래프): (None, False).
    - 유효한 구조화/자유서술 값(has_real): 곧바로 저장.
    - 핵심 목표 슬롯(`critical_slots`): '없어/모름' 스킵 불가 → 상한까지 재질문(pending),
      상한(MAX_SLOT_ATTEMPTS) 도달 시 마지막 비지 않은 답을 best-effort 로 채택.
    - 보기가 정해진 칩 슬롯(`has_chip_options`): 보기로 못 맞춘 답은 '없음'으로 닫지 않고
      **보기를 들고 다시 묻는다**. 상한 도달 시 핵심 슬롯과 같은 best-effort 경로.
    - 비핵심: 스킵 의사·제약 슬롯·상한 도달이면 스킵(default)로 진행, 아니면 재질문(pending).
    - 마감이 **이미 지난 날짜**면 상한까지 되묻는다 (#231, `_is_past_deadline`).

    `attempts` 는 이번 시도 포함 누적 횟수(pending 마커에서 복원). pending 은 '미충족'으로
    읽혀(FSM 이 같은 슬롯 재질문) 시도 횟수를 턴 사이에 나른다.

    `today` (KST) 는 지난 마감 판정용. 넘기지 않으면 그 판정만 꺼진다(기존 동작 그대로).
    `critical_slots`/`deadline_slot` 은 카탈로그(kind) 별로 다르다 — 기본값은 plan 카탈로그
    (하위호환, 기존 호출부·단위 테스트 전부 무변경으로 안전).
    `slot`/`options` 는 칩 값을 대조할 보기(카탈로그 보기 / 런타임 보기) — `_match_options`.
    """
    if last_answer is None:
        return None, False  # 배치 그래프 등 답 미주입 턴

    answer_text = sv.display_text(last_answer)
    # 빈 답 = 명시적 '넘기기' — "없으면 넘겨도 돼요" 라고 물어 놓고 빈 답을 3회 재질문하던
    # 문제(실측: approach·materials 가 문구만 바뀐 같은 질문을 각 3번 반복). 빈 문자열은
    # `_looks_like_skip` 의 스킵 정규식에 안 걸리고, date_picker 같은 제약 타입만
    # `is_constrained` 로 한 번에 통과해 타입마다 동작이 갈렸다. **최상단**에서 자르는
    # 이유: 빈 답에서 LLM 이 뭔가 '추출'했다 주장해도(has_real) 믿으면 안 된다 —
    # 사용자는 아무것도 입력하지 않았다. 핵심 슬롯은 기존 재질문 경로로 내려보낸다.
    if not answer_text.strip() and slot_key not in critical_slots:
        return _skip("empty"), True
    llm_skip = isinstance(normalized, str) and not normalized.strip()

    if llm_skip:
        real_value: dict[str, Any] | None = None
        is_constrained = answer_type in _CONSTRAINED_TYPES or last_answer.get("type") in {
            "chip",
            "range",
        }
    else:
        real_value, is_constrained = _resolve_stored_value(
            slot_key, answer_type, last_answer, normalized, slot=slot, options=options
        )
    has_real = real_value is not None and (is_constrained or clarity >= STORE_CLARITY_MIN)

    # 지난 마감이면 저장하지 않고 되묻는다 — 상한에 닿으면 사용자 뜻으로 보고 그대로 받고,
    # 그때부터는 플래닝 백스톱(`is_overdue_deadline`)이 배치 창 붕괴를 막는다 (#231).
    if (
        has_real
        and attempts < MAX_SLOT_ATTEMPTS
        and _is_past_deadline(slot_key, real_value, today, deadline_slot=deadline_slot)
    ):
        return _pending(attempts, _RETRY_PAST_DEADLINE), False

    if has_real:
        return real_value, True
    if slot_key in critical_slots:
        # 핵심 목표 — 스킵 불가, 상한 내에서는 유효한 답이 나올 때까지 재질문한다.
        # 상한(MAX_SLOT_ATTEMPTS) 도달 시: 비지 않은 답이면 best-effort 로 채택.
        # **빈 답이어도 여기서 멈추면 안 된다** — attempts>=MAX 인데 answer_text 가
        # 계속 비어 있으면(#79 회귀: 핵심 슬롯에 빈 답만 반복) `and answer_text.strip()`
        # 가 거짓이라 아래로 안 빠지고 매번 `_pending` 만 돌려줘 시도 횟수가 아무리
        # 늘어도 절대 끝나지 않았다. 비핵심 슬롯이 상한에서 스킵으로 닫는 것과 같은 탈출구를
        # 핵심 슬롯에도 열어준다 — `slot_value.is_settled` 가 스킵을 '닫힘' 으로 읽어 다시
        # 묻지 않고, `build_outcome` 은 그 슬롯을 `unresolved_slots` 에 기록해(#499) First Plan
        # 이 보완 질문으로 이어받는다.
        #
        # '모르겠어요'·'딱히 없어요' 같은 **스킵 의사는 채택하지 않는다** — 세 번 솔직하게
        # 답한 학생에게 '음 잘 모르겠어요' 라는 Focus 목표가 생기고, 그 이름으로 목표별
        # 질문과 계획까지 만들어졌다(미러 실측). 스킵 마커로 두면 `unresolved_slots` 에 남아
        # outcome 은 자리표시자 목표를 쓰고, 그 자리표시자는 영속되지 않는다(#88).
        #
        # 보기 중에서 골라야 하는 핵심 슬롯(`goals.heaviest`)은 자유 입력을 채택하지 않는다 —
        # 보기에 없는 이름이 목표로 덧붙는다(`_build_goals`). 스킵으로 닫고, 계획이 집중할
        # 목표는 읽는 쪽(`interview_adapter.resolved_heaviest`)이 첫 목표로 대체한다.
        if attempts >= MAX_SLOT_ATTEMPTS:
            if not answer_text.strip():
                return _skip("empty"), True
            if _looks_like_skip(answer_text):
                return _skip("declined"), True
            if answer_type in {"chip", "select"} and options:
                return _skip("off_options"), True
            return sv.dump(sv.TextAnswer(answer_text.strip())), True
        return _pending(attempts), False
    # 보기가 정해진 칩 슬롯에 보기로 맞출 수 없는 답이 왔다 — '없음' 으로 닫지 않는다.
    #
    # `is_constrained` 하나로 곧장 스킵하던 자리다. 그래서 칩 슬롯에 직접 입력한 답이 보기와
    # 안 맞으면(또는 채점 LLM 이 폴백하면) 사용자는 분명히 답했는데 기록은 '없음' 이 되고,
    # 계획은 그 슬롯의 기본값으로 만들어졌다 — 되물어 볼 기회도 없었다. v2.30-interview 가
    # 문서화한 의도는 그 반대다: "칩 보기에 없는 값 … 그 슬롯은 열린 채 남아(필수면 다시 묻는다)".
    # 보기를 실어 한 번 더 묻고(`_RETRY_OFF_CATALOG`), 그래도 같으면 핵심 슬롯과 **같은**
    # best-effort 경로로 빠져나간다 — 같은 질문을 무한히 반복하지 않는다.
    if (
        has_chip_options
        and not llm_skip
        and answer_text.strip()
        and not _looks_like_skip(answer_text)
    ):
        # 상한까지 보기 밖이면 스킵으로 닫는다 — 예전엔 칩 슬롯에 텍스트로 넣어, 읽으면 기본값인데
        # 판정은 '답함' 이라 그 기본값이 사용자의 답으로 프로필에 굳었다(#499 와 같은 구멍).
        if attempts >= MAX_SLOT_ATTEMPTS:
            return _skip("off_options"), True
        return _pending(attempts, _RETRY_OFF_CATALOG), False
    if _looks_like_skip(answer_text):
        return _skip("declined"), True
    if llm_skip:
        return _skip("llm"), True
    if is_constrained:
        return _skip("unreadable"), True
    if attempts >= MAX_SLOT_ATTEMPTS:
        return _skip("gave_up"), True
    return _pending(attempts), False


def _has_answer_text(state: InterviewState) -> bool:
    return bool(_last_answer_text(state))


def _last_answer_text(state: InterviewState) -> str:
    return sv.display_text(state["last_answer"])


# 러닝 요약의 값 하나당 최대 글자 수 — 붙여넣은 자료 원문(최대 2만 자)이 뒤이은 질문
# 생성 호출마다 통째로 다시 실려, 호출당 토큰이 크게 늘고 8초 타임아웃에 가까워져 룰 폴백
# 질문이 잦아졌다. 질문 말투를 이어가는 데는 앞부분이면 충분하다. 계획 파이프라인은 이
# 요약이 아니라 슬롯 원문(`_materials_note`)을 읽으므로 자료 내용은 잃지 않는다.
_CONTEXT_VALUE_MAX = 120


def _answered_context(state: InterviewState) -> str:
    """앞서 채워진 슬롯 → 다음 질문용 짧은 러닝 요약("태그=값 / …").

    아직 답이 없으면 명시 문구. LLM 이 이전 답을 이어받아(맥락 반복 없이) 자연스럽게 묻게 한다.
    태그 맵은 `state["kind"]` 로 카탈로그를 조회해 얻는다. 값은 `_CONTEXT_VALUE_MAX` 자에서 자른다.
    """
    answers = state["slot_answers"]
    parts: list[str] = []
    for slot_key, tag in CATALOGS[state["kind"]].context_labels.items():
        value = answers.get(slot_key)
        if not sv.has_answer(value):
            continue
        text = sv.display_text(value).strip()
        if len(text) > _CONTEXT_VALUE_MAX:
            text = text[:_CONTEXT_VALUE_MAX].rstrip() + "…"
        if text:
            parts.append(f"{tag}={text}")
    return " / ".join(parts) if parts else "(아직 답한 내용 없음)"


def _heaviest_goal_hint(state: InterviewState) -> str:
    """목표별 질문이 가리킬 목표 — 계획이 집중할 목표(`resolved_heaviest`), 아직 모르면 첫 목표."""
    answers = state["slot_answers"]
    return (
        interview_adapter.resolved_heaviest(answers)
        or next(iter(sv.items(answers.get("goals.list"))), None)
        or "당신의 목표"
    )


_NOT_SET = "아직 정하지 않음"


def _summary_variables(state: InterviewState) -> dict[str, str]:
    """요약 프롬프트 변수 — 슬롯에서 사람이 읽을 문자열로 추출 (룰).

    확인 카드(Analysis Confirm)가 사용자가 실제로 답한 내용을 최대한 반영하도록, 목표·시간뿐
    아니라 마감·성공 이미지·노터치·휴식 수용·다운스코프 단위까지 함께 싣는다(빈 항목은
    "아직 정하지 않음"). 미입력 항목을 지어내지 않게 프롬프트가 이 default 를 그대로 노출.
    """
    answers = state["slot_answers"]
    role = sv.first_chip(answers.get("identity.role")) or "미상"
    season = sv.first_chip(answers.get("identity.season")) or ""
    goals = ", ".join(sv.items(answers.get("goals.list"))) or _NOT_SET
    # 계획이 집중할 목표와 **같은 값** — 못 골랐으면(스킵) 첫 목표다(`resolved_heaviest`).
    # 확인 카드는 [이대로 진행] 을 누르는 화면이라, 계획이 다룰 목표를 그대로 보여 줘야 한다.
    heaviest = interview_adapter.resolved_heaviest(answers) or _NOT_SET
    deadlines = sv.text(answers.get("goals.deadlines")) or _NOT_SET
    success_image = sv.text(answers.get("goals.success_image")) or _NOT_SET
    window = sv.time_range(answers.get("time.activity_window"))
    time_window = f"{window[0]}~{window[1]}" if window else _NOT_SET
    peak = ", ".join(sv.chips(answers.get("time.peak_window"))) or _NOT_SET
    # 미답이면 기본값('담백')이 아니라 _NOT_SET — 요약 프롬프트가 "말하지 않은 사실을 지어내지
    # 말 것" 규칙대로 생략하게 한다. outcome 의 안전 기본값과 요약 문구는 다른 문제다.
    tone = sv.first_chip(answers.get("recovery.tone")) or _NOT_SET
    rest_ok = sv.first_chip(answers.get("recovery.rest_ok")) or _NOT_SET
    downscope_unit = sv.first_chip(answers.get("recovery.downscope_unit")) or _NOT_SET
    identity = f"{role} {season}".strip()
    # 계산된 주당 총량 — 사용자에게 **곱셈 결과를 되돌려준다**. '한 번에 2시간 · 매일' 이
    # 주 14시간이라는 걸 확인 카드에서 보고 조정할 수 있게(그동안은 셋을 따로 묻고, 어긋나면
    # 계획 단계에서 경고만 냈다). 빈도가 '상관없음' 이면 계산이 안 되므로 답한 값을 그대로.
    #
    # ⚠️ **계획이 쓰는 값과 순서가 같아야 한다**(`interview_adapter.weekly_hours_for_plan`).
    # 예전엔 여기만 유도값을 먼저 읽어서, 주당 시간을 직접 답한 사용자에게 카드가 "약 주
    # 3.5시간" 을 보여 주고 계획은 답한 주 2시간으로 만들어졌다(실측). 확인 카드는 사용자가
    # [이대로 진행] 을 누르는 화면이라, 거기 적힌 숫자가 곧 계획의 숫자여야 한다.
    answered_weekly = sv.first_chip(answers.get("goals.weekly_time"))
    derived = interview_adapter.derived_weekly_hours(answers)
    if answered_weekly:
        weekly_load = answered_weekly
    elif derived:
        length = sv.first_chip(answers.get("goals.session_length")) or "?"
        freq = sv.first_chip(answers.get("goals.frequency")) or "?"
        weekly_load = f"약 주 {derived:g}시간 (한 번 {length} × {freq})"
    else:
        weekly_load = _NOT_SET
    return {
        "identity": identity,
        "weekly_load": weekly_load,
        "goals": goals,
        "heaviest": heaviest,
        "deadlines": deadlines,
        "success_image": success_image,
        "time_window": time_window,
        "peak_window": peak,
        "tone": tone,
        "rest_ok": rest_ok,
        "downscope_unit": downscope_unit,
    }


def _per_goal_harvest_allowed(slot_answers: dict[str, dict[str, Any] | None]) -> bool:
    """목표별 슬롯(`interview_catalog.PLAN_CATALOG.per_goal_slots`)을 하베스팅해도 되는가 —

    귀속 대상이 확정됐는가.

    heaviest 가 채워졌으면(사용자 선택 또는 단일 목표 자동확정) 이후 답은 그 목표에 관한
    것이므로 안전하다. 아니면 goals.list 가 정확히 한 개일 때만 — 여러 개거나 아직 목표를
    모르면(goals.list 미답) 속성이 어느 목표 것인지 알 수 없어, 오채움이 재질문보다 나쁘다는
    이 노드의 원칙대로 정식 질문에 맡긴다.
    """
    if sv.is_settled(slot_answers.get("goals.heaviest")):
        return True
    return len(sv.items(slot_answers.get("goals.list"))) == 1


def _autofill_single_goal_heaviest(slot_answers: dict[str, dict[str, Any] | None]) -> None:
    """목표가 1개뿐이면 goals.heaviest 를 그 목표로 자동 채워 자명한 select 질문을 건너뛴다.

    heaviest 는 '어느 목표가 가장 무거운가'를 고르는 select 인데, 목표가 하나면 선택지가 없어
    보기가 직전 답(goals.list)을 그대로 반복(echo)한다 → 그 하나를 heaviest 로 자동 확정한다.
    사용자가 이미 답한 경우(재조립 등)엔 건드리지 않는다.
    """
    if sv.is_settled(slot_answers.get("goals.heaviest")):
        return
    items = sv.items(slot_answers.get("goals.list"))
    if len(items) == 1:
        slot_answers["goals.heaviest"] = sv.dump(sv.ChipAnswer((items[0],)))


def build_interview_graph() -> CompiledStateGraph[
    InterviewState, Any, InterviewState, InterviewState
]:
    """Cyclic StateGraph 컴파일. 라우터는 보통 `interview_runner` 로 턴 단위 구동하고,
    batch 시뮬레이션/테스트는 `await graph.ainvoke(initial, config=...)`."""
    graph = StateGraph(InterviewState)
    graph.add_node("ask_question", ask_question)
    graph.add_node("receive_answer", receive_answer)
    graph.add_node("validate_answer", validate_answer)
    graph.add_node("summarize_interview", summarize_interview)
    graph.add_node("finalize_outcome", finalize_outcome)

    graph.set_entry_point("ask_question")
    graph.add_edge("ask_question", "receive_answer")
    graph.add_edge("receive_answer", "validate_answer")
    graph.add_conditional_edges(
        "validate_answer",
        should_continue,
        {"continue": "ask_question", "finish": "summarize_interview"},
    )
    graph.add_edge("summarize_interview", "finalize_outcome")
    graph.add_edge("finalize_outcome", END)
    return graph.compile()
