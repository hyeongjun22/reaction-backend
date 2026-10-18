"""Interview route (#6 배선) — mock 걷어내고 엔진+영속화 연결 검증.

ADR-0005 §7.3 패턴대로 aiClient.run 만 stub. 라우터→interview_runner→노드 경로와
interview_sessions/slot_answers 재조립·영속(FakeInterviewRepo)을 HTTP 레벨로 검증한다.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from reaction_backend.llm import RunResult, aiClient
from reaction_backend.schemas.interview import (
    AmbiguityUpdate,
    AnswerIntake,
    InterviewSummary,
    NextQuestionSchema,
)
from tests.conftest import DEMO_USER_UUID, FakeGoalRepo, FakeInterviewRepo


def _stub(
    *,
    clarity: float = 0.9,
    new_ambiguity: float = 0.9,
    suggested: tuple[str, ...] = (),
    fell_back: bool = False,
    echo_normalized: bool = False,
) -> Any:
    """aiClient.run stub — clarity 높게 두어 답을 저장, 종료는 FSM(필수 슬롯)이 운전.

    `echo_normalized=True` 면 채점 노드가 사용자 답 원문을 그대로 `normalized_value` 로
    돌려준다 — 실제 LLM 처럼 **구조화 값이 슬롯에 저장돼야** 하는 흐름(유도 슬롯 판정 등)을
    검증할 때 쓴다. 기본값(False)은 정규화 없이 룰 경로로 저장하던 기존 테스트 그대로.
    """

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        if schema is NextQuestionSchema:
            value: Any = NextQuestionSchema(
                question="다음 질문이에요",
                empathy_one_liner="좋아요",
                suggested_answers=list(suggested),
            )
        elif schema in (AmbiguityUpdate, AnswerIntake):
            # `AnswerIntake` 는 `AmbiguityUpdate` 의 상위집합이다(+ slots). 채점 전용 호출과
            # 수확이 합쳐진 호출이 **같은 스키마**를 쓰므로 여기서 갈리지 않는다.
            value = schema(
                slot_key=kwargs["variables"]["slot_key"],
                clarity_score=clarity,
                new_ambiguity=new_ambiguity,
                normalized_value=(kwargs["variables"].get("answer") if echo_normalized else None),
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


def test_start_creates_plan_kind_session(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: Any
) -> None:
    """세션 시작이 kind='plan' 행을 만든다 — interview_sessions.kind 배선 회귀 가드.

    ambiguityScore/필수 슬롯 카운트가 기존과 동일하다는 것만으론 kind 인자가 실제로
    끝까지 전달됐는지 보장하지 않는다(값이 하나뿐이면 안 넘겨도 우연히 같아 보인다) —
    저장된 행에서 직접 확인한다.
    """
    monkeypatch.setattr(aiClient, "run", _stub())

    res = client.post("/interview/sessions")
    assert res.status_code == 201

    row = next(iter(fake_interview_repo._sessions.values()))
    assert row.kind == "plan"


def test_start_returns_first_question(client: TestClient, monkeypatch: Any) -> None:
    """세션 시작 → FSM 첫 필수 슬롯(identity.role) 질문 + chip 보기 동봉."""
    monkeypatch.setattr(aiClient, "run", _stub())

    res = client.post("/interview/sessions")
    assert res.status_code == 201
    body = res.json()

    assert body["currentQuestion"]["slotKey"] == "identity.role"
    assert body["currentQuestion"]["answerType"] == "chip"
    assert "1학년" in body["currentQuestion"]["options"]  # 카탈로그 보기 매핑
    assert (
        body["ambiguityScore"] == 18
    )  # 미해결 필수 슬롯 수 (goals.frequency 추가 #per-goal-frequency)
    assert body["endReason"] is None


def test_submit_advances_and_persists(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: Any
) -> None:
    """답 제출 → 다음 슬롯으로 진행 + DB(slot_answers) 영속."""
    monkeypatch.setattr(aiClient, "run", _stub())

    start = client.post("/interview/sessions").json()
    sid = start["sessionId"]

    res = client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["currentQuestion"]["slotKey"] == "identity.season"  # 다음 필수 슬롯
    assert body["ambiguityScore"] == 17  # 하나 채워져 감소 (필수 18개, #per-goal-frequency)

    # 영속화 검증 — fake repo 에 세션 1개 + identity.role 답 저장
    assert len(fake_interview_repo._sessions) == 1
    stored = fake_interview_repo._answers[next(iter(fake_interview_repo._sessions))]
    assert "identity.role" in stored
    assert stored["identity.role"].value == {"type": "chip", "values": ["3학년"]}


def test_submit_does_not_finish_until_required_slots_are_filled(
    client: TestClient, monkeypatch: Any
) -> None:
    """LLM ambiguity 가 낮아도 미해결 필수 슬롯이 남아 있으면 다음 질문을 계속한다."""
    monkeypatch.setattr(aiClient, "run", _stub(new_ambiguity=0.1))

    start = client.post("/interview/sessions").json()
    sid = start["sessionId"]

    res = client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ambiguityScore"] == 17
    assert body["endReason"] is None
    assert body["currentQuestion"]["slotKey"] == "identity.season"


def _answer_for(question: dict[str, Any]) -> Any:
    """현재 질문의 answerType 에 맞는 답 1개 — 카탈로그가 바뀌어도 도는 드라이버용.

    chip/select 는 첫 보기를 고른다. `goals.session_length`·`goals.frequency` 의 첫 보기가
    각각 '15분'·'매일' 이라 주당 시간이 실제로 유도된다(= '몰아서·상관없음' 이 아니다).
    """
    kind = question["answerType"]
    if kind in {"chip", "select"}:
        return [question["options"][0]]
    if kind == "time_range":
        return {"start": "09:00", "end": "23:00"}
    if kind == "date_picker":
        return "2099-12-31"  # 미래 — 지난 마감 재질문(#231) 경로를 타지 않게
    return "테스트로 적어보는 자유 서술 답변이에요"


def test_completed_interview_reports_zero_remaining_slots(
    client: TestClient, monkeypatch: Any
) -> None:
    """필수 슬롯을 다 채우면 `ambiguityScore` 가 **0** 이 된다 — 진행바가 100%에 닿는다.

    회귀 가드(유도 슬롯 누수): `goals.weekly_time` 은 세션 길이 × 빈도로 유도되면 FSM 이
    아예 묻지 않는다(`is_slot_needed`). 그런데 FE 지표만 그 규칙을 안 보고 세던 시절엔,
    인터뷰가 `completed` 로 끝나고 `unresolved_slots` 도 비었는데 `ambiguityScore` 만
    1 로 남아 진행바가 17/18 에 멈췄다. 세 판정이 한 함수(`open_required_keys`)를 쓰는지
    HTTP 레벨에서 확인한다 — '묻지 않은 슬롯은 세지도 않는다'.
    """
    monkeypatch.setattr(aiClient, "run", _stub(echo_normalized=True))

    body = client.post("/interview/sessions").json()
    sid = body["sessionId"]

    asked: list[str] = []
    while body["currentQuestion"] is not None:
        question = body["currentQuestion"]
        asked.append(question["slotKey"])
        body = client.post(
            f"/interview/sessions/{sid}/answers",
            json={
                "slotKey": question["slotKey"],
                "value": _answer_for(question),
                "clientTurn": len(asked),
            },
        ).json()
        assert len(asked) <= 40, f"인터뷰가 끝나지 않는다 — 마지막 슬롯 {question['slotKey']}"

    assert body["endReason"] == "completed"
    assert body["ambiguityScore"] == 0  # ← 고치기 전에는 1 (goals.weekly_time 이 남아 있었다)
    assert body["outcome"]["unresolvedSlots"] == []
    # 유도된 슬롯은 묻지 않았고, 그래서 세지도 않았다.
    assert "goals.weekly_time" not in asked


def test_finish_returns_summary_and_outcome(client: TestClient, monkeypatch: Any) -> None:
    """[충분해요] 조기 종료 → end_reason + summary(S03) + outcome(First Plan 시드)."""
    monkeypatch.setattr(aiClient, "run", _stub())

    sid = client.post("/interview/sessions").json()["sessionId"]
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )

    res = client.post(f"/interview/sessions/{sid}/finish")
    assert res.status_code == 200
    body = res.json()
    assert body["endReason"] == "early_user"
    assert body["currentQuestion"] is None
    assert body["summary"]["confirmQuestion"]  # S03 확인 카드
    assert body["outcome"]["sessionId"] == sid  # First Plan 시드
    assert "identity.role" not in body["outcome"]["unresolvedSlots"]  # 채운 슬롯
    assert body["outcome"]["analysisSource"] == "llm"  # fallback 없음 → llm


def test_profile_persist_failure_does_not_break_finish(
    client: TestClient, monkeypatch: Any
) -> None:
    """프로필 영속이 예외를 던져도 인터뷰 완료(finish)는 200 으로 성공한다 (#130 best-effort).

    프로필 메모리 영속은 부가 기능이라, 실패가 절대 안 깨져야 하는 finalize 경로를 막으면 안 된다.
    """
    monkeypatch.setattr(aiClient, "run", _stub())

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("profile write blew up")

    monkeypatch.setattr(
        "reaction_backend.orchestrator.profile_memory.persist_profile_from_outcome", _boom
    )

    sid = client.post("/interview/sessions").json()["sessionId"]
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )
    res = client.post(f"/interview/sessions/{sid}/finish")
    assert res.status_code == 200  # 프로필 실패에도 인터뷰 완료 성공
    body = res.json()
    assert body["endReason"] == "early_user"
    assert body["outcome"]["sessionId"] == sid


def test_finish_materializes_extracted_goals(client: TestClient, monkeypatch: Any) -> None:
    """[충분해요] 조기 종료도 추출한 목표를 materialize 한다 (완료 경로 submit_answer 와 대칭).

    회귀: 예전엔 finish_session 에 materialize_goals 가 없어, [충분해요] 로 끝낸 사용자는
    목표 분류 화면이 빈 상태가 됐다 (사용자 테스트로 발견).
    """
    monkeypatch.setattr(aiClient, "run", _stub())
    seen: dict[str, Any] = {}

    async def _spy(
        session: Any, *, user_id: Any, core_goals: Any, status: str = "proposed"
    ) -> tuple[list[Any], Any]:
        seen["called"] = True
        seen["n"] = len(core_goals)
        seen["status"] = status
        return [], None

    async def _spy_supersede(session: Any, *, user_id: Any, keep: Any, **kw: Any) -> int:
        seen["superseded"] = True
        return 0

    monkeypatch.setattr("reaction_backend.orchestrator.first_plan_adapter.materialize_goals", _spy)
    monkeypatch.setattr(
        "reaction_backend.orchestrator.first_plan_adapter.supersede_proposed_goals",
        _spy_supersede,
    )

    sid = client.post("/interview/sessions").json()["sessionId"]
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "goals.list", "value": "포트폴리오 사이트, 토익", "clientTurn": 1},
    )
    res = client.post(f"/interview/sessions/{sid}/finish")
    assert res.status_code == 200
    assert seen.get("called") is True  # 조기 종료도 목표 영속 시도
    assert seen.get("n", 0) >= 1
    # 인터뷰가 만드는 목표는 **잠정(proposed)** — 계획 승인 전이라 진짜 목표가 아니다.
    assert seen.get("status") == "proposed"
    # 지난 인터뷰의 잠정 목표는 이번 인터뷰가 대체한다(누적 방지).
    assert seen.get("superseded") is True


def test_used_fallback_persists_to_analysis_source(client: TestClient, monkeypatch: Any) -> None:
    """이전 턴에 LLM 룰 fallback 이 있었으면, 재조립을 넘어 outcome.analysisSource='rule'.

    used_fallback 은 턴마다 재조립되는 transient 라 세션 컬럼에 OR 누적 영속돼야 전체 인터뷰
    기준으로 정확하다 (마지막 턴만 보고 'llm' 로 잘못 찍히던 것을 고침)."""
    monkeypatch.setattr(aiClient, "run", _stub(fell_back=True))  # 시작 턴부터 fallback
    sid = client.post("/interview/sessions").json()["sessionId"]

    # 이후 턴은 정상(fallback 아님) — 그래도 앞선 fallback 이 영속돼야 한다
    monkeypatch.setattr(aiClient, "run", _stub(fell_back=False))
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )
    body = client.post(f"/interview/sessions/{sid}/finish").json()
    assert body["outcome"]["analysisSource"] == "rule"

    # 재조회(이미 종료된 세션)도 동일하게 영속된 플래그 기준
    again = client.get(f"/interview/sessions/{sid}").json()
    assert again["outcome"]["analysisSource"] == "rule"


def test_critical_slot_reask_persists_attempts_across_db(
    client: TestClient, monkeypatch: Any
) -> None:
    """핵심 슬롯 재질문 시 시도 횟수가 DB 왕복(재조립)을 넘어 누적돼, 상한 후 진행한다.

    매 턴 라우터가 slot_answers 를 DB(fake)에 영속하고 다시 재조립하는데, pending 마커가
    그 왕복을 견뎌야 3회차에서 best-effort 로 goals.list 를 벗어난다(무한 재질문 방지)."""

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        if schema is NextQuestionSchema:
            value: Any = NextQuestionSchema(question="다음 질문", empathy_one_liner="좋아요")
        else:  # AmbiguityUpdate — goals.list 는 계속 애매(재질문), 나머지는 유효
            slot = kwargs["variables"]["slot_key"]
            value = AmbiguityUpdate(
                slot_key=slot,
                clarity_score=0.1 if slot == "goals.list" else 0.9,
                new_ambiguity=0.5,
            )
        return RunResult(
            value=value,
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)

    sid = client.post("/interview/sessions").json()["sessionId"]

    def answer(slot: str, val: Any) -> dict[str, Any]:
        turn = client.get(f"/interview/sessions/{sid}").json()["totalTurns"]
        return client.post(
            f"/interview/sessions/{sid}/answers",
            json={"slotKey": slot, "value": val, "clientTurn": turn},
        ).json()

    # 프로필 chip 슬롯(정체성 2 + 전역 집중 시간대)을 유효하게 채워 goals.list 에 도달
    answer("identity.role", ["3학년"])
    answer("identity.season", ["방학"])
    body = answer("time.peak_window", ["오전"])
    assert body["currentQuestion"]["slotKey"] == "goals.list"
    amb_at_goals = body["ambiguityScore"]

    # 1·2회차: 애매한 자유서술 → 재질문 (ambiguityScore 그대로, 여전히 goals.list)
    body = answer("goals.list", "음 그냥 이것저것")
    assert body["currentQuestion"]["slotKey"] == "goals.list"
    assert body["ambiguityScore"] == amb_at_goals  # pending — 미충족 유지
    body = answer("goals.list", "여전히 잘 정리가 안돼")
    assert body["currentQuestion"]["slotKey"] == "goals.list"
    assert body["ambiguityScore"] == amb_at_goals

    # 3회차(상한): DB 를 넘어 누적된 시도 → best-effort 채택하고 다음 슬롯으로.
    # 목표가 1개(단일)라 goals.heaviest 가 자동 채워짐 → heaviest 질문을 건너뛰고
    # 남은 필수 슬롯이 2개 감소, 다음은 goals.current_level (#B — heaviest 다음 슬롯).
    body = answer("goals.list", "그럼 프로젝트 하나 할래")
    assert body["currentQuestion"]["slotKey"] == "goals.current_level"
    assert body["ambiguityScore"] == amb_at_goals - 2  # goals.list + goals.heaviest 충족


def test_suggested_answers_only_for_free_text_slots(client: TestClient, monkeypatch: Any) -> None:
    """LLM 추천 답변 카드(suggestedAnswers)는 고정 보기가 없는 자유서술 슬롯에서만 노출된다.

    chip(카탈로그 보기 있음) → suggestedAnswers 빈 배열 / goals.list(자유서술) → LLM 추천 노출."""
    monkeypatch.setattr(aiClient, "run", _stub(suggested=("캡스톤 마무리", "토익 900점")))

    # 첫 질문 identity.role — chip 보기가 있으니 추천 카드는 비어야
    start = client.post("/interview/sessions").json()
    assert start["currentQuestion"]["slotKey"] == "identity.role"
    assert start["currentQuestion"]["options"]  # 카탈로그 보기 존재
    assert start["currentQuestion"]["suggestedAnswers"] == []

    sid = start["sessionId"]
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.season", "value": ["방학"], "clientTurn": 2},
    )
    body = client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "time.peak_window", "value": ["오전"], "clientTurn": 3},
    ).json()

    # goals.list — 자유서술(카탈로그 보기 없음) → LLM 추천 카드 노출
    assert body["currentQuestion"]["slotKey"] == "goals.list"
    assert body["currentQuestion"]["options"] == []
    assert body["currentQuestion"]["suggestedAnswers"] == ["캡스톤 마무리", "토익 900점"]


def test_placeholder_suggested_answers_are_dropped(client: TestClient, monkeypatch: Any) -> None:
    """LLM 이 이름을 몰라 채운 자리표시자 카드("ㅇㅇ 출판사", "ㅁㅁㅁ 채널")는 노출되지 않는다.

    카드는 탭 한 번이 곧 사용자의 답이라, 지어낸 틀이 슬롯에 저장되고 계획까지 흘러간다
    (2026-09-18 배포 미러에서 goals.materials 카드 4장 중 2장이 이 모양이었다).
    """
    monkeypatch.setattr(
        aiClient,
        "run",
        _stub(
            suggested=(
                "정보처리기사 실기 수험서 (ㅇㅇ 출판사)",
                "인터넷 강의 (유튜브 ㅁㅁㅁ 채널)",
                "○○ 스터디 자료",
                "스터디 그룹에서 공유받은 자료",
            )
        ),
    )
    start = client.post("/interview/sessions").json()
    sid = start["sessionId"]
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.season", "value": ["방학"], "clientTurn": 2},
    )
    body = client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "time.peak_window", "value": ["오전"], "clientTurn": 3},
    ).json()

    assert body["currentQuestion"]["slotKey"] == "goals.list"
    assert body["currentQuestion"]["suggestedAnswers"] == ["스터디 그룹에서 공유받은 자료"]


def test_slot_catalog_includes_options(client: TestClient) -> None:
    """슬롯 카탈로그가 chip 보기를 노출(텍스트 슬롯은 빈 배열)."""
    res = client.get("/interview/slot-catalog")
    assert res.status_code == 200
    by_key = {e["slotKey"]: e for e in res.json()}
    assert "1학년" in by_key["identity.role"]["options"]
    assert by_key["identity.major"]["options"] == []  # text 슬롯


def test_unknown_session_returns_404(client: TestClient, monkeypatch: Any) -> None:
    monkeypatch.setattr(aiClient, "run", _stub())
    res = client.post(
        f"/interview/sessions/{uuid4()}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )
    assert res.status_code == 404


def test_start_with_active_session_abandons_old_and_creates(
    client: TestClient, monkeypatch: Any
) -> None:
    """재시작 승리(restart-wins) — 진행 중 세션이 있어도 abandoned 로 닫고 새로 시작(201).

    FE 가 sessionId 를 잃어도 재시작만으로 복구된다 (이전 409 는 영구 차단이었다).
    """
    monkeypatch.setattr(aiClient, "run", _stub())

    first = client.post("/interview/sessions")
    assert first.status_code == 201  # 첫 세션은 생성
    first_sid = first.json()["sessionId"]

    second = client.post("/interview/sessions")  # 진행 중 세션 존재 → abandon 후 새로
    assert second.status_code == 201
    second_sid = second.json()["sessionId"]
    assert second_sid != first_sid

    old = client.get(f"/interview/sessions/{first_sid}")
    assert old.status_code == 200
    assert old.json()["endReason"] == "abandoned"


def test_start_after_finish_is_allowed(client: TestClient, monkeypatch: Any) -> None:
    """종료(end_reason 채워짐)된 세션은 활성으로 치지 않음 — 새 세션 시작 가능."""
    monkeypatch.setattr(aiClient, "run", _stub())

    sid = client.post("/interview/sessions").json()["sessionId"]
    assert client.post(f"/interview/sessions/{sid}/finish").status_code == 200

    again = client.post("/interview/sessions")  # 이전 세션 종료됨 → 충돌 없음
    assert again.status_code == 201


def test_concurrent_access_returns_409(client: TestClient, monkeypatch: Any) -> None:
    """동시성 lock — advisory lock 미획득 시 409 AGENT_CONCURRENT_ACCESS (ADR-0005 §7.6)."""
    from collections.abc import AsyncIterator

    from reaction_backend.db.session import get_db
    from tests.conftest import _FakeSession  # noqa: PLC0415

    monkeypatch.setattr(aiClient, "run", _stub())

    async def _locked_session() -> AsyncIterator[_FakeSession]:
        # 다른 디바이스가 lock 보유 중 → pg_try_advisory_lock 이 False.
        yield _FakeSession(lock_acquired=False)

    client.app.dependency_overrides[get_db] = _locked_session  # type: ignore[attr-defined]

    res = client.post("/interview/sessions")
    assert res.status_code == 409
    assert res.json()["code"] == "AGENT_CONCURRENT_ACCESS"


# ─────────────────────────────────────────────────────────────────────────────
# 목표 하나만 계획하는 인터뷰 (#442)
#
# 목표 관리의 "미계획" 카드에서 들어온다. 대상이 이미 정해졌으므로 `goals.list` ·
# `goals.heaviest` 를 **묻지 않는다** — 그 둘을 다시 물으면 사용자가 누른 버튼의 약속을
# 어기는 것이고, 다른 목표를 말하면 **엉뚱한 목표의 계획**이 나온다.
# ─────────────────────────────────────────────────────────────────────────────


def _seed_goal(fake_goal_repo: FakeGoalRepo, *, title: str, status: str = "proposed") -> str:
    from reaction_backend.db.models.goal import Goal as GoalModel

    g = GoalModel()
    g.id = uuid4()
    g.user_id = DEMO_USER_UUID
    g.title = title
    g.category = "study"
    g.goal_tier = "focus"
    g.priority_level = 1
    g.status = status
    g.is_ultimate = False
    g.archived_at = None
    fake_goal_repo._items[g.id] = g
    return f"goal_{g.id}"


def test_start_with_goal_id_does_not_ask_which_goal(
    client: TestClient, fake_goal_repo: FakeGoalRepo, monkeypatch: Any
) -> None:
    """⚠️ **이 테스트가 #442 의 전부다.** 대상이 정해졌으면 목표를 다시 묻지 않는다."""
    monkeypatch.setattr(aiClient, "run", _stub())
    goal_id = _seed_goal(fake_goal_repo, title="교환학생 파견 확정 받기")

    res = client.post("/interview/sessions", json={"goalId": goal_id})
    assert res.status_code == 201

    body = res.json()
    assert body["currentQuestion"]["slotKey"] not in ("goals.list", "goals.heaviest")


def test_start_with_goal_id_seeds_that_goal_as_the_target(
    client: TestClient,
    fake_interview_repo: FakeInterviewRepo,
    fake_goal_repo: FakeGoalRepo,
    monkeypatch: Any,
) -> None:
    """시드 값이 **사용자가 직접 답했을 때와 같은 모양**이어야 한다.

    모양이 다르면 뒤의 정규화·추출 경로가 이 시드만 다르게 읽어, 인터뷰는 멀쩡한데
    계획이 엉뚱한 목표로 나간다.
    """
    monkeypatch.setattr(aiClient, "run", _stub())
    goal_id = _seed_goal(fake_goal_repo, title="교환학생 파견 확정 받기")

    client.post("/interview/sessions", json={"goalId": goal_id})

    row = next(iter(fake_interview_repo._sessions.values()))
    answers = {k: a.value for k, a in fake_interview_repo._answers.get(row.id, {}).items()}
    assert answers["goals.list"] == {
        "type": "text",
        "raw": "교환학생 파견 확정 받기",
        "normalized": ["교환학생 파견 확정 받기"],
    }
    assert answers["goals.heaviest"] == {"type": "chip", "values": ["교환학생 파견 확정 받기"]}


def test_start_without_goal_id_still_asks_for_goals(client: TestClient, monkeypatch: Any) -> None:
    """온보딩·전체 재인터뷰는 그대로 — 목표부터 묻는다."""
    monkeypatch.setattr(aiClient, "run", _stub())

    res = client.post("/interview/sessions")
    assert res.status_code == 201
    # 목표를 지정하지 않았으니 명료성은 아직 0에 가깝고(아무것도 안 답했다),
    # `goals.list` 는 **열려 있어야** 한다 — 시드가 새면 여기서 잡힌다.
    assert res.json()["ambiguityScore"] > 0


def test_start_rejects_an_unknown_goal(client: TestClient, monkeypatch: Any) -> None:
    """남의 목표나 없는 목표로 인터뷰를 열 수 없다."""
    monkeypatch.setattr(aiClient, "run", _stub())

    res = client.post("/interview/sessions", json={"goalId": f"goal_{uuid4()}"})
    assert res.status_code == 404


def test_start_rejects_a_malformed_goal_id(client: TestClient, monkeypatch: Any) -> None:
    monkeypatch.setattr(aiClient, "run", _stub())

    assert client.post("/interview/sessions", json={"goalId": "not-a-goal"}).status_code == 404
    assert client.post("/interview/sessions", json={"goalId": "goal_zzz"}).status_code == 404


def test_start_rejects_an_archived_goal(
    client: TestClient, fake_goal_repo: FakeGoalRepo, monkeypatch: Any
) -> None:
    """보관된 목표는 화면에 없다 — 그걸로 인터뷰를 열면 유령 목표가 되살아난다."""
    from datetime import UTC, datetime

    monkeypatch.setattr(aiClient, "run", _stub())
    goal_id = _seed_goal(fake_goal_repo, title="지난 학기 목표")
    stored = next(iter(fake_goal_repo._items.values()))
    stored.archived_at = datetime.now(UTC)

    assert client.post("/interview/sessions", json={"goalId": goal_id}).status_code == 404


def test_ultimate_kind_cannot_target_a_goal(
    client: TestClient, fake_goal_repo: FakeGoalRepo, monkeypatch: Any
) -> None:
    """궁극목표 인터뷰엔 `goals.*` 슬롯이 없다 — 조용히 무시하지 말고 거절한다."""
    monkeypatch.setattr(aiClient, "run", _stub())
    goal_id = _seed_goal(fake_goal_repo, title="교환학생")

    res = client.post("/interview/sessions", json={"kind": "ultimate", "goalId": goal_id})
    assert res.status_code == 400


def test_carry_over_never_seeds_goal_slots() -> None:
    """이월 시드에 `goals.*` 가 없어야 목표 지정 시드가 가려지지 않는다.

    ⚠️ 이건 **주석으로만 있던 전제**였다. 변이 테스트에서 "시드를 이월 앞에 두기" 가
    안 잡혀 파보니, 위험이 없어서가 아니라 **이월에 `goals.*` 가 애초에 없어서**였다.
    그 전제를 여기서 못 박는다 — 나중에 누가 `goals.list` 를 이월 대상에 넣으면
    목표 지정 인터뷰가 조용히 엉뚱한 목표를 묻게 된다.
    """
    from reaction_backend.orchestrator import interview_adapter, ultimate_adapter

    for keys in (
        interview_adapter.CARRY_OVER_SLOT_KEYS,
        ultimate_adapter.ULTIMATE_CARRY_OVER_SLOT_KEYS,
    ):
        assert not [k for k in keys if k.startswith("goals.")]


def test_drop_placeholder_cards_keeps_real_names() -> None:
    """실제 이름·영문 약어는 남긴다 — 자리표시자 패턴만 뺀다."""
    from reaction_backend.orchestrator.interview import drop_placeholder_cards

    cards = [
        "OO대학교 도서관 자료",
        "XX 강의",
        "시나공 정보처리기사 실기",
        "OOP 개념 정리 노트",
        "BOOK 챌린지",
        "ㅋㅋ 그냥 해볼래요",
    ]
    assert drop_placeholder_cards(cards) == [
        "시나공 정보처리기사 실기",
        "OOP 개념 정리 노트",
        "BOOK 챌린지",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 답 제출 밖에서 마지막 슬롯이 채워진 세션의 재개 (interview-4)
#
# 자료 확정(`POST /plans/materials/spec-confirm`)은 `goals.materials` 를 슬롯에 **직접**
# 쓴다. 재인터뷰에서는 활동창·회복 슬롯이 이월돼 materials 가 마지막 빈 슬롯이 되는데,
# 그걸 채운 뒤 재개(`next-question`)하면 물을 슬롯 없이 빈 질문만 돌아왔다 — FE 는
# "다음 질문을 받지 못했어요" 를 되풀이했고 인터뷰는 영영 끝나지 않았다.
# ─────────────────────────────────────────────────────────────────────────────


def _fill_all_but_materials(client: TestClient) -> str:
    """plan 세션을 `goals.materials` 직전까지 채운다(실제 답 제출 경로)."""
    body = client.post("/interview/sessions").json()
    sid = str(body["sessionId"])
    turn = 0
    while body["currentQuestion"] is not None:
        question = body["currentQuestion"]
        if question["slotKey"] == "goals.materials":
            return sid
        turn += 1
        body = client.post(
            f"/interview/sessions/{sid}/answers",
            json={
                "slotKey": question["slotKey"],
                "value": _answer_for(question),
                "clientTurn": turn,
            },
        ).json()
        assert turn <= 40
    raise AssertionError("goals.materials 에 닿기 전에 인터뷰가 끝났다")


def _spec_confirm_directly(repo: FakeInterviewRepo, sid: str) -> None:
    """spec-confirm 이 하는 일 — 답 제출을 거치지 않고 슬롯에 바로 쓴다."""
    asyncio.run(
        repo.upsert_slot_answer(
            UUID(sid),
            "goals.materials",
            {"type": "spec", "items": [{"kind": "book", "title": "토익 보카"}]},
            is_required=True,
        )
    )


def test_next_question_finalizes_when_the_last_slot_was_filled_out_of_band(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: Any
) -> None:
    monkeypatch.setattr(aiClient, "run", _stub(echo_normalized=True))
    sid = _fill_all_but_materials(client)
    # 활동창·회복 슬롯은 재인터뷰에서 이월된다 — materials 를 마지막 빈 슬롯으로 만든다.
    row_id = UUID(sid)
    for slot_key, value in (
        ("time.activity_window", {"type": "range", "start": "09:00", "end": "23:00"}),
        ("recovery.tone", {"type": "chip", "values": ["담백"]}),
        ("recovery.rest_ok", {"type": "chip", "values": ["네"]}),
        ("recovery.downscope_unit", {"type": "chip", "values": ["10분"]}),
    ):
        asyncio.run(
            fake_interview_repo.upsert_slot_answer(row_id, slot_key, value, is_required=True)
        )
    _spec_confirm_directly(fake_interview_repo, sid)

    res = client.post(f"/interview/sessions/{sid}/next-question")
    assert res.status_code == 200
    body = res.json()
    assert body["endReason"] == "completed"
    assert body["currentQuestion"] is None
    assert body["outcome"] is not None
    assert body["outcome"]["unresolvedSlots"] == []
    assert fake_interview_repo._sessions[row_id].end_reason == "completed"

    # 다시 불러도 같은 종료 응답이다(재마감·중복 영속 없음).
    again = client.post(f"/interview/sessions/{sid}/next-question").json()
    assert again["endReason"] == "completed"
    assert again["currentQuestion"] is None
    assert again["outcome"] is not None


def test_next_question_still_asks_when_slots_remain(client: TestClient, monkeypatch: Any) -> None:
    """재개는 여전히 재개다 — 빈 슬롯이 남아 있으면 마감하지 않고 그 슬롯을 묻는다."""
    monkeypatch.setattr(aiClient, "run", _stub())
    sid = client.post("/interview/sessions").json()["sessionId"]

    body = client.post(f"/interview/sessions/{sid}/next-question").json()
    assert body["endReason"] is None
    assert body["currentQuestion"]["slotKey"] == "identity.role"


# ─────────────────────────────────────────────────────────────────────────────
# 인터뷰 종료가 답하지 않은 칸을 프로필에 쓰지 않는다 (interview-6·8)
# ─────────────────────────────────────────────────────────────────────────────


def _complete_plan_interview(client: TestClient) -> dict[str, Any]:
    body = client.post("/interview/sessions").json()
    sid = body["sessionId"]
    turn = 0
    while body["currentQuestion"] is not None:
        question = body["currentQuestion"]
        turn += 1
        body = client.post(
            f"/interview/sessions/{sid}/answers",
            json={
                "slotKey": question["slotKey"],
                "value": _answer_for(question),
                "clientTurn": turn,
            },
        ).json()
        assert turn <= 40
    return body


def test_reinterview_keeps_profile_values_edited_in_settings(
    client: TestClient, fake_profile_repo: Any, monkeypatch: Any
) -> None:
    """⚠️ 내 정보에서 고친 집중 길이·최소 단위를 재인터뷰가 되돌리지 않는다 (interview-8).

    45분·20분은 칩 보기에 없어 시드로 못 옮긴다. 고치기 전엔 그때 지난 인터뷰 원답(5분)이
    대신 이월돼, 재인터뷰를 열고 [충분해요] 만 눌러도 최소 단위가 5분으로, 집중 길이는
    `or 30` 기본값으로 덮였다 — 설정 화면이 "인터뷰를 다시 하지 않아도 반영돼요" 라고
    약속한 값이었다.
    """
    from reaction_backend.orchestrator import profile_memory

    monkeypatch.setattr(aiClient, "run", _stub(echo_normalized=True))
    monkeypatch.setattr(profile_memory, "ProfileRepo", lambda _session: fake_profile_repo)

    first = _complete_plan_interview(client)
    assert first["endReason"] == "completed"
    edited = client.patch("/settings/profile", json={"attentionSpan": 45, "downscopeUnitMin": 20})
    assert edited.status_code == 200, edited.text

    sid = client.post("/interview/sessions").json()["sessionId"]
    assert client.post(f"/interview/sessions/{sid}/finish").status_code == 200

    profile = client.get("/settings/profile").json()
    assert profile["behavioral"]["attentionSpan"] == 45
    assert profile["downscopeUnitMin"] == 20


def test_reinterview_asks_again_what_an_early_exit_left_unanswered(
    client: TestClient, fake_profile_repo: Any, monkeypatch: Any
) -> None:
    """몇 문항만 답하고 [충분해요] 한 뒤 다시 시작하면, 안 답한 칸은 **다시 묻는다** (interview-6).

    고치기 전엔 그 종료가 피크 '변동'·톤 '담백'·휴식 '네'·최소 단위 10분을 프로필에 썼고,
    다음 세션이 그걸 시드로 읽어 네 칸을 묻지 않았다(ambiguityScore 17 → 13).
    """
    from reaction_backend.orchestrator import profile_memory

    monkeypatch.setattr(aiClient, "run", _stub())
    monkeypatch.setattr(profile_memory, "ProfileRepo", lambda _session: fake_profile_repo)

    sid = client.post("/interview/sessions").json()["sessionId"]
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )
    assert client.post(f"/interview/sessions/{sid}/finish").status_code == 200

    again = client.post("/interview/sessions").json()
    # 역할 하나만 이월된다 — 나머지 필수 17칸은 그대로 열려 있다.
    assert again["ambiguityScore"] == 17
    assert client.get("/settings/profile").json()["interaction"] is None


# ─────────────────────────────────────────────────────────────────────────────
# 온보딩 진행 상태 — 목표를 남긴 계획 인터뷰 종료가 WELCOME 을 벗어나게 한다 (critic-3)
# ─────────────────────────────────────────────────────────────────────────────


def test_finishing_the_onboarding_interview_moves_to_goal_confirm(
    client: TestClient, demo_user_orm: Any, monkeypatch: Any
) -> None:
    """⚠️ 인터뷰를 끝낸 온보딩 사용자는 ONBOARDING_CONFIRM 이 된다 (critic-3).

    고치기 전엔 WELCOME 에서 나가는 전이가 아무 데도 없어, 계획 생성을 기다리다 앱을 껐다
    켜면 소개 화면과 새 인터뷰부터 다시 해야 했다. 두 번 불러도(종료 응답 재조회) 그대로다.
    """
    monkeypatch.setattr(aiClient, "run", _stub(echo_normalized=True))
    demo_user_orm.onboarding_state = "WELCOME"

    body = _complete_plan_interview(client)
    assert body["endReason"] == "completed"
    assert demo_user_orm.onboarding_state == "ONBOARDING_CONFIRM"
    assert client.get("/auth/me").json()["onboardingState"] == "ONBOARDING_CONFIRM"

    again = client.post(f"/interview/sessions/{body['sessionId']}/finish")
    assert again.status_code == 200
    assert demo_user_orm.onboarding_state == "ONBOARDING_CONFIRM"


def test_early_finish_with_goals_also_moves_to_goal_confirm(
    client: TestClient, demo_user_orm: Any, monkeypatch: Any
) -> None:
    """[충분해요] 로 끝내도 목표가 저장됐으면 분류 단계에서 이어간다."""
    monkeypatch.setattr(aiClient, "run", _stub(echo_normalized=True))
    demo_user_orm.onboarding_state = "ONBOARDING_INTERVIEW"

    body = client.post("/interview/sessions").json()
    sid = body["sessionId"]
    turn, answered = 0, ""
    while answered != "goals.list":  # 목표를 적은 직후 [충분해요]
        question = body["currentQuestion"]
        answered = question["slotKey"]
        turn += 1
        body = client.post(
            f"/interview/sessions/{sid}/answers",
            json={"slotKey": answered, "value": _answer_for(question), "clientTurn": turn},
        ).json()
        assert turn <= 10
    assert client.post(f"/interview/sessions/{sid}/finish").json()["endReason"] == "early_user"
    assert demo_user_orm.onboarding_state == "ONBOARDING_CONFIRM"


def test_early_finish_without_goals_keeps_welcome(
    client: TestClient, demo_user_orm: Any, monkeypatch: Any
) -> None:
    """목표 없이 끝난 인터뷰(역할만 답하고 종료)는 이어갈 게 없다 — 상태를 올리지 않는다."""
    monkeypatch.setattr(aiClient, "run", _stub())
    demo_user_orm.onboarding_state = "WELCOME"

    sid = client.post("/interview/sessions").json()["sessionId"]
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )
    assert client.post(f"/interview/sessions/{sid}/finish").status_code == 200
    assert demo_user_orm.onboarding_state == "WELCOME"


