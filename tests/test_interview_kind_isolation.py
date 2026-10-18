"""궁극목표 인터뷰(kind="ultimate") — 계획 인터뷰와의 격리 + 전용 흐름 회귀 (PR2, #6-B).

설계서(docs/ultimate-goal-mandalart-strategy.md) §2.7 함정 ①/①b/②/③/⑤/⑧ 의 실측 가드.
`_stub()`(tests/test_interview_route.py)이 schema 타입으로만 분기해 kind 와 무관하게
재사용 가능하다.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from reaction_backend.llm import aiClient
from tests.conftest import FakeInterviewRepo
from tests.test_interview_route import _stub

# ultimate 카탈로그 필수 9슬롯 — 정의 순서(interview_catalog.ULTIMATE_SLOTS)와 동일해야
# _next_required_slot 이 고르는 순서와 맞는다.
_ULTIMATE_ANSWERS: list[tuple[str, Any]] = [
    ("ultimate.statement", "메이저리그 8구단 드래프트 1순위"),
    ("ultimate.domain", ["체력·컨디션"]),
    ("ultimate.horizon", ["5년"]),
    ("ultimate.measure", "드래프트 1라운드 지명"),
    ("ultimate.success_image", "구단 유니폼을 입고 첫 공을 던지는 순간"),
    ("ultimate.identity", "매일 훈련을 거르지 않는 프로 지망생"),
    ("ultimate.current_position", "고교 3학년, 지역 대회 4강"),
    ("ultimate.pillars_hint", "구위와 멘탈"),
    ("ultimate.constraints", "부상 이력"),
]


def _answer(client: TestClient, sid: str, slot_key: str, value: Any) -> dict[str, Any]:
    # FE 처럼 직전 상태의 totalTurns 를 clientTurn 으로 보낸다(다르면 409 INTERVIEW_STALE_TURN).
    turn = client.get(f"/interview/sessions/{sid}").json()["totalTurns"]
    res = client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": slot_key, "value": value, "clientTurn": turn},
    )
    assert res.status_code == 200, res.text
    return res.json()


def _run_ultimate_interview_to_completion(client: TestClient) -> dict[str, Any]:
    start = client.post("/interview/sessions", json={"kind": "ultimate"})
    assert start.status_code == 201
    sid = start.json()["sessionId"]
    body: dict[str, Any] = {}
    for slot_key, value in _ULTIMATE_ANSWERS:
        body = _answer(client, sid, slot_key, value)
    return body


def test_start_ultimate_does_not_abandon_active_plan_session(
    client: TestClient, monkeypatch: Any
) -> None:
    """궁극목표 인터뷰 시작이 진행 중인 계획 인터뷰를 abandon 시키지 않는다 (함정 ①).

    restart-wins 는 같은 kind 안에서만 적용된다 — 다른 kind 의 진행 중 세션은 무관해야 한다.
    """
    monkeypatch.setattr(aiClient, "run", _stub())

    plan = client.post("/interview/sessions")
    assert plan.status_code == 201
    plan_sid = plan.json()["sessionId"]

    ultimate = client.post("/interview/sessions", json={"kind": "ultimate"})
    assert ultimate.status_code == 201
    assert ultimate.json()["sessionId"] != plan_sid

    # plan 세션은 여전히 진행 중 — abandoned 로 닫히지 않았다.
    still_active = client.get(f"/interview/sessions/{plan_sid}")
    assert still_active.status_code == 200
    assert still_active.json()["endReason"] is None


def test_start_plan_does_not_abandon_active_ultimate_session(
    client: TestClient, monkeypatch: Any
) -> None:
    """반대 방향 — 계획 인터뷰 시작이 진행 중인 궁극목표 인터뷰를 abandon 시키지 않는다."""
    monkeypatch.setattr(aiClient, "run", _stub())

    ultimate = client.post("/interview/sessions", json={"kind": "ultimate"})
    assert ultimate.status_code == 201
    ultimate_sid = ultimate.json()["sessionId"]

    plan = client.post("/interview/sessions")
    assert plan.status_code == 201
    assert plan.json()["sessionId"] != ultimate_sid

    still_active = client.get(f"/interview/sessions/{ultimate_sid}")
    assert still_active.status_code == 200
    assert still_active.json()["endReason"] is None


def test_restart_wins_still_works_within_same_kind(client: TestClient, monkeypatch: Any) -> None:
    """같은 kind 안에서는 여전히 restart-wins(진행 중 세션 abandon) 가 적용된다."""
    monkeypatch.setattr(aiClient, "run", _stub())

    first = client.post("/interview/sessions", json={"kind": "ultimate"})
    first_sid = first.json()["sessionId"]

    second = client.post("/interview/sessions", json={"kind": "ultimate"})
    assert second.status_code == 201
    assert second.json()["sessionId"] != first_sid

    old = client.get(f"/interview/sessions/{first_sid}")
    assert old.json()["endReason"] == "abandoned"


def test_two_kinds_concurrent_progress_does_not_409(client: TestClient, monkeypatch: Any) -> None:
    """두 kind 를 번갈아 진행해도 서로를 409 로 막지 않는다 (함정 ①b — 락이 kind 로 스코프)."""
    monkeypatch.setattr(aiClient, "run", _stub())

    plan_sid = client.post("/interview/sessions").json()["sessionId"]
    ultimate_sid = client.post("/interview/sessions", json={"kind": "ultimate"}).json()["sessionId"]

    plan_res = client.post(
        f"/interview/sessions/{plan_sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )
    assert plan_res.status_code == 200

    ultimate_res = client.post(
        f"/interview/sessions/{ultimate_sid}/answers",
        json={"slotKey": "ultimate.statement", "value": "메이저리그 진출", "clientTurn": 1},
    )
    assert ultimate_res.status_code == 200


def test_ultimate_ambiguity_score_starts_at_nine(client: TestClient, monkeypatch: Any) -> None:
    """ultimate 필수 슬롯은 9개 — plan(18개)과 분모가 다르다 (함정 ③)."""
    monkeypatch.setattr(aiClient, "run", _stub())
    res = client.post("/interview/sessions", json={"kind": "ultimate"})
    assert res.status_code == 201
    assert res.json()["ambiguityScore"] == 9


def test_ultimate_first_question_is_statement(client: TestClient, monkeypatch: Any) -> None:
    monkeypatch.setattr(aiClient, "run", _stub())
    res = client.post("/interview/sessions", json={"kind": "ultimate"})
    assert res.json()["currentQuestion"]["slotKey"] == "ultimate.statement"
    assert res.json()["currentQuestion"]["answerType"] == "text"


def test_slot_catalog_default_is_plan(client: TestClient) -> None:
    """`kind` 쿼리 없으면 기존과 완전히 동일 — plan 카탈로그(U0 기본값 무변경)."""
    res = client.get("/interview/slot-catalog")
    assert res.status_code == 200
    keys = {e["slotKey"] for e in res.json()}
    assert "identity.role" in keys
    assert not any(k.startswith("ultimate.") for k in keys)


def test_slot_catalog_kind_ultimate_returns_ultimate_slots(client: TestClient) -> None:
    res = client.get("/interview/slot-catalog?kind=ultimate")
    assert res.status_code == 200
    entries = res.json()
    keys = {e["slotKey"] for e in entries}
    assert keys == {
        "ultimate.statement",
        "ultimate.domain",
        "ultimate.horizon",
        "ultimate.measure",
        "ultimate.success_image",
        "ultimate.identity",
        "ultimate.current_position",
        "ultimate.pillars_hint",
        "ultimate.constraints",
        "ultimate.values",
        "ultimate.assets",
        "ultimate.role_model",
    }
    domain = next(e for e in entries if e["slotKey"] == "ultimate.domain")
    assert "역량" in domain["options"]


def test_slot_catalog_unknown_kind_returns_422(client: TestClient) -> None:
    """카탈로그 밖 kind 는 텍스트 폴백이 아니라 422 (함정 ⑤ — 애매한 폴백 금지)."""
    res = client.get("/interview/slot-catalog?kind=bogus")
    assert res.status_code == 422


def test_start_session_unknown_kind_returns_422(client: TestClient) -> None:
    res = client.post("/interview/sessions", json={"kind": "bogus"})
    assert res.status_code == 422


def test_ultimate_interview_completes_with_ultimate_outcome(
    client: TestClient, monkeypatch: Any
) -> None:
    """9개 필수 슬롯을 다 채우면 종료 + ultimateOutcome 동봉, outcome 은 null (U0c)."""
    monkeypatch.setattr(aiClient, "run", _stub())
    body = _run_ultimate_interview_to_completion(client)

    assert body["endReason"] == "completed"
    assert body["currentQuestion"] is None
    assert body["outcome"] is None
    assert body["ultimateOutcome"] is not None
    outcome = body["ultimateOutcome"]
    assert outcome["statement"] == "메이저리그 8구단 드래프트 1순위"
    assert outcome["domain"] == "체력·컨디션"
    assert outcome["horizonYears"] == 5
    assert outcome["measure"] == "드래프트 1라운드 지명"
    assert outcome["unresolvedSlots"] == []
    assert outcome["analysisSource"] == "llm"


def test_ultimate_finish_does_not_materialize_goals(client: TestClient, monkeypatch: Any) -> None:
    """궁극목표 세션 완료가 계획 목표 영속 경로(materialize_goals 등)를 타지 않는다 (함정 ⑧).

    회귀 배경: 완료 경로가 `result.outcome is not None` 로만 게이트돼 있는데, kind="ultimate"
    는 `result.outcome` 이 애초에 None(대신 result.ultimate_outcome)이라 이 블록이 절대
    실행되지 않는다 — 그걸 스파이로 증명한다. 잘못 타면 직전 계획 인터뷰의 proposed 목표를
    `supersede_proposed_goals(keep=[])` 가 전부 archive 해버린다.
    """
    monkeypatch.setattr(aiClient, "run", _stub())
    called: dict[str, bool] = {"materialize": False, "supersede": False}

    async def _spy_materialize(*args: Any, **kwargs: Any) -> tuple[list[Any], Any]:
        called["materialize"] = True
        return [], None

    async def _spy_supersede(*args: Any, **kwargs: Any) -> int:
        called["supersede"] = True
        return 0

    monkeypatch.setattr(
        "reaction_backend.orchestrator.first_plan_adapter.materialize_goals", _spy_materialize
    )
    monkeypatch.setattr(
        "reaction_backend.orchestrator.first_plan_adapter.supersede_proposed_goals",
        _spy_supersede,
    )

    _run_ultimate_interview_to_completion(client)

    assert called["materialize"] is False
    assert called["supersede"] is False


def test_ultimate_finish_early_also_returns_ultimate_outcome(
    client: TestClient, monkeypatch: Any
) -> None:
    """[충분해요] 조기 종료도 ultimateOutcome 을 동봉한다 (완료 경로와 대칭)."""
    monkeypatch.setattr(aiClient, "run", _stub())
    sid = client.post("/interview/sessions", json={"kind": "ultimate"}).json()["sessionId"]
    _answer(client, sid, "ultimate.statement", "우주비행사가 되는 것")

    res = client.post(f"/interview/sessions/{sid}/finish")
    assert res.status_code == 200
    body = res.json()
    assert body["endReason"] == "early_user"
    assert body["outcome"] is None
    assert body["ultimateOutcome"] is not None
    assert body["ultimateOutcome"]["statement"] == "우주비행사가 되는 것"
    assert "ultimate.measure" in body["ultimateOutcome"]["unresolvedSlots"]


def test_get_latest_finished_default_does_not_pick_up_ultimate_session(
    client: TestClient, monkeypatch: Any
) -> None:
    """`get_latest_finished()` 기본값(kind="plan")이 완료된 ultimate 세션을 시드로 잡지

    않는다(함정 ②, 가장 위험). 완료된 세션이 ultimate 뿐인 상태에서 빈 본문
    `POST /plans/generate` 를 호출하면, plan 세션이 하나도 없으므로 여전히 422 여야 한다 —
    ultimate 세션을 잘못 집어 "(미입력 목표)" 계획을 만들면 이 assert 가 200 으로 깨진다.
    """
    monkeypatch.setattr(aiClient, "run", _stub())
    _run_ultimate_interview_to_completion(client)

    res = client.post("/plans/generate", json={})
    assert res.status_code == 422
    assert res.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_second_plan_interview_carries_over_ultimate_statement(
    client: TestClient, monkeypatch: Any, fake_interview_repo: FakeInterviewRepo
) -> None:
    """궁극목표 → 계획 이월 — ultimate.* 는 다음 plan 인터뷰 시작 시 slot_answers 에 미리

    실린다(§2.6, 전량 이월). 다른 슬롯키(`goals.list` 등)로는 절대 새지 않는다 — plan
    카탈로그엔 `ultimate.*` 슬롯이 없으므로 첫 질문은 여전히 `identity.role` 이어야 한다.
    """
    monkeypatch.setattr(aiClient, "run", _stub())
    _run_ultimate_interview_to_completion(client)

    plan = client.post("/interview/sessions")
    assert plan.status_code == 201
    plan_sid = UUID(plan.json()["sessionId"])  # fake repo keys by UUID, not str

    assert plan.json()["currentQuestion"]["slotKey"] == "identity.role"

    stored = fake_interview_repo._answers[plan_sid]
    assert "ultimate.statement" in stored  # 이월된 슬롯 — 사용자에게 다시 안 묻는다
    assert stored["ultimate.statement"].value["raw"] == "메이저리그 8구단 드래프트 1순위"
    assert "goals.list" not in stored  # 다른 슬롯으로 자동 채워지지 않는다


def test_second_ultimate_interview_asks_again_from_the_statement(
    client: TestClient, monkeypatch: Any
) -> None:
    """⚠️ 궁극목표 인터뷰를 다시 열면 **처음부터 묻는다** (interview-5).

    고치기 전엔 지난 궁극목표 세션의 `ultimate.*` 전부가 새 궁극목표 세션에 이월돼 필수
    슬롯이 시작부터 다 찼고, 201 이 `currentQuestion=null`·`endReason=null` 로 왔다. FE 는
    그 상태에서 '나중에 할게요' 버튼만 그려, 만다라트 전에 나간 사용자는 궁극목표를 다시
    세울 길이 없었다.
    """
    monkeypatch.setattr(aiClient, "run", _stub())
    _run_ultimate_interview_to_completion(client)

    again = client.post("/interview/sessions", json={"kind": "ultimate"})
    assert again.status_code == 201
    body = again.json()
    assert body["currentQuestion"]["slotKey"] == "ultimate.statement"
    assert body["ambiguityScore"] == 9
    assert body["endReason"] is None


def test_next_question_finalizes_an_ultimate_session_whose_slots_are_all_filled(
    client: TestClient, monkeypatch: Any, fake_interview_repo: FakeInterviewRepo
) -> None:
    """답 제출 밖에서 필수 슬롯이 다 찬 궁극목표 세션도 재개하면 마감된다 (interview-4)."""
    monkeypatch.setattr(aiClient, "run", _stub())
    start = client.post("/interview/sessions", json={"kind": "ultimate"}).json()
    sid = UUID(start["sessionId"])
    for slot_key, value in _ULTIMATE_ANSWERS:
        stored = (
            {"type": "chip", "values": value}
            if isinstance(value, list)
            else {"type": "text", "raw": value}
        )
        asyncio.run(fake_interview_repo.upsert_slot_answer(sid, slot_key, stored, is_required=True))

    res = client.post(f"/interview/sessions/{sid}/next-question")
    assert res.status_code == 200
    body = res.json()
    assert body["endReason"] == "completed"
    assert body["currentQuestion"] is None
    assert body["ultimateOutcome"]["statement"] == "메이저리그 8구단 드래프트 1순위"
    assert body["outcome"] is None
