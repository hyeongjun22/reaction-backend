"""Interview 도메인 스키마 (api-contract §4) — S02 딥 인터뷰.

#3-B 단계는 정적 mock 스텁. 적응형 질문 선택·모호함 채점·LLM 호출은 #6.

#6 추가분:
- LLM Structured Output 스키마 (`NextQuestionSchema`, `AmbiguityUpdate`) —
  `aiClient.run(schema=...)` 로 강제 검증. 룰 fallback 도 같은 schema 로 반환.
- **경계 계약 `InterviewOutcome`** — Deep Interview(#6) 의 최종 산출물이자
  First Plan(#32) 의 유일한 입력 시드. slot_answers 의 결정적 투영으로 빌드(LLM 0회).
  자세한 흐름은 `orchestrator/interview.py` / `orchestrator/interview_adapter.py` 참조.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import Field, JsonValue, field_validator

from reaction_backend.schemas.common import CamelModel, KstDatetime
from reaction_backend.schemas.ultimate_goal import UltimateGoalOutcome

# "2026-10" · "2026-10-00" — 월만 말한 마감. LLM 이 "10월에 시험이에요" 를 이렇게 낸다.
_MONTH_ONLY_DEADLINE_RE = re.compile(r"^(\d{4})-(\d{2})(?:-0{1,2})?$")


def normalize_deadline(raw: str | None) -> str | None:
    """마감 문자열을 **실재하는 ISO 날짜**로 정규화. 못 읽으면 `None`(마감 없음).

    이 값은 사용자가 고른 날짜가 아니라 **LLM 이 자유 서술에서 뽑은 문자열**이 될 수 있다
    (`goals.deadlines` 슬롯은 date_picker 지만, 답을 안 해도 `goals.list` 하베스트가 채운다).
    그런데 하류는 전부 `date.fromisoformat` 로 곧장 읽는다 — `first_plan.py`,
    `first_plan_adapter`(5곳), `materialize_goals`.

    라이브 실측(2026-08-29): "10월에 시험이에요" 를 LLM 이 `2026-10-00` 으로 하베스트했고,
    인터뷰 **마지막 턴**의 `materialize_goals` 가 `ValueError: day is out of range for month`
    로 터졌다 — 500 이 나고 세션이 `end_reason=None` 으로 남아 **인터뷰 15턴이 통째로**
    날아갔다. 사용자는 다시 처음부터 해야 한다.

    월만 있는 값은 **그 달 1일**로 읽는다. 늦게 잡는 것보다 이르게 잡는 쪽이 안전하다 —
    시험이 10/5인데 10/31로 잡으면 준비가 늦고, 반대는 계획이 조금 촘촘해질 뿐이다. 이 값은
    확인 카드(`goal_summary`)에 그대로 실려 사용자가 보고 고칠 수 있다.

    아예 못 읽는 값은 `None` — 마감을 지어내느니 없는 게 낫다(마감 없는 목표도 정상 경로다).
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    month_only = _MONTH_ONLY_DEADLINE_RE.match(text)
    if month_only is not None:
        year, month = int(month_only.group(1)), int(month_only.group(2))
        if 1 <= month <= 12:
            return date(year, month, 1).isoformat()
    return None


class SlotCatalogEntry(CamelModel):
    """슬롯 카탈로그 한 항목 — GET /interview/slot-catalog."""

    slot_key: str
    label: str
    answer_type: str
    is_required: bool
    category: str
    options: list[str] = Field(default_factory=list)  # chip/select 보기 (text 등은 빈 배열)
    # 보기를 여러 개 골라도 전부 쓰는가 — false 면 첫 값만 쓰인다(단일 선택). v2.30-interview.
    multiple: bool = False


