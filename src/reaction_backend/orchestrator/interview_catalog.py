"""Interview Catalog Registry (#6-B) — kind 별 슬롯 카탈로그 + 엔진 상수 묶음.

`orchestrator/interview.py` 의 FSM(질문 생성/채점/하베스트/종료 판정)은 슬롯키 문자열에
결합돼 있지 않다 — "인터뷰 = 계획 인터뷰" 라는 단일 전제만 있었다. 그 전제가 흩어진 자리는
모듈 전역 상수 7개(`CRITICAL_SLOTS`/`_HARVEST_EXCLUDE`/`_PER_GOAL_SLOTS`/
`REQUIRED_SLOT_SEQUENCE`/`_CONTEXT_LABELS`/`_DEADLINE_SLOT`/`_DEFAULT_SLOT_QUESTIONS`)와
`api/mock/interview.py` 의 `SLOT_CATALOG` 였다. 이 모듈이 그 전부를 `InterviewCatalog`
하나로 묶어, FSM 노드가 `CATALOGS[state["kind"]]` 로 조회하게 한다.

`api/mock/interview.py` 는 이 모듈로 이전되며 삭제된다 — 프로덕션 FSM 이 `api/mock/` 을
import 하는 폴더 규칙 위반이 이미 있었다(별칭 재수출은 두지 않는다 — 두 번째 진실 방지).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from reaction_backend.domain import slot_value as sv
from reaction_backend.orchestrator import interview_adapter

# ─────────────────────────────────────────────────────────────────────────────
# 슬롯 / 카탈로그 타입
# ─────────────────────────────────────────────────────────────────────────────

# answer_type 값: chip | text | date_picker | time_range | select


@dataclass(frozen=True, slots=True)
class InterviewSlot:
    """슬롯 카탈로그 한 항목 (api-contract §4 — id·label·type·isRequired·category)."""

    slot_key: str
    label: str
    answer_type: str
    is_required: bool
    category: str
    # chip/select 보기. text/date_picker/time_range 는 (). goals.heaviest 는 런타임 동적 생성.
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InterviewCatalog:
    """kind 하나(=인터뷰 종류)의 슬롯·엔진 상수 묶음. `InterviewState` 에는 이 객체가 아니라
    `kind: str` 만 싣는다 — state 는 직렬화 가능해야 한다(ADR-0005 §7.1). 각 노드가
    `CATALOGS[state["kind"]]` 로 조회한다.
    """

    kind: str
    slots: tuple[InterviewSlot, ...]
    required_keys: tuple[str, ...]
    critical_slots: frozenset[str]
    harvest_enabled: bool
    harvest_exclude: frozenset[str]
    per_goal_slots: frozenset[str]
    default_questions: Mapping[str, str]
    context_labels: Mapping[str, str]
    deadline_slot: str | None
    prompt_next_question: str
    prompt_ambiguity: str
    prompt_summary: str
    # 답 채점·정규화·수확을 **한 번에** 하는 프롬프트. `None` 이면 `prompt_ambiguity` 를
    # 쓰고 수확을 하지 않는다(궁극목표 인터뷰 — 슬롯 9개가 서로 독립이라 교차 추출 이득이
    # 없다). 이 분기가 곧 "이 카탈로그가 수확을 하는가" 다.
    prompt_intake: str | None = None
    by_key: Mapping[str, InterviewSlot] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_key", {s.slot_key: s for s in self.slots})


# ─────────────────────────────────────────────────────────────────────────────
# PLAN 카탈로그 — `api/mock/interview.py:SLOT_CATALOG` 그대로 이전 (값 변경 없음)
# ─────────────────────────────────────────────────────────────────────────────

PLAN_SLOTS: tuple[InterviewSlot, ...] = (
    # [A] 정체성
    InterviewSlot(
        "identity.role",
        "어떤 학년/시기예요?",
        "chip",
        True,
        "identity",
        options=("1학년", "2학년", "3학년", "4학년", "졸업유예", "대학원", "기타"),
    ),
    InterviewSlot(
        "identity.season",
        "지금 학기 중이에요, 방학이에요?",
        "chip",
        True,
        "identity",
        options=("학기 중", "방학", "계절학기"),
    ),
    InterviewSlot("identity.major", "어떤 전공이에요?", "text", False, "identity"),
    # 전역 집중 시간대 — **목표를 묻기 전** 사용자 프로필 단계에서 받는다.
    #
    # 예전엔 [C] 시간에 있어 goals.preferred_time("이 목표는 언제 하고 싶어요?") **뒤에** 나왔고,
    # 그래서 같은 걸 두 번 묻는 것처럼 읽혔다(실측 피드백). 앞으로 옮기면 '나는 이런 사람이다'
    # 를 먼저 말하고 목표별로 다르면 그때 덧붙이는 순서가 되어 중복감이 사라진다.
    #
    # 게다가 이 슬롯은 CARRY_OVER_SLOT_KEYS 라 재인터뷰에서 지난 답을 그대로 이어받는다 —
    # 즉 **최초 1회만** 묻고 다회차 계획에선 생략된다. 사용자 프로필 성격에 맞는 위치다.
    #
    # 배치에는 goals.preferred_time(목표별)이 **우선**이고, 이 값은 그 목표별 창이 막혔을 때
    # 폴백으로 쓰인다(peak_windows_for_plan). 그 밖에 behavioral_profiles.energy_cycle 의 원천.
    InterviewSlot(
        "time.peak_window",
        "보통 하루 중 언제 가장 집중이 잘 되세요?",
        "chip",
        True,
        "identity",
        options=("오전", "오후", "저녁", "심야", "변동"),
    ),
    # [B] 목표
    InterviewSlot(
        "goals.list", "지금 머릿속에 있는 일들을 편하게 알려주세요", "text", True, "goals"
    ),
    # goals.heaviest 보기는 goals.list 응답에서 런타임 동적 생성 (라우터 _question_options).
    InterviewSlot(
        "goals.heaviest", "그중 가장 무겁게 느끼는 건 어떤 거예요?", "select", True, "goals"
    ),
    InterviewSlot(
        "goals.current_level",
        "그 목표, 지금 어느 정도까지 해봤어요? (처음이면 '처음이에요' 라고 알려주세요)",
        "text",
        True,
        "goals",
    ),
    # 목표별 주당 가용 시간 — 분해가 '얼마나 만들지'를 사용자의 실제 시간에 맞춰 산정한다.
    InterviewSlot(
        "goals.weekly_time",
        "이 목표에 일주일에 몇 시간 정도 쓸 수 있어요?",
        "chip",
        True,
        "goals",
        # 빈도를 '몰아서·상관없음'으로 답해 주당 총량을 계산할 수 없을 때만 묻는다.
        # 실측: 옛 척도(2/4/6/8+)에서 최상단을 29% 가 골랐고, "8시간 이상" 은 8인지
        # 20인지 알 수 없어 계획이 8로 고정됐다. 위로 넓히고 하단도 촘촘하게.
        options=("1시간", "2시간", "3시간", "5시간", "7시간", "10시간", "15시간 이상"),
    ),
    # 목표별 한 번에 집중/수행 가능한 시간 — 세션 길이·개수를 목표마다 다르게 잡는다.
    InterviewSlot(
        "goals.session_length",
        "이 목표는 한 번에 어느 정도 집중해서 할 수 있어요?",
        "chip",
        True,
        "goals",
        # 실측: 상단 "2시간" 을 27% 가 골랐다 — 척도가 위에서 잘려 있다는 신호.
        # 아래로도 넓힌다(15분 단위 마이크로 습관). 이 값이 주당 총량을 결정하므로
        # (주당 = 길이 × 빈도) 척도가 정확할수록 계획 분량이 정확해진다.
        options=("15분", "30분", "1시간", "1시간 30분", "2시간", "3시간", "4시간 이상"),
    ),
    # 목표별 선호 시간대 — 스케줄러가 이 목표를 배치할 때 전역 peak 대신 이 시간대를 우선한다.
    InterviewSlot(
        "goals.preferred_time",
        "이 목표는 주로 언제 하고 싶어요?",
        "chip",
        True,
        "goals",
        options=("오전", "오후", "저녁", "심야", "상관없음"),
    ),
    # 목표별 빈도(케이던스) — 주당 며칠 할지. 볼륨(weekly_time)과 별개로 '매일/주3회' 의도를 받아
    # 서로 다른 날에 분산 배치한다('몰아서'는 빈도 무관 → 볼륨 기반). '매일 운동'이 주 1일로만
    # 반영되던 문제를 해결(#per-goal-frequency).
    InterviewSlot(
        "goals.frequency",
        "이 목표는 얼마나 자주 하고 싶어요?",
        "chip",
        True,
        "goals",
        options=("매일", "주 5회", "주 4회", "주 3회", "주 2회", "주 1회", "몰아서 · 상관없음"),
    ),
    InterviewSlot("goals.deadlines", "마감일이 정해진 게 있어요?", "date_picker", True, "goals"),
    InterviewSlot(
        "goals.success_image",
        "이 목표를 다 이뤘다고 느낄 때, 어떤 모습일까요?",
        "text",
        True,
        "goals",
    ),
    # 목표 접근 — 사용자가 선호하는 방식·순서로 분해를 grounding (없으면 넘겨도 됨).
    InterviewSlot(
        "goals.approach",
        "이 목표, 어떻게 해나가고 싶어요? 선호하는 방식·순서가 있으면 알려주세요 (없으면 넘겨도 돼요)",
        "text",
        True,
        "goals",
    ),
    # 목표 참고 자료 원문 — pointer 가 아니라 실제 내용을 붙여넣어야 분해가 그대로 뼈대로 쓴다.
    InterviewSlot(
        "goals.materials",
        "참고할 자료가 있으면 그 내용을 그대로 붙여넣어 주세요 — 프로젝트 설명·README·강의계획서·요구사항 등 (없으면 넘겨도 돼요)",
        "text",
        True,
        "goals",
    ),
    # [C] 시간
    InterviewSlot(
        "time.activity_window",
        "하루 중 계획을 잡아도 되는 시간대는 몇 시부터 몇 시까지예요? (이 시간 밖엔 일정을 안 잡아요)",
        "time_range",
        True,
        "time",
    ),
    # time.fixed_blocks(#audit 제거): 답이 fixed_block_hints 로만 남고 **어디에서도 소비되지
    # 않았다** — 계획 코드 참조 0회. 필수→선택으로 강등만 해두고 묻는 것은 남겨서, 사용자는
    # 답했는데 아무 일도 일어나지 않았다. 실제 고정 시간 차단은 활동창 + 고정일정(S05,
    # 요일·시각 보유)이 담당한다.
    # time.peak_window 는 [A] 정체성으로 옮겼다 — 위 주석 참고.
    # time.no_touch(#audit 제거): chip 카테고리만 받아 스케줄러가 소비할 실제 시각이 없었고,
    # 어댑터가 days_of_week=[]·window=활동창 전체로 전개해 매 요일 skip → 계획에 무효였다(잠복
    # 지뢰). 실제 시각 차단은 활동창(수면 여집합) + 고정일정(S05, 요일·시각 보유)이 담당한다.
    # [D] 패턴 & 에너지
    InterviewSlot(
        "energy.focus_duration",
        "한 번에 집중할 수 있는 시간은요?",
        "chip",
        False,
        "energy",
        options=("25분", "50분", "90분", "2시간 이상"),
    ),
    InterviewSlot(
        "energy.break_pattern",
        "작업 사이 쉬는 시간은 어떻게 가져요?",
        "chip",
        False,
        "energy",
        options=("짧게 자주", "길게 가끔", "거의 안 쉼"),
    ),
    InterviewSlot(
        "energy.weekly_drain",
        "이번 주 컨디션은 어때요?",
        "chip",
        False,
        "energy",
        options=("좋음", "보통", "지친 편", "많이 지침"),
    ),
    # [E] 회복 선호
    InterviewSlot(
        "recovery.tone",
        "못 한 날 어떤 톤이 좋아요?",
        "chip",
        True,
        "recovery",
        options=("담백", "따뜻", "유머", "코치처럼"),
    ),
    InterviewSlot(
        "recovery.rest_ok",
        "쉬는 게 어때요 하는 제안을 받을 의향 있어요?",
        "chip",
        True,
        "recovery",
        options=("네", "아니오"),
    ),
    InterviewSlot(
        "recovery.downscope_unit",
        "계획이 밀렸을 때, 할 일을 몇 분짜리까지 줄이면 그래도 해볼 만할까요?",
        "chip",
        True,
        "recovery",
        options=("5분", "10분", "15분", "30분"),
    ),
    # [F] 외부 제약(#audit 제거): constraints.special_events·current_burden 은 slot_answers 로만
    # 남고 build_outcome 이 어떤 필드로도 투영하지 않아 계획·프롬프트에 전혀 쓰이지 않았다(死코드).
)

# 러닝 컨텍스트용 짧은 태그 — 앞서 답한 슬롯을 다음 질문 프롬프트에 실어(ask_question) LLM 이
# 이전 답을 이어받아 자연스럽게 묻게 한다(맥락 없이 슬롯키만 보고 추측하던 문제 보완).
_PLAN_CONTEXT_LABELS: dict[str, str] = {
    "identity.role": "학년/시기",
    "identity.season": "학기",
    "goals.list": "목표",
    "goals.heaviest": "가장 무거운 목표",
    "goals.current_level": "현재 수준",
    "goals.weekly_time": "주당 가용 시간",
    "goals.session_length": "한 번 집중 길이",
    "goals.preferred_time": "선호 시간대",
    "goals.frequency": "빈도(주당 며칠)",
    "goals.deadlines": "마감",
    "goals.success_image": "목표 완료 기준",
    "goals.approach": "접근 방식",
    "goals.materials": "참고 자료 원문",
    "time.activity_window": "활동 시간대",
    "time.peak_window": "집중 시간대",
    "recovery.tone": "회복 톤",
    "recovery.rest_ok": "휴식 수용",
    "recovery.downscope_unit": "최소 실행 단위",
}

# 카탈로그 기본 질문 (LLM 죽었을 때 회귀).
_PLAN_DEFAULT_QUESTIONS: dict[str, str] = {
    "identity.role": "어떤 학년/시기예요?",
    "identity.season": "지금 학기 중이에요, 방학이에요?",
    "goals.list": "지금 머릿속에 있는 일들을 편하게 알려주세요.",
    "goals.heaviest": "그중 가장 무겁게 느끼는 건 어떤 거예요?",
    # 목표별 슬롯은 `{goal}` 자리에 대상 목표 이름이 들어간다 (`_fill_goal`, #187).
    # 조사(은/는·이/가)는 받침에 따라 달라져 목표 제목마다 틀리므로, **쉼표로 끊어** 조사를
    # 아예 쓰지 않는 문형으로 적는다 — "'토익 900점'는" 같은 어색한 조합을 원천 차단.
    "goals.current_level": "'{goal}', 지금 어느 정도까지 해봤어요? (처음이면 '처음이에요' 라고 알려주세요)",
    "goals.weekly_time": "'{goal}', 일주일에 몇 시간 정도 쓸 수 있어요?",
    "goals.session_length": "'{goal}', 한 번에 어느 정도 집중해서 할 수 있어요?",
    "goals.preferred_time": "'{goal}', 주로 언제 하고 싶어요?",
    "goals.frequency": "'{goal}', 얼마나 자주 하고 싶어요?",
    "goals.deadlines": "'{goal}', 마감일이 정해진 게 있어요?",
    "goals.success_image": "'{goal}', 다 이뤘다고 느낄 때 어떤 모습일까요?",
    "goals.approach": "'{goal}', 어떻게 해나가고 싶어요? 선호하는 방식·순서가 있으면 알려주세요.",
    "goals.materials": "'{goal}' 관련해 참고할 자료가 있으면 그 내용을 그대로 붙여넣어 주세요.",
    "time.activity_window": "하루 중 계획을 잡아도 되는 시간대는 몇 시부터 몇 시까지예요? (이 시간 밖엔 일정을 안 잡아요)",
    "time.fixed_blocks": "매주 고정으로 비워야 하는 시간 있어요?",
    "time.peak_window": "가장 잘 집중되는 시간대는요?",
    "recovery.tone": "못 한 날 어떤 톤이 좋아요?",
    "recovery.rest_ok": "쉬는 게 어때요 하는 제안을 받을 의향 있어요?",
    "recovery.downscope_unit": "밀렸을 때 할 일을 몇 분짜리까지 줄이면 해볼 만해요?",
}

# 하베스팅(다른 답에 섞여 나온 값으로 미리 채우기)을 **하지 않는** 슬롯.
#
# 기준은 하나다 — **그 슬롯의 질문이 묻는 것과 사용자가 흘린 말이 같은 것이어야** 한다.
# 하베스팅된 값은 '미충족' 이 아니어서, 안 물어본 답이 프로필로 새는 걸 막는 가드
# (v2.30-interview, `profile_memory.persist_profile_from_outcome` 의 unresolved 검사)가
# 아예 적용되지 않는다. 그래서 여기서 막지 못하면 되돌릴 자리가 없다.
#
# - goals.heaviest — goals.list 응답에서 파생(동적 보기)이라 별도 경로.
# - time.activity_window — 질문이 "이 시간 밖엔 일정을 안 잡아요" 라고 **약속**하는 하루
#   전체의 경계다. 실측: "새벽에만 집중이 돼서 밤에 작업해요" 한 마디에서 00:00~06:00 이
#   수확돼, 한 번도 묻지 않은 활동창이 그 사용자의 **유일한 배치 가능 시간**이 됐다.
#   프로필(내 정보)에도 그대로 저장되고, 계획은 22:00 에 일정을 잡아 놓고 "활동 가능
#   시간(00:00~06:00)과 겹치지 않아서" 라고 스스로를 반박했다. 집중이 잘 되는 때(peak)를
#   말한 것을 **일정을 잡아도 되는 경계**로 옮겨 적은 셈 — 두 질문은 다른 질문이다.
# - goals.weekly_time — 길이×빈도로 **유도되면 아예 묻지 않는** 슬롯이다(`is_slot_needed`).
#   그런데 수확은 그 유도 규칙을 보지 않아, 묻지 않기로 한 슬롯이 말없이 채워졌다. 그 값은
#   `weekly_hours_for_plan` 에서 유도값을 이기므로, 확인 카드는 "약 주 3.5시간(한 번 30분 ×
#   매일)" 이라 보여 주고 계획은 주 2시간으로 만들어졌다(실측). 묻지 않을 슬롯이면 채우지도
#   않는다 — 빈도를 '몰아서' 로 답해 유도가 안 될 때는 정식으로 묻는다.
#
# 여기 **넣지 않은** 슬롯들(검토 결과): time.peak_window·recovery.* 는 프로필에 저장되지만
# "오전에 집중이 잘돼"·"담백하게 해주세요" 처럼 사용자가 **그 질문의 답 그대로** 흘린 말을
# 받는다 — 질문이 묻는 것과 같은 것이라 위 기준에 걸리지 않고, 계획 전체를 가두는 경계도
# 아니다. identity.*·goals.*(목표 내용)는 애초에 하베스팅이 겨냥한 대상이다.
#
# ⚠️ 앞으로 슬롯을 더할 때: 그 값이 **계획 전체를 가두는 경계**(수면·취침 등 시간 창)이거나
# 프로필로 영속되는데 사용자가 그 질문에 직접 답해야만 알 수 있는 것이라면 여기 넣어라.
_PLAN_HARVEST_EXCLUDE: frozenset[str] = frozenset(
    {"goals.heaviest", "time.activity_window", "goals.weekly_time"}
)

PLAN_CATALOG = InterviewCatalog(
    kind="plan",
    slots=PLAN_SLOTS,
    required_keys=interview_adapter.REQUIRED_SLOT_KEYS,
    critical_slots=frozenset({"goals.list", "goals.heaviest"}),
    harvest_enabled=True,
    # 하베스팅 대상에서 제외 — goals.heaviest 는 goals.list 응답에서 파생(동적 보기)이라 별도.
    harvest_exclude=_PLAN_HARVEST_EXCLUDE,
    # heaviest 목표의 속성을 묻는 슬롯들 — 귀속이 확정되기 전에는 하베스팅하지 않는다
    # (`_per_goal_harvest_allowed`).
    per_goal_slots=frozenset(
        {
            "goals.current_level",
            "goals.session_length",
            "goals.frequency",
            "goals.preferred_time",
            "goals.weekly_time",
            "goals.deadlines",
            "goals.success_image",
            "goals.approach",
            "goals.materials",
        }
    ),
    default_questions=_PLAN_DEFAULT_QUESTIONS,
    context_labels=_PLAN_CONTEXT_LABELS,
    deadline_slot="goals.deadlines",
    prompt_next_question="interview/next_question",
    prompt_ambiguity="interview/ambiguity_score",
    prompt_summary="interview/summary",
    prompt_intake="interview/answer_intake",
)


# ─────────────────────────────────────────────────────────────────────────────
# ULTIMATE 카탈로그 — 궁극목표 인터뷰 (필수 9 + 선택 3)
# ─────────────────────────────────────────────────────────────────────────────

# ultimate.domain chip 8종 — §5.6 "도메인별 8축 카탈로그"(mandala_subgoals 완전 폴백의
# 축 이름과 1:1). goal.py 의 9종 category 와는 별개 축이다(사람이 고르는 "이 목표의 영역").
ULTIMATE_DOMAIN_OPTIONS: tuple[str, ...] = (
    "역량",
    "기술·방법",
    "체력·컨디션",
    "멘탈·루틴",
    "환경·도구",
    "사람·피드백",
    "점검·기록",
    "운·기회",
)

ULTIMATE_SLOTS: tuple[InterviewSlot, ...] = (
    InterviewSlot(
        "ultimate.statement",
        "당신이 인생에서 이루고 싶은 궁극적인 목표는 무엇인가요? 최대한 구체적으로 한 문장으로 알려주세요.",
        "text",
        True,
        "ultimate",
    ),
    InterviewSlot(
        "ultimate.domain",
        "이 목표는 어떤 영역에 가장 가까운가요?",
        "chip",
        True,
        "ultimate",
        options=ULTIMATE_DOMAIN_OPTIONS,
    ),
    InterviewSlot(
        "ultimate.horizon",
        "이 목표를 이루기까지 얼마나 걸릴 것 같으세요?",
        "chip",
        True,
        "ultimate",
        options=("3년", "5년", "7년", "10년", "10년 이상", "기한 없음"),
    ),
    InterviewSlot(
        "ultimate.measure",
        "그 목표를 이뤘는지 어떻게 확인할 수 있을까요? (숫자·사건·자격 등 판정 가능한 기준)",
        "text",
        True,
        "ultimate",
    ),
    InterviewSlot(
        "ultimate.success_image",
        "그 목표를 이룬 날, 어떤 장면이 떠오르나요?",
        "text",
        True,
        "ultimate",
    ),
    InterviewSlot(
        "ultimate.identity",
        "그때의 당신은 어떤 사람인가요?",
        "text",
        True,
        "ultimate",
    ),
    InterviewSlot(
        "ultimate.current_position",
        "지금은 이 목표에서 어느 정도 위치에 있나요?",
        "text",
        True,
        "ultimate",
    ),
    InterviewSlot(
        "ultimate.pillars_hint",
        "이 목표를 이루기 위해 꼭 필요한 축이 있다면 알려주세요 (없으면 넘겨도 돼요)",
        "text",
        True,
        "ultimate",
    ),
    InterviewSlot(
        "ultimate.constraints",
        "이 목표를 가로막는 걸림돌이 있다면 알려주세요 (없으면 넘겨도 돼요)",
        "text",
        True,
        "ultimate",
    ),
    InterviewSlot(
        "ultimate.values",
        "이 여정에서 타협하고 싶지 않은 가치가 있다면 골라주세요.",
        "chip",
        False,
        "ultimate",
        options=("성장", "자유", "안정", "인정", "영향력", "관계", "건강", "재미"),
    ),
    InterviewSlot(
        "ultimate.assets",
        "이미 가진 무기(강점·자원)가 있다면 알려주세요.",
        "text",
        False,
        "ultimate",
    ),
    InterviewSlot(
        "ultimate.role_model",
        "참고하고 싶은 선례나 롤모델이 있나요?",
        "text",
        False,
        "ultimate",
    ),
)

_ULTIMATE_CONTEXT_LABELS: dict[str, str] = {
    "ultimate.statement": "궁극 목표",
    "ultimate.domain": "영역",
    "ultimate.horizon": "시간 지평",
    "ultimate.measure": "판정 기준",
    "ultimate.success_image": "이룬 모습",
    "ultimate.identity": "그때의 나",
    "ultimate.current_position": "지금 위치",
    "ultimate.pillars_hint": "8축 힌트",
    "ultimate.constraints": "걸림돌",
    "ultimate.values": "가치",
    "ultimate.assets": "가진 무기",
    "ultimate.role_model": "선례",
}

_ULTIMATE_DEFAULT_QUESTIONS: dict[str, str] = {s.slot_key: s.label for s in ULTIMATE_SLOTS}

ULTIMATE_REQUIRED_SLOT_KEYS: tuple[str, ...] = tuple(
    s.slot_key for s in ULTIMATE_SLOTS if s.is_required
)

ULTIMATE_CATALOG = InterviewCatalog(
    kind="ultimate",
    slots=ULTIMATE_SLOTS,
    required_keys=ULTIMATE_REQUIRED_SLOT_KEYS,
    # 궁극 목표 선언(statement)과 판정 기준(measure) — 둘 다 없으면 만다라트를 세울 근거
    # 자체가 없어(#186), '없어/모름' 스킵을 받지 않고 상한까지 재질문한다.
    critical_slots=frozenset({"ultimate.statement", "ultimate.measure"}),
    # 하베스팅을 끈다 — harvest_slots 는 goals.* 10개가 서로 독립이라 교차 추출 이득이
    # 있던 구조를 전제로 만들어졌다. ultimate.* 9개는 각자 다른 질문이라 그 이득이 없다.
    harvest_enabled=False,
    harvest_exclude=frozenset(),
    per_goal_slots=frozenset(),
    default_questions=_ULTIMATE_DEFAULT_QUESTIONS,
    context_labels=_ULTIMATE_CONTEXT_LABELS,
    # 마감 개념이 없다 — horizon 은 확정 날짜가 아니라 chip 범위 추정이라 '지난 마감'
    # 재질문(#231)이 적용될 대상이 없다.
    deadline_slot=None,
    prompt_next_question="interview/ultimate_next_question",
    prompt_ambiguity="interview/ultimate_ambiguity_score",
    prompt_summary="interview/ultimate_summary",
)


# ─────────────────────────────────────────────────────────────────────────────
# 칩 답 정규화 — 저장 경계의 단일 관문
# ─────────────────────────────────────────────────────────────────────────────


def canonical_chip(slot: InterviewSlot, raw: str) -> str | None:
    """칩 답 하나를 **그 슬롯이 실제로 제시한 옵션**으로 정규화. 못 맞추면 None.

    왜 필요한가: 칩 값이 옵션으로 검증되지 않아, 슬롯에 없는 문자열이 그대로 저장돼 왔다.
    `_coerce_normalized` 는 harvest LLM 의 `normalized_value` 를 `str()` 해서 담고,
    `_coerce_answer` 는 클라이언트가 보낸 리스트를 그대로 믿는다. 그 구멍으로 사고가 두 번
    났다 — 둘 다 파서가 예상 못 한 문자열을 만나 숫자를 잘못 읽은 것이다:

    - v2.00: `"2시간 이상"` → **2분** (세션 길이 상한이 2분이 돼 계획이 붕괴)
    - v2.01: `"30분"` 이 주당 시간 슬롯에 → **30시간**(주 1800분)

    파서를 하나씩 고치는 건 두더지잡기다. 저장 경계에서 **옵션에 없는 값을 아예 안 받으면**
    그 부류가 통째로 닫힌다. 파서는 자기가 정의한 어휘만 보게 된다.

    세 단계로 맞춘다:
      1. 정확 일치.
      2. 공백 무시 일치 — LLM 이 `"2시간이상"` 처럼 붙여 쓰는 경우.
      3. **시간 값 일치** — `"120분"` ↔ `"2시간 이상"` 처럼 표기는 달라도 같은 시간이면
         옵션 쪽 표기로 정규화한다. `profile_memory.seed_slots_from_profile` 이 프로필의
         분 값을 `f"{n}분"` 으로 되돌려 시드로 넣는데, 그게 옵션에 없는 표기라 그대로
         저장돼 왔다(백필을 망가뜨릴 뻔한 시드 루프의 원인).

    옵션이 없는 chip 슬롯(`goals.heaviest` 처럼 런타임에 보기를 만드는 것)은 대조할 대상이
    없으므로 다듬기만 하고 통과시킨다.
    """
    cleaned = raw.strip()
    if not cleaned:
        return None
    if not slot.options:
        return cleaned
    if cleaned in slot.options:
        return cleaned
    squashed = "".join(cleaned.split())
    for option in slot.options:
        if "".join(option.split()) == squashed:
            return option
    minutes = duration_minutes_of(cleaned)
    if minutes is not None:
        for option in slot.options:
            if duration_minutes_of(option) == minutes:
                return option
    return None


# 우리말 수관형사로 쓴 시간 길이 — "한 시간"·"두 시간 반" 은 보기 "1시간"·"2시간 30분" 과
# **같은 답**이지 다른 답이 아니다. 칩 슬롯에도 직접 입력이 열려 있어(FE 플레이스홀더
# "직접 입력해도 돼요…") 이 표기가 실제로 들어온다. 숫자 파서(`chip_duration_min`)는 숫자만
# 읽으므로, 대조 직전에 숫자 표기로 옮겨 준다 — 보기를 넓히는 게 아니라 **같은 보기의 다른
# 표기**를 알아보는 것이다.
_KO_DURATION_NUMERALS: dict[str, str] = {
    "한": "1",
    "두": "2",
    "세": "3",
    "네": "4",
    "다섯": "5",
    "여섯": "6",
    "일곱": "7",
    "여덟": "8",
    "아홉": "9",
    "열": "10",
}
_KO_NUMERAL_RE = re.compile(
    "(" + "|".join(sorted(_KO_DURATION_NUMERALS, key=len, reverse=True)) + r")\s*(시간|분)"
)
# "1시간 반"·"한 시간 반" → 30분을 더한다. '반' 을 먼저 풀어야 수관형사 치환과 겹치지 않는다.
_HALF_HOUR_RE = re.compile(r"시간\s*반")


def duration_minutes_of(text: str) -> int | None:
    """자유 입력 한 줄에서 시간 길이(분)를 읽는다. 못 읽으면 None.

    `interview_adapter.chip_duration_min` 과 **같은 파서**를 쓰되(단위 오독 사고 v2.00/v2.01
    이후 파서를 새로 만들지 않는다) 우리말 수관형사·'반' 을 먼저 숫자로 옮긴다.
    """
    normalized = _HALF_HOUR_RE.sub("시간 30분", text)
    normalized = _KO_NUMERAL_RE.sub(
        lambda m: _KO_DURATION_NUMERALS[m.group(1)] + m.group(2), normalized
    )
    return interview_adapter.chip_duration_min(sv.dump(sv.ChipAnswer((normalized,))))


def is_duration_slot(slot: InterviewSlot | None) -> bool:
    """보기가 **전부 시간 길이**인 칩 슬롯인가 (15분 / 1시간 30분 / 4시간 이상 …).

    이런 슬롯에서는 **숫자가 곧 답**이다 — "20분" 과 "30분" 은 비슷한 답이 아니라 다른 답이고,
    그 차이가 모든 블록의 길이와 주당 분량으로 그대로 번진다. 그래서 자유 입력을 보기로
    맞출 때 의미 추론(LLM)이 아니라 숫자 대조(룰)를 쓴다 — `interview._chip_rule_decides`.
    """
    return bool(slot and slot.options) and all(
        duration_minutes_of(option) is not None for option in (slot.options if slot else ())
    )


def canonical_chip_values(
    slot: InterviewSlot | None, values: Sequence[Any], *, drop_unknown: bool = True
) -> list[str]:
    """칩 답 목록을 옵션으로 정규화.

    `drop_unknown=True`(기본) — 못 맞춘 값을 **버린다**. 지어낸 값을 사용자의 답으로 저장하지
    않는다는 뜻이다. 전부 버려져 빈 목록이 되면 호출자가 그 슬롯을 '미응답' 으로 두고,
    인터뷰가 **실제 보기를 들고 다시 묻는다**. 추측한 값으로 슬롯을 닫는 것보다 한 번 더 묻는
    편이 낫다. 신뢰할 수 없는 출처(harvest LLM, 프로필 시드)에 쓴다.

    `drop_unknown=False` — 못 맞춘 값을 원문 그대로 남긴다. **사용자가 방금 누른 답**에 쓴다:
    거기서 값을 버리면 "칩을 눌렀는데 서버가 미응답으로 보고 같은 질문을 또 하는" 루프가 된다.
    카탈로그 옵션은 시간이 지나며 바뀌는데(주당 시간 척도가 한 번 개편됐다) 옛 척도 값을 든
    클라이언트를 그 루프에 빠뜨릴 수는 없다. 표기 정규화만 얻고 거부는 하지 않는다.

    `slot` 이 None 이면(카탈로그에 없는 키) 대조할 대상이 없으므로 다듬기만 한다.
    """
    out: list[str] = []
    for value in values:
        raw = str(value)
        picked = canonical_chip(slot, raw) if slot is not None else raw.strip()
        if picked is None and not drop_unknown:
            picked = raw.strip()
        if picked and picked not in out:
            out.append(picked)
    return out


GLOBAL_SCOPE_HINT = "(전역 설정 — 특정 목표가 아니다. 목표 이름을 문장에 넣지 마라.)"
"""목표별이 **아닌** 슬롯을 물을 때 `{{goal_title}}` 자리에 넣는 값.

