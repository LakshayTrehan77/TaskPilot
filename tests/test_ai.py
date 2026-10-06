import json
from uuid import UUID
import pytest
from google.genai import errors

from app.ai.graph import build_graph
from app.ai.planner import (
    GeminiLLM,
    LLMError,
    MockLLM,
    PlanValidationError,
    parse_plan,
)
from app.ai.tools import DEFAULT_STEP_COUNT, PlannerTools
from app.db.models import Plan, PlanStatus, PlanStep, Task, User


def initial_state(**overrides):
    state = {
        "task_title": "Build a thing",
        "task_description": "with tests",
        "constraints": [],
        "plan": [],
        "summary": "",
        "raw_output": "",
        "error": "",
        "attempts": 0,
    }
    return {**state, **overrides}


class FakeTools:
    def get_task_context(self):
        return {"title": "t", "description": "", "status": "TODO", "priority": "HIGH"}

    def get_related_tasks(self, limit=5):
        return [{"title": "Other task", "priority": "LOW"}]

    def get_user_preferences(self):
        return {"preferred_step_count": 4}


class ScriptedLLM:
    """Returns canned outputs in order and records what it was asked."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def generate_plan(self, title, description, constraints, feedback=None):
        self.calls.append({"constraints": constraints, "feedback": feedback})
        return self.outputs.pop(0)


VALID = json.dumps(
    {
        "summary": "Do it",
        "steps": [
            {"title": "Second", "description": "b", "position": 2},
            {"title": "First", "description": "a", "position": 1},
        ],
    }
)

def test_mock_llm_is_deterministic_and_valid():
    llm = MockLLM()

    first = llm.generate_plan("Ship v1", "", ["Task priority is HIGH"])
    second = llm.generate_plan("Ship v1", "", ["Task priority is HIGH"])
    plan = parse_plan(first)

    assert first == second
    assert len(plan.steps) == 5
    assert "Ship v1" in plan.summary


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "{}",
        json.dumps({"summary": "x", "steps": []}),
        json.dumps({"summary": "", "steps": [{"title": "a", "position": 1}]}),
        json.dumps({"summary": "x", "steps": [{"title": "", "position": 1}]}),
        json.dumps({"summary": "x", "steps": [{"title": "a", "position": 0}]}),
        json.dumps(
            {
                "summary": "x",
                "steps": [
                    {"title": "a", "position": 1},
                    {"title": "b", "position": 1},
                ],
            }
        ),
        json.dumps(
            {
                "summary": "x",
                "steps": [
                    {"title": "a", "position": 1},
                    {"title": "b", "position": 3},
                ],
            }
        ),
    ],
)
def test_parse_plan_rejects_invalid_output(raw):
    with pytest.raises(PlanValidationError):
        parse_plan(raw)


def test_parse_plan_accepts_unordered_positions():
    plan = parse_plan(VALID)
    assert sorted(s.position for s in plan.steps) == [1, 2]


def test_graph_runs_analyze_create_validate_save():
    saved = []
    llm = ScriptedLLM(VALID)

    graph = build_graph(
        llm,
        FakeTools(),
        save_plan=lambda summary, steps: saved.append((summary, steps)),
    )

    graph.invoke(initial_state())

    summary, steps = saved[0]

    assert summary == "Do it"
    assert [s["title"] for s in steps] == ["First", "Second"]

    constraints = llm.calls[0]["constraints"]

    assert any("HIGH" in c for c in constraints)
    assert any("about 4 steps" in c for c in constraints)
    assert any("Other task" in c for c in constraints)


def test_graph_retries_once_with_feedback_then_succeeds():
    saved = []
    llm = ScriptedLLM("garbage", VALID)

    graph = build_graph(
        llm,
        FakeTools(),
        save_plan=lambda s, p: saved.append(s),
    )

    graph.invoke(initial_state())

    assert len(llm.calls) == 2
    assert llm.calls[0]["feedback"] is None
    assert llm.calls[1]["feedback"]
    assert saved == ["Do it"]


def test_graph_gives_up_after_one_retry_and_never_saves():
    saved = []
    llm = ScriptedLLM("garbage", "still garbage", VALID)

    graph = build_graph(
        llm,
        FakeTools(),
        save_plan=lambda s, p: saved.append(s),
    )

    with pytest.raises(PlanValidationError):
        graph.invoke(initial_state())

    assert len(llm.calls) == 2
    assert saved == []

def seed_user_with_tasks(db):
    user = User(email="tools@example.com", password_hash="x")
    db.add(user)
    db.flush()

    main = Task(
        user_id=user.id,
        title="Main task",
        description="d",
    )

    others = [
        Task(user_id=user.id, title=f"Open {i}")
        for i in range(2)
    ]

    done = Task(
        user_id=user.id,
        title="Finished",
        status="COMPLETED",
    )

    stranger = User(
        email="stranger@example.com",
        password_hash="x",
    )

    db.add_all([main, *others, done, stranger])
    db.flush()

    db.add(
        Task(
            user_id=stranger.id,
            title="Not mine",
        )
    )

    db.commit()

    return user, main


def test_tools_read_task_related_tasks_and_preferences(session_factory):
    with session_factory() as db:
        user, main = seed_user_with_tasks(db)

        tools = PlannerTools(db, main)

        context = tools.get_task_context()
        related = tools.get_related_tasks()
        preferences = tools.get_user_preferences()

    assert context["title"] == "Main task"
    assert sorted(t["title"] for t in related) == ["Open 0", "Open 1"]
    assert preferences == {"preferred_step_count": DEFAULT_STEP_COUNT}


def test_preferences_follow_the_size_of_accepted_plans(session_factory):
    with session_factory() as db:
        user, main = seed_user_with_tasks(db)

        accepted = Plan(
            task_id=main.id,
            status=PlanStatus.APPROVED,
        )

        accepted.steps = [
            PlanStep(title=f"s{i}", position=i)
            for i in range(1, 4)
        ]

        rejected = Plan(
            task_id=main.id,
            status=PlanStatus.REJECTED,
        )

        rejected.steps = [
            PlanStep(title=f"r{i}", position=i)
            for i in range(1, 9)
        ]

        db.add_all([accepted, rejected])
        db.commit()

        preferences = PlannerTools(
            db,
            main,
        ).get_user_preferences()

    assert preferences == {"preferred_step_count": 3}


def test_generated_plan_uses_real_tool_output(client, auth_headers, task):
    client.post(
        "/tasks",
        json={"title": "Water the plants"},
        headers=auth_headers,
    )

    plan_id = client.post(
        f"/tasks/{task['id']}/plans",
        headers=auth_headers,
    ).json()["id"]

    plan = client.get(
        f"/plans/{plan_id}",
        headers=auth_headers,
    ).json()

    assert "Water the plants" in plan["steps"][0]["description"]
    assert UUID(plan["id"])

class FakeGeminiResponse:
    def __init__(self, text):
        self.text = text


def test_gemini_client_sends_prompt_and_returns_content(monkeypatch):
    seen = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            seen["kwargs"] = kwargs
            return FakeGeminiResponse(VALID)

    class FakeClient:
        def __init__(self):
            self.models = FakeModels()

    monkeypatch.setattr(
        "app.ai.planner.genai.Client",
        lambda api_key: FakeClient(),
    )

    llm = GeminiLLM(
        "test-key",
        "gemini-2.5-flash",
    )

    raw = llm.generate_plan(
        "Title",
        "Desc",
        ["c1"],
        feedback="bad positions",
    )

    assert raw == VALID

    assert seen["kwargs"]["model"] == "gemini-2.5-flash"
    assert seen["kwargs"]["contents"]
    assert "Title" in seen["kwargs"]["contents"]
    assert "Desc" in seen["kwargs"]["contents"]
    assert "c1" in seen["kwargs"]["contents"]
    assert "bad positions" in seen["kwargs"]["contents"]

    config = seen["kwargs"]["config"]

    assert config.temperature == 0.2
    assert config.response_mime_type == "application/json"
    assert config.response_schema is not None


def test_gemini_client_retries_api_errors(monkeypatch):
    attempts = []

    class FakeModels:
        def generate_content(self, **kwargs):
            attempts.append(1)

            if len(attempts) == 1:
                raise errors.APIError(503, {"message": "temporary Gemini failure"})

            return FakeGeminiResponse(VALID)

    class FakeClient:
        def __init__(self):
            self.models = FakeModels()

    monkeypatch.setattr(
        "app.ai.planner.genai.Client",
        lambda api_key: FakeClient(),
    )

    llm = GeminiLLM(
        "test-key",
        "gemini-2.5-flash",
    )

    assert llm.generate_plan("t", "", []) == VALID
    assert len(attempts) == 2


def test_gemini_client_rejects_empty_response(monkeypatch):
    class FakeModels:
        def generate_content(self, **kwargs):
            return FakeGeminiResponse("")

    class FakeClient:
        def __init__(self):
            self.models = FakeModels()

    monkeypatch.setattr(
        "app.ai.planner.genai.Client",
        lambda api_key: FakeClient(),
    )

    with pytest.raises(LLMError):
        GeminiLLM(
            "test-key",
            "gemini-2.5-flash",
        ).generate_plan("t", "", [])


def test_gemini_client_converts_api_errors_to_llm_error(monkeypatch):
    class FakeModels:
        def generate_content(self, **kwargs):
            raise RuntimeError("Gemini API failed")

    class FakeClient:
        def __init__(self):
            self.models = FakeModels()

    monkeypatch.setattr(
        "app.ai.planner.genai.Client",
        lambda api_key: FakeClient(),
    )

    with pytest.raises(LLMError):
        GeminiLLM(
            "test-key",
            "gemini-2.5-flash",
        ).generate_plan("t", "", [])