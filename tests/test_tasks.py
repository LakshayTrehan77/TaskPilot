from app.cache.redis import TaskCache, get_cache
from app.main import app
from tests.conftest import BrokenRedis


def create_task(client, headers, title="Write report", **extra):
    return client.post("/tasks", json={"title": title, **extra}, headers=headers).json()


def test_create_and_get_task(client, auth_headers):
    response = client.post(
        "/tasks", json={"title": "  Ship it  ", "priority": "HIGH"}, headers=auth_headers
    )

    assert response.status_code == 201
    created = response.json()
    assert created["title"] == "Ship it"
    assert created["status"] == "TODO"
    assert created["priority"] == "HIGH"

    fetched = client.get(f"/tasks/{created['id']}", headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]


def test_create_rejects_empty_title_and_bad_priority(client, auth_headers):
    assert client.post("/tasks", json={"title": "  "}, headers=auth_headers).status_code == 422
    bad_priority = {"title": "x", "priority": "URGENT"}
    assert client.post("/tasks", json=bad_priority, headers=auth_headers).status_code == 422


def test_get_unknown_task_is_404(client, auth_headers):
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/tasks/{missing}", headers=auth_headers).status_code == 404
    assert client.get("/tasks/not-a-uuid", headers=auth_headers).status_code == 422


def test_patch_updates_only_sent_fields(client, auth_headers, task):
    response = client.patch(
        f"/tasks/{task['id']}", json={"status": "IN_PROGRESS"}, headers=auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "IN_PROGRESS"
    assert body["title"] == task["title"]


def test_patch_rejects_null_title(client, auth_headers, task):
    response = client.patch(f"/tasks/{task['id']}", json={"title": None}, headers=auth_headers)
    assert response.status_code == 422


def test_completed_task_cannot_go_back_to_todo(client, auth_headers, task):
    url = f"/tasks/{task['id']}"
    client.patch(url, json={"status": "IN_PROGRESS"}, headers=auth_headers)
    client.patch(url, json={"status": "COMPLETED"}, headers=auth_headers)

    response = client.patch(url, json={"status": "TODO"}, headers=auth_headers)

    assert response.status_code == 409
    assert "COMPLETED" in response.json()["detail"]


def test_todo_cannot_jump_straight_to_completed(client, auth_headers, task):
    response = client.patch(
        f"/tasks/{task['id']}", json={"status": "COMPLETED"}, headers=auth_headers
    )
    assert response.status_code == 409


def test_delete_task(client, auth_headers, task):
    assert client.delete(f"/tasks/{task['id']}", headers=auth_headers).status_code == 204
    assert client.get(f"/tasks/{task['id']}", headers=auth_headers).status_code == 404


def test_pagination(client, auth_headers):
    for i in range(5):
        create_task(client, auth_headers, title=f"Task {i}")

    first = client.get("/tasks?page=1&page_size=2", headers=auth_headers).json()
    last = client.get("/tasks?page=3&page_size=2", headers=auth_headers).json()

    assert first["total"] == 5
    assert len(first["items"]) == 2
    assert len(last["items"]) == 1
    assert client.get("/tasks?page_size=500", headers=auth_headers).status_code == 422


def test_filtering_by_status_and_priority(client, auth_headers):
    create_task(client, auth_headers, title="a", priority="HIGH")
    b = create_task(client, auth_headers, title="b", priority="LOW")
    create_task(client, auth_headers, title="c", priority="HIGH")
    client.patch(f"/tasks/{b['id']}", json={"status": "IN_PROGRESS"}, headers=auth_headers)

    high = client.get("/tasks?priority=HIGH", headers=auth_headers).json()
    in_progress = client.get("/tasks?status=IN_PROGRESS", headers=auth_headers).json()

    assert high["total"] == 2
    assert [t["title"] for t in in_progress["items"]] == ["b"]
    assert client.get("/tasks?status=NOPE", headers=auth_headers).status_code == 422


def test_sorting(client, auth_headers):
    for title in ("banana", "apple", "cherry"):
        create_task(client, auth_headers, title=title)

    ascending = client.get("/tasks?sort=title", headers=auth_headers).json()["items"]
    descending = client.get("/tasks?sort=-title", headers=auth_headers).json()["items"]

    assert [t["title"] for t in ascending] == ["apple", "banana", "cherry"]
    assert [t["title"] for t in descending] == ["cherry", "banana", "apple"]
    assert client.get("/tasks?sort=password", headers=auth_headers).status_code == 422


def test_users_cannot_touch_each_others_tasks(client, auth_headers, other_headers, task):
    url = f"/tasks/{task['id']}"

    assert client.get(url, headers=other_headers).status_code == 403
    assert client.patch(url, json={"title": "hijacked"}, headers=other_headers).status_code == 403
    assert client.delete(url, headers=other_headers).status_code == 403
    assert client.get("/tasks", headers=other_headers).json()["total"] == 0


def test_admin_can_see_and_manage_all_tasks(client, admin_headers, task):
    listing = client.get("/tasks", headers=admin_headers).json()
    assert listing["total"] == 1

    patched = client.patch(f"/tasks/{task['id']}", json={"priority": "LOW"}, headers=admin_headers)
    assert patched.status_code == 200
    assert client.delete(f"/tasks/{task['id']}", headers=admin_headers).status_code == 204


def test_list_is_cached_and_invalidated_on_write(client, auth_headers, fake_redis):
    create_task(client, auth_headers, title="first")
    client.get("/tasks", headers=auth_headers)
    cached_keys = [k for k in fake_redis.data if k.startswith("tasks:list:")]
    assert len(cached_keys) == 1

    # Poison the cached page to prove the next read is served from Redis.
    fake_redis.data[cached_keys[0]] = fake_redis.data[cached_keys[0]].replace("first", "from-cache")
    assert client.get("/tasks", headers=auth_headers).json()["items"][0]["title"] == "from-cache"

    create_task(client, auth_headers, title="second")
    titles = [t["title"] for t in client.get("/tasks", headers=auth_headers).json()["items"]]
    assert sorted(titles) == ["first", "second"]


def test_api_works_when_redis_is_down(client, auth_headers):
    app.dependency_overrides[get_cache] = lambda: TaskCache(BrokenRedis(), ttl_seconds=30)

    created = client.post("/tasks", json={"title": "still works"}, headers=auth_headers)
    listing = client.get("/tasks", headers=auth_headers)

    assert created.status_code == 201
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