⚠️ 여기에 **실제 목표 이름이 절대 들어가면 안 된다.** `identity.*` · `time.*` · `recovery.*`
는 모든 목표에 공통으로 적용되는 전역 설정인데, 질문에 목표 이름이 붙으면 사용자는 그
설정이 **그 목표에만 적용된다고 오해한다**(#187 과교정 실측: 실 LLM 3회에 8건).

프롬프트에도 같은 규칙이 산문으로 있다. 그건 **측정된 회귀의 가드**라 지우지 않는다 —
이 상수는 그 위에 **이름을 아예 주지 않는** 층을 하나 더 얹는 것이다. 규칙을 어기려 해도
어길 이름이 없다.
"""


def is_goal_scoped(slot_key: str) -> bool:
    """이 슬롯이 **특정 목표 하나**에 대한 질문인가.

    `goals.list`(여러 목표를 모으는 자리)와 `goals.heaviest`(그중 하나를 고르는 자리)는
    **제외**다 — 보기 중 하나를 질문의 대상 명사로 지목하면 나머지를 배제하게 된다.

    ⚠️ 이 판정은 `default_questions` 의 `{goal}` 자리와 **같은 집합이어야 한다**(룰 폴백이
    거기서 이름을 채운다). 두 경로가 갈리면 LLM 질문과 폴백 질문이 다른 대상을 가리킨다 —
    `tests/test_interview_goal_scope.py` 가 그 일치를 못 박는다.
    """
    return slot_key.startswith("goals.") and slot_key not in {"goals.list", "goals.heaviest"}


CATALOGS: dict[str, InterviewCatalog] = {"plan": PLAN_CATALOG, "ultimate": ULTIMATE_CATALOG}

# 보기를 **여러 개** 담아도 전부 쓰는 칩 슬롯 — 나머지 칩·select 는 어댑터가 첫 값만 읽는다
# (`interview_adapter._first`: 역할·학기·가장 무거운 목표·빈도·세션 길이·톤 등). FE 가 모든
# 칩 질문에 "여러 개 골라도 돼요" 를 띄워, 두 번째로 고른 목표·빈도가 말없이 버려졌다.
# `Question.multiple`·`SlotCatalogEntry.multiple` 로 내보내 FE 가 단일/복수 선택을 가른다.
# ⚠️ 어댑터가 목록 전체를 읽는 슬롯(`time.peak_window` → peak_window, `ultimate.values` →
# values)과 **같아야** 한다 — `tests/test_interview_route.py` 가 못 박는다.
MULTI_SELECT_SLOTS: frozenset[str] = frozenset({"time.peak_window", "ultimate.values"})

__all__ = [
    "CATALOGS",
    "MULTI_SELECT_SLOTS",
    "GLOBAL_SCOPE_HINT",
    "is_goal_scoped",
    "canonical_chip",
    "canonical_chip_values",
    "duration_minutes_of",
    "is_duration_slot",
    "PLAN_CATALOG",
    "ULTIMATE_CATALOG",
    "ULTIMATE_DOMAIN_OPTIONS",
    "ULTIMATE_REQUIRED_SLOT_KEYS",
    "InterviewCatalog",
    "InterviewSlot",
]