class Question(CamelModel):
    """인터뷰 질문 — 세션의 currentQuestion.

    `options` = 카탈로그 고정 보기 (chip/select 의 유효 선택지, 정적 진실 소스).
    `suggested_answers` = LLM 이 슬롯 맥락에 맞춰 추천한 답변 카드 — 주로 고정 보기가 없는
    자유서술 슬롯(goals.list·success_image·time.fixed_blocks 등)에서 탭/참고용으로 채워진다.
    """

    slot_key: str
    text: str
    answer_type: str
    options: list[str]
    suggested_answers: list[str] = Field(default_factory=list)
    # 보기를 여러 개 골라도 전부 쓰는가(`SlotCatalogEntry.multiple` 과 같다). false 면 단일 선택.
    multiple: bool = False


class InterviewSession(CamelModel):
    """인터뷰 세션 상태 — sessions·answers·next-question·finish 공통 응답.

    `ambiguity_score` 는 남은 미해결 필수 슬롯 수(정수). 진행될수록 감소 → 0 이면 충분.
    종료 턴(`end_reason` 채워지고 `current_question=None`)에는 `summary`(S03 확인 카드)와
    `outcome`(First Plan 시드) 또는 `ultimate_outcome`(만다라 시드)이 함께 실린다. `kind`
    별로 정확히 하나만 채워지고 나머지는 null — `outcome` 을 union 으로 바꾸지 않는 이유는
    `UltimateGoalOutcome` docstring 참고(기존 FE 계획 인터뷰 타입 무변경). 진행 중에는 셋 다 null.
    """

    session_id: str
    ambiguity_score: int
    total_turns: int
    end_reason: str | None
    current_question: Question | None
    summary: InterviewSummary | None = None
    outcome: InterviewOutcome | None = None
    ultimate_outcome: UltimateGoalOutcome | None = None


class SlotAnswerRequest(CamelModel):
    """POST /interview/sessions/{id}/answers 요청 — 슬롯 답 UPSERT."""

    slot_key: str = Field(min_length=1)
    value: JsonValue
    client_turn: int = Field(ge=0)


class StartSessionRequest(CamelModel):
    """POST /interview/sessions 요청 — kind 생략 시 계획 인터뷰(하위호환, U0b)."""

    kind: Literal["plan", "ultimate"] = "plan"
    # 이 목표 **하나**만 계획하려고 들어온 인터뷰. 목표 관리의 "미계획" 카드에서 진입한다.
    #
    # 주면 `goals.list`·`goals.heaviest` 를 **그 목표로 채운 채** 시작하므로 그 둘을 묻지
    # 않는다 — 대상이 이미 정해졌는데 "지금 머릿속에 있는 일들을 적어주세요" 로 다시 묻는
    # 것은 사용자가 누른 버튼의 약속을 어기는 것이다. 나머지 `goals.*` 속성만 묻는다.
    #
    # `kind="ultimate"` 와는 함께 쓸 수 없다 — 궁극목표 인터뷰엔 `goals.*` 슬롯이 없다.
    goal_id: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# LLM Structured Output 스키마 (#6) — aiClient.run(schema=...) 강제 검증.
# 룰 fallback 도 동일 schema 인스턴스를 반환한다 (tool_executor 계약).
# ─────────────────────────────────────────────────────────────────────────────


class NextQuestionSchema(CamelModel):
    """LLM ① — `interview/next_question` 응답. 다음 질문 1개 (+ 공감 한 줄).

    직전 답의 채점(clarity)·정규화(normalized_value)는 `AmbiguityUpdate`
    (`interview/ambiguity_score`) 가 전담한다 — 여기 두면 두 프롬프트가 같은 걸 중복
    계산하고 스키마가 드리프트한다.

    `suggested_answers` = 이 슬롯에 대해 사용자가 탭/참고할 답변 카드 추천 (0~4개). 고정 보기가
    있는 chip/select 는 그 보기로 답하므로 보통 비우고, 자유서술 슬롯에서 예시를 채운다.
    """

    question: str
    empathy_one_liner: str
    suggested_answers: list[str] = Field(default_factory=list, max_length=4)


