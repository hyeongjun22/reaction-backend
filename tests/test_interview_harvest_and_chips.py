"""인터뷰가 **사용자가 말한 적 없는 값**으로 계획을 세우지 않는다 (r2 실측 회귀).

배포 미러에서 한 세션을 끝까지 돌려 확인된 네 가지를 못 박는다.

1. 활동 시간대(`time.activity_window`)를 **한 번도 묻지 않았는데** 답이 있었다 —
   "새벽에만 집중이 돼서 밤에 작업해요" 에서 00:00~06:00 이 수확돼 프로필까지 저장됐고,
   계획은 22:00 에 일정을 잡아 놓고 "활동 가능 시간(00:00~06:00)과 겹치지 않아서" 라고
   스스로를 반박했다.
2. 칩 슬롯에 직접 입력한 길이가 **가장 가까운 보기로 반올림**됐다 — "20분" → 30분,
   "45분" → 30분, "7분" → 15분. 세션 길이는 모든 블록의 길이라, 계획 전체가 사용자가
   말한 적 없는 숫자 위에 세워졌다.
3. FE 는 칩을 탭해도 **문자열**로 보내는데, 채점 LLM 이 폴백하면 그 답이 버려졌다.
4. 같은 사용자에게 주당 시간이 두 개였다 — 확인 카드는 "주 3.5시간", 목표 기록은 2시간.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from reaction_backend.domain import slot_value
from reaction_backend.llm import RunResult, aiClient
from reaction_backend.orchestrator import (
    interview,
    interview_adapter,
    interview_catalog,
    interview_runner,
)
from reaction_backend.orchestrator.interview import InterviewState
from reaction_backend.orchestrator.interview_catalog import PLAN_CATALOG
from reaction_backend.schemas.interview import (
    AmbiguityUpdate,
    AnswerIntake,
    HarvestedSlot,
    InterviewSummary,
    NextQuestionSchema,
)

_SLOT_META: dict[str, dict[str, Any]] = {
    s.slot_key: {"label": s.label, "answer_type": s.answer_type, "options": list(s.options)}
    for s in PLAN_CATALOG.slots
}


def _filled(slot_key: str) -> dict[str, Any]:
    """그 슬롯 형식에 맞는 '이미 답했다' 값 — 테스트 대상 슬롯만 비워 두려고 쓴다."""
    slot = PLAN_CATALOG.by_key[slot_key]
    if slot.answer_type == "time_range":
        return {"type": "range", "start": "09:00", "end": "23:00"}
    if slot.answer_type == "date_picker":
        return {"type": "text", "raw": "2026-12-31"}
    if slot.options:
        return {"type": "chip", "values": [slot.options[0]]}
    if slot_key == "goals.list":
        return {"type": "text", "raw": "캡스톤", "normalized": ["캡스톤"]}
    if slot_key == "goals.heaviest":
        return {"type": "chip", "values": ["캡스톤"]}
    return {"type": "text", "raw": "답변"}


def _state_missing(*open_keys: str) -> InterviewState:
    """`open_keys` 만 비어 있는 인터뷰 상태 — FSM 이 곧바로 그 슬롯을 묻는다."""
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = {
        key: _filled(key) for key in PLAN_CATALOG.required_keys if key not in open_keys
    }
    return state


def _stub(
    *,
    normalized: Any = None,
    clarity: float = 0.9,
    fell_back: bool = False,
    harvested: list[HarvestedSlot] | None = None,
):
    """aiClient.run stub — 채점 LLM 이 낸 정규화 값/수확 결과를 테스트가 정한다."""

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
                **({"slots": harvested} if schema is AnswerIntake and harvested else {}),
            )
        elif schema is InterviewSummary:
            value = InterviewSummary(
                headline="요약",
                goal_summary="목표 요약",
                time_summary="시간 요약",
                preference_summary="선호 요약",
                confirm_question="이대로 계획을 세워볼까요?",
            )
        else:  # pragma: no cover
            raise AssertionError(f"unexpected schema {schema}")
        return RunResult(
            value=value,
            fell_back=fell_back,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    return stub_run


async def _answer(state: InterviewState, slot_key: str, raw: Any) -> interview_runner.TurnResult:
    """라우터와 **같은 방식**으로 답 1개를 넣는다 — 슬롯 메타(형식·보기)까지 실어서."""
    slot = PLAN_CATALOG.by_key[slot_key]
    return await interview_runner.submit_and_advance(
        state=state,
        slot_key=slot_key,
        answer_value=raw,
        answer_type=slot.answer_type,
        options=list(slot.options),
        slot_meta=_SLOT_META,
    )


# ─────────────────────────────────────────────────────────────────────────────
# P1 — 활동 시간대는 묻지 않고 채우지 않는다
# ─────────────────────────────────────────────────────────────────────────────


async def test_activity_window_is_not_harvested_from_a_free_text_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시간대를 언급한 자유서술이 활동 시간대를 채우지 못한다 — 그리고 슬롯은 계속 열려 있다.

    회귀(실측): "새벽에만 집중이 돼서 밤에 작업해요" 한 마디로 00:00~06:00 이 수확돼,
    한 번도 묻지 않은 활동창이 그 사용자의 **유일한 배치 가능 시간**이 됐다. 질문 자체가
    "이 시간 밖엔 일정을 안 잡아요" 라고 약속하는 경계라, 확인받지 않은 값이 정의하면 안 된다.
    """
    monkeypatch.setattr(
        aiClient,
        "run",
        _stub(
            normalized="처음이에요",
            harvested=[
                HarvestedSlot(
                    slot_key="time.activity_window",
                    normalized_value={"start": "00:00", "end": "06:00"},
                    confidence=0.95,
                )
            ],
        ),
    )
    state = _state_missing("goals.current_level", "time.activity_window")

    result = await _answer(
        state,
        "goals.current_level",
        "새벽에만 집중이 돼서 밤에 작업해요. 아직 시작은 못 했어요.",
    )

    assert "time.activity_window" not in result.harvested
    assert result.state["slot_answers"].get("time.activity_window") is None
    # 슬롯이 열린 채 남아 **정식으로 묻는다** — 다음에 물을 슬롯이 바로 그것이다.
    assert result.done is False
    assert result.state["next_slot_key"] == "time.activity_window"