def test_reinterview_by_active_user_keeps_active(
    client: TestClient, demo_user_orm: Any, monkeypatch: Any
) -> None:
    """온보딩을 마친 사용자의 재인터뷰는 상태를 되돌리지 않는다."""
    monkeypatch.setattr(aiClient, "run", _stub(echo_normalized=True))
    demo_user_orm.onboarding_state = "ACTIVE"

    assert _complete_plan_interview(client)["endReason"] == "completed"
    assert demo_user_orm.onboarding_state == "ACTIVE"


def test_ultimate_interview_does_not_touch_onboarding_state(
    client: TestClient, demo_user_orm: Any, monkeypatch: Any
) -> None:
    """궁극목표 인터뷰 종료는 계획 온보딩 단계와 무관하다."""
    monkeypatch.setattr(aiClient, "run", _stub())
    demo_user_orm.onboarding_state = "WELCOME"

    sid = client.post("/interview/sessions", json={"kind": "ultimate"}).json()["sessionId"]
    assert client.post(f"/interview/sessions/{sid}/finish").status_code == 200
    assert demo_user_orm.onboarding_state == "WELCOME"


def test_reinterview_asks_the_season_again(
    client: TestClient, fake_profile_repo: Any, monkeypatch: Any
) -> None:
    """학기 중/방학은 재인터뷰에서 다시 묻는다 — 역할은 그대로 이월된다 (interview-19).

    고치기 전엔 8월에 '방학' 이라 답하면 9월 재인터뷰에서도 묻지 않고, 고칠 곳도 없어 계획이
    계속 방학 맥락으로 만들어졌다.
    """
    from reaction_backend.orchestrator import profile_memory

    monkeypatch.setattr(aiClient, "run", _stub(echo_normalized=True))
    monkeypatch.setattr(profile_memory, "ProfileRepo", lambda _session: fake_profile_repo)

    assert _complete_plan_interview(client)["endReason"] == "completed"

    again = client.post("/interview/sessions").json()
    assert again["currentQuestion"]["slotKey"] == "identity.season"  # 역할은 이월, 학기는 다시


