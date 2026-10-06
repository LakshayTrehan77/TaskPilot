from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.db.models import PlanStatus, StepStatus


class PlanStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str
    position: int
    status: StepStatus


class PlanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID
    status: PlanStatus
    summary: str
    failure_reason: str | None
    created_at: datetime
    approved_at: datetime | None
    steps: list[PlanStepRead]
