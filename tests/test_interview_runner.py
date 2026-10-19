"""Interview turn driver (#6) — Rule-based Slot FSM end-to-end.

ADR-0005 §7.3 패턴: aiClient.run 만 stub, 노드/러너는 일반 async 함수라 직접 호출.
- 필수 슬롯 수집 → Analysis Confirm(summary) → InterviewOutcome 까지 끊김 없이 연결
- clarity 미달 시 같은 슬롯 재질문 (저장 안 함)
- [충분해요] 조기 종료 → 빈 슬롯 default + unresolved_slots 기록
- LLM fallback 이면 outcome.analysis_source='rule'
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from reaction_backend.domain import slot_value
from reaction_backend.llm import RunResult, aiClient
from reaction_backend.orchestrator import interview_catalog, interview_runner
from reaction_backend.schemas.interview import (
    AmbiguityUpdate,
    AnswerIntake,
    InterviewOutcome,
    InterviewSummary,
    NextQuestionSchema,
)

# 슬롯 타입별 대표 답 (slot answerType 대로 클라이언트가 보낼 법한 raw 값)
_RANGE_SLOTS = {"time.activity_window"}
_CHIP_SLOTS = {
    "identity.role",
    "identity.season",
    "time.peak_window",
    "recovery.tone",
    "recovery.rest_ok",
    "recovery.downscope_unit",
}


def _answer_for(slot_key: str) -> Any:
    if slot_key in _RANGE_SLOTS:
        return {"start": "09:00", "end": "23:00"}
    if slot_key in _CHIP_SLOTS:
        if slot_key == "time.peak_window":
            return ["오전"]
        if slot_key == "recovery.downscope_unit":
            return ["10분"]  # 분 단위 select
        return ["네"]
    if slot_key == "goals.list":
        return "캡스톤, 토익"
    if slot_key == "goals.heaviest":
        return "캡스톤"
    if slot_key == "goals.deadlines":
        return "2026-06-20"
    return "테스트 답변"


def _stub(*, clarity: float = 0.9, new_ambiguity: float = 0.1, fell_back: bool = False):
    """aiClient.run stub — clarity 만 조절해 저장 여부를 제어.

    new_ambiguity 는 일부러 **낮게**(0.1) 둔다: float 모호도는 더 이상 종료를 운전하지
    않으므로(=명료성 100% 보장), 낮은 값에서도 필수 슬롯 완료(FSM)로만 마감돼야 한다.
    이 기본값이 '모호도 조기 종료' 회귀를 잡는다."""

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        if schema is NextQuestionSchema:
            value: Any = NextQuestionSchema(
                question="다음 질문",
                empathy_one_liner="좋아요",
            )
        elif schema in (AmbiguityUpdate, AnswerIntake):
            # `AnswerIntake` 는 `AmbiguityUpdate` 의 상위집합이다(+ slots). 채점 전용 호출과
            # 수확이 합쳐진 호출이 **같은 스키마**를 쓰므로 여기서 갈리지 않는다.
            value = schema(
                slot_key=kwargs["variables"]["slot_key"],
                clarity_score=clarity,
                new_ambiguity=new_ambiguity,
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


async def test_fsm_collects_required_slots_then_confirms(monkeypatch: pytest.MonkeyPatch) -> None:
    """필수 슬롯을 순서대로 채우면 Analysis Confirm + InterviewOutcome 으로 마감."""
    monkeypatch.setattr(aiClient, "run", _stub(clarity=0.9))

    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    assert result.question is not None
    assert result.state["next_slot_key"] == "identity.role"  # FSM 첫 필수 슬롯

    guard = 0
    while not result.done and guard < 30:
        slot = result.state["next_slot_key"]
        assert slot is not None
        result = await interview_runner.submit_and_advance(
            state=result.state, slot_key=slot, answer_value=_answer_for(slot)
        )
        guard += 1

    assert result.done is True
    # 회귀: 낮은 모호도(stub new_ambiguity=0.1)에도 float 임계로 조기 종료하지 않고
    # 필수 슬롯 완료(FSM)로 마감 → 명료성 100%(남은 필수 슬롯 0).
    assert result.end_reason == "completed"
    assert isinstance(result.summary, InterviewSummary)
    assert isinstance(result.outcome, InterviewOutcome)
    assert result.outcome.unresolved_slots == []  # 필수 슬롯 모두 충족 = 명료성 100%
    assert all(
        k in result.state["slot_answers"] for k in interview_catalog.PLAN_CATALOG.required_keys
    )
    assert result.outcome.analysis_source == "llm"
    # 핵심 목표/가용 시간/선호 방식이 실제로 채워졌는지
    assert {g.title for g in result.outcome.core_goals} == {"캡스톤", "토익"}
    assert result.outcome.availability.activity_window.start == "09:00"
    assert result.outcome.preferences.recovery_tone == "네"


async def test_seed_answers_skip_durable_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """재인터뷰: 지난 인터뷰의 지속형 슬롯을 seed 로 넘기면 FSM 이 건너뛰고 첫 목표 슬롯부터 묻는다.

    #reduce-reask — 학년·시간·회복 등 '너에 대한' 정보를 다시 묻지 않는다. seed 에 없는
    목표 관련(goals.*)만 남으므로 첫 질문은 goals.list.
    """
    monkeypatch.setattr(aiClient, "run", _stub(clarity=0.9))
    seed = {
        "identity.role": {"type": "chip", "values": ["대3"]},
        "identity.season": {"type": "chip", "values": ["학기중"]},
        "time.activity_window": {"type": "range", "start": "09:00", "end": "23:00"},
        "time.peak_window": {"type": "chip", "values": ["오전"]},
        "recovery.tone": {"type": "chip", "values": ["담백"]},
        "recovery.rest_ok": {"type": "chip", "values": ["네"]},
        "recovery.downscope_unit": {"type": "chip", "values": ["10분"]},
    }
    result = await interview_runner.start_interview(
        session_id=uuid4(), user_id=uuid4(), seed_answers=seed
    )
    # 학년(identity.role) 을 다시 묻지 않고 첫 미충족 필수(goals.list)부터.
    assert result.state["next_slot_key"] == "goals.list"
    # 남은 필수 슬롯이 7개(지속형) 만큼 줄었다 — 18 - 7 = 11 (목표 관련만 남음).
    remaining = sum(
        1
        for k in interview_catalog.PLAN_CATALOG.required_keys
        if not slot_value.is_settled(result.state["slot_answers"].get(k))
    )
    assert remaining == 11
    # 이어받은 값은 그대로 상태에 있다(끝까지 가면 outcome 에 실린다).
    assert result.state["slot_answers"]["identity.role"] == {"type": "chip", "values": ["대3"]}


async def test_low_clarity_reasks_same_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """자유 서술(text) 답의 clarity 가 임계 미만이면 저장하지 않고 같은 슬롯을 다시 묻는다."""
    monkeypatch.setattr(aiClient, "run", _stub(clarity=0.1))

    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    first_slot = result.state["next_slot_key"]

    # text 답 (자유 서술) → clarity 게이트 적용 → 저장 안 됨
    result = await interview_runner.submit_and_advance(
        state=result.state, slot_key=first_slot, answer_value={"type": "text", "raw": "음..."}
    )
    assert result.done is False
    assert result.state["next_slot_key"] == first_slot  # 미충족 → 같은 슬롯 재질문
    # 실제 답으로 안 채워짐 — pending(재질문 대기) 마커라 _is_filled 는 False
    assert not slot_value.is_settled(result.state["slot_answers"].get(first_slot))


async def test_constrained_answer_stored_despite_low_clarity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chip/range 제약 입력은 LLM clarity 가 낮아도(0.1) 선택 자체로 저장된다.

    회귀: 실 Gemini 가 "1학년" 같은 유효 chip 을 0.3 으로 낮게 채점해도 영구 재질문에
    빠지지 않고 다음 슬롯으로 진행해야 한다(명료성이 0% 에 갇히던 버그).

    ⚠️ **값을 뚝뚝히 단언해야 한다.** 예전엔 `first_slot in slot_answers` 만 봤고, 그건 이
    테스트가 약속하는 것을 안 지켰다 — `_decide_storage` 의 `is_constrained` 단축을 지우면
    답이 스킵(`{"type":"skip",…}`)으로 들어가는데, **키는 그대로 있고**
    다음 슬롯으로도 넘어가서 둘 다 통과했다. 즉 사용자의 "1학년" 이 **통째로 버려지는**
    회귀를 이 테스트가 놓치고 있었다(#448 감사에서 변이로 확인)."""
    monkeypatch.setattr(aiClient, "run", _stub(clarity=0.1))  # clarity 임계 미만

    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    first_slot = result.state["next_slot_key"]
    assert first_slot == "identity.role"  # chip 슬롯

    result = await interview_runner.submit_and_advance(
        state=result.state,
        slot_key=first_slot,
        answer_value=["1학년"],  # chip
    )
    # 키 존재가 아니라 **고른 값 그대로** 들어가 있어야 한다.
    assert result.state["slot_answers"][first_slot] == {"type": "chip", "values": ["1학년"]}
    assert result.state["next_slot_key"] != first_slot  # 다음 슬롯으로 진행