class AmbiguityUpdate(CamelModel):
    """LLM ② — `interview/ambiguity_score` 응답. 슬롯 채점 + 모호함 + 구조화 정규화 값.

    `normalized_value` 는 자유서술 답을 슬롯 answer_type 에 맞는 구조로 추출한 값이다
    (딥 인터뷰는 채팅이라 답이 전부 자유서술 text 로 들어오는데, First Plan 시드
    `build_outcome` 은 chip/range/date 구조를 읽으므로 여기서 LLM 이 구조화해 저장한다):
    - chip/select   → 보기 중 하나(또는 배열)  예: "3학년" / ["오전","저녁"]
    - time_range    → {"start":"HH:MM","end":"HH:MM"}
    - date_picker   → "YYYY-MM-DD" (오늘 기준 상대표현 해석)
    - text          → 정리된 핵심값(문자열 또는 배열)
    추출 불가/무관한 답이면 null → 구조화 슬롯은 재질문, text 는 원문 저장으로 폴백.
    """

    slot_key: str
    clarity_score: float = Field(ge=0.0, le=1.0)
    new_ambiguity: float = Field(ge=0.0, le=1.0)
    normalized_value: JsonValue | None = None


class HarvestedSlot(CamelModel):
    """직전 자유서술 답에서 함께 추출된 다른 슬롯 값 1개 (`interview/slot_extraction`).

    `normalized_value` 규칙은 `AmbiguityUpdate` 와 동일 — 슬롯 answer_type 에 맞춘 구조화 값.
    `confidence` 가 낮으면 채우지 않고(잘못 채우면 재질문보다 나쁨) 정식 질문으로 넘어간다.
    """

    slot_key: str
    normalized_value: JsonValue
    confidence: float = Field(ge=0.0, le=1.0)


class AnswerIntake(AmbiguityUpdate):
    """LLM — 답 1개를 **한 번에** 채점·정규화하고, 같은 답에 섞인 다른 슬롯까지 추출한다.

    `AmbiguityUpdate` + 수확 결과를 합친 계약이다. 예전엔 두 프롬프트를 **각각 호출**해
    같은 답을 두 번 읽었고, 그래서 자유서술 답 한 턴이 LLM **3콜**(질문 생성 + 채점 + 수확)이
    됐다. 레포는 그 증가로 실제 사고를 겪었다:

    > 더 붙이는 순간 같은 사고가 재발한다(실제로 `harvest_slots` 가 추가되며 2콜→3콜이 됐다).
    > — `observability/correlation.py` · `safety/endpoint_rate_limit.py`

    ⚠️ **`slots` 는 선택이다.** 궁극목표 인터뷰는 수확을 하지 않으므로(슬롯 9개가 서로 독립)
    그 프롬프트는 이 필드를 내지 않는다 — 기본값 빈 배열로 같은 스키마를 공유한다.
    """

    slots: list[HarvestedSlot] = Field(default_factory=list)


class InterviewSummary(CamelModel):
    """LLM ③ — `interview/summary` 응답. Analysis Confirm(S03) 요약 확인 카드.

    필수 슬롯이 모두 채워진 뒤 `summarize_interview` 노드가 1회 생성한다.
    사람이 [이대로 진행/수정] 을 고르는 화면에 그대로 노출되는 표현 계층일 뿐,
    First Plan 의 입력 시드는 어디까지나 `InterviewOutcome` 이다(요약은 시드 아님).
    8s timeout / rate limit 시 슬롯에서 결정적으로 빌드한 룰 요약으로 fallback.
    """

    headline: str
    goal_summary: str
    time_summary: str
    preference_summary: str
    confirm_question: str


# ─────────────────────────────────────────────────────────────────────────────
# 경계 계약 — InterviewOutcome (Deep Interview #6 → First Plan #32)
#
# Interview 그래프 터미널에서 LLM 0회로 결정적 빌드 → S03 Analysis Confirm 화면에
# is_draft=true 로 노출 → 사용자 확정 후 First Plan 의 유일한 입력 시드.
# is_draft / ai_source 는 응답 시 라우터가 DraftMixin 으로 강제 (ADR-0005 §7.2).
# ─────────────────────────────────────────────────────────────────────────────

InterviewEndReason = Literal["completed", "turn_limit", "early_user", "abandoned"]


