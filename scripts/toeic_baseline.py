"""토익 700 기준선 — 지금 파이프라인이 토익 목표·교재로 어떤 계획을 내는지 고정해 둔다.

"토익 700점이 목표예요" 라고 했을 때 계획이 **책의 단원을 따라가는지**를 재기 위한 기준선이다.
개선(교재 카드 + 단원 스케줄러) 전후를 같은 페르소나·같은 채점으로 비교한다.

프로덕션과 같은 경로를 탄다 — `l1_6_run.py` 와 같은 이유로 인터뷰는 건너뛰고 outcome 을
직접 조립한 뒤, 계획 그래프의 노드를 그대로 부른다:

    outcome → first_plan.validate_inputs → first_plan.decompose_goal
            (goal_decompose@v3 → shape_action_plan → extend_action_plan_to_horizon)

시나리오 세 가지:
- `no_material` — 자료 칸을 비웠다.
- `book_pages_only` — 해커스 토익 기출 VOCA 를 자료 검색으로 확정했다. 지금 파이프라인이
  해커스 교재에 대해 실제로 얻는 것은 **페이지 수뿐**이다(알라딘 목차 0/10, seoji 해커스 0/3 —
  `docs/experiments/l0-materials-source-results.md`). 그래서 확정 경로(`compute_book_pace` →
  `spec_slot_value` → `_materials_note`)를 그대로 태워 "세션당 약 N쪽" 텍스트를 만든다.
- `curriculum` — 교재 카드 추천(`curriculum_plan.recommend`)에서 기본으로 고른 책들을 확정하고,
  카드 마일스톤(`curriculum_milestones`)을 그대로 확정한 뒤 세운다. 분해 LLM 을 부르지 않는다.

`session=None` — 예산 검사·`llm_runs` 기록을 건너뛴다(오프라인 연구 호출, `l1_6_run.py` 와 같음).
⚠️ 실 Gemini 호출이 LLM 시나리오당 1회다(`--dry-run` 이면 0회, `curriculum` 은 늘 0회).

실행:
  uv run python -m scripts.toeic_baseline
  uv run python -m scripts.toeic_baseline --dry-run
  uv run python -m scripts.toeic_baseline --only curriculum
  uv run python -m scripts.toeic_baseline --out docs/experiments/data/toeic-baseline.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from reaction_backend.domain import slot_value as sv
from reaction_backend.orchestrator import (
    curriculum_plan,
    first_plan,
    first_plan_milestones,
    interview_adapter,
    materials_spec,
)
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)
from reaction_backend.schemas.materials_spec import BookSpecDetail

GOAL_TITLE = "토익 700점"
DEADLINE_DAYS = 56  # 8주 뒤 정기시험
SESSION_MIN = 60
SESSIONS_PER_WEEK = 4
SCENARIOS = ("no_material", "book_pages_only", "curriculum")

# 해커스 토익 기출 VOCA — 페이지 수·ISBN 은 공개 서지 정보(알라딘·예스24 상품 페이지).
VOCA = BookSpecDetail(
    title="해커스 토익 기출 VOCA (보카)",
    author="David Cho",
    isbn13="9788965422785",
    page_count=556,
)

# 세션 제목이 **책의** 단원을 가리키는가 — DAY 03 · Chapter 2 · Unit 4 · 3장 · 실전 문제 1.
# ⚠️ "파트 5"·"Part 3" 은 넣지 않는다 — 토익 **시험**의 파트라 책 없이도 나온다(자료 없는
# 시나리오에서 16세션 중 9개가 걸려, 책을 따랐다는 증거가 못 됐다). 그건 따로 센다.
_UNIT_REF = re.compile(
    r"(?:DAY|Day|day)\s*\d+|Chapter\s*\d|CH\.?\s*\d|Unit\s*\d|UNIT\s*\d|\d+\s*장\b|"
    r"실전\s*문제\s*\d|TEST\s*\d"
)
_EXAM_PART = re.compile(r"(?:Part|PART|파트)\s*\d")
# 마감까지 채우려고 규칙이 덧붙인 자리표시자 — "{목표} 21회차".
_PLACEHOLDER = re.compile(r"\d+\s*회차\s*$")


def build_outcome(
    *, today: date, materials_note: str | None, curriculum: list[str] | None = None
) -> InterviewOutcome:
    deadline = (today + timedelta(days=DEADLINE_DAYS)).isoformat()
    goal = GoalCandidate(
        title=GOAL_TITLE,
        category="study",
        is_heaviest=True,
        deadline=deadline,
        success_image="정기시험에서 700점 이상 받기",
        current_level="작년 모의고사 550점 정도, LC가 더 약해요",
        weekly_hours=SESSION_MIN * SESSIONS_PER_WEEK // 60,
        session_length_min=SESSION_MIN,
        frequency_per_week=SESSIONS_PER_WEEK,
        preferred_time="저녁",
        approach_note=None,
        materials_note=materials_note,
        curriculum=curriculum or [],
        confidence=1.0,
    )
    return InterviewOutcome(
        session_id="toeic-baseline",
        generated_at=now_kst(),
        end_reason="completed",
        ambiguity_final=0.0,
        identity=IdentityContext(role="3학년", season="학기 중"),
        core_goals=[goal],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"), peak_window=["저녁"]
        ),
        preferences=PreferenceProfile(
            recovery_tone="담백", rest_ok=True, downscope_unit_min=15, focus_duration_min=50
        ),
        horizon=deadline,
    )


def book_pages_only_note(*, today: date) -> str | None:
    """자료 확정(spec-confirm) 경로가 해커스 VOCA 에 대해 만드는 `materials_note` 그대로."""
    probe = build_outcome(today=today, materials_note=None)
    pace = materials_spec.compute_book_pace(
        page_count=VOCA.page_count, chapters=VOCA.chapters, outcome=probe, target_date=today
    )
    slot = materials_spec.spec_slot_value([VOCA], book_pace=pace)
    return interview_adapter._materials_note(slot)  # noqa: SLF001 - 프로덕션과 같은 변환을 탄다


def curriculum_choice(*, today: date) -> tuple[str | None, list[str]]:
    """카드 추천에서 기본으로 골라 둔 책들을 확정했을 때의 (`materials_note`, 카드 id)."""
    probe = build_outcome(today=today, materials_note=None)
    picked = [r.book for r in curriculum_plan.recommend(probe.core_goals[0]) if r.recommended]
    slot = sv.dump(sv.SpecAnswer(tuple(curriculum_plan.card_spec_item(b) for b in picked)))
    return interview_adapter._materials_note(slot), [b.id for b in picked]  # noqa: SLF001


def score(titles: list[str]) -> dict[str, Any]:
    """채점 기준 ①단원 정합 ⑤이어짐 — 제목만으로 결정적으로 판정한다(하한)."""
    n = len(titles)
    unit_refs = [t for t in titles if _UNIT_REF.search(t)]
    exam_parts = [t for t in titles if _EXAM_PART.search(t)]
    placeholders = [t for t in titles if _PLACEHOLDER.search(t)]
    return {
        "sessions": n,
        "unit_ref": len(unit_refs),
        "unit_ref_rate": round(len(unit_refs) / n, 3) if n else None,
        "exam_part_ref": len(exam_parts),
        "placeholders": len(placeholders),
        "placeholder_rate": round(len(placeholders) / n, 3) if n else None,
    }


async def run_scenario(
    name: str,
    note: str | None,
    *,
    today: date,
    dry_run: bool,
    curriculum: list[str] | None = None,
) -> dict[str, Any]:
    outcome = build_outcome(today=today, materials_note=note, curriculum=curriculum)
    # 카드 계획은 사용자가 카드 마일스톤을 그대로 확정했다고 본다(Stage A → Stage B 정상 흐름).
    milestones = (
        first_plan_milestones.curriculum_milestones(outcome, cycle_weeks=4, target_date=today)
        if curriculum
        else None
    )
    state = first_plan.initial_state(
        user_id=uuid.UUID(int=0),
        outcome=outcome,
        target_date=today.isoformat(),
        milestones=milestones,
    )
    config: Any = {"configurable": {"session": None}}
    state = await first_plan.validate_inputs(state, config)
    row: dict[str, Any] = {
        "scenario": name,
        "materials_prompt": state["planning_context"]["prompt_vars"]["materials"],
        "curriculum": curriculum or [],
        "milestones": [m.title for m in milestones or []],
    }
    if dry_run and not curriculum:
        return row
    state = await first_plan.decompose_goal(state, config)
    plan = state["goal_plan"]
    titles = [a.title for a in plan.action_items] if plan else []
    row.update(
        {
            "curriculum_plan": state["curriculum_plan"],
            "curriculum_notice": state["curriculum_notice"],
            "fell_back": state["used_fallback"],
            "fallback_reason": state["decompose_fallback_reason"],
            "coverage_extended": state["coverage_extended"],
            "nodes": [n.title for n in plan.goal_nodes] if plan else [],
            "actions": [
                {"title": a.title, "minutes": a.estimated_minutes, "first_step": a.first_step}
                for a in (plan.action_items if plan else [])
            ],
            "score": score(titles),
        }
    )
    return row


async def main_async(args: argparse.Namespace) -> None:
    today = now_kst().date()
    card_note, card_ids = curriculum_choice(today=today)
    scenarios: list[tuple[str, str | None, list[str] | None]] = [
        ("no_material", None, None),
        ("book_pages_only", book_pages_only_note(today=today), None),
        ("curriculum", card_note, card_ids),
    ]
    if args.only:
        scenarios = [s for s in scenarios if s[0] in args.only]
    rows = [
        await run_scenario(name, note, today=today, dry_run=args.dry_run, curriculum=ids)
        for name, note, ids in scenarios
    ]
    for row in rows:
        print(f"\n## {row['scenario']}")
        print("자료 프롬프트:", row["materials_prompt"].replace("\n", " / ")[:300])
        if row["curriculum"]:
            print("교재 카드:", row["curriculum"])
            for m in row["milestones"]:
                print("  마일스톤:", m)
        if "score" in row:
            if row.get("curriculum_notice"):
                print("  고지:", row["curriculum_notice"])
            print("폴백:", row["fell_back"], row["fallback_reason"] or "")
            print("채점:", row["score"], "| 이어가기 보충:", row["coverage_extended"])
            for a in row["actions"][:12]:
                print(f"  - [{a['minutes']}분] {a['title']}")
            if len(row["actions"]) > 12:
                print(
                    f"  … 외 {len(row['actions']) - 12}개 (마지막: {row['actions'][-1]['title']})"
                )
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"date": today.isoformat(), "goal": GOAL_TITLE, "rows": rows}
        out.write_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
        print(f"\n저장: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 자료 프롬프트만")
    parser.add_argument("--out", help="원자료 JSON 저장 경로")
    parser.add_argument("--only", nargs="+", choices=SCENARIOS, help="이 시나리오만 돌린다")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
