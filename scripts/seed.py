"""Create an admin and a demo user with a few tasks. Safe to run more than once.

python -m scripts.seed
"""

import os

from app.core.security import hash_password
from app.db.database import SessionLocal
from app.db.models import Task, TaskPriority, User, UserRole
from app.repositories.user import UserRepository

ADMIN_EMAIL = os.getenv("SEED_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("SEED_ADMIN_PASSWORD", "admin-change-me")
DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo-password"

DEMO_TASKS = [
    ("Prepare a data engineering interview project", "Airflow and PostgreSQL", TaskPriority.HIGH),
    ("Revise SQL window functions", "", TaskPriority.MEDIUM),
    ("Write the README for TaskPilot", "Architecture and design decisions", TaskPriority.LOW),
]


def get_or_create_user(db, email: str, password: str, role: UserRole) -> User:
    users = UserRepository(db)
    user = users.get_by_email(email)
    if user:
        return user
    return users.add(User(email=email, password_hash=hash_password(password), role=role))


def main() -> None:
    with SessionLocal() as db:
        get_or_create_user(db, ADMIN_EMAIL, ADMIN_PASSWORD, UserRole.ADMIN)
        demo = get_or_create_user(db, DEMO_EMAIL, DEMO_PASSWORD, UserRole.USER)
        if not db.query(Task).filter(Task.user_id == demo.id).first():
            for title, description, priority in DEMO_TASKS:
                db.add(
                    Task(user_id=demo.id, title=title, description=description, priority=priority)
                )
        db.commit()
    print(f"Seeded admin {ADMIN_EMAIL} and demo user {DEMO_EMAIL}")


if __name__ == "__main__":
    main()