async def test_free_text_normalized_into_structured_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """채팅 자유서술 답을 LLM normalized_value 로 구조화해 저장한다 (chip 슬롯).

    회귀: FE 는 답을 전부 string 으로 보내는데(채팅), chip 슬롯을 text 로 저장하면
    build_outcome 이 못 읽어 default 로 떨어졌다. 이제 LLM 이 자유서술("컴공 3학년이야")을
    보기값("3학년")으로 정규화해 chip 으로 저장 → 시드에 실제 답이 반영된다."""

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        if schema is NextQuestionSchema:
            return RunResult(
                value=NextQuestionSchema(
                    question="다음 질문",
                    empathy_one_liner="좋아요",
                ),
                fell_back=False,
                reason=None,
                prompt_id=kwargs["prompt_id"],
                prompt_version="v1",
            )
        # 채점 전용 호출과 수확이 합쳐진 호출이 **같은 스키마**(상위집합)를 쓴다.
        assert schema in (AmbiguityUpdate, AnswerIntake)
        slot = kwargs["variables"]["slot_key"]
        # 자유서술이라 clarity 는 낮게(0.2) 주지만, 구조화 값은 정확히 추출.
        return RunResult(
            value=AmbiguityUpdate(
                slot_key=slot, clarity_score=0.2, new_ambiguity=0.5, normalized_value="3학년"
            ),
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)

    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    result = await interview_runner.submit_and_advance(
        state=result.state,
        slot_key="identity.role",
        answer_value="나는 컴공 3학년이야",  # 자유서술 string (채팅 입력과 동일)
        answer_type="chip",
        options=["1학년", "2학년", "3학년", "4학년"],
    )
    # 자유서술이 chip 구조로 정규화 저장됨 → build_outcome 이 읽을 수 있음
    assert result.state["slot_answers"]["identity.role"] == {"type": "chip", "values": ["3학년"]}
    assert result.state["next_slot_key"] != "identity.role"  # 다음 슬롯으로 진행