def test_start_is_refused_once_the_daily_interview_limit_is_reached(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: Any
) -> None:
    """일일 인터뷰 한도에 닿았으면 시작부터 429 로 알린다 (interview-14).

    고치기 전엔 시작만 한도 검사를 건너뛰어, 첫 질문(LLM 호출)은 받고 그 뒤 모든 답이 429 로
    실패했다. 거절된 시작은 세션을 만들지도, 진행 중 세션을 닫지도 않는다.
    """
    from reaction_backend.safety import endpoint_rate_limit

    async def _exhausted(*_args: Any, **kwargs: Any) -> None:
        raise endpoint_rate_limit.EndpointCallLimitExceeded(kwargs["module"], used=60, limit=60)

    monkeypatch.setattr(aiClient, "run", _stub())
    monkeypatch.setattr(endpoint_rate_limit, "check", _exhausted)

    res = client.post("/interview/sessions")
    assert res.status_code == 429
    assert res.json()["code"] == "RATE_LIMIT_DAILY_CALLS_EXCEEDED"
    assert res.headers["Retry-After"].isdigit()
    assert fake_interview_repo._sessions == {}


def test_only_slots_that_keep_every_pick_are_marked_multiple(client: TestClient) -> None:
    """`multiple` 은 어댑터가 목록 전체를 쓰는 슬롯에만 true 다 (interview-17).

    FE 는 모든 칩 질문에 "여러 개 골라도 돼요" 를 띄웠는데, 역할·가장 무거운 목표·빈도 등은
    첫 값만 쓰여 두 번째 선택이 말없이 버려졌다. 이 플래그로 단일/복수 선택을 가른다.
    """
    plan = client.get("/interview/slot-catalog").json()
    assert {e["slotKey"] for e in plan if e["multiple"]} == {"time.peak_window"}
    ultimate = client.get("/interview/slot-catalog", params={"kind": "ultimate"}).json()
    assert {e["slotKey"] for e in ultimate if e["multiple"]} == {"ultimate.values"}


