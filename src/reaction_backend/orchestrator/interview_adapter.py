"""Interview State ↔ 경계 계약 어댑터 (ADR-0005 §7.4 규약).

`build_outcome` 은 Interview 그래프 터미널에서 호출되는 **순수 함수**다:
- slot_answers(인터뷰가 누적한 정규화 답) → `InterviewOutcome` 결정적 투영.
- **LLM 호출 0회 / DB 무관** → 경계에서 8s timeout·rate limit 실패 표면이 없다.
- early_finish/정체로 빈 필수 슬롯은 안전한 default 로 채우고 `unresolved_slots` 에 기록.

slot_answers 값 형식과 읽기 함수는 `domain/slot_value` 하나에 있다(chip·text·range·spec·
pending·skip). 여기서 슬롯 dict 를 직접 열어 보지 않는다 — 읽기 함수가 4벌이던 시절 서로 조금씩
달랐다. `spec`(자료 확정, ADR-0010 §1 ③)은 `_materials_note` 가 각 항목을 텍스트로 풀어
`materials_note` 에 싣는다 — 새 프롬프트 변수·새 인젝션 방어를 만들지 않고 `materials_for_prompt`
의 기존 경로를 그대로 통과시키기 위해서다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from reaction_backend.domain import slot_value as sv
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewEndReason,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)

# 필수 슬롯 중 비어 있으면 default 로 채우되 unresolved_slots 에 기록할 키.
# (mock.interview.SLOT_CATALOG 의 is_required=True 슬롯과 일치)
REQUIRED_SLOT_KEYS: tuple[str, ...] = (
    "identity.role",
    "identity.season",
    # 전역 집중 시간대는 목표를 묻기 **전** 프로필 단계에서 — goals.preferred_time 과 나란히
    # 두면 같은 질문을 두 번 하는 것처럼 읽힌다. 재인터뷰에선 이월(CARRY_OVER)돼 생략된다.
    "time.peak_window",
    "goals.list",
    "goals.heaviest",
    "goals.current_level",
    # 길이·빈도를 **먼저** 묻는다. 주당 시간 = 세션 길이 × 빈도라, 둘을 알면 세 번째는
    # 계산된다(`derived_weekly_hours`). 사람은 "한 번에 얼마나"(체감)·"주 며칠"(달력)은
    # 잘 답하지만 주 단위 총량은 곱셈이라 틀린다 — 실측에서 세 답이 다 있는 세션의 **절반**이
    # 1시간 이상 어긋났고(최대 6시간), 그 불일치가 매 계획의 분량 경고로 새어 나왔다.
    "goals.session_length",
    "goals.frequency",
    "goals.preferred_time",
    # 빈도를 '상관없음/몰아서'로 답해 계산이 안 될 때만 묻는다 (`is_slot_needed`).
    "goals.weekly_time",
    "goals.deadlines",
    "goals.success_image",
    "goals.approach",
    "goals.materials",
    "time.activity_window",
    "recovery.tone",
    "recovery.rest_ok",
    "recovery.downscope_unit",
)

# 재인터뷰 시 지난 완료 인터뷰에서 그대로 이어받는 '지속형(너에 대한)' 슬롯 — 매번 다시 묻지
# 않는다(#reduce-reask). 목표 관련(goals.*)과 이번 주 한정(energy.weekly_drain '이번 주 컨디션')은
# 계획 주기마다 바뀌므로 제외하고 새로 묻는다. 프로필은 설정에서 수정 가능하므로, 여기서 이어받아도
# 사용자가 언제든 바꿀 수 있다.
#
# ⚠️ `identity.season`(학기 중/방학)은 **이월하지 않는다** — 학기 단위로 바뀌는데 설정에 고칠
# 칸이 없어, 8월에 '방학' 이라 답한 사용자가 9월 재인터뷰에서도 다시 묻지 않고 계획이 계속
# 방학 맥락으로 만들어졌다. 다시 묻는 비용은 칩 한 번이다.
CARRY_OVER_SLOT_KEYS: frozenset[str] = frozenset(
    {
        "identity.role",
        "identity.major",
        "time.activity_window",
        "time.peak_window",
        "energy.focus_duration",
        "energy.break_pattern",
        "recovery.tone",
        "recovery.rest_ok",
        "recovery.downscope_unit",
    }
)

_DEFAULT_ACTIVITY = TimeRange(start="09:00", end="23:00")
_DEFAULT_TONE = "담백"
_DEFAULT_DOWNSCOPE_MIN = 10

# goals.list 미입력(early_finish 등) 시 core_goals min_length=1 계약을 맞추는 placeholder.
# 실제 Goal 로 영속하면 안 되며(#88), First Plan SAVING 이 이 sentinel 을 걸러낸다.
PLACEHOLDER_GOAL_TITLE = "(미입력 목표)"


def is_placeholder_goal(goal: GoalCandidate) -> bool:
    """미입력 placeholder 목표인지 — 영속/노출 대상에서 제외 (#88)."""
    return goal.confidence == 0.0 and goal.title == PLACEHOLDER_GOAL_TITLE


# ─────────────────────────────────────────────────────────────────────────────
# 값 추출 헬퍼 — 슬롯 값 읽기는 `domain/slot_value`, 여기는 이 어댑터 고유의 해석만
# ─────────────────────────────────────────────────────────────────────────────


def _materials_note(value: Mapping[str, Any] | None) -> str | None:
    """`goals.materials` 슬롯 → `materials_note` 텍스트. `text` 는 원문 그대로(`slot_value.text`
    와 동일 규칙), `spec`(ADR-0010 §1 ③)은 구조화 데이터를 텍스트로 풀어낸다. `spec` 은
    `items` 에 1~2건(책 1개·영상 1개까지, `MaterialsSpecConfirmRequest` 가 강제)을 담을 수
    있다 — 두 건이면 빈 줄로 이어붙인다("이론은 책으로, 반복 트레이닝은 영상으로" 처럼
    Method Agent 가 `both` 를 권했을 때).

    텍스트로 푸는 이유: 이미 검증된 `first_plan_adapter.materials_for_prompt` 의 울타리
    (injection fence)·"자료 미제공" 가드를 그대로 통과시키기 위해서다 — 새 프롬프트
    변수도 새 방어 로직도 만들지 않는다. `goal_decompose` 는 이미 "참고 자료 원문에
    목차·주차·챕터가 있으면 그대로 뼈대로 삼아라" 는 규칙을 갖고 있어서, 영상 커리큘럼
    처럼 항목마다 분량(분)이 붙어 있으면 그 규칙이 세션 길이까지 잡아준다(L0 핵심 발견 —
    "3. DFS & BFS [58분]" 형태로 이미 텍스트에 실려 있다).
    """
    if isinstance(sv.parse(value), sv.SpecAnswer):
        return _format_materials_spec(value or {})
    return sv.text(value)


def _format_materials_spec(value: Mapping[str, Any]) -> str | None:
    items = value.get("items")
    if not isinstance(items, list) or not items:
        return None
    notes = [n for n in (_format_one_spec_item(item) for item in items) if n]
    return "\n\n".join(notes) if notes else None


def _format_one_spec_item(item: Mapping[str, Any]) -> str | None:
    if item.get("kind") == "video":
        return _format_video_spec(item)
    if item.get("kind") == "book":
        return _format_book_spec(item)
    return None


def _format_book_spec(value: Mapping[str, Any]) -> str:
    """도서 자료 → 텍스트. `pages_per_session`/`total_sessions` 가 있으면(항상, `page_count`
    만으로 계산됨 — `materials_spec.compute_book_pace`) 세션당 권장 페이지를 명시적으로
    적는다 — 이게 "책을 하루에 얼마나" 의 실질이다. `chapters` 항목에 `sessions` 가 있으면
    (목차 전 챕터에 페이지가 있을 때만, best-effort) 균등 분할이 아니라 **그 챕터가 정확히
    몇 세션에 끝나는지** 를 적는다 — 세션이 챕터 중간에서 끊기지 않는다는 뜻."""
    title = str(value.get("title") or "")
    author = str(value.get("author") or "")
    lines = [f"[도서] {title}" + (f" ({author})" if author else "")]
    page_count = value.get("page_count") or 0
    if page_count:
        lines.append(f"총 {page_count}쪽")

    pages_per_session = value.get("pages_per_session")
    total_sessions = value.get("total_sessions")
    if pages_per_session and total_sessions:
        lines.append(f"권장 진도: 세션당 약 {pages_per_session}쪽 (총 {total_sessions}세션 예상)")

    chapters = value.get("chapters") or []
    if chapters:
        lines.append("목차:")
        for c in chapters:
            chapter_title = c.get("title", "")
            end_page = c.get("end_page")
            sessions = c.get("sessions")
            if end_page and sessions:
                lines.append(f"- {chapter_title} (~{end_page}쪽까지, 약 {sessions}세션)")
            elif end_page:
                lines.append(f"- {chapter_title} (~{end_page}쪽까지)")
            else:
                lines.append(f"- {chapter_title}")
    return "\n".join(lines)


def _format_video_spec(value: Mapping[str, Any]) -> str:
    title = str(value.get("title") or "")
    channel = str(value.get("channel_title") or "")
    lines = [f"[영상 강의] {title}" + (f" ({channel})" if channel else "")]
    video_count = value.get("video_count") or 0
    total_minutes = value.get("total_minutes") or 0
    if video_count or total_minutes:
        lines.append(f"총 {video_count}편 · {total_minutes}분")
    curriculum = value.get("curriculum") or []
    if curriculum:
        lines.append("커리큘럼:")
        lines.extend(
            f"{i}. {item.get('title', '')} ({item.get('minutes', 0)}분)"
            for i, item in enumerate(curriculum, start=1)
        )
    if value.get("truncated"):
        lines.append("(재생목록이 더 있어 앞부분까지만 담았어요)")
    return "\n".join(lines)


def _range(value: Mapping[str, Any] | None) -> TimeRange | None:
    window = sv.time_range(value)
    return TimeRange(start=window[0], end=window[1]) if window else None


def _bool(value: Mapping[str, Any] | None, *, default: bool = False) -> bool:
    """chip 형태의 예/아니오 슬롯 해석. 첫 chip 값이 긍정 어휘면 True."""
    first = sv.first_chip(value)
    if first is None:
        return default
    return first not in {"아니오", "no", "false", "싫어요", "거절"}


# ─────────────────────────────────────────────────────────────────────────────
# 경계 계약 빌드 (순수 함수)
# ─────────────────────────────────────────────────────────────────────────────


def build_outcome(
    *,
    session_id: str,
    slot_answers: Mapping[str, Mapping[str, Any] | None],
    ambiguity_final: float,
    end_reason: InterviewEndReason,
    analysis_source: Literal["llm", "rule"],
) -> InterviewOutcome:
    """slot_answers → InterviewOutcome. LLM 0회·순수함수.

    빈 필수 슬롯은 default 로 채우고 `unresolved_slots` 에 키를 남긴다 (First Plan 이
    VALIDATING 에서 보완 질문/재입력 분기를 띄울 수 있도록). **스킵으로 닫힌 슬롯도 남긴다** —
    그 슬롯의 값도 default 다(#499, `unresolved_required_keys`).
    """
    unresolved = unresolved_required_keys(REQUIRED_SLOT_KEYS, slot_answers)

    identity = IdentityContext(
        role=sv.first_chip(slot_answers.get("identity.role")) or "미상",
        season=sv.first_chip(slot_answers.get("identity.season")) or "미상",
        major=sv.text(slot_answers.get("identity.major")),
    )

    core_goals = _build_goals(slot_answers)
    availability = _build_availability(slot_answers)
    preferences = _build_preferences(slot_answers)
    horizon = _max_deadline(core_goals)

    return InterviewOutcome(
        session_id=session_id,
        generated_at=now_kst(),
        end_reason=end_reason,
        ambiguity_final=ambiguity_final,
        analysis_source=analysis_source,
        identity=identity,
        core_goals=core_goals,
        availability=availability,
        preferences=preferences,
        horizon=horizon,
        unresolved_slots=unresolved,
    )


def _build_goals(slot_answers: Mapping[str, Mapping[str, Any] | None]) -> list[GoalCandidate]:
    """goals.list 항목들을 GoalCandidate 로. heaviest 1개는 focus tier 로 표시.

    core_goals 는 min_length=1 계약이므로 비어 있으면 placeholder 1개를 둔다
    (unresolved_slots 에 'goals.list' 가 이미 기록되어 First Plan 이 보완 분기).

    `heaviest` 가 `titles` 에 없을 수 있다 — `goals.heaviest` 의 chip 보기가
    `goals.list` 응답뿐 아니라 승격된 만다라 축 목표도 함께 내려가기 때문이다
    (`routes/interview.py::_question_options`, ADR-0008 §8 "B"). 여기서 합쳐 두지
    않으면 사용자가 그 보기를 고른 순간 `is_heaviest` 가 어떤 후보에도 안 붙어
    (title 매칭 실패) 마감·주당시간 등 heaviest 전용 필드가 통째로 유실된다.
    """
    titles = sv.items(slot_answers.get("goals.list"))
    heaviest = resolved_heaviest(slot_answers)
    if heaviest and heaviest not in titles:
        titles = [*titles, heaviest]
    deadline = sv.text(slot_answers.get("goals.deadlines"))  # date_picker → raw "YYYY-MM-DD"
    success_image = sv.text(slot_answers.get("goals.success_image"))
    current_level = sv.text(slot_answers.get("goals.current_level"))
    weekly_hours = weekly_hours_for_plan(slot_answers)
    session_length = chip_duration_min(slot_answers.get("goals.session_length"))
    preferred_time = sv.first_chip(slot_answers.get("goals.preferred_time"))
    frequency = _chip_frequency(slot_answers.get("goals.frequency"))
    approach_note = sv.text(slot_answers.get("goals.approach"))
    materials_note = _materials_note(slot_answers.get("goals.materials"))

    if not titles:
        return [
            GoalCandidate(
                title=PLACEHOLDER_GOAL_TITLE,
                category="other",
                tentative_tier="maintain",
                confidence=0.0,
            )
        ]

    goals: list[GoalCandidate] = []
    for title in titles:
        is_heaviest = heaviest is not None and title == heaviest
        goals.append(
            GoalCandidate(
                title=title,
                category="other",  # First Plan 이 카테고리 정규화 (베이스라인은 보류)
                is_heaviest=is_heaviest,
                deadline=deadline if is_heaviest else None,
                success_image=success_image if is_heaviest else None,
                current_level=current_level if is_heaviest else None,
                weekly_hours=weekly_hours if is_heaviest else None,
                session_length_min=session_length if is_heaviest else None,
                preferred_time=preferred_time if is_heaviest else None,
                frequency_per_week=frequency if is_heaviest else None,
                approach_note=approach_note if is_heaviest else None,
                materials_note=materials_note if is_heaviest else None,
                tentative_tier="focus" if is_heaviest else "maintain",
                confidence=0.5,
            )
        )
    return goals


def _build_availability(
    slot_answers: Mapping[str, Mapping[str, Any] | None],
) -> AvailabilityProfile:
    activity = _range(slot_answers.get("time.activity_window")) or _DEFAULT_ACTIVITY
    peak = sv.chips(slot_answers.get("time.peak_window"))
    # no_touch_windows / fixed_block_hints 는 인터뷰에서 더 이상 채우지 않는다(#audit).
    # 둘 다 답을 받아도 **소비하는 코드가 없었다** — chip 카테고리만으로는 스케줄러가 쓸 실제
    # 시각이 없고(no_touch), 자유서술 힌트는 계획 코드 참조가 0회였다(fixed_blocks).
    # 실제 시각 차단은 활동창(수면 여집합) + 고정일정(S05, 요일·시각 보유)이 담당한다.
    # `fixed_block_hints` 필드 자체는 스키마에 남긴다 — 이미 저장된 outcome(JSON)이 이 키를
    # 갖고 있어, 필드를 지우면 옛 세션 역직렬화가 깨진다.
    return AvailabilityProfile(activity_window=activity, peak_window=peak)


def _build_preferences(
    slot_answers: Mapping[str, Mapping[str, Any] | None],
) -> PreferenceProfile:
    return PreferenceProfile(
        recovery_tone=sv.first_chip(slot_answers.get("recovery.tone")) or _DEFAULT_TONE,
        rest_ok=_bool(slot_answers.get("recovery.rest_ok"), default=True),
        downscope_unit_min=(
            chip_duration_min(slot_answers.get("recovery.downscope_unit")) or _DEFAULT_DOWNSCOPE_MIN
        ),
        # ⚠️ 반드시 `chip_duration_min` — 시간 단위를 읽는 파서다. 예전에는 숫자만 긁는
        # 파서를 써서 이 슬롯의 마지막 칩 **"2시간 이상" 을 2(분)** 으로 읽었다. 그 값은
        # `session_min_for` 의 전역 폴백이라 목표별 세션 길이를 안 답한 사용자의 계획에서
        # **세션 길이 상한이자 주당 분량의 기준**이 된다 — 계획 전체가 2~10분 카드로
        # 붕괴하고 주당 분량이 30분이 됐다(정상 360분). `goals.session_length` 는 처음부터
        # 이 파서를 쓰고 있었다.
        focus_duration_min=chip_duration_min(slot_answers.get("energy.focus_duration")),
        break_pattern=sv.first_chip(slot_answers.get("energy.break_pattern")),
        weekly_energy=sv.first_chip(slot_answers.get("energy.weekly_drain")),
    )


def derived_weekly_hours(slot_answers: Mapping[str, Any]) -> float | None:
    """세션 길이 × 빈도 → 주당 시간. 둘 중 하나라도 없으면 None.

    주당 시간을 **묻지 않고 계산하는** 이유: 세 값은 수학적으로 종속(총량 = 길이 × 빈도)인데
    셋 다 물으면 사용자가 곱셈을 대신해야 한다. 실측(세 답이 모두 있는 세션 8개) 중 4개가
    1시간 이상 어긋났고 최대 6시간 차이였다 — '주 8시간 · 한 번에 2시간 · 매일'(= 실제 14시간)
    같은 답이 거짓이 아니라, 머릿속에서 2×7 을 하지 않았을 뿐이다.
    """
    minutes = chip_duration_min(slot_answers.get("goals.session_length"))
    freq = _chip_frequency(slot_answers.get("goals.frequency"))
    if not minutes or not freq:
        return None
    return round(minutes * freq / 60, 1)


def weekly_hours_for_plan(slot_answers: Mapping[str, Any]) -> int | None:
    """계획이 실제로 쓰는 주당 시간(정수). 없으면 None.

    **확인 카드와 목표 기록이 반드시 같은 값을 봐야 한다** — 이 함수가 그 단일 진실이다.
    예전엔 목표 기록(`_build_goals`)은 직접 답한 `goals.weekly_time` 을, 확인 카드
    (`interview._summary_variables`)와 분량 경고는 유도값(길이×빈도)을 각자 읽었다. 그래서
    한 사용자의 같은 인터뷰가 "주 3.5시간 쓰게 돼요" 라고 확인받고 계획은 주 2시간으로
    만들어졌다(실측). 사용자가 [이대로 진행]을 누른 숫자와 계획이 쓰는 숫자가 다르면,
    그 확인 카드는 확인이 아니다.

    **직접 답한 값이 이긴다.** 새 인터뷰는 길이·빈도를 답하면 주당 시간을 아예 묻지 않고
    (`is_slot_needed`) 수확도 하지 않으므로(`_PLAN_HARVEST_EXCLUDE`) 자연히 유도값을 쓴다 —
    즉 이 분기는 **사용자가 정말로 그 질문에 답했을 때만** 탄다. 그때는 답한 값을 그대로
    쓴다: 유도값을 우선하면 이미 저장된 세션의 계획 분량이 말없이 바뀐다(실측에 '주 8시간 ·
    한 번 2시간 · 매일' 세션이 있는데 유도하면 14시간, **부하가 두 배**다).

    한 시간 미만(예: 15분씩 주 1회)은 1시간으로 올린다 — `weekly_hours` 가 정수 계약이라
    0.5 를 담을 수 없고, 0 이면 '미입력' 과 구분되지 않는다.
    """
    answered = _chip_hours(slot_answers.get("goals.weekly_time"))
    if answered is not None:
        return answered
    derived = derived_weekly_hours(slot_answers)
    return max(1, round(derived)) if derived else None


def is_slot_needed(slot_key: str, slot_answers: Mapping[str, Any]) -> bool:
    """이 슬롯을 **지금 사용자에게 물어야 하는가**. 다른 답에서 유도되면 False.

    FSM(`interview._next_required_slot`)과 `unresolved_slots` 판정이 **같은 술어**를 써야
    한다 — 한쪽만 건너뛰면 묻지도 않은 슬롯이 영영 미해결로 남아 인터뷰가 끝나지 않는다.
    직접 부르지 말고 `open_required_keys`·`unresolved_required_keys` 를 쓰라 — 이 술어와
    값 판정을 **같이** 적용해야 판정이 갈리지 않는다(아래 함수 주석 참고).
    """
    if slot_key == "goals.weekly_time":
        return derived_weekly_hours(slot_answers) is None
    return True


def open_required_keys(required_keys: Sequence[str], slot_answers: Mapping[str, Any]) -> list[str]:
    """아직 **열려 있는**(다시 물어야 하는) 필수 슬롯 키 — 순서 보존.

    열려 있다 = 물어야 하고(`is_slot_needed`) 아직 닫히지 않았다(`slot_value.is_settled`).
    스킵은 닫힌 슬롯이다 — 다시 물으면 같은 질문이 끝없이 반복된다(#79). 쓰는 곳 둘이 같은
    값을 봐야 한다:

    - `interview._next_required_slot` — 다음에 물을 슬롯 (없으면 FSM 완료)
    - `api/routes/interview._remaining_required` — FE 명료성 지표(`ambiguityScore`)

    둘이 손으로 각자 조립하다 실제로 갈렸다: FE 지표만 `is_slot_needed` 를 빠뜨려,
    `goals.weekly_time` 이 길이×빈도로 **유도돼 묻지 않은** 세션에서 영원히 1로 남았다.
    FSM 은 `completed` 로 끝났는데 진행바만 17/18 에 멈췄다.

    ⚠️ outcome 의 `unresolved_slots` 는 **다른 질문**이다 — `unresolved_required_keys`.
    """
    return [
        k
        for k in required_keys
        if is_slot_needed(k, slot_answers) and not sv.is_settled(slot_answers.get(k))
    ]


def unresolved_required_keys(
    required_keys: Sequence[str], slot_answers: Mapping[str, Any]
) -> list[str]:
    """사용자가 **값을 주지 않은** 필수 슬롯 키 — outcome 의 `unresolved_slots`, 순서 보존.

    아직 안 물은 슬롯 + **스킵으로 닫힌 슬롯**이다(`slot_value.has_answer`). 둘 다 outcome 이
    기본값으로 채우는 칸이라 `unresolved_slots`("default 처리된 필수 슬롯")에 들어가야 한다 —
    스킵을 빼면 `profile_memory` 가 그 기본값을 사용자의 답으로 영속하고, 재인터뷰가 그 칸을
    다시 묻지 않았다(#499). 유도 슬롯(`is_slot_needed` 가 False)은 값이 계산되므로 넣지 않는다.

    `open_required_keys`(다시 물을 슬롯)와 일부러 다르다: 스킵한 슬롯은 다시 묻지 않지만
    사용자의 답도 아니다. 그래서 정상 종료(`completed`)여도 이 목록이 비지 않을 수 있다.
    """
    return [
        k
        for k in required_keys
        if is_slot_needed(k, slot_answers) and not sv.has_answer(slot_answers.get(k))
    ]


def resolved_heaviest(slot_answers: Mapping[str, Any]) -> str | None:
    """이번 계획이 집중할 목표 제목 — 고른 heaviest, 못 골랐으면(스킵) **첫 목표**.

    못 고른 경우를 첫 목표로 읽는 건 계획 쪽 규칙과 같은 대상이다(`first_plan_adapter` —
    is_heaviest 가 없으면 첫 목표). 그래야 목표별 답(마감·세션 길이·빈도)이 그 목표에 붙는다.
    대체는 **읽을 때만** 한다 — 저장은 스킵 그대로라 `unresolved_slots` 에 heaviest 가 남는다
    (시스템이 고른 값은 사용자의 답이 아니다, #499). 한 번도 안 물은 경우(조기 종료)는
    대체하지 않는다 — 그건 고르지 못한 게 아니라 묻지 않은 것이다.

    목표 기록(`_build_goals`)·확인 카드(`interview._summary_variables`)·목표별 질문의 대상
    (`interview._heaviest_goal_hint`)이 이 함수 하나를 읽는다.
    """
    value = slot_answers.get("goals.heaviest")
    picked = sv.first_chip(value) or next(iter(sv.items(value)), None)
    if picked:
        return picked
    if isinstance(sv.parse(value), sv.Skipped):
        return next(iter(sv.items(slot_answers.get("goals.list"))), None)
    return None


def _chip_hours(value: Mapping[str, Any] | None) -> int | None:
    """'6시간'·'15시간 이상' 같은 chip 답에서 **시간** 수를 추출. 없으면 None.

    ⚠️ 단위를 **읽어야** 한다. 예전에는 숫자만 긁어서 `"30분"` 을 **30시간**(주 1800분)으로,
    `"1시간 30분"` 을 **130시간**으로 읽었다. `energy.focus_duration` 에서 같은 파서가
    "2시간 이상" 을 2분으로 만든 사고(v2.00)와 **같은 버그의 반대 방향**이다.

    카탈로그 옵션은 전부 "N시간" 이라 지금까지 드러나지 않았지만, 옵션 밖 값이 **도달할 수
    있다** — 채점·수확 LLM 값은 `interview._coerce_normalized` 가 보기로 거르지만, 사용자가
    누른 칩 구조(`interview_runner._coerce_answer`, `drop_unknown=False`)와 이미 저장된 옛
    세션 값은 거르지 않는다. 그래서 단위를 읽는 파서가 여전히 필요하다.

    한 시간 미만은 **1시간으로 올린다** — `weekly_hours` 가 정수 계약이라 0.5 를 담을 수 없고,
    0 으로 내리면 '미입력' 과 구분이 안 돼 density 프리셋으로 떨어진다. 유도 경로
    (`derived_weekly_hours` → `max(1, round(...))`)가 이미 쓰는 규약과 같다.
    """
    minutes = chip_duration_min(value)
    return None if minutes is None else max(1, round(minutes / 60))


def chip_duration_min(value: Mapping[str, Any] | None) -> int | None:
    """'1시간 30분'·'2시간'·'30분' 같은 chip 답에서 총 분(min)을 추출. 없으면 None.

    **공개 함수다** — `scripts.preview_attention_span_backfill` 이 저장된 슬롯 답을 고친
    파서로 다시 읽는 데 쓴다. 시간 단위를 못 읽는 파서를 새로 만들지 말 것(v2.00 사고).

    숫자 뒤 '시'(간)면 ×60, '분'이면 그대로 누적. 예: '1시간 30분' → 90, '2시간' → 120.
    """
    first = sv.first_chip(value)
    if first is None:
        return None
    total, num = 0, ""
    for ch in first:
        if ch.isdigit():
            num += ch
        elif ch == "시" and num:  # '시간'
            total += int(num) * 60
            num = ""
        elif ch == "분" and num:
            total += int(num)
            num = ""
    return total or None


def _chip_frequency(value: Mapping[str, Any] | None) -> int | None:
    """'매일'·'주 3회' 같은 빈도 chip 에서 주당 횟수를 추출. '몰아서/상관없음'·미입력은 None.

    '매일'은 7로, '주 N회'는 N으로 환원한다. 숫자도 '매일'도 아니면(몰아서·상관없음) None →
    볼륨(weekly_hours) 기반 세션 수 산정으로 폴백한다.
    """
    first = sv.first_chip(value)
    if first is None:
        return None
    if "매일" in first:
        return 7
    digits = "".join(c for c in first if c.isdigit())
    return int(digits) if digits else None


def _max_deadline(goals: Sequence[GoalCandidate]) -> str | None:
    deadlines = [g.deadline for g in goals if g.deadline]
    return max(deadlines) if deadlines else None  # ISO "YYYY-MM-DD" 는 사전식 = 시간식
