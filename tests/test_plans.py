from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from app.ai.planner import LLMError
from app.db.models import Plan, PlanStatus, utcnow
from tests.conftest import register_and_login


def request_plan(client, headers, task_id, key=None):
    extra = {"Idempotency-Key": key} if key else {}
    return client.post(f"/tasks/{task_id}/plans", headers={**headers, **extra})


def make_pending_plan(client, headers, task_id) -> dict:
    # The background job finishes before TestClient returns, so a GET shows the final state.
    plan_id = request_plan(client, headers, task_id).json()["id"]
    return client.get(f"/plans/{plan_id}", headers=headers).json()


def test_create_plan_returns_202_then_background_generation_finishes(client, auth_headers, task):
    response = request_plan(client, auth_headers, task["id"])

    assert response.status_code == 202
    assert response.json()["status"] == "GENERATING"
    assert response.json()["steps"] == []

    plan = client.get(f"/plans/{response.json()['id']}", headers=auth_headers).json()
    assert plan["status"] == "PENDING_APPROVAL"
    assert len(plan["steps"]) == 5
    assert [s["position"] for s in plan["steps"]] == [1, 2, 3, 4, 5]
    assert all(s["status"] == "PENDING" for s in plan["steps"])


def test_generated_plan_does_not_change_the_task(client, auth_headers, task):
    make_pending_plan(client, auth_headers, task["id"])

    after = client.get(f"/tasks/{task['id']}", headers=auth_headers).json()

    assert after["status"] == "TODO"
    assert after["title"] == task["title"]


def test_list_plans_for_task(client, auth_headers, task):
    make_pending_plan(client, auth_headers, task["id"])

    plans = client.get(f"/tasks/{task['id']}/plans", headers=auth_headers).json()

    assert len(plans) == 1


def test_plan_of_unknown_task_is_404(client, auth_headers):
    missing = "00000000-0000-0000-0000-000000000000"
    assert request_plan(client, auth_headers, missing).status_code == 404


def test_other_users_cannot_see_or_change_a_plan(client, auth_headers, other_headers, task):
    plan = make_pending_plan(client, auth_headers, task["id"])

    assert request_plan(client, other_headers, task["id"]).status_code == 403
    assert client.get(f"/plans/{plan['id']}", headers=other_headers).status_code == 403
    assert client.post(f"/plans/{plan['id']}/approve", headers=other_headers).status_code == 403
    assert client.get(f"/tasks/{task['id']}/plans", headers=other_headers).status_code == 403