def test_harvest_excludes_whole_plan_boundaries_but_keeps_goal_content() -> None:
    """수확 제외 목록의 기준을 못 박는다 — 계획 전체를 가두는 값은 물어서만 받는다."""
    assert "time.activity_window" in PLAN_CATALOG.harvest_exclude
    assert "goals.weekly_time" in PLAN_CATALOG.harvest_exclude
    # 목표 내용은 그대로 수확 대상 — 하베스팅을 끈 게 아니라 좁힌 것이다.
    for key in ("goals.deadlines", "goals.success_image", "identity.role"):
        assert key not in PLAN_CATALOG.harvest_exclude


# ─────────────────────────────────────────────────────────────────────────────
# P2 — 칩 보기에 없는 길이를 말없이 반올림하지 않는다
# ─────────────────────────────────────────────────────────────────────────────


async def test_typed_session_length_is_not_snapped_to_a_nearby_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "20분" 이라고 적었는데 30분으로 저장되지 않는다 — 대신 보기를 들고 한 번 더 묻는다."""
    monkeypatch.setattr(aiClient, "run", _stub(normalized="30분"))
    state = _state_missing("goals.session_length")

    result = await _answer(state, "goals.session_length", "20분")

    stored = result.state["slot_answers"].get("goals.session_length")
    assert stored != {"type": "chip", "values": ["30분"]}, "사용자가 말한 적 없는 숫자다"
    assert stored == interview._pending(1, interview._RETRY_OFF_CATALOG)
    assert slot_value.is_settled(stored) is False
    assert result.state["next_slot_key"] == "goals.session_length"  # 같은 슬롯을 다시 묻는다


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("30분", "30분"),  # (a) 보기 그대로
        ("1시간30분", "1시간 30분"),  # (a) 공백만 다른 표기
        ("90분", "1시간 30분"),  # (b) 같은 길이의 다른 표기
        ("한 시간", "1시간"),  # (b) 우리말 수관형사
        ("한 시간 반", "1시간 30분"),
        ("한 번에 2시간 정도요", "2시간"),  # 문장에 섞여도 길이가 같으면 그 보기
    ],
)
async def test_typed_answers_that_mean_an_option_are_accepted(
    monkeypatch: pytest.MonkeyPatch, typed: str, expected: str
) -> None:
    """보기와 **같은 길이**면 그 보기로 받는다 — 표기가 달라도 다시 묻지 않는다.

    채점 LLM 이 다른 보기를 제안해도(여기선 늘 "30분") 길이 슬롯에서는 룰이 이긴다.
    """
    monkeypatch.setattr(aiClient, "run", _stub(normalized="30분"))
    state = _state_missing("goals.session_length")

    result = await _answer(state, "goals.session_length", typed)

    assert result.state["slot_answers"]["goals.session_length"] == {
        "type": "chip",
        "values": [expected],
    }
    assert result.state["next_slot_key"] != "goals.session_length"  # 다시 묻지 않는다


async def test_repeated_off_catalog_answer_ends_instead_of_looping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """같은 답을 계속 적으면 상한에서 best-effort 로 진행한다 — 무한 재질문 금지."""
    monkeypatch.setattr(aiClient, "run", _stub(normalized="30분"))
    state = _state_missing("goals.session_length")

    result = await _answer(state, "goals.session_length", "20분")
    for _ in range(interview.MAX_SLOT_ATTEMPTS - 1):
        assert result.state["next_slot_key"] == "goals.session_length"
        result = await _answer(result.state, "goals.session_length", "20분")

    stored = result.state["slot_answers"]["goals.session_length"]
    # 다시 묻지는 않되(닫힘) 사용자의 값으로 치지 않는다 — 보기 밖이라 스킵으로 닫는다(#499).
    assert stored == {"type": "skip", "reason": "off_options"}
    # 반올림한 숫자가 계획으로 새어 나가지 않는다 — 세션 길이는 '미입력' 으로 남고
    # 계획이 기본값을 쓴다는 걸 분량 경고가 사용자에게 그대로 말한다.
    assert interview_adapter.chip_duration_min(stored) is None


def test_off_catalog_retry_hint_does_not_blame_and_shows_the_options() -> None:
    """되묻는 이유가 '모호해서' 가 아니다 — 또렷하게 답했는데 보기 밖이었을 뿐이다."""
    hint = interview._retry_hint("goals.session_length", 1, interview._RETRY_OFF_CATALOG)
    assert "보기" in hint
    assert "모호해서가 아니라" in hint  # 기본 재질문 힌트("조금 모호했다")와 다른 문장이다
    assert "대신 고르지 마라" in hint  # 되물으면서 임의로 보기를 고르면 P2 가 되살아난다
    assert hint != interview._retry_hint("goals.session_length", 1, None)


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        (1, (interview._pending(1, interview._RETRY_OFF_CATALOG), False)),
        (2, (interview._pending(2, interview._RETRY_OFF_CATALOG), False)),
        (interview.MAX_SLOT_ATTEMPTS, ({"type": "skip", "reason": "off_options"}, True)),
    ],
)
def test_decide_storage_reasks_optioned_chip_slots(
    attempts: int, expected: tuple[dict[str, Any] | None, bool]
) -> None:
    """보기가 정해진 칩 슬롯은 '없음' 으로 닫지 않는다 — 상한까지 되묻고 그다음 진행."""
    assert (
        interview._decide_storage(
            "goals.session_length",
            "chip",
            {"type": "text", "raw": "하루종일"},
            None,
            0.1,
            attempts,
            has_chip_options=True,
        )
        == expected
    )


def test_decide_storage_still_skips_when_the_user_says_there_is_none() -> None:
    """'없어요/모르겠어요' 는 되묻지 않는다 — 스킵 의사는 유효한 답이다."""
    assert interview._decide_storage(
        "recovery.downscope_unit",
        "chip",
        {"type": "text", "raw": "잘 모르겠어요"},
        None,
        0.1,
        1,
        has_chip_options=True,
    ) == ({"type": "skip", "reason": "declined"}, True)


# ─────────────────────────────────────────────────────────────────────────────
# P3 — 탭한 칩이 LLM 폴백에 휩쓸려 사라지지 않는다 (문자열로 오는 실제 FE 기준)
# ─────────────────────────────────────────────────────────────────────────────


async def test_tapped_chip_sent_as_plain_string_survives_an_llm_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """채점 LLM 이 폴백해도 탭한 칩이 남는다 — FE 는 탭을 **문자열**로 보낸다.

    회귀: 폴백 한 번에 칩 답이 '없음'(스킵)으로 저장되고, 이월 슬롯이라 다음 인터뷰에서도
    다시 묻지 않아 학년이 영영 '미상' 이었다.
    """
    monkeypatch.setattr(aiClient, "run", _stub(normalized=None, clarity=0.0, fell_back=True))
    state = _state_missing("identity.role")

    result = await _answer(state, "identity.role", "3학년")  # dict 가 아니라 문자열이다

    assert result.state["slot_answers"]["identity.role"] == {"type": "chip", "values": ["3학년"]}
    assert result.state["next_slot_key"] != "identity.role"


# ─────────────────────────────────────────────────────────────────────────────
# P4 — 확인 카드의 주당 시간과 계획의 주당 시간이 같다
# ─────────────────────────────────────────────────────────────────────────────


def _card_and_record(answers: dict[str, Any]) -> tuple[str, int | None]:
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = answers
    outcome = interview_adapter.build_outcome(
        session_id=str(state["session_id"]),
        slot_answers=answers,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    return interview._summary_variables(state)["weekly_load"], outcome.core_goals[0].weekly_hours


_GOAL_BASE: dict[str, Any] = {
    "goals.list": {"type": "text", "raw": "캡스톤", "normalized": ["캡스톤"]},
    "goals.heaviest": {"type": "chip", "values": ["캡스톤"]},
    "goals.session_length": {"type": "chip", "values": ["30분"]},
    "goals.frequency": {"type": "chip", "values": ["매일"]},
}


def test_confirm_card_quotes_the_weekly_hours_the_plan_will_use() -> None:
    """주당 시간을 직접 답했으면 카드도 그 값을 말한다.

    회귀(실측): 카드는 "약 주 3.5시간 (한 번 30분 × 매일)" 이라 확인받고, 목표 기록은
    2시간이었다. 사용자가 [이대로 진행] 을 누른 숫자와 계획이 쓰는 숫자가 다르면 그
    확인 카드는 확인이 아니다.
    """
    card, record = _card_and_record(
        {**_GOAL_BASE, "goals.weekly_time": {"type": "chip", "values": ["2시간"]}}
    )
    assert record == 2
    assert card == "2시간"
    assert "3.5" not in card


def test_confirm_card_shows_the_multiplication_when_weekly_hours_were_not_asked() -> None:
    """주당 시간을 묻지 않은 새 흐름에서는 곱셈 결과를 그대로 되돌려준다 — 같은 값의 두 표기."""
    card, record = _card_and_record(dict(_GOAL_BASE))
    assert card == "약 주 3.5시간 (한 번 30분 × 매일)"
    assert record == interview_adapter.weekly_hours_for_plan(_GOAL_BASE) == 4  # 3.5 의 정수 반올림


def test_weekly_hours_has_one_source_of_truth() -> None:
    """목표 기록은 `weekly_hours_for_plan` 하나만 읽는다 — 두 번째 진실을 만들지 않는다."""
    for answers in (
        dict(_GOAL_BASE),
        {**_GOAL_BASE, "goals.weekly_time": {"type": "chip", "values": ["2시간"]}},
        {"goals.list": {"type": "text", "raw": "캡스톤", "normalized": ["캡스톤"]}},
    ):
        outcome = interview_adapter.build_outcome(
            session_id="s",
            slot_answers=answers,
            ambiguity_final=0.1,
            end_reason="completed",
            analysis_source="llm",
        )
        assert outcome.core_goals[0].weekly_hours == interview_adapter.weekly_hours_for_plan(
            answers
        )


def test_catalog_duration_slots_are_exactly_the_length_valued_ones() -> None:
    """길이 슬롯 판정이 카탈로그와 맞는지 — 여기가 틀리면 P2 보호가 엉뚱한 슬롯에 걸린다."""
    duration = {s.slot_key for s in PLAN_CATALOG.slots if interview_catalog.is_duration_slot(s)}
    assert duration == {
        "goals.weekly_time",
        "goals.session_length",
        "energy.focus_duration",
        "recovery.downscope_unit",
    }


# ─────────────────────────────────────────────────────────────────────────────
# P4 — 채점 LLM 의 정규화 값도 보기로 검증한다 (본 답 경로)
#
# 수확 경로는 `_coerce_normalized(slot=…)` 로 보기 밖 값을 버리는데, 답한 슬롯 자신의
# 정규화 값은 슬롯 없이 불려 LLM 이 낸 문자열이 그대로 저장됐다. "주 1~2회" 가 저장되면
# `_chip_frequency` 가 숫자를 이어 붙여 주 12회로 읽고, 한 번 2시간이면 계획이 주 24시간이 된다.
# ─────────────────────────────────────────────────────────────────────────────


async def test_llm_chip_value_outside_the_options_is_not_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """보기에 없는 값("주 1~2회")은 저장하지 않고 보기를 들고 다시 묻는다."""
    monkeypatch.setattr(aiClient, "run", _stub(normalized="주 1~2회"))
    state = _state_missing("goals.frequency")

    result = await _answer(state, "goals.frequency", "일주일에 한두 번요")

    stored = result.state["slot_answers"].get("goals.frequency")
    assert stored == interview._pending(1, interview._RETRY_OFF_CATALOG)
    assert result.state["next_slot_key"] == "goals.frequency"


async def test_llm_chip_value_in_another_spelling_is_stored_as_the_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """띄어쓰기만 다른 값("주3회")은 보기 표기("주 3회")로 저장한다 — 다시 묻지 않는다."""
    monkeypatch.setattr(aiClient, "run", _stub(normalized="주3회"))
    state = _state_missing("goals.frequency")

    result = await _answer(state, "goals.frequency", "일주일에 세 번이요")

    assert result.state["slot_answers"]["goals.frequency"] == {
        "type": "chip",
        "values": ["주 3회"],
    }
    assert result.state["next_slot_key"] != "goals.frequency"


async def test_tapped_option_survives_an_off_catalog_llm_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """보기를 그대로 보냈으면(FE 칩 탭) LLM 이 보기 밖 값을 내도 탭한 보기가 저장된다."""
    monkeypatch.setattr(aiClient, "run", _stub(normalized="주 3번"))
    state = _state_missing("goals.frequency")

    result = await _answer(state, "goals.frequency", "주 3회")

    assert result.state["slot_answers"]["goals.frequency"] == {
        "type": "chip",
        "values": ["주 3회"],
    }


async def test_llm_skip_signal_still_closes_an_optioned_chip_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM 의 '없음' 신호(빈 문자열)는 보기 검증에 걸려 사라지지 않는다 — 그대로 스킵한다."""
    monkeypatch.setattr(aiClient, "run", _stub(normalized=""))
    # 길이 슬롯이 아닌 칩이어야 한다 — 길이 슬롯은 직접 입력을 룰이 정한다(`_chip_rule_decides`).
    state = _state_missing("recovery.tone")

    result = await _answer(state, "recovery.tone", "그건 패스할게요")

    assert result.state["slot_answers"]["recovery.tone"] == {"type": "skip", "reason": "llm"}


