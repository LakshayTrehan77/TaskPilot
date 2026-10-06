from typing import TypedDict


class PlannerState(TypedDict):
    task_title: str
    task_description: str
    constraints: list[str]
    plan: list[dict]
    summary: str
    # Bookkeeping for the validate -> retry loop.
    raw_output: str
    error: str
    attempts: int