class TimeRange(CamelModel):
    """KST 로컬 시각 구간 (날짜 없음). 예: 09:00~23:00."""

    start: str = Field(description='"HH:MM" KST 로컬')
    end: str = Field(description='"HH:MM" KST 로컬')


class NoTouchWindow(CamelModel):
    """절대 일정 금지 구간 — time.no_touch. First Plan 의 no_touch 정책으로 전개."""

    days_of_week: list[str]  # WEEKDAY_KEYS 규약: ["mon","tue",...]
    window: TimeRange
    label: str | None = None


class AvailabilityProfile(CamelModel):
    """가용 시간 (time.* 슬롯군).

    First Plan 이 `time_policies` + `fixed_schedules` 로 전개해 free/busy 계산에 쓴다
    (`orchestrator/goal_structuring.py` 입력).
    """

    activity_window: TimeRange  # time.activity_window (필수)
    peak_window: list[str]  # time.peak_window chips (필수)
    no_touch_windows: list[NoTouchWindow] = Field(default_factory=list)  # time.no_touch
    fixed_block_hints: list[str] = Field(default_factory=list)  # time.fixed_blocks 자유입력 원문


class GoalCandidate(CamelModel):
    """핵심 목표 후보 (goals.* 슬롯군). First Plan 의 goal_node 분해 입력."""

    title: str  # goals.list 항목
    category: str  # study|health|career|... (자유 문자열, First Plan 이 정규화)
    is_heaviest: bool = False  # goals.heaviest
    deadline: str | None = None  # goals.deadlines "YYYY-MM-DD"
    success_image: str | None = None  # goals.success_image
    current_level: str | None = (
        None  # goals.current_level — 지금까지 진행한 수준(분해 baseline, #B)
    )
    # goals.weekly_time — 이 목표에 주당 투입 가능한 시간(시간). 분해 세션 수 산정 기준.
    weekly_hours: int | None = None
    # goals.session_length — 이 목표를 한 번에 집중/수행 가능한 시간(분). 세션 길이·개수 산정.
    session_length_min: int | None = None
    # goals.preferred_time — 이 목표를 언제 하고 싶은지(오전/오후/저녁/심야). 스케줄러가 이 목표를
    # 배치할 때 전역 peak 대신 이 시간대를 우선한다('상관없음'/미입력이면 전역 peak 폴백).
    preferred_time: str | None = None
    # goals.frequency — 이 목표를 주당 며칠 하고 싶은지(매일=7 / 주 N회=N). 볼륨(weekly_hours)과
    # 별개인 '케이던스' 의도. 있으면 주당 세션 수를 이 값으로 잡아 스케줄러가 그만큼 서로 다른
    # 날에 분산한다('상관없음/몰아서'·미입력이면 None → 볼륨 기반 산정으로 폴백).
    frequency_per_week: int | None = None
    # goals.approach — 이 목표를 어떻게 해나가고 싶은지(방식·순서 서술). 분해가 일반적 방식이
    # 아니라 사용자가 밝힌 방향을 따르도록 하는 grounding.
    approach_note: str | None = None
    # goals.materials — 참고 자료의 **실제 원문**(프로젝트 설명·README·강의계획서·요구사항 등)
    # 또는 자료 검색 파이프라인(ADR-0010)이 확정한 도서/영상 상세를 텍스트로 풀어낸 요약.
    # pointer('내 프로젝트')가 아니라 내용이 있어야 분해가 그 기능·목차대로 뼈대를 잡는다.
    materials_note: str | None = None
    # goals.materials 에서 사용자가 **교재 카드**(`domain/curriculum_cards`)로 확정한 책의 id.
    # 있으면 마일스톤·세션이 LLM 이 아니라 그 책의 단원 순서로 만들어진다(`curriculum_plan`).
    # 카드에 없는 책(알라딘 검색·붙여넣기)은 여기 들어오지 않고 `materials_note` 로만 간다.
    curriculum: list[str] = Field(default_factory=list)
    tentative_tier: Literal["focus", "maintain", "parked"] = "maintain"
    # ⚠️ **`clarity_score` 와 무관하다.** `interview_adapter` 가 목표 출처에 따라 코드로
    # 박는다 — 추론 목록이면 0.0, 사용자가 고른 heaviest 면 0.5. 예전 주석이 "해당 슬롯
    # clarity_score" 라고 적어 둬서 채점값이 하류로 흐르는 것처럼 읽혔다(#448 감사).
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("deadline", mode="before")
    @classmethod
    def _coerce_deadline(cls, value: object) -> object:
        """하류가 전부 `date.fromisoformat` 로 곧장 읽으므로 **경계에서** 실재하는 날짜만 통과.

        422 로 거절하지 않는 이유: 이 값의 출처가 사용자 입력이 아니라 LLM 하베스트라,
        거절해도 사용자가 고칠 방법이 없고 인터뷰만 죽는다. 근거는 `normalize_deadline`.
        """
        return normalize_deadline(value) if isinstance(value, str) else value