async def test_skip_answer_advances_without_reask(monkeypatch: pytest.MonkeyPatch) -> None:
    """'없어/모르겠어' 처럼 항목 없음 의사(LLM 이 normalized_value="")면 스킵 저장 후 진행한다.

    회귀: text 슬롯에 '없어'를 답하면 clarity 낮게 채점돼 같은 질문을 무한 재질문하던 문제.
    이제 빈 문자열 신호를 스킵 마커로 저장 → 다음 슬롯으로 넘어간다."""

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        if schema is NextQuestionSchema:
            value: Any = NextQuestionSchema(
                question="다음 질문",
                empathy_one_liner="네",
            )
        else:  # AmbiguityUpdate — '없어' → 스킵 신호(빈 문자열), clarity 는 낮게
            value = AmbiguityUpdate(
                slot_key=kwargs["variables"]["slot_key"],
                clarity_score=0.1,
                new_ambiguity=0.5,
                normalized_value="",
            )
        return RunResult(
            value=value,
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)

    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    first_slot = result.state["next_slot_key"]
    result = await interview_runner.submit_and_advance(
        state=result.state,
        slot_key=first_slot,
        answer_value="딱히 없어",  # 항목 없음/건너뛰기 의사
        answer_type="text",
    )
    # 스킵 마커로 채워지고(빈 text) 다음 슬롯으로 진행 — 같은 슬롯 재질문 안 함
    assert result.state["slot_answers"][first_slot] == {"type": "skip", "reason": "declined"}
    assert result.state["next_slot_key"] != first_slot