@pytest.mark.parametrize(
    ("llm_value", "expected"),
    [
        ("주 1~2회", (interview._pending(1, interview._RETRY_OFF_CATALOG), False)),
        ("주3회", ({"type": "chip", "values": ["주 3회"]}, True)),
        (["주 3회", "주 9회"], ({"type": "chip", "values": ["주 3회"]}, True)),
    ],
)
def test_decide_storage_checks_llm_chip_values_against_the_slot_options(
    llm_value: Any, expected: tuple[dict[str, Any] | None, bool]
) -> None:
    """저장 경계에서 보기로 맞춘다 — 수확 경로(`_apply_harvested`)와 같은 규칙."""
    assert (
        interview._decide_storage(
            "goals.frequency",
            "chip",
            {"type": "text", "raw": "일주일에 몇 번"},
            llm_value,
            0.9,
            1,
            slot=PLAN_CATALOG.by_key["goals.frequency"],
            has_chip_options=True,
        )
        == expected
    )


# ─────────────────────────────────────────────────────────────────────────────
# P5 — heaviest 는 사용자가 적은 목표 중에서만 고른다 (유령 목표 방지)
#
# 보기가 런타임 보기(목표 제목)라 카탈로그 검증이 없고, LLM 이 "토익 900점" 을 "토익" 으로
# 줄이면 `_build_goals` 가 목록에 없는 heaviest 를 새 목표로 덧붙여 목표가 하나 더 생겼다.
# ─────────────────────────────────────────────────────────────────────────────