class PreferenceProfile(CamelModel):
    """선호 방식 (recovery.* + energy.* 슬롯군).

    First Plan 이 behavioral_profile / interaction_style 컨텍스트로 사용.
    """

    recovery_tone: str  # recovery.tone (필수)
    rest_ok: bool  # recovery.rest_ok (필수)
    # recovery.downscope_unit (필수) — 회복 시 할 일을 이 분(min) 단위까지 줄이면 해볼 만함.
    downscope_unit_min: int = Field(default=10, ge=1)
    focus_duration_min: int | None = None  # energy.focus_duration (선택)
    break_pattern: str | None = None  # energy.break_pattern (선택)
    weekly_energy: str | None = None  # energy.weekly_drain (선택)


class IdentityContext(CamelModel):
    """정체성 (identity.* 슬롯군)."""

    role: str  # identity.role (필수)
    season: str  # identity.season (필수)
    major: str | None = None  # identity.major (선택)


class InterviewOutcome(CamelModel):
    """Deep Interview(#6) 의 최종 산출물이자 First Plan(#32) 의 유일한 시드.

    LLM 0회로 slot_answers 에서 결정적으로 빌드된다(`interview_adapter.build_outcome`).
    경계에서 추가 LLM 실패 표면을 만들지 않는다 (제약: 8s timeout / rate limit 안전).

    `schema_version` 은 경계 계약 버전 — #6/#32 가 독립 배포돼도 호환성을 검증할 수 있게
    명시한다. 깨지는 변경 시 bump.
    """

    session_id: str
    schema_version: Literal["1.0"] = "1.0"
    generated_at: KstDatetime  # now_kst() (시간 규칙: KST)
    end_reason: InterviewEndReason
    ambiguity_final: float = Field(ge=0.0, le=1.0)
    analysis_source: Literal["llm", "rule"] = "llm"  # 정규화가 룰 fallback 됐으면 "rule"

    identity: IdentityContext
    core_goals: list[GoalCandidate] = Field(min_length=1)  # 핵심 목표
    availability: AvailabilityProfile  # 가용 시간
    preferences: PreferenceProfile  # 선호 방식
    horizon: str | None = None  # 파생: max(core_goals.deadline) "YYYY-MM-DD"
    # default 처리된 필수 슬롯 키 — 아직 안 물은 슬롯 + 스킵으로 닫힌 슬롯(#499).
    unresolved_slots: list[str] = Field(default_factory=list)

    @field_validator("horizon", mode="before")
    @classmethod
    def _coerce_horizon(cls, value: object) -> object:
        """`GoalCandidate.deadline` 과 같은 정규화 — 지평도 `date.fromisoformat` 로 읽힌다.

        보통은 정규화된 마감들의 max 라 이미 안전하지만, `POST /plans/generate` 는 outcome 을
        **인라인으로도** 받는다(`FirstPlanGenerateRequest.outcome`). 그 경로까지 같은 보장.
        """
        return normalize_deadline(value) if isinstance(value, str) else value


# InterviewSession 이 InterviewSummary/InterviewOutcome 보다 먼저 정의되므로
# (forward ref) 모두 정의된 뒤 재빌드해 응답 직렬화를 보장한다.
InterviewSession.model_rebuild()