async def test_critical_slot_rejects_skip_then_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """핵심 목표 슬롯(goals.list)은 '없어'(skip)를 받지 않고 재질문, 상한 후 best-effort 채택.

    비핵심은 '없어'면 바로 넘어가지만, 계획의 근간인 goals.list 는 유효 답을 유도해야 한다.
    2회 재질문(총 3시도)에도 유효하지 않으면 마지막 비지 않은 답을 채택하고 진행(무한 루프 방지)."""

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        if schema is NextQuestionSchema:
            value: Any = NextQuestionSchema(
                question="다음 질문",
                empathy_one_liner="네",
            )
        else:
            slot = kwargs["variables"]["slot_key"]
            # goals.list 는 계속 skip 신호(""), 나머지 슬롯은 유효로 채워 진행시킨다.
            norm = "" if slot == "goals.list" else None
            clarity = 0.1 if slot == "goals.list" else 0.9
            value = AmbiguityUpdate(
                slot_key=slot, clarity_score=clarity, new_ambiguity=0.5, normalized_value=norm
            )
        return RunResult(
            value=value,
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)

    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    # 프로필 슬롯(정체성 2 + 전역 집중 시간대)을 유효 chip 으로 채워 goals.list 에 도달
    result = await interview_runner.submit_and_advance(
        state=result.state, slot_key="identity.role", answer_value=["3학년"], answer_type="chip"
    )
    result = await interview_runner.submit_and_advance(
        state=result.state, slot_key="identity.season", answer_value=["방학"], answer_type="chip"
    )
    result = await interview_runner.submit_and_advance(
        state=result.state, slot_key="time.peak_window", answer_value=["오전"], answer_type="chip"
    )
    assert result.state["next_slot_key"] == "goals.list"

    # 1회차 '없어' → 스킵 거부, 재질문 (pending)
    result = await interview_runner.submit_and_advance(
        state=result.state, slot_key="goals.list", answer_value="없어", answer_type="text"
    )
    assert result.state["next_slot_key"] == "goals.list"
    assert not slot_value.is_settled(result.state["slot_answers"].get("goals.list"))

    # 2회차 재질문에도 skip → 여전히 goals.list
    result = await interview_runner.submit_and_advance(
        state=result.state, slot_key="goals.list", answer_value="없어", answer_type="text"
    )
    assert result.state["next_slot_key"] == "goals.list"

    # 3회차(상한) → 마지막 비지 않은 답을 best-effort 로 채택하고 다음 슬롯으로
    result = await interview_runner.submit_and_advance(
        state=result.state,
        slot_key="goals.list",
        answer_value="그냥 뭐라도 할래",
        answer_type="text",
    )
    assert result.state["slot_answers"]["goals.list"] == {"type": "text", "raw": "그냥 뭐라도 할래"}
    assert result.state["next_slot_key"] != "goals.list"


