"""스킵은 다시 묻지 않지만 사용자의 답도 아니다 (#499).

스킵 마커가 빈 텍스트(`{"type":"text","raw":""}`)라 "다시 물을 필요 없음" 과 "사용자가 값을
줬음" 이 한 술어로 판정됐다. 그래서 스킵한 슬롯이 `unresolved_slots` 에서 빠졌고, 서버 기본값이
사용자의 답으로 프로필에 굳고, 재인터뷰 이월도 그 칸을 '답' 으로 넘겨 다시 묻지 않았다.

여기 입력은 **저장 모양 그대로의 dict** 다 — 예전 스킵(빈 텍스트)과 새 스킵(`type: skip`) 둘 다.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from reaction_backend.api.routes import interview as interview_route
from reaction_backend.llm import RunResult, aiClient
from reaction_backend.orchestrator import (
    interview,
    interview_adapter,
    interview_runner,
    ultimate_adapter,
)
from reaction_backend.orchestrator.interview import InterviewState
from reaction_backend.orchestrator.interview_catalog import CATALOGS, PLAN_CATALOG
from reaction_backend.schemas.interview import (
    AmbiguityUpdate,
    AnswerIntake,
    InterviewSummary,
    NextQuestionSchema,
)
from tests.conftest import FakeInterviewRepo

LEGACY_SKIP: dict[str, Any] = {"type": "text", "raw": ""}
NEW_SKIP: dict[str, Any] = {"type": "skip", "reason": "declined"}
SKIPS = pytest.mark.parametrize("skip", [LEGACY_SKIP, NEW_SKIP], ids=["legacy", "new"])

_SLOT_META: dict[str, dict[str, Any]] = {
    s.slot_key: {"label": s.label, "answer_type": s.answer_type, "options": list(s.options)}
    for s in PLAN_CATALOG.slots
}
_TWO_GOALS = ["토익 900점", "캡스톤 마무리"]


def _filled(slot_key: str) -> dict[str, Any]:
    slot = PLAN_CATALOG.by_key[slot_key]
    if slot.answer_type == "time_range":
        return {"type": "range", "start": "09:00", "end": "23:00"}
    if slot.answer_type == "date_picker":
        return {"type": "text", "raw": "2026-12-31"}
    if slot.options:
        return {"type": "chip", "values": [slot.options[0]]}
    if slot_key == "goals.list":
        return {"type": "text", "raw": ", ".join(_TWO_GOALS), "normalized": list(_TWO_GOALS)}
    if slot_key == "goals.heaviest":
        return {"type": "chip", "values": [_TWO_GOALS[0]]}
    return {"type": "text", "raw": "답변"}


def _answers(**overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """필수 슬롯을 전부 정상으로 채운 답 + `overrides`(키의 '.' 는 '__' 로)."""
    answers = {k: _filled(k) for k in PLAN_CATALOG.required_keys}
    answers.update({k.replace("__", "."): v for k, v in overrides.items()})
    return answers


def _state_missing(*open_keys: str) -> InterviewState:
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = {k: v for k, v in _answers().items() if k not in open_keys}
    return state


def _stub(*, normalized: Any = None, clarity: float = 0.9) -> Any:
    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        if schema is NextQuestionSchema:
            value: Any = NextQuestionSchema(question="다음 질문", empathy_one_liner="좋아요")
        elif schema in (AmbiguityUpdate, AnswerIntake):
            value = schema(
                slot_key=kwargs["variables"]["slot_key"],
                clarity_score=clarity,
                new_ambiguity=0.1,
                normalized_value=normalized,
            )
        else:
            value = InterviewSummary(
                headline="요약",
                goal_summary="목표",
                time_summary="시간",
                preference_summary="선호",
                confirm_question="이대로 계획을 세워볼까요?",
            )
        return RunResult(
            value=value, fell_back=False, reason=None, prompt_id="p", prompt_version="v1"
        )

    return stub_run


async def _answer(
    state: InterviewState, slot_key: str, raw: Any, options: list[str] | None = None
) -> interview_runner.TurnResult:
    slot = PLAN_CATALOG.by_key[slot_key]
    return await interview_runner.submit_and_advance(
        state=state,
        slot_key=slot_key,
        answer_value=raw,
        answer_type=slot.answer_type,
        options=list(slot.options) if options is None else options,
        slot_meta=_SLOT_META,
    )


def _outcome(answers: dict[str, Any]) -> Any:
    return interview_adapter.build_outcome(
        session_id="s",
        slot_answers=answers,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )


# ── 1. adapter — 스킵된 필수 슬롯은 unresolved_slots 에 남는다 ────────────────────


@SKIPS
def test_skipped_required_slot_is_unresolved_in_the_plan_outcome(skip: dict[str, Any]) -> None:
    outcome = _outcome(_answers(time__activity_window=skip, recovery__tone=skip))
    assert {"time.activity_window", "recovery.tone"} <= set(outcome.unresolved_slots)


@SKIPS
def test_skipped_required_slot_is_unresolved_in_the_ultimate_outcome(
    skip: dict[str, Any],
) -> None:
    catalog = CATALOGS["ultimate"]
    answers: dict[str, Any] = {k: {"type": "text", "raw": "답"} for k in catalog.required_keys}
    answers["ultimate.constraints"] = skip
    outcome = ultimate_adapter.build_ultimate_outcome(
        session_id="s",
        slot_answers=answers,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    assert "ultimate.constraints" in outcome.unresolved_slots


def test_answered_required_slots_are_not_unresolved() -> None:
    """회귀 없음 — 실제로 답한 슬롯은 종전대로 unresolved 가 아니다."""
    assert _outcome(_answers()).unresolved_slots == []


# ── 3·4. FSM·진행바는 그대로 — 스킵한 슬롯을 다시 묻거나 남은 칸으로 세지 않는다 ─────────


@SKIPS
def test_fsm_does_not_reask_a_skipped_slot(skip: dict[str, Any]) -> None:
    state = _state_missing()
    state["slot_answers"]["recovery.tone"] = skip
    assert interview._next_required_slot(state) is None  # 무한 재질문 회귀(#79) 방지


@SKIPS
def test_progress_metric_does_not_count_a_skipped_slot(skip: dict[str, Any]) -> None:
    assert interview_route._remaining_required(_answers(recovery__tone=skip)) == 0


# ── 5. 핵심 슬롯 — 목표를 세 번 못 정하면 unresolved 에 goals.list 가 남는다 ──────────


async def test_goals_list_skipped_three_times_stays_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 실제 채점 LLM 은 '모르겠어요' 를 목표로 보지 않는다 — clarity 를 낮게 준다.
    monkeypatch.setattr(aiClient, "run", _stub(clarity=0.1))
    state = _state_missing("goals.list", "goals.heaviest")
    result = await _answer(state, "goals.list", "잘 모르겠어요")
    for _ in range(interview.MAX_SLOT_ATTEMPTS - 1):
        result = await _answer(result.state, "goals.list", "잘 모르겠어요")

    assert result.state["next_slot_key"] != "goals.list"  # 다시 묻지는 않는다
    assert "goals.list" in _outcome(result.state["slot_answers"]).unresolved_slots


# ── 이월 — 스킵한 슬롯은 다음 인터뷰로 넘기지 않는다(그래야 다시 묻는다) ─────────────


@SKIPS
def test_carry_over_does_not_seed_a_skipped_slot(skip: dict[str, Any]) -> None:
    repo = FakeInterviewRepo()
    user_id = uuid4()

    async def run() -> dict[str, Any]:
        row = await repo.create_session(user_id, "m", kind="plan")
        await repo.upsert_slot_answer(row.id, "time.activity_window", skip, is_required=True)
        await repo.upsert_slot_answer(
            row.id, "time.peak_window", {"type": "chip", "values": ["저녁"]}, is_required=True
        )
        await repo.finalize(row, end_reason="completed", total_turns=3, ambiguity_final=0.0)
        return await interview_route._carry_over_slots(
            repo,
            user_id,
            source_kind="plan",
            keys=interview_adapter.CARRY_OVER_SLOT_KEYS,
        )

    carried = asyncio.run(run())
    assert "time.activity_window" not in carried
    assert carried["time.peak_window"] == {"type": "chip", "values": ["저녁"]}


# ── 보기 밖 답이 상한에 닿으면 — 칩 슬롯에 텍스트로 넣지 않고 스킵으로 닫는다 ──────────


async def test_off_options_answer_at_the_cap_is_a_skip_not_an_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'친구처럼 편하게' 를 세 번 — 예전엔 텍스트로 저장돼 결과는 기본값 '담백' 인데 '답함' 이었다."""
    monkeypatch.setattr(aiClient, "run", _stub())
    result = await _answer(_state_missing("recovery.tone"), "recovery.tone", "친구처럼 편하게")
    for _ in range(interview.MAX_SLOT_ATTEMPTS - 1):
        result = await _answer(result.state, "recovery.tone", "친구처럼 편하게")

    assert result.state["slot_answers"]["recovery.tone"] == {
        "type": "skip",
        "reason": "off_options",
    }
    assert "recovery.tone" in _outcome(result.state["slot_answers"]).unresolved_slots


