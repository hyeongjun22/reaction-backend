"""집중 지속(attention_span) 백필 — 파서 사고로 오염된 프로필 복구 (v2.00 후속).

배경: 인터뷰 슬롯 `energy.focus_duration` 의 칩 **"2시간 이상"** 을 숫자만 긁는 파서가
**2(분)** 으로 읽었다. **인터뷰 finalize** 시 `profile_memory.persist_profile_from_outcome`
이 그 값을 `behavioral_profiles.attention_span` 에 그대로 적었으므로(계획 승인이 아니다 —
`routes/interview.py` 의 finalize 경로다), 수정(v2.00) 이전에 그 칩을 고르고 **인터뷰를
끝낸** 사용자의 프로필에 **2** 가 남아 있다.

⚠️ **슬롯 답도 오염될 수 있다.** `profile_memory.seed_slots_from_profile` 이 프로필의
`attention_span` 을 `f"{n}분"` 칩으로 되돌려 다음 인터뷰의 시드로 넣고,
`routes/interview._persist_turn` 이 그걸 `interview_slot_answers` 에 UPSERT 한다. 그래서
오염 후 재인터뷰한 사용자에겐 **`"2분"` 이 본인 답인 것처럼** 남는다. 그 값을 그대로 읽으면
백필이 틀린 값을 확정하므로, **카탈로그 옵션에 실제로 있는 답**만 신뢰하고 없으면 더 과거
세션으로 거슬러 올라간다(`preview_attention_span_backfill.trusted_focus_chip`).

그래서 이 백필은 **두 곳**을 고친다:
  1. `behavioral_profiles` — attention_span / time_chunk_preference
  2. `interview_slot_answers` — 시드로 되돌아온 `"2분"` 답. 이걸 안 고치면
     `POST /plans/generate` 가 slot_answers 에서 outcome 을 **재투영**하므로
     (`routes/planning._resolve_outcome`) 프로필만 고쳐도 계획이 계속 눌린다.

신뢰할 수 있는 답이 없으면 프로필은 기본값(30분)으로 올려 **붕괴만 막고**, 슬롯 답은
건드리지 않는다 — 지어낸 값을 사용자의 답으로 저장하지 않는다. 정확한 값은 재인터뷰의 몫이다.

`time_chunk_preference` 도 같은 값에서 파생되므로 함께 고친다(`profile_memory.chunk_bucket`).

⚠️ **계획 자체는 백필 대상이 아니다.** 이미 만들어진 계획의 카드 길이는 사용자가 보고
승인한 값이라 서버가 조용히 바꾸면 안 된다(HITL). 프로필을 고치면 **다음 계획 생성부터**
정상이 된다.

안전:
  - 기본은 **dry-run**(아무것도 쓰지 않음). 실제 적용은 `--apply` 명시.
  - `--user-email` 로 범위를 좁힐 수 있다.
  - `--apply` 는 (user_id, email, old/new attention_span, old/new chunk, 고친 슬롯 답) 를
    원장 JSON 으로 남긴다. 원본 값은 술어로 재구성할 수 없으므로 이 파일이 유일한 복원 경로다.
  - 원장 기본 경로는 **`~/reaction-backfill-ledgers/`** — cwd(=`$APP_DIR`)에 쓰면 다음 배포에
    사라진다. 아래 경고 참고.
  - `--revert-from <원장>` 으로 되돌린다. 이것도 기본 dry-run.
    프로필의 **현재** 값이 원장의 new 와 다르면(그 사이 재인터뷰 등으로 바뀜) skip 한다.
  - ⚠️ **되돌리기는 프로필만 되돌린다 — 수리한 슬롯 답은 그대로 둔다.** 의도적이다:
    거기 있던 `"2분"` 은 사용자가 고른 답이 아니라 시드가 남긴 잡음이라, 되돌린다는 건
    **알려진 오염 데이터를 복원하는 것**이다. 그건 아무에게도 도움이 안 된다.

⚠️ **원장은 `$APP_DIR` 밖에 쓸 것** — `deploy.yml` 의 `rsync -a --delete` 가 앱 디렉터리를
매번 갈아엎는다(`backfill_card_target_dates` 와 같은 이유).

실행:
  uv run python -m scripts.preview_attention_span_backfill                        # 대상 확인
  uv run python -m scripts.backfill_attention_span                                # dry-run
  uv run python -m scripts.backfill_attention_span --apply --ledger-path <경로>     # 적용
  uv run python -m scripts.backfill_attention_span --revert-from <원장> --apply     # 되돌리기
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.behavioral_profile import BehavioralProfile
from reaction_backend.db.models.interview_session import InterviewSession
from reaction_backend.db.models.interview_slot_answer import InterviewSlotAnswer
from reaction_backend.db.session import get_sessionmaker
from reaction_backend.domain import slot_value as sv
from reaction_backend.orchestrator.profile_memory import chunk_bucket
from reaction_backend.schemas.common import now_kst
from scripts.preview_attention_span_backfill import (
    CORRUPTED_ATTENTION_SPAN,
    FOCUS_SLOT_KEY,
    PollutedProfile,
    collect,
    is_trusted_chip,
)

# 슬롯 답을 못 찾은 행에 넣을 값 — `profile_memory` 의 `prefs.focus_duration_min or 30` 과 동일.
FALLBACK_ATTENTION_SPAN = 30


# ─────────────────────────────────────────────────────────────────────────────
# 순수 로직 (DB 무관, 단위 테스트 대상)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BackfillRow:
    user_id: UUID
    email: str
    old_attention_span: int
    new_attention_span: int
    old_chunk: str
    new_chunk: str
    source: str  # "chip" | "fallback"
    # 신뢰할 수 있는 과거 답에서 되찾은 칩 원문. 시드로 오염된 슬롯 답을 이 값으로 되돌린다.
    recovered_chip: str | None = None


def plan_backfill(rows: list[PollutedProfile]) -> list[BackfillRow]:
    """오염 행 → 적용할 변경. 값이 안 바뀌는 행은 제외한다(빈 UPDATE 방지)."""
    out: list[BackfillRow] = []
    for r in rows:
        if r.fixable:
            assert r.recomputed is not None  # fixable 정의상  # noqa: S101
            new_span, source = r.recomputed, "chip"
        else:
            new_span, source = FALLBACK_ATTENTION_SPAN, "fallback"
        new_chunk = chunk_bucket(new_span)
        if new_span == r.current_attention_span and new_chunk == r.current_chunk:
            continue
        out.append(
            BackfillRow(
                user_id=r.user_id,
                email=r.email,
                old_attention_span=r.current_attention_span,
                new_attention_span=new_span,
                old_chunk=r.current_chunk,
                new_chunk=new_chunk,
                source=source,
                recovered_chip=r.chip_answer if source == "chip" else None,
            )
        )
    return out


def ledger_document(rows: list[BackfillRow], *, applied_at: datetime) -> dict[str, object]:
    """원장 JSON. 사용자마다 원본 값이 달라 술어로 재구성이 불가능하므로 유일한 복원 경로다."""
    return {
        "applied_at": applied_at.isoformat(),
        "entries": [
            {
                "user_id": str(r.user_id),
                "email": r.email,
                "old_attention_span": r.old_attention_span,
                "new_attention_span": r.new_attention_span,
                "old_chunk": r.old_chunk,
                "new_chunk": r.new_chunk,
                "source": r.source,
                "recovered_chip": r.recovered_chip,
            }
            for r in rows
        ],
    }


def parse_ledger_document(data: dict[str, object]) -> list[BackfillRow]:
    """`ledger_document` 의 역함수 — round-trip 이 깨지면 되돌리기가 조용히 틀린다."""
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError("원장 형식이 아니다 — 'entries' 배열이 없다")
    return [
        BackfillRow(
            user_id=UUID(e["user_id"]),
            email=e["email"],
            old_attention_span=int(e["old_attention_span"]),
            new_attention_span=int(e["new_attention_span"]),
            old_chunk=str(e["old_chunk"]),
            new_chunk=str(e["new_chunk"]),
            source=str(e["source"]),
            recovered_chip=e.get("recovered_chip"),
        )
        for e in entries
    ]


@dataclass(frozen=True, slots=True)
class RevertDecision:
    row: BackfillRow
    action: str  # "revert" | "skip_missing" | "skip_mismatch"
    reason: str | None = None


def plan_revert(entries: list[BackfillRow], current: dict[UUID, int]) -> list[RevertDecision]:
    """되돌려도 되는지 사용자별로 판단 (아무것도 변형하지 않는 순수 함수).

    프로필이 사라졌거나 현재 값이 원장의 `new_attention_span` 과 다르면(그 사이 재인터뷰
    등으로 다시 바뀌었다는 뜻) **건드리지 않는다** — 되돌리기가 최신 변경을 덮으면 안 된다.
    """
    decisions: list[RevertDecision] = []
    for e in entries:
        now = current.get(e.user_id)
        if now is None:
            decisions.append(RevertDecision(e, "skip_missing", "프로필이 없다"))
        elif now != e.new_attention_span:
            decisions.append(
                RevertDecision(e, "skip_mismatch", f"현재 {now}분 ≠ 원장 {e.new_attention_span}분")
            )
        else:
            decisions.append(RevertDecision(e, "revert"))
    return decisions


async def _repair_seeded_answers(session: AsyncSession, row: BackfillRow) -> int:
    """시드로 되돌아온 `energy.focus_duration` 답을 되찾은 칩으로 되돌린다.

    대상은 **카탈로그 옵션이 아닌 답**뿐이다 — 사용자가 실제로 고른 칩은 건드리지 않는다.
    `_persist_turn` 이 시드를 UPSERT 해 만든 행만 사용자의 원답으로 되돌리는 것이다.
    """
    if row.recovered_chip is None:
        return 0
    result = await session.execute(
        select(InterviewSlotAnswer)
        .join(InterviewSession, InterviewSession.id == InterviewSlotAnswer.session_id)
        .where(
            InterviewSession.user_id == row.user_id,
            InterviewSlotAnswer.slot_key == FOCUS_SLOT_KEY,
        )
    )
    fixed = 0
    for answer in result.scalars():
        value = answer.value
        chip = sv.first_chip(value) if isinstance(value, dict) else None
        if chip is None or is_trusted_chip(chip):
            continue  # 사용자가 고른 답 — 건드리지 않는다
        answer.value = sv.dump(sv.ChipAnswer((row.recovered_chip,)))
        fixed += 1
    return fixed


# ─────────────────────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────────────────────


# 원장 기본 위치 — **`$APP_DIR` 밖**이어야 한다. `deploy.yml` 의 `rsync -a --delete` 가 앱
# 디렉터리를 매번 갈아엎으므로 cwd 에 쓰면 다음 배포에 유일한 복원 경로가 사라진다.
# self-hosted EC2 runner 는 실행 간 디스크가 유지되므로 $HOME 아래는 살아남는다.
LEDGER_DIR = Path.home() / "reaction-backfill-ledgers"


def _default_ledger_path() -> Path:
    stamp = now_kst().strftime("%Y%m%d-%H%M%S")
    return LEDGER_DIR / f"attention-span-backfill-{stamp}.json"


async def run_backfill(
    *, apply: bool, user_email: str | None = None, ledger_path: Path | None = None
) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        polluted = await collect(session, user_email)
        rows = plan_backfill(polluted)

        if not rows:
            print(
                f"✅ 백필할 행 없음 (attention_span == {CORRUPTED_ATTENTION_SPAN} 인 프로필이 없다)."
            )
            return

        print(f"{'적용' if apply else 'dry-run'} — 대상 {len(rows)}건\n")
        for r in rows:
            print(
                f"  {r.email:<38} attention_span {r.old_attention_span:>3} → "
                f"{r.new_attention_span:>3}  chunk {r.old_chunk} → {r.new_chunk}  ({r.source})"
            )
        if not apply:
            print("\n적용하려면 --apply --ledger-path <경로> 를 붙여 다시 실행한다.")
            return

        path = ledger_path or _default_ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(ledger_document(rows, applied_at=now_kst()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        by_user = {r.user_id: r for r in rows}
        result = await session.execute(
            select(BehavioralProfile).where(BehavioralProfile.user_id.in_(list(by_user)))
        )
        updated = 0
        for profile in result.scalars():
            row = by_user[profile.user_id]
            profile.attention_span = row.new_attention_span
            profile.time_chunk_preference = row.new_chunk
            updated += 1

        # 시드로 되돌아온 슬롯 답도 함께 고친다 — 안 고치면 `POST /plans/generate` 가
        # slot_answers 에서 outcome 을 재투영해(`routes/planning._resolve_outcome`)
        # 프로필만 고쳐도 계획이 계속 눌린다.
        repaired_answers = 0
        for row in rows:
            if row.source != "chip" or row.recovered_chip is None:
                continue  # 지어낸 값을 사용자의 '답' 으로 저장하지 않는다
            repaired_answers += await _repair_seeded_answers(session, row)

        await session.commit()
        print(f"\n✅ 백필 완료 — 프로필 {updated}건 · 슬롯 답 {repaired_answers}건. 원장: {path}")


async def run_revert(*, apply: bool, ledger_path: Path, user_email: str | None = None) -> None:
    entries = parse_ledger_document(json.loads(ledger_path.read_text(encoding="utf-8")))
    if user_email:
        entries = [e for e in entries if e.email == user_email]
    if not entries:
        print("원장에 되돌릴 항목이 없다.")
        return

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(BehavioralProfile).where(
                BehavioralProfile.user_id.in_([e.user_id for e in entries])
            )
        )
        profiles = {p.user_id: p for p in result.scalars()}
        decisions = plan_revert(entries, {uid: p.attention_span for uid, p in profiles.items()})

        for d in decisions:
            mark = "되돌림" if d.action == "revert" else f"skip({d.reason})"
            print(
                f"  {d.row.email:<38} {d.row.new_attention_span} → "
                f"{d.row.old_attention_span}  {mark}"
            )
        if not apply:
            print("\n실제로 되돌리려면 --apply 를 붙인다.")
            return

        reverted = 0
        for d in decisions:
            if d.action != "revert":
                continue
            profile = profiles[d.row.user_id]
            profile.attention_span = d.row.old_attention_span
            profile.time_chunk_preference = d.row.old_chunk
            reverted += 1
        await session.commit()
        print(f"\n✅ 되돌리기 완료 — {reverted}건.")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="attention_span 파서 사고 백필 (v2.00 후속)")
    p.add_argument("--apply", action="store_true", help="실제 적용 (미지정 시 dry-run)")
    p.add_argument("--user-email", default=None, help="특정 사용자만 (기본: 전체)")
    p.add_argument("--ledger-path", default=None, help="원장 파일 경로 (미지정 시 자동 생성)")
    p.add_argument("--revert-from", default=None, help="이 원장 파일을 읽어 되돌린다")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    if args.revert_from:
        asyncio.run(
            run_revert(
                apply=args.apply, ledger_path=Path(args.revert_from), user_email=args.user_email
            )
        )
    else:
        asyncio.run(
            run_backfill(
                apply=args.apply,
                user_email=args.user_email,
                ledger_path=Path(args.ledger_path) if args.ledger_path else None,
            )
        )


if __name__ == "__main__":
    main()
