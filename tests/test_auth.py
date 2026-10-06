from tests.conftest import register_and_login


def test_register_returns_user_without_password(client):
    response = client.post(
        "/auth/register", json={"email": "New@Example.com", "password": "password123"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "new@example.com"
    assert body["role"] == "USER"
    assert "password" not in body and "password_hash" not in body


def test_register_duplicate_email_conflicts(client):
    payload = {"email": "dup@example.com", "password": "password123"}
    client.post("/auth/register", json=payload)

    assert client.post("/auth/register", json=payload).status_code == 409


def test_register_rejects_short_password_and_bad_email(client):
    assert (
        client.post(
            "/auth/register", json={"email": "a@example.com", "password": "short"}
        ).status_code
        == 422
    )
    assert (
        client.post("/auth/register", json={"email": "nope", "password": "password123"}).status_code
        == 422
    )


def test_login_returns_token_that_works(client):
    headers = register_and_login(client)

    response = client.get("/users/me", headers=headers)

    assert response.status_code == 200
    assert response.json()["email"] == "alice@example.com"


def test_login_with_wrong_password_or_unknown_user_is_401(client):
    client.post("/auth/register", json={"email": "a@example.com", "password": "password123"})

    wrong = client.post("/auth/login", data={"username": "a@example.com", "password": "wrongpass1"})
    unknown = client.post(
        "/auth/login", data={"username": "x@example.com", "password": "password123"}
    )

    assert wrong.status_code == 401
    assert unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_protected_endpoints_require_a_valid_token(client):
    assert client.get("/users/me").status_code == 401
    assert client.get("/tasks").status_code == 401
    assert client.get("/users/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