# ── 스킵 사유 — 동작은 같고 기록만 다르다 ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("slot_key", "answer_type", "raw", "normalized", "attempts", "reason"),
    [
        ("goals.approach", "text", "", None, 1, "empty"),
        ("recovery.tone", "chip", "잘 모르겠어요", None, 1, "declined"),
        ("goals.approach", "text", "음", "", 1, "llm"),
        ("time.activity_window", "time_range", "그때그때 달라요 진짜로", None, 1, "unreadable"),
        ("goals.approach", "text", "음", None, interview.MAX_SLOT_ATTEMPTS, "gave_up"),
        ("recovery.tone", "chip", "친구처럼", None, interview.MAX_SLOT_ATTEMPTS, "off_options"),
        ("goals.list", "text", "모르겠어요", None, interview.MAX_SLOT_ATTEMPTS, "declined"),
    ],
)
def test_skip_records_why(
    slot_key: str,
    answer_type: str,
    raw: str,
    normalized: Any,
    attempts: int,
    reason: str,
) -> None:
    slot = PLAN_CATALOG.by_key[slot_key]
    stored, filled = interview._decide_storage(
        slot_key,
        answer_type,
        {"type": "text", "raw": raw},
        normalized,
        0.1,
        attempts,
        date(2026, 10, 19),
        critical_slots=PLAN_CATALOG.critical_slots,
        deadline_slot=PLAN_CATALOG.deadline_slot,
        has_chip_options=bool(slot.options) and answer_type in {"chip", "select"},
        slot=slot,
        options=list(slot.options),
    )
    assert (stored, filled) == ({"type": "skip", "reason": reason}, True)


