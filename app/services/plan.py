import logging
from datetime import timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.ai.graph import build_graph
from app.ai.planner import get_llm
from app.ai.tools import PlannerTools
from app.cache.redis import TaskCache
from app.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError
from app.db.models import Plan, PlanStatus, PlanStep, StepStatus, TaskStatus, User, utcnow
from app.observability.metrics import AI_PLAN_FAILURES, AI_PLAN_GENERATIONS
from app.repositories.plan import PlanRepository
from app.repositories.task import TaskRepository
from app.services.task import can_transition, ensure_can_access, ensure_task_transition

logger = logging.getLogger(__name__)

CLOSED_TASK_STATUSES = (TaskStatus.COMPLETED, TaskStatus.CANCELLED)


class PlanService:
    def __init__(self, db: Session, cache: TaskCache):
        self.db = db
        self.cache = cache
        self.plans = PlanRepository(db)
        self.tasks = TaskRepository(db)

    def _task_for(self, user: User, task_id: UUID):
        task = self.tasks.get(task_id)
        if task is None:
            raise NotFoundError("Task not found")
        ensure_can_access(user, task)
        return task

    def _plan_for(self, user: User, plan_id: UUID, lock: bool = False) -> Plan:
        plan = self.plans.get(plan_id, lock=lock)
        if plan is None:
            raise NotFoundError("Plan not found")
        ensure_can_access(user, plan.task)
        return plan

    def get(self, user: User, plan_id: UUID) -> Plan:
        return self._plan_for(user, plan_id)

    def list_for_task(self, user: User, task_id: UUID) -> list[Plan]:
        task = self._task_for(user, task_id)
        return self.plans.list_for_task(task.id)

    def request_plan(
        self, user: User, task_id: UUID, idempotency_key: str | None
    ) -> tuple[Plan, bool]:
        """Create a GENERATING plan. Returns (plan, created); created is False when the
        Idempotency-Key matched an earlier request, in which case nothing new is started."""
        task = self._task_for(user, task_id)

        if idempotency_key:
            existing = self.plans.get_by_idempotency_key(task.id, idempotency_key)
            if existing:
                return existing, False

        timeout = timedelta(seconds=get_settings().plan_generation_timeout_seconds)
        self.plans.fail_stale_generating(task.id, utcnow() - timeout)

        if self.plans.get_active_for_task(task.id):
            raise ConflictError("This task already has an active plan")

        plan = Plan(task_id=task.id, status=PlanStatus.GENERATING, idempotency_key=idempotency_key)
        try:
            self.plans.add(plan)
            self.db.commit()
        except IntegrityError:
            # Lost a race: either the same key or another active plan was committed first.
            self.db.rollback()
            if idempotency_key:
                existing = self.plans.get_by_idempotency_key(task.id, idempotency_key)
                if existing:
                    return existing, False
            raise ConflictError("This task already has an active plan") from None

        logger.info("plan requested plan_id=%s task_id=%s user_id=%s", plan.id, task.id, user.id)
        return plan, True

    def generate(self, plan_id: UUID) -> None:
        """Runs in a background task with its own session; never raises."""
        plan = self.plans.get(plan_id)
        if plan is None or plan.status != PlanStatus.GENERATING:
            return

        task = plan.task
        task_id = task.id
        AI_PLAN_GENERATIONS.inc()
        graph = build_graph(
            get_llm(),
            PlannerTools(self.db, task),
            save_plan=lambda summary, steps: self._save_generated(plan_id, summary, steps),
        )
        initial_state = {
            "task_title": task.title,
            "task_description": task.description,
            "constraints": [],
            "plan": [],
            "summary": "",
            "raw_output": "",
            "error": "",
            "attempts": 0,
        }
        try:
            graph.invoke(initial_state)
        except Exception as exc:
            self.db.rollback()
            AI_PLAN_FAILURES.inc()
            logger.error(
                "plan generation failed plan_id=%s task_id=%s error=%s",
                plan_id,
                task_id,
                exc.__class__.__name__,
                exc_info=exc,
            )
            try:
                self._mark_failed(plan_id, f"{exc.__class__.__name__}: {exc}"[:500])
            except Exception:
                # Plan stays GENERATING and is failed by the timeout check on the next request.
                logger.exception("could not mark plan as failed plan_id=%s", plan_id)

    def _save_generated(self, plan_id: UUID, summary: str, steps: list[dict]) -> None:
        plan = self.plans.get(plan_id, lock=True)
        # The plan may have timed out and been replaced while the LLM was thinking.
        if plan is None or plan.status != PlanStatus.GENERATING:
            logger.warning("discarding generated plan plan_id=%s", plan_id)
            self.db.rollback()
            return
        plan.summary = summary
        plan.steps = [PlanStep(**step) for step in steps]
        plan.status = PlanStatus.PENDING_APPROVAL
        self.db.commit()
        logger.info("plan generated plan_id=%s steps=%s", plan_id, len(steps))

    def _mark_failed(self, plan_id: UUID, reason: str) -> None:
        plan = self.plans.get(plan_id, lock=True)
        if plan is not None and plan.status == PlanStatus.GENERATING:
            plan.status = PlanStatus.FAILED
            plan.failure_reason = reason
        self.db.commit()

    def approve(self, user: User, plan_id: UUID) -> Plan:
        plan = self._plan_for(user, plan_id, lock=True)
        if plan.status != PlanStatus.PENDING_APPROVAL:
            raise ConflictError(f"Only plans pending approval can be approved, not {plan.status}")
        # Steps are never edited after this point, so execution runs exactly what was reviewed.
        plan.status = PlanStatus.APPROVED
        plan.approved_at = utcnow()
        self.db.commit()
        logger.info("plan approved plan_id=%s user_id=%s", plan.id, user.id)
        return plan

    def reject(self, user: User, plan_id: UUID) -> Plan:
        plan = self._plan_for(user, plan_id, lock=True)
        if plan.status != PlanStatus.PENDING_APPROVAL:
            raise ConflictError(f"Only plans pending approval can be rejected, not {plan.status}")
        plan.status = PlanStatus.REJECTED
        self.db.commit()
        logger.info("plan rejected plan_id=%s user_id=%s", plan.id, user.id)
        return plan

    def execute(self, user: User, plan_id: UUID) -> Plan:
        plan = self._plan_for(user, plan_id, lock=True)
        if plan.status != PlanStatus.APPROVED:
            raise ConflictError("Only approved plans can be executed")

        task = plan.task
        if task.status in CLOSED_TASK_STATUSES:
            raise ConflictError(f"Cannot execute a plan for a {task.status.value} task")
        ensure_task_transition(task.status, TaskStatus.IN_PROGRESS)

        # Plan, first step and task change together or not at all.
        plan.status = PlanStatus.EXECUTING
        plan.steps[0].status = StepStatus.IN_PROGRESS
        task.status = TaskStatus.IN_PROGRESS
        self.db.commit()
        self.cache.invalidate_user(task.user_id)
        logger.info("plan executing plan_id=%s task_id=%s user_id=%s", plan.id, task.id, user.id)
        return plan

    def complete_step(self, user: User, plan_id: UUID, step_id: UUID) -> Plan:
        plan = self._plan_for(user, plan_id, lock=True)
        if plan.status != PlanStatus.EXECUTING:
            raise ConflictError("Plan is not being executed")
        task = plan.task
        if task.status in CLOSED_TASK_STATUSES:
            raise ConflictError(f"Task is {task.status.value}")

        step = next((s for s in plan.steps if s.id == step_id), None)
        if step is None:
            raise NotFoundError("Step not found in this plan")
        if step.status != StepStatus.IN_PROGRESS:
            raise ConflictError("Only the step currently in progress can be completed")

        step.status = StepStatus.COMPLETED
        next_step = next((s for s in plan.steps if s.status == StepStatus.PENDING), None)
        if next_step:
            next_step.status = StepStatus.IN_PROGRESS
        else:
            plan.status = PlanStatus.COMPLETED
            if can_transition(task.status, TaskStatus.COMPLETED):
                task.status = TaskStatus.COMPLETED
        self.db.commit()
        self.cache.invalidate_user(task.user_id)
        logger.info("step completed plan_id=%s step_id=%s user_id=%s", plan.id, step.id, user.id)
        return plan


def run_plan_generation(plan_id: UUID, session_factory: sessionmaker, cache: TaskCache) -> None:
    # The request's session is closed by the time this runs, so open a fresh one.
    with session_factory() as db:
        PlanService(db, cache).generate(plan_id)
