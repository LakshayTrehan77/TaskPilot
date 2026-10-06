from sqlalchemy.orm import Session

from app.db.models import Task
from app.repositories.plan import PlanRepository
from app.repositories.task import TaskRepository

DEFAULT_STEP_COUNT = 5


class PlannerTools:
    """Read-only helpers the planner calls to gather context. They only query the
    current user's data and have no write access."""

    def __init__(self, db: Session, task: Task):
        self.task = task
        self.tasks = TaskRepository(db)
        self.plans = PlanRepository(db)

    def get_task_context(self) -> dict:
        return {
            "title": self.task.title,
            "description": self.task.description,
            "status": self.task.status.value,
            "priority": self.task.priority.value,
        }

    def get_related_tasks(self, limit: int = 5) -> list[dict]:
        others = self.tasks.list_open_for_user(self.task.user_id, self.task.id, limit)
        return [{"title": t.title, "priority": t.priority.value} for t in others]

    def get_user_preferences(self) -> dict:
        # No preferences table: infer from how big the user's previously accepted plans were.
        average = self.plans.average_step_count_for_user(self.task.user_id)
        steps = round(float(average)) if average else DEFAULT_STEP_COUNT
        return {"preferred_step_count": steps}
