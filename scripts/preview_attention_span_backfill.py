"""집중 지속(attention_span) 오염 행 미리보기 — 백필 대상 선별 (읽기 전용).

배경: 인터뷰 슬롯 `energy.focus_duration` 의 칩 **"2시간 이상"** 을 숫자만 긁는 파서가
**2(분)** 으로 읽었다(v2.00 에서 수정). 인터뷰 finalize 시 `profile_memory` 가 그 값을
`behavioral_profiles.attention_span` 에 그대로 적었다.

**선별 술어가 `attention_span == 2` 인 이유** — 깨진 파서와 고친 파서를 칩 4종에 전부
돌려보면 결과가 갈리는 값은 하나뿐이다:

    '25분'      깨진=25  고친=25   동일
    '50분'      깨진=50  고친=50   동일
    '90분'      깨진=90  고친=90   동일
    '2시간 이상' 깨진= 2  고친=120  ← 오염된 유일한 값

즉 **2 만 사고의 흔적**이다. 처음엔 `< 15` 로 잡았다가 바꿨다 —
`PATCH /settings/profile` 이 `attention_span` 을 `ge=5` 로 허용하므로(`schemas/settings.py`),
`< 15` 는 사용자가 **직접 설정한 5~14분을 말없이 덮는다**. 서버가 사용자 설정을 조용히
바꾸면 안 된다.

⚠️ **저장된 슬롯 답도 오염될 수 있다.** `profile_memory.seed_slots_from_profile` 이
프로필의 `attention_span` 을 `f"{n}분"` 칩으로 되돌려 다음 인터뷰의 시드로 넣고,
`routes/interview._persist_turn` 이 그 시드를 `interview_slot_answers` 에 UPSERT 한다.
그래서 오염 후 재인터뷰한 사용자의 최신 세션에는 **`"2분"` 이 사용자의 답인 것처럼** 남는다.
그 값을 그대로 읽으면 백필이 틀린 값을 확정하므로, 여기서는 **카탈로그 옵션에 실제로 있는
답**만 신뢰하고 없으면 더 과거 세션으로 거슬러 올라간다.

읽기 전용이다 — 실제 갱신은 `scripts.backfill_attention_span` 이 한다.

실행:
  uv run python -m scripts.preview_attention_span_backfill
  uv run python -m scripts.preview_attention_span_backfill --user-email someone@example.com
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.behavioral_profile import BehavioralProfile
from reaction_backend.db.models.interview_session import InterviewSession
from reaction_backend.db.models.interview_slot_answer import InterviewSlotAnswer
from reaction_backend.db.models.user import User
from reaction_backend.db.session import get_sessionmaker
from reaction_backend.domain import slot_value as sv
from reaction_backend.orchestrator.interview_adapter import chip_duration_min
from reaction_backend.orchestrator.interview_catalog import PLAN_SLOTS

FOCUS_SLOT_KEY = "energy.focus_duration"

# 깨진 파서가 만들어낸 **유일한** 값 — 위 docstring 의 대조표 참고.
CORRUPTED_ATTENTION_SPAN = 2

# 이 슬롯에서 사용자가 실제로 고를 수 있는 칩 — 시드로 되돌아온 값("2분", "120분" 등)과
# 구분하는 기준이다. 카탈로그를 단일 소스로 삼아 옵션이 바뀌면 자동으로 따라온다.
TRUSTED_FOCUS_CHIPS: frozenset[str] = frozenset(
    next(s for s in PLAN_SLOTS if s.slot_key == FOCUS_SLOT_KEY).options
)


@dataclass(frozen=True, slots=True)
class PollutedProfile:
    """백필 후보 한 행 — 현재 값과, 신뢰할 수 있는 답에서 다시 읽은 값."""

    user_id: UUID
    email: str
    current_attention_span: int
    current_chunk: str
    chip_answer: str | None
    recomputed: int | None
    # 최신 세션의 답이 시드 유래(카탈로그 옵션 아님)라 건너뛴 적이 있는가 — 보고용.
    skipped_seeded: bool = False

    @property
    def fixable(self) -> bool:
        """신뢰할 수 있는 칩이 남아 있어 정확한 값을 되찾을 수 있는가."""
        return self.recomputed is not None and self.recomputed > CORRUPTED_ATTENTION_SPAN


def is_trusted_chip(raw: str | None) -> bool:
    """사용자가 실제로 고른 칩인가 — 시드로 되돌아온 `"2분"`/`"120분"` 은 아니다."""
    return raw is not None and raw in TRUSTED_FOCUS_CHIPS


def polluted_profiles_stmt(user_email: str | None = None) -> Select[tuple[BehavioralProfile, User]]:
    """오염된 `behavioral_profiles` 행 + 사용자. 백필/미리보기가 **같은 술어**를 쓴다."""
    stmt = (
        select(BehavioralProfile, User)
        .join(User, User.id == BehavioralProfile.user_id)
        .where(BehavioralProfile.attention_span == CORRUPTED_ATTENTION_SPAN)
    )
    if user_email:
        stmt = stmt.where(User.email == user_email)
    return stmt.order_by(User.email)


def _chip_of(value: Any) -> str | None:
    """슬롯 답 JSON → 첫 칩 문자열. chip 타입이 아니면 None(`slot_value.first_chip`)."""
    return sv.first_chip(value) if isinstance(value, dict) else None


def focus_answers_stmt(user_id: UUID) -> Select[tuple[Any]]:
    """그 사용자의 집중 지속 답을 **최근 세션부터** — 정상 종료한 plan 인터뷰만.

    `end_reason`/`kind` 를 거르는 이유: 프로필을 만든 경로들(`get_latest_finished`,
    `_carry_over_slots`)이 전부 '정상 종료' 만 보므로 백필도 같은 술어를 써야 한다.
    중단된 인터뷰의 답이 이기면 백필이 사용자가 확정하지 않은 값을 확정한다.
    """
    return (
        select(InterviewSlotAnswer.value)
        .join(InterviewSession, InterviewSession.id == InterviewSlotAnswer.session_id)
        .where(
            InterviewSession.user_id == user_id,
            InterviewSession.kind == "plan",
            InterviewSession.end_reason == "completed",
            InterviewSlotAnswer.slot_key == FOCUS_SLOT_KEY,
        )
        .order_by(InterviewSession.started_at.desc())
    )


async def trusted_focus_chip(session: AsyncSession, user_id: UUID) -> tuple[str | None, bool]:
    """가장 최근의 **믿을 수 있는** 집중 지속 칩. 반환: (칩, 시드 답을 건너뛰었는가).

    최신 답이 시드 유래(`"2분"` 등 카탈로그에 없는 값)면 건너뛰고 더 과거로 간다 —
    프로필→슬롯 시드 루프 때문에 "최신" 이 곧 "사용자가 고른 것" 이 아니다.
    """
    result = await session.execute(focus_answers_stmt(user_id))
    skipped = False
    for (value,) in result.all():
        chip = _chip_of(value)
        if is_trusted_chip(chip):
            return chip, skipped
        if chip is not None:
            skipped = True
    return None, skipped


async def collect(session: AsyncSession, user_email: str | None = None) -> list[PollutedProfile]:
    """오염 행마다 '지금 값' 과 '신뢰할 수 있는 답에서 다시 읽은 값' 을 나란히 모은다."""
    result = await session.execute(polluted_profiles_stmt(user_email))
    out: list[PollutedProfile] = []
    for profile, user in result.all():
        chip, skipped = await trusted_focus_chip(session, user.id)
        out.append(
            PollutedProfile(
                user_id=user.id,
                email=user.email,
                current_attention_span=profile.attention_span,
                current_chunk=profile.time_chunk_preference,
                chip_answer=chip,
                recomputed=chip_duration_min(sv.dump(sv.ChipAnswer((chip,)))) if chip else None,
                skipped_seeded=skipped,
            )
        )
    return out


async def run(user_email: str | None = None) -> list[PollutedProfile]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = await collect(session, user_email)

    if not rows:
        print(f"✅ 오염된 프로필 없음 (attention_span == {CORRUPTED_ATTENTION_SPAN} 인 행 0건).")
        return rows

    print(f"⚠️  오염 프로필 {len(rows)}건 (attention_span == {CORRUPTED_ATTENTION_SPAN})\n")
    for r in rows:
        arrow = f"{r.recomputed}분" if r.fixable else "복구 불가(신뢰할 답 없음)"
        seeded = " [시드답 건너뜀]" if r.skipped_seeded else ""
        print(
            f"  {r.email:<38} attention_span={r.current_attention_span:>3} "
            f"chunk={r.current_chunk:<3} 칩={r.chip_answer or '-':<10} → {arrow}{seeded}"
        )
    fixable = sum(1 for r in rows if r.fixable)
    print(f"\n  복구 가능 {fixable}건 / 복구 불가 {len(rows) - fixable}건")
    if fixable < len(rows):
        print("  복구 불가 행은 백필이 기본값(30분)으로 올린다 — 재인터뷰하면 정확해진다.")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="attention_span 오염 행 미리보기 (읽기 전용)")
    parser.add_argument("--user-email", default=None, help="특정 사용자만 (기본: 전체)")
    args = parser.parse_args()
    asyncio.run(run(args.user_email))


if __name__ == "__main__":
    main()
