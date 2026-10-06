from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header
from sqlalchemy.orm import sessionmaker

from app.api.dependencies import get_current_user, get_plan_service, get_session_factory
from app.cache.redis import TaskCache, get_cache
from app.db.models import User
from app.schemas.plan import PlanRead
from app.services.plan import PlanService, run_plan_generation

router = APIRouter(tags=["plans"])


@router.post(
    "/tasks/{task_id}/plans",
    response_model=PlanRead,
    status_code=202,
    summary="Generate an AI plan for a task",
    responses={409: {"description": "The task already has an active plan"}},
)
def create_plan(
    task_id: UUID,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    user: User = Depends(get_current_user),
    service: PlanService = Depends(get_plan_service),
    session_factory: sessionmaker = Depends(get_session_factory),
    cache: TaskCache = Depends(get_cache),
):
    """Returns 202 immediately with the plan in GENERATING state; poll `GET /plans/{id}`
    until it becomes PENDING_APPROVAL (or FAILED). Retrying with the same
    `Idempotency-Key` returns the original plan instead of starting another one."""
    plan, created = service.request_plan(user, task_id, idempotency_key)
    if created:
        background_tasks.add_task(run_plan_generation, plan.id, session_factory, cache)
    return plan


@router.get("/tasks/{task_id}/plans", response_model=list[PlanRead], summary="List plans of a task")
def list_plans(
    task_id: UUID,
    user: User = Depends(get_current_user),
    service: PlanService = Depends(get_plan_service),
):
    return service.list_for_task(user, task_id)


@router.get("/plans/{plan_id}", response_model=PlanRead, summary="Get a plan with its steps")
def get_plan(
    plan_id: UUID,
    user: User = Depends(get_current_user),
    service: PlanService = Depends(get_plan_service),
):
    return service.get(user, plan_id)


@router.post("/plans/{plan_id}/approve", response_model=PlanRead, summary="Approve a plan")
def approve_plan(
    plan_id: UUID,
    user: User = Depends(get_current_user),
    service: PlanService = Depends(get_plan_service),
):
    """Only plans in PENDING_APPROVAL can be approved (409 otherwise)."""
    return service.approve(user, plan_id)


@router.post("/plans/{plan_id}/reject", response_model=PlanRead, summary="Reject a plan")
def reject_plan(
    plan_id: UUID,
    user: User = Depends(get_current_user),
    service: PlanService = Depends(get_plan_service),
):
    """A rejected plan frees the task, so a new plan can be generated."""
    return service.reject(user, plan_id)


@router.post("/plans/{plan_id}/execute", response_model=PlanRead, summary="Start executing a plan")
def execute_plan(
    plan_id: UUID,
    user: User = Depends(get_current_user),
    service: PlanService = Depends(get_plan_service),
):
    """Allowed only after approval. Moves the task to IN_PROGRESS and starts the first step."""
    return service.execute(user, plan_id)


@router.post(
    "/plans/{plan_id}/steps/{step_id}/complete",
    response_model=PlanRead,
    summary="Mark the current step as completed",
)
def complete_step(
    plan_id: UUID,
    step_id: UUID,
    user: User = Depends(get_current_user),
    service: PlanService = Depends(get_plan_service),
):
    """Advances to the next step. Completing the last step completes the plan and the task."""
    return service.complete_step(user, plan_id, step_id)
