"""교재 카드로 계획 만들기 — 마일스톤과 세션을 책 단원 순서로 짠다 (LLM 0회).

목표에 교재 카드가 확정돼 있으면(`GoalCandidate.curriculum`) 두 LLM 단계를 이 모듈이 대신한다:

    Stage A  POST /plans/milestones  → `milestones`      (plan_milestones 대신)
    Stage B  분해(decompose_goal)     → `decomposition`   (goal_decompose 대신)

대신하는 이유는 `domain/curriculum.py` 독스트링에 있다 — 해커스 교재 목차는 API 로 못 얻어
LLM 이 책에 없는 단위를 지어냈고(기준선: 책 단원을 가리키는 세션 0/16), 카드가 있으면 단원
순서는 **사실**이라 지어낼 이유가 없다.

## 단계 ↔ 마일스톤 대응

단계는 **단원 개수로** 나눈 것이라 같은 교재·같은 단계 수면 언제 다시 계산해도 같다. 그래서
2주기 이후에도 영속된 마일스톤 개수만 보고 단계 수를 역산해(`_phase_count_from`) 같은 단계를
다시 만들고, **위치(index)로** 짝짓는다. 다음 주기가 어디서부터 책을 이어야 하는지는 기존
마일스톤 커서(ADR-0007)가 이미 안다 — 진도용 저장소가 따로 필요 없다.

짝이 안 맞으면(사용자가 마일스톤을 추가·삭제했다) `None` 을 돌려 기존 LLM 분해로 넘긴다 —
사용자가 고친 뼈대를 카드 단계로 덮어쓰지 않는다. 제목만 고친 건 괜찮다(위치로 짝짓고 branch
제목은 사용자가 확정한 제목을 그대로 쓴다).
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from reaction_backend.domain import curriculum as cur
from reaction_backend.schemas.interview import GoalCandidate
from reaction_backend.schemas.planning import (
    ActionItemDraft,
    GoalDecomposition,
    GoalNodeDraft,
    MilestoneDraft,
)

__all__ = [
    "CurriculumPlan",
    "Recommendation",
    "books_for",
    "card_spec_item",
    "decomposition",
    "milestones",
    "phase_count_for",
    "recommend",
]

# 마일스톤 상한(MilestonePlan max_length=6)에서 실전 모의고사 단계 1칸을 남긴다.
_MAX_LESSON_PHASES = 5
# goal_nodes.title 은 String(200).
_NODE_TITLE_MAX = 200
# ActionItemDraft.estimated_minutes 범위(1~240) 안에서, 너무 짧은 카드는 만들지 않는다.
_MIN_CARD_MINUTES = 10
_MAX_CARD_MINUTES = 240


def books_for(goal: GoalCandidate | None) -> list[cur.Book]:
    """목표에 확정된 교재 카드 — 카드 목록에서 없어진 id 는 조용히 건너뛴다(그 책만 빠진다)."""
    if goal is None:
        return []
    return [b for b in (cur.book(i) for i in goal.curriculum) if b is not None]


def phase_count_for(*, full_weeks: int | None, cycle_weeks: int) -> int:
    """단계(=마일스톤) 수 — **주기마다 같은 몫**을 받도록 주기 수의 배수로 잡는다.

    마일스톤 창(`cycle_milestone_window`)은 `round(남은 수 × 이번 창 ÷ 남은 기간)` 개를 담는다.
    8주 마감·4주 주기에 단계가 3개면 1주기가 round(1.5)=2 개, 2주기가 1개를 받아 책의 2/3 를
    앞 4주에 몰아 넣는다. 주기 수(2)의 배수인 4로 나누면 2·2 로 고르다.
    """
    if not full_weeks or full_weeks <= cycle_weeks:
        return cur.DEFAULT_PHASES
    cycles = math.ceil(full_weeks / cycle_weeks)
    per_cycle = math.ceil(cur.DEFAULT_PHASES / cycles)
    return min(_MAX_LESSON_PHASES, cycles * per_cycle)


def milestones(
    books: Sequence[cur.Book], *, full_weeks: int | None, cycle_weeks: int
) -> list[MilestoneDraft]:
    """Stage A — 교재 단계를 마일스톤으로. 제목에 책별 단원 범위가 들어간다."""
    n = phase_count_for(full_weeks=full_weeks, cycle_weeks=cycle_weeks)
    return [
        MilestoneDraft(title=p.title(), summary=p.summary()) for p in cur.phases(books, n_phases=n)
    ]


def _phase_count_from(books: Sequence[cur.Book], n_milestones: int) -> int:
    """영속된 마일스톤 개수 → 그걸 만든 단계 수(실전 모의고사 단계는 빼고)."""
    return n_milestones - (1 if cur.has_test_phase(books) else 0)


def _same_skeleton(milestones: Sequence[MilestoneDraft], ps: Sequence[cur.Phase]) -> bool:
    """이 마일스톤 목록이 이 단계들로 만든 것인가 — 제목이 절반 이상 그대로면 그렇다고 본다.

    개수만 보면 안 된다: 개수에서 단계 수를 역산하므로 사용자가 마일스톤을 하나 **더하면**
    단계 수도 하나 늘어 개수는 늘 맞는다(그러면 사용자가 더한 '영어 일기' 자리에 VOCA 단원이
    들어간다). 단계 수가 바뀌면 단원 범위가 전부 밀려 제목이 거의 다 달라지고, 제목 몇 개만
    고친 경우는 나머지가 그대로라 통과한다.
    """
    same = sum(m.title == p.title() for m, p in zip(milestones, ps, strict=True))
    return same * 2 >= len(ps)


@dataclass(frozen=True, slots=True)
class CurriculumPlan:
    decomposition: GoalDecomposition
    notice: str | None  # 분량 초과 고지 — 없으면 None


def decomposition(
    *,
    goal_title: str,
    books: Sequence[cur.Book],
    all_milestones: Sequence[MilestoneDraft],
    window: Sequence[MilestoneDraft],
    n_sessions: int,
    session_minutes: int,
) -> CurriculumPlan | None:
    """Stage B — 이번 주기 마일스톤 구간의 단원을 `n_sessions` 세션에 펼쳐 분해 결과를 만든다.

    `all_milestones` 는 이 목표의 마일스톤 **전체**(위치로 단계와 짝짓는 기준), `window` 는 그중
    이번 주기가 다룰 구간(`cycle_milestone_window` 가 고른 것 — 같은 객체들이다).

    짝이 안 맞으면 `None` — 호출자가 기존 LLM 분해로 넘어간다(모듈 독스트링 참고).
    """
    n_lesson = _phase_count_from(books, len(all_milestones))
    if n_lesson < 1:
        return None
    ps = cur.phases(books, n_phases=n_lesson)
    if len(ps) != len(all_milestones) or not _same_skeleton(all_milestones, ps):
        return None
    positions = [i for i, m in enumerate(all_milestones) if any(m is w for w in window)]
    if not positions:
        return None
    sp = cur.spread(
        [ps[i] for i in positions], n_sessions=n_sessions, session_minutes=session_minutes
    )
    if not sp.sessions:
        return None

    nodes: list[GoalNodeDraft] = [
        GoalNodeDraft(
            node_id="root",
            parent_id=None,
            title=goal_title[:_NODE_TITLE_MAX],
            node_type="root",
            order_index=0,
            is_leaf=False,
        )
    ]
    branch_of_phase: dict[int, str] = {}
    for order, pos in enumerate(positions):
        branch_id = f"milestone-{pos}"
        branch_of_phase[ps[pos].index] = branch_id
        nodes.append(
            GoalNodeDraft(
                node_id=branch_id,
                parent_id="root",
                # 사용자가 확정한 제목 그대로 — 고쳤으면 고친 제목이 뼈대다.
                title=all_milestones[pos].title[:_NODE_TITLE_MAX],
                node_type="branch",
                order_index=order,
                is_leaf=False,
            )
        )
    actions: list[ActionItemDraft] = []
    for s in sp.sessions:
        leaf_id = f"session-{s.index}"
        title = s.title()[:_NODE_TITLE_MAX]
        nodes.append(
            GoalNodeDraft(
                node_id=leaf_id,
                parent_id=branch_of_phase[s.phase],
                title=title,
                node_type="leaf",
                order_index=s.index,
                is_leaf=True,
            )
        )
        minutes = s.minutes or session_minutes  # 복습 세션은 사용자가 답한 한 번 길이
        actions.append(
            ActionItemDraft(
                node_id=leaf_id,
                title=title,
                estimated_minutes=max(_MIN_CARD_MINUTES, min(_MAX_CARD_MINUTES, minutes)),
                category="study",
                first_step=s.first_step(),
            )
        )
    return CurriculumPlan(
        decomposition=GoalDecomposition(goal_nodes=nodes, action_items=actions),
        notice=_overload_notice(sp) if sp.overloaded else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 추천 — 어떤 교재 카드를 권할지 (규칙, LLM 0회)
# ─────────────────────────────────────────────────────────────────────────────

_TOEIC = re.compile(r"토익|toeic", re.IGNORECASE)
_SCORE = re.compile(r"(?<!\d)([1-9]\d{2})(?!\d)")
_BEGINNER = re.compile(r"처음|기초|입문|해본 적 없|안 해 봤|안 해봤|모르")
# 이 점수 미만이면 기초서부터. 토익 응시자 분포에서 '기본서가 버거운' 구간으로 흔히 잡는 선이다.
_STARTER_BELOW = 450
# 목표가 이 점수 이하면 한 권짜리(LC+RC+VOCA)로 범위를 좁히는 게 낫다 — 그 위는 파트별 기본서.
_ALL_IN_ONE_UP_TO = 750


@dataclass(frozen=True, slots=True)
class Recommendation:
    book: cur.Book
    reason: str
    recommended: bool  # 기본으로 골라 둘 책인가 — False 는 대안·선택 사항


def _score(text: str | None) -> int | None:
    """문장 속 토익 점수(100~990) — 첫 번째 것. 없으면 None."""
    for m in _SCORE.finditer(text or ""):
        value = int(m.group(1))
        if 100 <= value <= 990:
            return value
    return None


def recommend(goal: GoalCandidate | None) -> list[Recommendation]:
    """목표가 토익이면 교재 카드를 추천한다 — 아니면 빈 목록(카드가 없는 시험이다).

    규칙은 **현재 점수와 목표 점수** 두 개만 본다(인터뷰가 이미 묻는 것):

    - 지금 450 미만이거나 '처음' 이면 → 기초서(LC·RC) + 단어장
    - 목표 750 이하면 → 한 권짜리(LC+RC+VOCA) · 단어장과 기본서는 대안
    - 그 위면 → 파트별 기본서(LC·RC) + 단어장 · 한 권짜리는 대안
    - 실전 모의고사집은 언제나 **선택**으로만 보여 준다 — 고르면 마지막 단계에 들어간다.

    사용자의 공부 성향(강의 선호 등)은 아직 보지 않는다 — 그건 학습자 프로필 단계의 몫이다.
    """
    if goal is None:
        return []
    text = " ".join(filter(None, [goal.title, goal.success_image, goal.approach_note]))
    if not _TOEIC.search(text):
        return []
    toeic = [b for b in cur.books() if b.exam == "toeic"]
    target = _score(goal.title) or _score(goal.success_image)
    current = _score(goal.current_level)
    beginner = (current is not None and current < _STARTER_BELOW) or (
        current is None and bool(_BEGINNER.search(goal.current_level or ""))
    )
    gap = (
        f"{current}점 → {target}점"
        if current and target
        else (f"목표 {target}점" if target else "")
    )

    def pick(role: str) -> list[cur.Book]:
        return [b for b in toeic if b.role == role]

    def fits(b: cur.Book) -> bool:
        lo, hi = b.score_band
        return current is None or lo <= current <= hi

    out: list[Recommendation] = []
    vocab = pick("vocab")[:1]
    all_in_one_main = not beginner and (target is None or target <= _ALL_IN_ONE_UP_TO)
    if all_in_one_main:
        # 한 권짜리엔 단어 DAY 가 이미 있다 — 단어장까지 기본으로 고르면 같은 일을 두 번 하고
        # 세션이 길어진다(700+ 만으로 8주 × 주 4회 × 60분이 꽉 찬다). 선택으로 둔다.
        for b in vocab:
            out.append(
                Recommendation(
                    b, "한 권짜리에도 단어 DAY가 있어요 — 단어를 더 다지려면 함께 골라요.", False
                )
            )
    else:
        for b in vocab:
            out.append(
                Recommendation(
                    b, "단어는 모든 파트의 바탕이에요 — 세션마다 한 DAY씩 앞에 넣어요.", True
                )
            )
    if beginner:
        for b in pick("starter"):
            out.append(
                Recommendation(
                    b, "지금은 기초부터가 빨라요 — 문법·듣기 기본기를 먼저 잡아요.", True
                )
            )
        main_alternatives = pick("all_in_one")
    elif all_in_one_main:
        fitting = [b for b in pick("all_in_one") if fits(b)] or pick("all_in_one")[:1]
        for b in fitting[:1]:
            reason = f"{gap}: " if gap else ""
            out.append(
                Recommendation(
                    b, f"{reason}LC·RC·VOCA 가 한 권에 있어 범위를 좁혀 끝내기 좋아요.", True
                )
            )
        # 같은 구간에 맞는 다른 한 권짜리도 대안으로 — 출판사·구성이 다르다.
        main_alternatives = [*fitting[1:], *pick("core")]
    else:
        for b in pick("core"):
            out.append(
                Recommendation(
                    b, f"{gap + ': ' if gap else ''}파트별로 깊게 다루는 기본서예요.", True
                )
            )
        main_alternatives = [b for b in pick("all_in_one") if fits(b)][:1]
    chosen = {r.book.id for r in out}
    for b in main_alternatives:
        if b.id not in chosen:
            out.append(
                Recommendation(b, "다른 구성을 원하면 이 책으로 바꿔도 돼요(분량이 달라요).", False)
            )
    for b in pick("practice"):
        out.append(
            Recommendation(
                b, "고르면 마지막 단계에 실전 모의고사로 넣어요 — 시간 배분 연습용이에요.", False
            )
        )
    return out


def card_spec_item(book: cur.Book) -> dict[str, object]:
    """교재 카드 → `goals.materials` spec 항목. `catalog_id` 가 있어야 계획이 카드 경로를 탄다.

    단원은 `chapters` 에 "DAY 01 · 채용" 꼴로 싣는다 — 카드 경로가 아니라 LLM 경로로 떨어질
    때(마일스톤을 사용자가 크게 바꿨을 때)도 `_materials_note` 가 이 목차를 텍스트로 풀어
    분해 프롬프트에 넘긴다.
    """
    return {
        "kind": "book",
        "catalog_id": book.id,
        "title": book.title,
        "author": book.publisher,
        "isbn13": book.isbn13,
        "page_count": book.page_count,
        "chapters": [{"title": f"{u.label} · {u.topic}"} for u in book.studyable()],
        "toc_source": "card",
    }


def _overload_notice(sp: cur.Spread) -> str:
    """단원을 버리지 않고 시간이 모자라다는 사실을 말한다 — 무엇을 바꾸면 되는지까지."""
    return (
        f"이번 주기 단원을 다 보려면 세션당 평균 {sp.minutes_needed}분이 필요해요"
        f"(지금 한 번에 {sp.session_minutes}분). 한 번 길이나 주당 횟수를 늘리거나, "
        "마감을 조금 미루는 걸 고려해 보세요."
    )
