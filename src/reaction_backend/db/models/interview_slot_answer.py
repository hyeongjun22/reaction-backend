"""InterviewSlotAnswer — 딥 인터뷰 슬롯별 답변.

핵심:
- (session_id, slot_key) UNIQUE — AI가 같은 슬롯 재질문 시 INSERT 아닌 UPDATE
- 19 슬롯 카탈로그 (identity/goals/time/energy/recovery/constraints)
- value 는 JSONB — 모양과 읽기·쓰기는 `domain/slot_value` 하나가 정한다:
  * chip 응답:  {"type": "chip", "values": ["학업", "건강"]}
  * 자유 입력:  {"type": "text", "raw": "캡스톤, 토익", "normalized": ["캡스톤", "토익"]}
  * 슬라이더:    {"type": "range", "start": "09:00", "end": "23:00"}
  * 자료 확정:  {"type": "spec", "items": [...]}
  * 재질문 대기: {"type": "pending", "attempts": 2}
  * 스킵:        {"type": "skip", "reason": "declined"} (예전 행은 빈 텍스트 {"type":"text","raw":""})
- clarity_score: 0~1 (LLM 또는 룰 기반 채점).
  ⚠️ **프로덕션에서 한 번도 쓰이지 않는다.** `upsert_slot_answer` 의 세 호출부
  (`routes/interview.py`·`routes/materials.py` ×2) 모두 이 인자를 넘기지 않아 항상
  NULL 이다. 채점값은 `_decide_storage` 안에서만 쓰이고 저장되지 않는다(#448 감사).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Numeric, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.interview_session import InterviewSession


class InterviewSlotAnswer(Base, TimestampMixin):
    __tablename__ = "interview_slot_answers"

    __table_args__ = (
        UniqueConstraint("session_id", "slot_key", name="uq_interview_slot_answers_session_slot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 슬롯 식별자. 카탈로그 예: identity.role / goals.list / time.activity_window
    slot_key: Mapped[str] = mapped_column(String(128), nullable=False)

    # 사용자 답 (discriminated by value.type) — DB 설계서 §5.3 nullable
    value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # 명확도 0~1. LLM 또는 룰 기반 채점. DB 설계서 §5.3: NUMERIC(4,3) nullable
    clarity_score: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)

    # 필수 슬롯 여부. 모호함 지표 계산의 분모.
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # ── relationships ──
    session: Mapped[InterviewSession] = relationship(back_populates="slot_answers")