def test_multi_select_slots_are_read_as_lists_by_the_adapters() -> None:
    """복수 선택으로 내보낸 슬롯은 어댑터가 실제로 **모든 값**을 쓴다 — 첫 값만 쓰는 슬롯을
    복수로 표시하면 같은 사고가 반대로 난다."""
    from reaction_backend.orchestrator import interview_adapter, ultimate_adapter

    outcome = interview_adapter.build_outcome(
        session_id="s1",
        slot_answers={"time.peak_window": {"type": "chip", "values": ["저녁", "심야"]}},
        ambiguity_final=0.5,
        end_reason="early_user",
        analysis_source="rule",
    )
    assert outcome.availability.peak_window == ["저녁", "심야"]
    ultimate = ultimate_adapter.build_ultimate_outcome(
        session_id="s1",
        slot_answers={"ultimate.values": {"type": "chip", "values": ["성장", "자유"]}},
        ambiguity_final=0.5,
        end_reason="early_user",
        analysis_source="rule",
    )
    assert ultimate.values == ["성장", "자유"]


def test_single_choice_question_is_not_multiple(client: TestClient, monkeypatch: Any) -> None:
    """첫 질문(역할)은 단일 선택이다 — `currentQuestion.multiple=false`."""
    monkeypatch.setattr(aiClient, "run", _stub())
    question = client.post("/interview/sessions").json()["currentQuestion"]
    assert question["slotKey"] == "identity.role"
    assert question["multiple"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 재개 톤 · clientTurn 중복 제출 · 카탈로그 밖 slotKey
# ─────────────────────────────────────────────────────────────────────────────


def _recording_stub(calls: list[dict[str, Any]]) -> Any:
    """`_stub()` 그대로 동작하면서 aiClient.run 호출 인자를 기록한다."""
    inner = _stub()

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        calls.append(kwargs)
        return await inner(**kwargs)

    return stub_run


def test_resume_question_uses_the_users_tone(client: TestClient, monkeypatch: Any) -> None:
    """재개(next-question)로 만든 질문도 시작·답 제출과 같은 사용자 톤으로 만든다.

    회귀: 라우트가 노드를 직접 부르며 config 에 tone_mode 를 안 실어, 재개 질문만 톤 없이 나갔다.
    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(aiClient, "run", _recording_stub(calls))
    sid = client.post("/interview/sessions").json()["sessionId"]
    calls.clear()

    res = client.post(f"/interview/sessions/{sid}/next-question")

    assert res.status_code == 200
    asked = [c for c in calls if c["schema"] is NextQuestionSchema]
    assert [c["tone_mode"] for c in asked] == ["gentle"]  # conftest 데모 사용자의 톤


def test_resubmitted_answer_with_a_stale_client_turn_is_rejected(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: Any
) -> None:
    """이미 처리된 턴의 답(같은 clientTurn 재전송)은 409 — 다시 채점·질문 생성하지 않는다.

    `clientTurn` 은 클라이언트가 직전 응답의 `totalTurns` 를 그대로 보내는 값이다
    (FE GoalIntakeScreen·UltimateGoalInterviewScreen). 맞지 않으면 그 사이 다른 요청이 턴을
    진행시킨 것이라, 처리하면 같은 답이 두 번 채점되고 LLM 호출이 두 배가 된다.
    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(aiClient, "run", _recording_stub(calls))
    start = client.post("/interview/sessions").json()
    sid = start["sessionId"]
    body = {"slotKey": "identity.role", "value": ["3학년"], "clientTurn": start["totalTurns"]}
    first = client.post(f"/interview/sessions/{sid}/answers", json=body)
    assert first.status_code == 200
    calls.clear()

    again = client.post(f"/interview/sessions/{sid}/answers", json=body)

    assert again.status_code == 409
    assert again.json()["code"] == "INTERVIEW_STALE_TURN"
    assert calls == []  # 채점·질문 생성 LLM 을 부르지 않았다
    row = fake_interview_repo._sessions[UUID(sid)]
    assert row.total_turns == first.json()["totalTurns"]  # 턴이 더 진행되지 않았다


def test_answer_with_a_client_turn_ahead_of_the_session_is_rejected(
    client: TestClient, monkeypatch: Any
) -> None:
    """서버보다 **앞선** clientTurn 도 거절한다 — 같은지 보는 검사지, 뒤처졌는지만 보는 게 아니다."""
    monkeypatch.setattr(aiClient, "run", _stub())
    start = client.post("/interview/sessions").json()

    res = client.post(
        f"/interview/sessions/{start['sessionId']}/answers",
        json={
            "slotKey": "identity.role",
            "value": ["3학년"],
            "clientTurn": start["totalTurns"] + 1,
        },
    )

    assert res.status_code == 409
    assert res.json()["code"] == "INTERVIEW_STALE_TURN"


def test_answer_with_the_current_client_turn_is_accepted(
    client: TestClient, monkeypatch: Any
) -> None:
    """직전 응답의 totalTurns 를 보내면 받는다 — 위 검사가 정상 흐름을 막지 않는다."""
    monkeypatch.setattr(aiClient, "run", _stub())
    start = client.post("/interview/sessions").json()
    sid = start["sessionId"]
    first = client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": start["totalTurns"]},
    ).json()

    second = client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.season", "value": ["방학"], "clientTurn": first["totalTurns"]},
    )

    assert second.status_code == 200


@pytest.mark.parametrize(
    ("kind", "slot_key"),
    [
        ("plan", "identity.nickname"),  # 어느 카탈로그에도 없는 키
        ("ultimate", "identity.role"),  # 계획 인터뷰 키 — 궁극목표 세션의 카탈로그 밖
    ],
)
def test_answer_for_a_slot_outside_the_session_catalog_is_rejected(
    client: TestClient,
    fake_interview_repo: FakeInterviewRepo,
    monkeypatch: Any,
    kind: str,
    slot_key: str,
) -> None:
    """세션 kind 의 카탈로그에 없는 slotKey 는 422 — 그 키로 슬롯 답을 저장하지 않는다."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(aiClient, "run", _recording_stub(calls))
    start = client.post("/interview/sessions", json={"kind": kind}).json()
    sid = start["sessionId"]
    calls.clear()

    res = client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": slot_key, "value": "아무 값", "clientTurn": start["totalTurns"]},
    )

    assert res.status_code == 422
    assert res.json()["code"] == "COMMON_VALIDATION_ERROR"
    assert res.json()["field"] == "slotKey"
    assert slot_key not in fake_interview_repo._answers.get(UUID(sid), {})
    assert calls == []
