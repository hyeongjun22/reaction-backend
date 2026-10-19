"""프로필 메모리 (#A-1·A-2) — 인터뷰 지속형 선호 → Policy Snapshot 레이어 영속 + 설정 편집.

3층: ① 매핑 순수 함수(한국어 칩→enum/버킷) ② GET/PATCH /settings/profile 라우트.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from reaction_backend.orchestrator import profile_memory as pm
from reaction_backend.orchestrator.interview_catalog import PLAN_CATALOG


def test_seed_slots_from_profile_reverses_editable_fields() -> None:
    """설정에서 수정 가능한 프로필 필드 → 재인터뷰 시드 슬롯값으로 역매핑(#reduce-reask)."""
    beh = cast(Any, SimpleNamespace(energy_cycle="evening", attention_span=50))
    inter = cast(Any, SimpleNamespace(recovery_tone="gentle"))
    seed = pm.seed_slots_from_profile(
        behavioral=beh,
        interaction=inter,
        focus_mode_prefs={"downscope_unit_min": 15, "rest_ok": False},
    )
    assert seed["time.peak_window"] == {"type": "chip", "values": ["저녁"]}
    assert seed["energy.focus_duration"] == {"type": "chip", "values": ["50분"]}
    assert seed["recovery.tone"] == {"type": "chip", "values": ["따뜻"]}
    assert seed["recovery.downscope_unit"] == {"type": "chip", "values": ["15분"]}
    assert seed["recovery.rest_ok"] == {"type": "chip", "values": ["아니오"]}
    # 활동창(preferred_*)은 설정 편집 대상이 아니라 프로필로 만들지 않는다 → 호출자가 원답 사용.
    assert "time.activity_window" not in seed


def test_seed_slots_from_profile_empty_when_absent() -> None:
    """프로필·focus_mode 가 없으면 빈 시드 → 오버레이가 지난 인터뷰 원답을 덮지 않는다."""
    assert pm.seed_slots_from_profile(behavioral=None, interaction=None, focus_mode_prefs={}) == {}


# ───────────────────────── 매핑 순수 함수 ─────────────────────────


def test_energy_cycle_from_peak() -> None:
    assert pm.energy_cycle_from_peak(["오전"]) == "morning"
    assert pm.energy_cycle_from_peak(["저녁", "오전"]) == "evening"  # 첫 값 기준
    assert pm.energy_cycle_from_peak(["변동"]) == "varies"
    assert pm.energy_cycle_from_peak([]) == "varies"
    assert pm.energy_cycle_from_peak(["없는칩"]) == "varies"  # 미지원 → 안전 폴백


def test_chunk_bucket() -> None:
    assert pm.chunk_bucket(None) == "30"
    assert pm.chunk_bucket(50) == "60"
    assert pm.chunk_bucket(90) == "90"
    assert pm.chunk_bucket(120) == "90"


def test_recovery_tone_enum() -> None:
    assert pm.recovery_tone_enum("따뜻") == "gentle"
    assert pm.recovery_tone_enum("담백") == "normal"
    assert pm.recovery_tone_enum("유머") == "encouraging"
    assert pm.recovery_tone_enum("코치처럼") == "encouraging"
    assert pm.recovery_tone_enum("모르는값") == "normal"  # 폴백


def test_every_tone_chip_is_mapped() -> None:
    """카탈로그 보기가 전부 매핑표의 키다 — 보기 표기가 바뀌면 여기서 먼저 깨진다.

    보기는 "코치처럼" 인데 키가 "코치" 뿐이라 그 칩이 조용히 'normal' 로 떨어지던 회귀 가드.
    """
    slot = next(s for s in PLAN_CATALOG.slots if s.slot_key == "recovery.tone")
    assert slot.options
    unmapped = [o for o in slot.options if o not in pm._TONE_TO_INTERACTION]
    assert unmapped == []


def test_user_tone_mode_from_chip() -> None:
    """인터뷰 톤 칩 → users.tone_mode. '담백' 은 기본 말투라 None."""
    assert pm.user_tone_mode_from_chip("따뜻") == "gentle"
    assert pm.user_tone_mode_from_chip("유머") == "encouraging"
    assert pm.user_tone_mode_from_chip("코치처럼") == "encouraging"
    assert pm.user_tone_mode_from_chip("담백") is None
    assert pm.user_tone_mode_from_chip("모르는값") is None


class _FakeProfileRepo:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def upsert_behavioral(self, user_id: Any, *, fields: dict[str, Any]) -> None:
        return None

    async def upsert_interaction(self, user_id: Any, *, fields: dict[str, Any]) -> None:
        return None


def _outcome(tone: str) -> Any:
    return SimpleNamespace(
        availability=SimpleNamespace(
            peak_window=["저녁"], activity_window=SimpleNamespace(start="08:00", end="22:00")
        ),
        preferences=SimpleNamespace(
            focus_duration_min=60, downscope_unit_min=15, rest_ok=True, recovery_tone=tone
        ),
        unresolved_slots=[],
    )


def _persist(monkeypatch: Any, user: Any, tone: str) -> None:
    monkeypatch.setattr(pm, "ProfileRepo", _FakeProfileRepo)
    asyncio.run(pm.persist_profile_from_outcome(cast(Any, None), user=user, outcome=_outcome(tone)))


def test_interview_tone_seeds_empty_tone_mode(monkeypatch: Any) -> None:
    """인터뷰에서 고른 톤이 AI 말투(users.tone_mode)로 이어진다 — 비어 있을 때만."""
    user = cast(Any, SimpleNamespace(id="u1", tone_mode=None, focus_mode_preferences=None))
    _persist(monkeypatch, user, "따뜻")
    assert user.tone_mode == "gentle"


def test_interview_tone_does_not_override_chosen_tone_mode(monkeypatch: Any) -> None:
    """설정에서 직접 고른 톤은 재인터뷰가 덮어쓰지 않는다."""
    user = cast(Any, SimpleNamespace(id="u1", tone_mode="strict", focus_mode_preferences={}))
    _persist(monkeypatch, user, "유머")
    assert user.tone_mode == "strict"


def test_plain_tone_leaves_tone_mode_empty(monkeypatch: Any) -> None:
    """'담백' 은 prefix 없는 기본 말투 — tone_mode 를 채우지 않는다."""
    user = cast(Any, SimpleNamespace(id="u1", tone_mode=None, focus_mode_preferences={}))
    _persist(monkeypatch, user, "담백")
    assert user.tone_mode is None


def test_recovery_speed_from_prefs() -> None:
    """회복 최소 단위 + 휴식 수용 → fast/medium/slow 파생."""
    assert pm.recovery_speed_from_prefs(10, True) == "fast"  # 작은 단위 + 휴식 OK
    assert pm.recovery_speed_from_prefs(5, True) == "fast"
    assert pm.recovery_speed_from_prefs(30, True) == "slow"  # 큰 단위만 가능
    assert pm.recovery_speed_from_prefs(45, False) == "slow"
    assert pm.recovery_speed_from_prefs(15, True) == "medium"
    assert pm.recovery_speed_from_prefs(10, False) == "medium"  # 휴식 거부 → fast 아님
    assert pm.recovery_speed_from_prefs(None, True) == "medium"


# ───────────────────────── GET/PATCH /settings/profile ─────────────────────────


def test_get_profile_empty_when_not_set(client: TestClient) -> None:
    """인터뷰가 아직 안 채웠으면 각 항목 null (행 미생성)."""
    resp = client.get("/settings/profile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["behavioral"] is None
    assert body["interaction"] is None


def test_patch_profile_creates_and_persists(client: TestClient) -> None:
    resp = client.patch(
        "/settings/profile",
        json={
            "energyCycle": "morning",
            "attentionSpan": 50,
            "timeChunkPreference": "60",
            "recoveryTone": "gentle",
            "reminderFrequency": "minimal",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["behavioral"]["energyCycle"] == "morning"
    assert body["behavioral"]["attentionSpan"] == 50
    assert body["behavioral"]["timeChunkPreference"] == "60"
    assert body["interaction"]["recoveryTone"] == "gentle"
    assert body["interaction"]["reminderFrequency"] == "minimal"

    # 재조회 시 유지 (영속)
    got = client.get("/settings/profile").json()
    assert got["behavioral"]["energyCycle"] == "morning"
    assert got["interaction"]["recoveryTone"] == "gentle"


def test_patch_profile_partial_keeps_others(client: TestClient) -> None:
    """지정 필드만 갱신 — 나머지는 유지."""
    client.patch("/settings/profile", json={"attentionSpan": 40, "recoveryTone": "encouraging"})
    resp = client.patch("/settings/profile", json={"energyCycle": "evening"})
    body = resp.json()
    assert body["behavioral"]["energyCycle"] == "evening"
    assert body["behavioral"]["attentionSpan"] == 40  # 유지
    assert body["interaction"]["recoveryTone"] == "encouraging"  # 유지


def test_patch_recovery_prefs_round_trip(client: TestClient) -> None:
    """회복 선호(downscopeUnitMin·restOk) → focus_mode_preferences 저장/조회."""
    resp = client.patch("/settings/profile", json={"downscopeUnitMin": 15, "restOk": False})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["downscopeUnitMin"] == 15
    assert body["restOk"] is False

    got = client.get("/settings/profile").json()
    assert got["downscopeUnitMin"] == 15
    assert got["restOk"] is False


def test_patch_profile_invalid_enum(client: TestClient) -> None:
    resp = client.patch("/settings/profile", json={"energyCycle": "bogus"})
    assert resp.status_code == 422


def test_profile_requires_auth(unauthed_client: TestClient) -> None:
    assert unauthed_client.get("/settings/profile").status_code == 401


def test_patch_activity_window_round_trip(client: TestClient) -> None:
    """활동 시간대(계획 배치 창) 편집 → focus_mode_preferences 저장/조회 (#editable-activity-window)."""
    resp = client.patch(
        "/settings/profile", json={"activityStart": "06:00", "activityEnd": "24:00"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["activityStart"] == "06:00"
    assert body["activityEnd"] == "24:00"
    got = client.get("/settings/profile").json()
    assert got["activityStart"] == "06:00"
    assert got["activityEnd"] == "24:00"


def test_patch_activity_window_invalid(client: TestClient) -> None:
    assert client.patch("/settings/profile", json={"activityStart": "25:00"}).status_code == 422


def test_seed_normalizes_to_catalog_notation() -> None:
    """프로필의 분 값이 **카탈로그 표기**로 시드된다 — `"120분"` 이 아니라 `"2시간 이상"`.

    시드는 `routes/interview._persist_turn` 을 타고 `interview_slot_answers` 에 UPSERT 되므로,
    옵션에 없는 표기를 넣으면 **사용자가 고른 적 없는 값이 사용자의 답으로** 남는다.
    실제로 그랬고(v2.01 시드 루프), 오염된 프로필에서는 `"2분"` 이 답인 것처럼 남아 백필을
    틀리게 할 뻔했다.
    """
    beh = cast(Any, SimpleNamespace(energy_cycle="evening", attention_span=120))
    seed = pm.seed_slots_from_profile(behavioral=beh, interaction=None, focus_mode_prefs={})
    assert seed["energy.focus_duration"] == {"type": "chip", "values": ["2시간 이상"]}


def test_seed_skips_values_the_user_could_never_have_picked() -> None:
    """카탈로그 옵션에 못 맞추는 값은 **시드하지 않는다** — 그 슬롯은 열린 채 다시 묻는다.

    `PATCH /settings/profile` 이 `attention_span` 을 `ge=5` 로 허용해 45 같은 값이 있을 수
    있고, 파서 사고로 2 가 남아 있을 수도 있다. 지어낸 답으로 슬롯을 닫는 것보다 실제 보기를
    들고 한 번 더 묻는 편이 낫다.
    """
    for span in (2, 45, 240):
        beh = cast(Any, SimpleNamespace(energy_cycle="evening", attention_span=span))
        seed = pm.seed_slots_from_profile(behavioral=beh, interaction=None, focus_mode_prefs={})
        assert "energy.focus_duration" not in seed, span
        assert seed["time.peak_window"] == {"type": "chip", "values": ["저녁"]}  # 나머지는 그대로


# ───────────────────── 답하지 않은 칸은 프로필에 쓰지 않는다 (interview-6·8) ─────────────────────


class _RecordingProfileRepo:
    """upsert 호출에 실린 fields 를 기록한다 — 무엇을 **쓰려 했는지**가 검증 대상이다."""

    calls: dict[str, list[dict[str, Any]]] = {}

    def __init__(self, session: Any) -> None:
        self.session = session

    async def upsert_behavioral(self, user_id: Any, *, fields: dict[str, Any]) -> None:
        type(self).calls.setdefault("behavioral", []).append(fields)

    async def upsert_interaction(self, user_id: Any, *, fields: dict[str, Any]) -> None:
        type(self).calls.setdefault("interaction", []).append(fields)


def _persist_real_outcome(
    monkeypatch: Any, user: Any, slot_answers: dict[str, Any], end_reason: str
) -> dict[str, list[dict[str, Any]]]:
    from reaction_backend.orchestrator import interview_adapter

    _RecordingProfileRepo.calls = {}
    monkeypatch.setattr(pm, "ProfileRepo", _RecordingProfileRepo)
    outcome = interview_adapter.build_outcome(
        session_id="s1",
        slot_answers=slot_answers,
        ambiguity_final=0.5,
        end_reason=cast(Any, end_reason),
        analysis_source="llm",
    )
    asyncio.run(pm.persist_profile_from_outcome(cast(Any, None), user=user, outcome=outcome))
    return _RecordingProfileRepo.calls


def test_early_finish_does_not_write_defaults_into_the_profile(monkeypatch: Any) -> None:
    """⚠️ 역할만 답하고 [충분해요]·이탈한 사용자의 프로필에 **기본값을 쓰지 않는다** (interview-6).

    고치기 전엔 피크 '변동'·활동창 09~23시·톤 normal·최소 단위 10분·휴식 수용 true 가 그대로
    프로필에 들어갔고, 다음 재인터뷰가 그걸 시드로 읽어 **묻지도 않은 네 칸을 건너뛰었다**.
    """
    user = cast(Any, SimpleNamespace(id="u1", tone_mode=None, focus_mode_preferences=None))
    calls = _persist_real_outcome(
        monkeypatch,
        user,
        {"identity.role": {"type": "chip", "values": ["3학년"]}},
        "early_user",
    )

    assert calls.get("behavioral") is None  # 쓸 칸이 없으면 행도 만들지 않는다
    assert calls.get("interaction") is None
    assert not (user.focus_mode_preferences or {})
    assert user.tone_mode is None


@pytest.mark.parametrize(
    "skip",
    [{"type": "text", "raw": ""}, {"type": "skip", "reason": "declined"}],
    ids=["legacy", "new"],
)
def test_skipped_fields_do_not_write_defaults_into_the_profile(
    monkeypatch: Any, skip: dict[str, Any]
) -> None:
    """⚠️ "없어요·모르겠어요" 로 **넘긴** 칸도 안 답한 칸이다 (#499).

    위 테스트는 슬롯 행 자체가 없는 경우다. 여기는 스킵으로 **닫힌** 경우 — 예전엔 스킵이
    '답함' 으로 읽혀 `unresolved_slots` 에서 빠졌고, outcome 이 채운 기본값(활동창 09~23시·
    톤 '담백'·최소 단위 10분·휴식 수용)이 사용자의 답으로 프로필에 굳었다.
    """
    user = cast(Any, SimpleNamespace(id="u1", tone_mode=None, focus_mode_preferences=None))
    calls = _persist_real_outcome(
        monkeypatch,
        user,
        {
            "identity.role": {"type": "chip", "values": ["3학년"]},
            "time.peak_window": skip,
            "time.activity_window": skip,
            "recovery.tone": skip,
            "recovery.rest_ok": skip,
            "recovery.downscope_unit": skip,
        },
        "completed",
    )

    assert calls.get("behavioral") is None
    assert calls.get("interaction") is None
    assert not (user.focus_mode_preferences or {})
    assert user.tone_mode is None


def test_answered_fields_are_still_persisted(monkeypatch: Any) -> None:
    """답한 칸은 그대로 영속한다 — 가드는 '안 답한 칸' 만 거른다."""
    user = cast(Any, SimpleNamespace(id="u1", tone_mode=None, focus_mode_preferences={}))
    calls = _persist_real_outcome(
        monkeypatch,
        user,
        {
            "time.peak_window": {"type": "chip", "values": ["저녁"]},
            "time.activity_window": {"type": "range", "start": "08:00", "end": "22:00"},
            "energy.focus_duration": {"type": "chip", "values": ["50분"]},
            "recovery.tone": {"type": "chip", "values": ["따뜻"]},
            "recovery.rest_ok": {"type": "chip", "values": ["네"]},
            "recovery.downscope_unit": {"type": "chip", "values": ["15분"]},
        },
        "early_user",
    )

    (behavioral,) = calls["behavioral"]
    assert behavioral["energy_cycle"] == "evening"
    assert behavioral["attention_span"] == 50
    assert behavioral["preferred_start_time"] is not None
    assert behavioral["recovery_speed_type"] == "medium"
    assert calls["interaction"] == [{"recovery_tone": "gentle"}]
    assert user.focus_mode_preferences == {"downscope_unit_min": 15, "rest_ok": True}
    assert user.tone_mode == "gentle"


def test_unanswered_focus_and_downscope_keep_settings_edits(monkeypatch: Any) -> None:
    """내 정보에서 고친 값(집중 45분·최소 단위 20분)을 인터뷰 종료가 되돌리지 않는다 (interview-8).

    집중 길이는 필수 슬롯이 아니라 계획 인터뷰가 묻지 않는다 — 고치기 전엔 `or 30` 으로 늘
    30 이 쓰였다. 최소 단위도 안 답했으면 기존 20 을 그대로 둔다.
    """
    user = cast(
        Any,
        SimpleNamespace(id="u1", tone_mode=None, focus_mode_preferences={"downscope_unit_min": 20}),
    )
    calls = _persist_real_outcome(
        monkeypatch,
        user,
        {"time.peak_window": {"type": "chip", "values": ["오전"]}},
        "early_user",
    )

    (behavioral,) = calls["behavioral"]
    assert "attention_span" not in behavioral
    assert "time_chunk_preference" not in behavioral
    assert user.focus_mode_preferences == {"downscope_unit_min": 20}


def test_profile_owned_slots_include_values_the_seed_could_not_map() -> None:
    """칩으로 못 옮긴 프로필 값도 '프로필이 가진 슬롯' 이다 — 호출자가 옛 이월 원답을 치운다."""
    beh = cast(Any, SimpleNamespace(energy_cycle="evening", attention_span=45))
    owned = pm.profile_owned_slots(
        behavioral=beh, interaction=None, focus_mode_prefs={"downscope_unit_min": 20}
    )
    seed = pm.seed_slots_from_profile(
        behavioral=beh, interaction=None, focus_mode_prefs={"downscope_unit_min": 20}
    )
    assert owned - seed.keys() == {"energy.focus_duration", "recovery.downscope_unit"}


def test_seed_keeps_the_carried_chip_when_the_profile_still_agrees() -> None:
    """'코치처럼' 을 고른 사용자의 재인터뷰 시드가 '유머' 로 바뀌지 않는다 (interview-9).

    톤 역매핑은 다대일(유머·코치처럼 → encouraging → '유머')이고 피크는 첫 값만 본다. 프로필만
    으로 되돌리면 사용자가 고른 칩이 재인터뷰마다 바뀌었다. 이월 원답이 같은 프로필 값으로
    이어지면 그 원답을 그대로 쓰고, 설정에서 실제로 바꿨을 때만 프로필이 이긴다.
    """
    beh = cast(Any, SimpleNamespace(energy_cycle="evening", attention_span=None))
    inter = cast(Any, SimpleNamespace(recovery_tone="encouraging"))
    carried = {
        "recovery.tone": {"type": "chip", "values": ["코치처럼"]},
        "time.peak_window": {"type": "chip", "values": ["저녁", "심야"]},
    }
    seed = pm.seed_slots_from_profile(
        behavioral=beh, interaction=inter, focus_mode_prefs={}, carried=carried
    )
    assert seed["recovery.tone"] == {"type": "chip", "values": ["코치처럼"]}
    assert seed["time.peak_window"] == {"type": "chip", "values": ["저녁", "심야"]}

    # 설정에서 톤을 gentle 로, 피크를 오전으로 바꿨다면 프로필이 이긴다.
    beh.energy_cycle = "morning"
    inter.recovery_tone = "gentle"
    seed = pm.seed_slots_from_profile(
        behavioral=beh, interaction=inter, focus_mode_prefs={}, carried=carried
    )
    assert seed["recovery.tone"] == {"type": "chip", "values": ["따뜻"]}
    assert seed["time.peak_window"] == {"type": "chip", "values": ["오전"]}
