import logging
from collections.abc import Callable

from langgraph.graph import END, START, StateGraph

from app.ai.planner import LLMClient, PlanValidationError, parse_plan
from app.ai.state import PlannerState
from app.ai.tools import PlannerTools

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # first try + one retry

SavePlan = Callable[[str, list[dict]], None]


def build_graph(llm: LLMClient, tools: PlannerTools, save_plan: SavePlan):
    """START -> analyze -> create_plan -> validate -> save -> END, with validate looping
    back to create_plan once if the model's output is invalid."""

    def analyze(state: PlannerState) -> dict:
        context = tools.get_task_context()
        related = tools.get_related_tasks()
        preferences = tools.get_user_preferences()

        constraints = [f"Task priority is {context['priority']}"]
        constraints.append(f"Aim for about {preferences['preferred_step_count']} steps")
        if related:
            titles = ", ".join(t["title"] for t in related)
            constraints.append(f"The user also has these open tasks: {titles}")
        return {"constraints": constraints, "attempts": 0, "error": ""}

    def create_plan(state: PlannerState) -> dict:
        raw = llm.generate_plan(
            state["task_title"],
            state["task_description"],
            state["constraints"],
            feedback=state["error"] or None,
        )
        return {"raw_output": raw, "attempts": state["attempts"] + 1}

    def validate(state: PlannerState) -> dict:
        try:
            output = parse_plan(state["raw_output"])
        except PlanValidationError as exc:
            if state["attempts"] >= MAX_ATTEMPTS:
                raise
            logger.warning("planner output invalid (attempt %s): %s", state["attempts"], exc)
            return {"error": str(exc)}
        steps = sorted((s.model_dump() for s in output.steps), key=lambda s: s["position"])
        return {"summary": output.summary, "plan": steps, "error": ""}

    def save(state: PlannerState) -> dict:
        save_plan(state["summary"], state["plan"])
        return {}

    def next_after_validate(state: PlannerState) -> str:
        return "create_plan" if state["error"] else "save"

    graph = StateGraph(PlannerState)
    graph.add_node("analyze", analyze)
    graph.add_node("create_plan", create_plan)
    graph.add_node("validate", validate)
    graph.add_node("save", save)
    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "create_plan")
    graph.add_edge("create_plan", "validate")
    graph.add_conditional_edges("validate", next_after_validate, ["create_plan", "save"])
    graph.add_edge("save", END)
    return graph.compile()
