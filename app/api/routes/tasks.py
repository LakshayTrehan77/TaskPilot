from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app.api.dependencies import get_current_user, get_task_service
from app.db.models import TaskPriority, TaskStatus, User
from app.schemas.task import TaskCreate, TaskPage, TaskRead, TaskUpdate
from app.services.task import TaskService

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", response_model=TaskRead, status_code=201, summary="Create a task")
def create_task(
    payload: TaskCreate,
    user: User = Depends(get_current_user),
    service: TaskService = Depends(get_task_service),
):
    return service.create(user, payload)


@router.get("", response_model=TaskPage, summary="List tasks")
def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: TaskStatus | None = None,
    priority: TaskPriority | None = None,
    sort: str = Query(
        "-created_at",
        pattern=r"^-?(created_at|updated_at|title)$",
        description="Field to sort by; prefix with - for descending.",
    ),
    user: User = Depends(get_current_user),
    service: TaskService = Depends(get_task_service),
):
    """Regular users see their own tasks; admins see everyone's."""
    return service.list(
        user, page=page, page_size=page_size, status=status, priority=priority, sort=sort
    )


@router.get("/{task_id}", response_model=TaskRead, summary="Get a task")
def get_task(
    task_id: UUID,
    user: User = Depends(get_current_user),
    service: TaskService = Depends(get_task_service),
):
    return service.get(user, task_id)


@router.patch("/{task_id}", response_model=TaskRead, summary="Partially update a task")
def update_task(
    task_id: UUID,
    payload: TaskUpdate,
    user: User = Depends(get_current_user),
    service: TaskService = Depends(get_task_service),
):
    """Only the fields you send are changed. Status changes must follow the allowed
    transitions (for example COMPLETED is final), otherwise 409."""
    return service.update(user, task_id, payload)


@router.delete("/{task_id}", status_code=204, summary="Delete a task")
def delete_task(
    task_id: UUID,
    user: User = Depends(get_current_user),
    service: TaskService = Depends(get_task_service),
):
    service.delete(user, task_id)
    return Response(status_code=204)