_TWO_GOALS = ["토익 900점", "캡스톤 마무리"]


def _state_choosing_heaviest() -> InterviewState:
    state = _state_missing("goals.heaviest")
    state["slot_answers"]["goals.list"] = {
        "type": "text",
        "raw": ", ".join(_TWO_GOALS),
        "normalized": list(_TWO_GOALS),
    }
    return state


async def _answer_heaviest(state: InterviewState, raw: str) -> interview_runner.TurnResult:
    """라우터처럼 heaviest 의 런타임 보기(`_question_options`)를 실어 답한다."""
    return await interview_runner.submit_and_advance(
        state=state,
        slot_key="goals.heaviest",
        answer_value=raw,
        answer_type="select",
        options=list(_TWO_GOALS),
        slot_meta=_SLOT_META,
    )


def _goal_titles(state: InterviewState) -> list[str]:
    outcome = interview_adapter.build_outcome(
        session_id="s",
        slot_answers=state["slot_answers"],
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    return [g.title for g in outcome.core_goals]


async def test_shortened_heaviest_pick_does_not_create_a_ghost_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM 이 보기를 줄여 낸 값("토익")은 받지 않는다 — 다시 묻고, 목표는 늘지 않는다."""
    monkeypatch.setattr(aiClient, "run", _stub(normalized="토익"))
    state = _state_choosing_heaviest()

    result = await _answer_heaviest(state, "토익이 제일 급해요")

    assert slot_value.is_settled(result.state["slot_answers"].get("goals.heaviest")) is False
    assert result.state["next_slot_key"] == "goals.heaviest"
    assert "토익" not in _goal_titles(result.state)


@pytest.mark.parametrize(
    ("llm_value", "raw", "expected"),
    [
        ("토익 900점", "토익이요", "토익 900점"),
        ("토익900점", "토익이요", "토익 900점"),  # 띄어쓰기만 다르면 보기 표기로
        ("캡스톤 마무리", "캡스톤이요", "캡스톤 마무리"),  # 첫 목표가 아닌 선택도 그대로 둔다
    ],
)
async def test_heaviest_pick_that_names_an_option_is_stored(
    monkeypatch: pytest.MonkeyPatch, llm_value: str, raw: str, expected: str
) -> None:
    """LLM 이 보기 중 하나를 고르면 받는다 — 목표는 적은 두 개 그대로, 그중 하나가 집중."""
    monkeypatch.setattr(aiClient, "run", _stub(normalized=llm_value))
    state = _state_choosing_heaviest()

    result = await _answer_heaviest(state, raw)

    assert result.state["slot_answers"]["goals.heaviest"] == {
        "type": "chip",
        "values": [expected],
    }
    assert _goal_titles(result.state) == _TWO_GOALS


@pytest.mark.parametrize("raw", ["토익이 제일 급해요", "잘 모르겠어요"])
async def test_heaviest_left_open_at_the_attempt_cap_falls_back_to_the_first_goal(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """상한까지 못 고르면 자유 입력을 목표 이름으로 만들지 않고 **첫 목표**를 집중으로 읽는다.

    계획 쪽 규칙(`first_plan_adapter` — is_heaviest 가 없으면 첫 목표)과 같은 대상이다.
    저장은 스킵이다 — 시스템이 고른 값은 사용자의 답이 아니다(#499,
    `tests/test_interview_skip_semantics.py`). 대체는 읽는 쪽(`resolved_heaviest`)이 한다.
    """
    monkeypatch.setattr(aiClient, "run", _stub(normalized="토익"))
    result = await _answer_heaviest(_state_choosing_heaviest(), raw)
    for _ in range(interview.MAX_SLOT_ATTEMPTS - 1):
        assert result.state["next_slot_key"] == "goals.heaviest"
        result = await _answer_heaviest(result.state, raw)

    assert result.state["slot_answers"]["goals.heaviest"]["type"] == "skip"
    assert _goal_titles(result.state) == _TWO_GOALS
    assert interview_adapter.resolved_heaviest(result.state["slot_answers"]) == _TWO_GOALS[0]