async def _advance_to_goals_list(monkeypatch: pytest.MonkeyPatch) -> Any:
    """프로필 슬롯(정체성 + 전역 집중 시간대)을 채우고 goals.list 지점까지 진행."""
    monkeypatch.setattr(aiClient, "run", _stub(clarity=0.9))
    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    for slot in ("identity.role", "identity.season", "time.peak_window"):
        assert result.state["next_slot_key"] == slot
        result = await interview_runner.submit_and_advance(
            state=result.state, slot_key=slot, answer_value=_answer_for(slot)
        )
    assert result.state["next_slot_key"] == "goals.list"
    return result


async def test_single_goal_autofills_heaviest_and_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """목표가 1개면 goals.heaviest 를 자동 채우고 자명한 select 질문을 건너뛴다.

    회귀: heaviest 는 goals.list 파생 select 라, 목표가 하나면 보기가 직전 답을 그대로
    반복(echo)한다 → 그 하나를 heaviest 로 자동 확정하고 다음 슬롯으로 넘어가야 한다.
    """
    result = await _advance_to_goals_list(monkeypatch)
    result = await interview_runner.submit_and_advance(
        state=result.state,
        slot_key="goals.list",
        answer_value="영어 공부 시작하기",  # 단일 목표
        answer_type="text",
    )
    assert result.state["slot_answers"]["goals.heaviest"] == {
        "type": "chip",
        "values": ["영어 공부 시작하기"],
    }
    assert result.state["next_slot_key"] == "goals.current_level"  # heaviest 건너뛰고 다음(#B)


async def test_multiple_goals_still_asks_heaviest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """목표가 2개 이상이면 heaviest 를 자동 채우지 않고 정상적으로 질문한다."""
    result = await _advance_to_goals_list(monkeypatch)
    result = await interview_runner.submit_and_advance(
        state=result.state,
        slot_key="goals.list",
        answer_value="캡스톤, 토익",  # 복수 목표
        answer_type="text",
    )
    assert not slot_value.is_settled(result.state["slot_answers"].get("goals.heaviest"))
    assert result.state["next_slot_key"] == "goals.heaviest"  # 정상 질문


async def test_finish_early_defaults_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """[충분해요] — 빈 필수 슬롯은 default + unresolved_slots, end_reason=early_user."""
    monkeypatch.setattr(aiClient, "run", _stub())

    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    result = await interview_runner.finish_early(state=result.state)

    assert result.done is True
    assert result.end_reason == "early_user"
    assert result.outcome is not None
    assert "goals.list" in result.outcome.unresolved_slots
    assert len(result.outcome.core_goals) >= 1  # min_length 계약 유지


async def test_fallback_marks_rule_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """노드가 룰 fallback 되면 outcome.analysis_source='rule' 로 표기."""
    monkeypatch.setattr(aiClient, "run", _stub(fell_back=True))

    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    result = await interview_runner.finish_early(state=result.state)

    assert result.state["used_fallback"] is True
    assert result.outcome is not None
    assert result.outcome.analysis_source == "rule"


def test_required_sequence_covers_three_pillars() -> None:
    """FSM 시퀀스가 핵심 목표·가용 시간·선호 방식 세 기둥을 모두 포함하는지."""
    seq = interview_catalog.PLAN_CATALOG.required_keys
    assert any(k.startswith("goals.") for k in seq)
    assert any(k.startswith("time.") for k in seq)
    assert any(k.startswith("recovery.") for k in seq)