# ── A — heaviest 를 못 고르면 저장은 스킵, '첫 목표' 대체는 읽는 쪽에서 ─────────────


@pytest.mark.parametrize("raw", ["토익이 제일 급해요", "잘 모르겠어요"])
async def test_unpicked_heaviest_is_stored_as_skip_and_read_as_the_first_goal(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """계획은 첫 목표를 집중으로 다루되(목표별 답이 붙는다) unresolved 에 heaviest 가 남는다."""
    monkeypatch.setattr(aiClient, "run", _stub(normalized="토익"))
    result = await _answer(_state_missing("goals.heaviest"), "goals.heaviest", raw, _TWO_GOALS)
    for _ in range(interview.MAX_SLOT_ATTEMPTS - 1):
        result = await _answer(result.state, "goals.heaviest", raw, _TWO_GOALS)

    answers = result.state["slot_answers"]
    assert answers["goals.heaviest"]["type"] == "skip"
    outcome = _outcome(answers)
    assert [g.title for g in outcome.core_goals] == _TWO_GOALS
    focus = [g for g in outcome.core_goals if g.is_heaviest]
    assert [g.title for g in focus] == [_TWO_GOALS[0]]
    assert focus[0].session_length_min is not None  # 목표별 답이 첫 목표에 붙었다
    assert "goals.heaviest" in outcome.unresolved_slots
    assert interview._summary_variables(result.state)["heaviest"] == _TWO_GOALS[0]


def test_heaviest_never_asked_is_not_replaced_by_the_first_goal() -> None:
    """한 번도 안 물은 heaviest(조기 종료)는 그대로 비어 있다 — 대체는 '못 고른' 경우만."""
    answers = _answers()
    del answers["goals.heaviest"]
    assert [g for g in _outcome(answers).core_goals if g.is_heaviest] == []
