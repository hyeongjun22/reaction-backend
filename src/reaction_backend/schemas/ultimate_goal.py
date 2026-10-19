"""궁극목표(Ultimate Goal) 인터뷰 경계 계약 — 딥 인터뷰(kind="ultimate") 의 산출물.

`InterviewOutcome`(계획 인터뷰의 경계 계약)을 확장하지 않는 **별도 스키마**다. 이유는
`orchestrator/ultimate_adapter.py` docstring 참고(요약: core_goals min_length=1 이 강제하는
placeholder 유령 목표, availability/preferences 필수값 지어내기, schema_version bump 시
`plan_drafts.payload` JSONB 역직렬화 파괴 — 셋 다 실코드 제약이라 확장이 아니라 분리가 맞다).

FE 호환: `InterviewSession.outcome` 을 union 으로 만들지 않는다. 궁극목표 인터뷰도 같은
`/interview/*` endpoint 를 쓰되 `outcome` 대신 이 스키마를 담는 **`ultimateOutcome`** 필드로
내려간다 — 기존 FE 타입(계획 인터뷰) 은 무변경.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, KstDatetime

# `schemas.interview.InterviewEndReason` 와 값이 같은 독립 정의 — 거기서 import 하면
# `schemas/interview.py` 가 이 파일의 `UltimateGoalOutcome` 을 담으려고 역참조할 때(U0c,
# `InterviewSession.ultimate_outcome`) 순환 임포트가 된다. DB 모델의 `INTERVIEW_END_REASON_VALUES`
# 튜플도 이미 같은 값을 별도로 들고 있어(db/models/interview_session.py), 이 중복은 기존 패턴과
# 같은 성격이다.
UltimateEndReason = Literal["completed", "turn_limit", "early_user", "abandoned"]


class UltimateGoalOutcome(CamelModel):
    """딥 인터뷰(kind="ultimate") 의 최종 산출물.

    LLM 0회로 slot_answers 에서 결정적으로 빌드된다(`ultimate_adapter.build_ultimate_outcome`).
    `schema_version` 은 이 계약 자체의 독립 버전선 — `InterviewOutcome.schema_version` 과
    별개로 진화한다.
    """

    session_id: str
    schema_version: Literal["1.0"] = "1.0"
    generated_at: KstDatetime  # now_kst() (시간 규칙: KST)
    end_reason: UltimateEndReason
    ambiguity_final: float = Field(ge=0.0, le=1.0)
    analysis_source: Literal["llm", "rule"] = "llm"  # 정규화가 룰 fallback 됐으면 "rule"

    statement: str  # ultimate.statement — 궁극 목표 본문
    domain: str  # ultimate.domain — 8축 프리셋 선택 키
    horizon_years: int | None = None  # ultimate.horizon — "기한 없음" 이면 None
    measure: str  # ultimate.measure — 판정 가능성(숫자·사건·자격)
    success_image: str  # ultimate.success_image — 이룬 날의 장면
    identity_note: str  # ultimate.identity — 그때의 나는 어떤 사람인가
    current_position: str  # ultimate.current_position — 지금 위치(baseline)
    constraints: list[str] = Field(default_factory=list)  # ultimate.constraints — 걸림돌
    values: list[str] = Field(default_factory=list)  # ultimate.values — 타협 불가 가치(선택)
    assets: str | None = None  # ultimate.assets — 이미 가진 무기(선택)
    # 8축 시드 — 비면 만다라 생성(Stage A)이 도메인 프리셋으로 채운다. 사용자가 직접 말한
    # 축은 만다라 승인 후 `goal_nodes.locked=true` 로 재생성이 못 건드리게 한다.
    pillars_hint: list[str] = Field(default_factory=list)  # ultimate.pillars_hint
    # default 처리된 필수 슬롯 키 — 아직 안 물은 슬롯 + 스킵으로 닫힌 슬롯(#499).
    unresolved_slots: list[str] = Field(default_factory=list)


class UltimateGoalRequest(CamelModel):
    """POST /goals/ultimate 요청 — 인터뷰 종료 턴이 이미 쥐고 있는 `ultimateOutcome` 을 그대로
    실어 보내거나(인라인), 생략하면 서버가 최근 '정상 종료' 궁극목표 인터뷰에서 복구한다
    (`ultimate_adapter.resolve_outcome`, `/plans/generate` 의 `_resolve_outcome` 과 같은 패턴).
    """

    outcome: UltimateGoalOutcome | None = None


__all__ = ["UltimateGoalOutcome", "UltimateGoalRequest"]