def test_approve_sets_status_and_timestamp(client, auth_headers, task):
    plan = make_pending_plan(client, auth_headers, task["id"])

    response = client.post(f"/plans/{plan['id']}/approve", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "APPROVED"
    assert response.json()["approved_at"] is not None


def test_approve_twice_is_a_conflict(client, auth_headers, task):
    plan = make_pending_plan(client, auth_headers, task["id"])
    client.post(f"/plans/{plan['id']}/approve", headers=auth_headers)

    assert client.post(f"/plans/{plan['id']}/approve", headers=auth_headers).status_code == 409
    assert client.post(f"/plans/{plan['id']}/reject", headers=auth_headers).status_code == 409


def test_reject_then_regenerate(client, auth_headers, task):
    plan = make_pending_plan(client, auth_headers, task["id"])

    rejected = client.post(f"/plans/{plan['id']}/reject", headers=auth_headers)
    again = request_plan(client, auth_headers, task["id"])

    assert rejected.json()["status"] == "REJECTED"
    assert again.status_code == 202
    assert again.json()["id"] != plan["id"]


def test_execute_requires_approval(client, auth_headers, task):
    plan = make_pending_plan(client, auth_headers, task["id"])

    response = client.post(f"/plans/{plan['id']}/execute", headers=auth_headers)

    assert response.status_code == 409
    assert client.get(f"/tasks/{task['id']}", headers=auth_headers).json()["status"] == "TODO"


def test_full_lifecycle_from_plan_to_completed_task(client, auth_headers, task):
    plan = make_pending_plan(client, auth_headers, task["id"])
    client.post(f"/plans/{plan['id']}/approve", headers=auth_headers)

    started = client.post(f"/plans/{plan['id']}/execute", headers=auth_headers).json()

    assert started["status"] == "EXECUTING"
    assert [s["status"] for s in started["steps"]] == ["IN_PROGRESS"] + ["PENDING"] * 4
    assert (
        client.get(f"/tasks/{task['id']}", headers=auth_headers).json()["status"] == "IN_PROGRESS"
    )

    current = started
    for _ in range(5):
        step = next(s for s in current["steps"] if s["status"] == "IN_PROGRESS")
        response = client.post(
            f"/plans/{plan['id']}/steps/{step['id']}/complete", headers=auth_headers
        )
        assert response.status_code == 200
        current = response.json()

    assert current["status"] == "COMPLETED"
    assert all(s["status"] == "COMPLETED" for s in current["steps"])
    assert client.get(f"/tasks/{task['id']}", headers=auth_headers).json()["status"] == "COMPLETED"


def test_only_the_current_step_can_be_completed(client, auth_headers, task):
    plan = make_pending_plan(client, auth_headers, task["id"])
    client.post(f"/plans/{plan['id']}/approve", headers=auth_headers)
    started = client.post(f"/plans/{plan['id']}/execute", headers=auth_headers).json()
    later_step = started["steps"][2]

    response = client.post(
        f"/plans/{plan['id']}/steps/{later_step['id']}/complete", headers=auth_headers
    )

    assert response.status_code == 409


def test_cannot_execute_plan_for_a_cancelled_task(client, auth_headers, task):
    plan = make_pending_plan(client, auth_headers, task["id"])
    client.post(f"/plans/{plan['id']}/approve", headers=auth_headers)
    client.patch(f"/tasks/{task['id']}", json={"status": "CANCELLED"}, headers=auth_headers)

    assert client.post(f"/plans/{plan['id']}/execute", headers=auth_headers).status_code == 409


def test_execution_invalidates_the_task_list_cache(client, auth_headers, task, fake_redis):
    client.get("/tasks", headers=auth_headers)
    plan = make_pending_plan(client, auth_headers, task["id"])
    client.post(f"/plans/{plan['id']}/approve", headers=auth_headers)
    client.post(f"/plans/{plan['id']}/execute", headers=auth_headers)

    listing = client.get("/tasks", headers=auth_headers).json()

    assert listing["items"][0]["status"] == "IN_PROGRESS"


# ---- reliability -----------------------------------------------------------------------


def test_same_idempotency_key_returns_the_same_plan(client, auth_headers, task):
    first = request_plan(client, auth_headers, task["id"], key="abc123")
    retry = request_plan(client, auth_headers, task["id"], key="abc123")

    assert first.status_code == retry.status_code == 202
    assert first.json()["id"] == retry.json()["id"]
    assert len(client.get(f"/tasks/{task['id']}/plans", headers=auth_headers).json()) == 1


def test_different_idempotency_key_while_plan_is_active_conflicts(client, auth_headers, task):
    request_plan(client, auth_headers, task["id"], key="one")

    response = request_plan(client, auth_headers, task["id"], key="two")

    assert response.status_code == 409


def test_duplicate_generation_without_key_conflicts(client, auth_headers, task):
    assert request_plan(client, auth_headers, task["id"]).status_code == 202

    response = request_plan(client, auth_headers, task["id"])

    assert response.status_code == 409
    assert len(client.get(f"/tasks/{task['id']}/plans", headers=auth_headers).json()) == 1


def test_retry_with_key_still_works_after_plan_was_rejected(client, auth_headers, task):
    first = request_plan(client, auth_headers, task["id"], key="k1").json()
    client.post(f"/plans/{first['id']}/reject", headers=auth_headers)

    retry = request_plan(client, auth_headers, task["id"], key="k1")

    assert retry.json()["id"] == first["id"]


def test_database_allows_only_one_active_plan_per_task(session_factory, client, auth_headers, task):
    # Bypasses the service check to prove the partial unique index is the real guard.
    with session_factory() as db:
        db.add(Plan(task_id=UUID(task["id"]), status=PlanStatus.GENERATING))
        db.commit()
        db.add(Plan(task_id=UUID(task["id"]), status=PlanStatus.PENDING_APPROVAL))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(Plan(task_id=UUID(task["id"]), status=PlanStatus.REJECTED))
        db.commit()


def test_failed_generation_marks_plan_failed_and_allows_retry(
    client, auth_headers, task, monkeypatch
):
    class BrokenLLM:
        def generate_plan(self, *args, **kwargs):
            raise LLMError("provider is down")

    monkeypatch.setattr("app.services.plan.get_llm", lambda: BrokenLLM())

    plan_id = request_plan(client, auth_headers, task["id"]).json()["id"]
    plan = client.get(f"/plans/{plan_id}", headers=auth_headers).json()

    assert plan["status"] == "FAILED"
    assert "provider is down" in plan["failure_reason"]
    assert plan["steps"] == []

    monkeypatch.undo()
    assert request_plan(client, auth_headers, task["id"]).status_code == 202


def test_invalid_llm_output_twice_fails_the_plan(client, auth_headers, task, monkeypatch):
    class GarbageLLM:
        calls = 0

        def generate_plan(self, *args, **kwargs):
            GarbageLLM.calls += 1
            return '{"summary": "x", "steps": []}'

    monkeypatch.setattr("app.services.plan.get_llm", lambda: GarbageLLM())

    plan_id = request_plan(client, auth_headers, task["id"]).json()["id"]
    plan = client.get(f"/plans/{plan_id}", headers=auth_headers).json()

    assert plan["status"] == "FAILED"
    assert GarbageLLM.calls == 2


def test_stale_generating_plan_does_not_block_the_task_forever(
    client, session_factory, auth_headers, task
):
    with session_factory() as db:
        db.add(
            Plan(
                task_id=UUID(task["id"]),
                status=PlanStatus.GENERATING,
                created_at=utcnow() - timedelta(hours=1),
            )
        )
        db.commit()

    response = request_plan(client, auth_headers, task["id"])

    assert response.status_code == 202
    statuses = [
        p["status"] for p in client.get(f"/tasks/{task['id']}/plans", headers=auth_headers).json()
    ]
    assert "FAILED" in statuses


def test_deleting_a_task_removes_its_plans(client, session_factory, auth_headers, task):
    plan = make_pending_plan(client, auth_headers, task["id"])

    client.delete(f"/tasks/{task['id']}", headers=auth_headers)

    assert client.get(f"/plans/{plan['id']}", headers=auth_headers).status_code == 404


def test_admin_can_approve_other_users_plan(client, admin_headers, auth_headers, task):
    plan = make_pending_plan(client, auth_headers, task["id"])

    assert client.post(f"/plans/{plan['id']}/approve", headers=admin_headers).status_code == 200


def test_register_helper_gives_distinct_users(client):
    a = register_and_login(client, "a@example.com")
    b = register_and_login(client, "b@example.com")
    assert (
        client.get("/users/me", headers=a).json()["id"]
        != client.get("/users/me", headers=b).json()["id"]
    )