def _fallback_stub() -> Any:
    """aiClient 가 **실제로 폴백했을 때**와 같은 값 — 각 노드의 `fallback()` 을 그대로 부른다.

    타임아웃·레이트리밋·전역 토큰 예산 소진·금지어 차단 어느 쪽이든 aiClient 는 이 경로다.
    """

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        return RunResult(
            value=kwargs["fallback"](),
            fell_back=True,
            reason="timeout",
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    return stub_run


@pytest.mark.parametrize(
    ("slot_key", "text", "want"),
    [
        ("identity.role", "3학년", ["3학년"]),
        ("time.peak_window", "저녁, 심야", ["저녁", "심야"]),
        ("recovery.tone", "따뜻", ["따뜻"]),
        ("goals.session_length", "1시간 30분", ["1시간 30분"]),
    ],
)
async def test_chip_text_answer_survives_llm_fallback(
    monkeypatch: pytest.MonkeyPatch, slot_key: str, text: str, want: list[str]
) -> None:
    """⚠️ LLM 이 폴백해도 **보기 그대로 온 칩 답은 저장된다** (interview-3).

    FE 는 칩을 탭해도 문자열("3학년")로 보낸다. 폴백 채점은 정규화 값이 없어, 고치기 전엔
    제약 슬롯이라는 이유로 곧장 스킵 마커(`{"type":"text","raw":""}`)가 저장됐다 — 사용자는
    답했는데 '없음' 이 됐고, 이월 슬롯이라 다음 인터뷰도 그 칸을 다시 묻지 않았다.
    """
    monkeypatch.setattr(aiClient, "run", _fallback_stub())
    start = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    slot = interview_catalog.PLAN_CATALOG.by_key[slot_key]

    result = await interview_runner.submit_and_advance(
        state=start.state,
        slot_key=slot_key,
        answer_value=text,
        answer_type=slot.answer_type,
        options=list(slot.options),
    )

    assert result.state["slot_answers"][slot_key] == {"type": "chip", "values": want}


async def test_heaviest_text_answer_survives_llm_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """런타임 보기(`goals.heaviest`)는 라우터가 넘긴 보기와 정확히 같을 때만 받는다.

    고치기 전엔 pending 으로 남아 같은 룰 질문을 세 번 되풀이했다.
    """
    monkeypatch.setattr(aiClient, "run", _fallback_stub())
    start = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    state = {
        **start.state,
        "slot_answers": {
            "goals.list": {"type": "text", "raw": "토익, 운동", "normalized": ["토익", "운동"]}
        },
    }

    result = await interview_runner.submit_and_advance(
        state=state,  # type: ignore[arg-type]
        slot_key="goals.heaviest",
        answer_value="토익",
        answer_type="select",
        options=["토익", "운동"],
    )

    assert result.state["slot_answers"]["goals.heaviest"] == {"type": "chip", "values": ["토익"]}
    assert result.state["next_slot_key"] != "goals.heaviest"


async def test_off_menu_text_is_not_forced_into_a_chip(monkeypatch: pytest.MonkeyPatch) -> None:
    """보기에 없는 말은 룰이 억지로 맞히지 않는다 — 지어낸 값으로 슬롯을 닫지 않는다."""
    monkeypatch.setattr(aiClient, "run", _fallback_stub())
    start = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    slot = interview_catalog.PLAN_CATALOG.by_key["identity.role"]

    result = await interview_runner.submit_and_advance(
        state=start.state,
        slot_key="identity.role",
        answer_value="휴학하고 알바 중이에요",
        answer_type=slot.answer_type,
        options=list(slot.options),
    )

    assert result.state["slot_answers"]["identity.role"].get("type") != "chip"


async def test_start_with_every_required_slot_seeded_finishes_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시드만으로 필수 슬롯이 다 찼으면 빈 질문 대신 곧바로 마감한다 (interview-5 방어선).

    빈 질문으로 시작하면 FE 는 답할 칸도, 끝났다는 신호도 없는 화면에 갇힌다.
    """
    monkeypatch.setattr(aiClient, "run", _stub())
    seed = {
        key: (
            {"type": "chip", "values": ["5년"]}
            if key in {"ultimate.domain", "ultimate.horizon"}
            else {"type": "text", "raw": "테스트 답"}
        )
        for key in interview_catalog.ULTIMATE_CATALOG.required_keys
    }

    result = await interview_runner.start_interview(
        session_id=uuid4(), user_id=uuid4(), kind="ultimate", seed_answers=seed
    )

    assert result.done is True
    assert result.end_reason == "completed"
    assert result.ultimate_outcome is not None
