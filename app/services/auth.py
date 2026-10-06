import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import User, UserRole
from app.repositories.user import UserRepository

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(self, db: Session):
        self.db = db
        self.users = UserRepository(db)

    def register(self, email: str, password: str) -> User:
        email = email.lower()
        if self.users.get_by_email(email):
            raise ConflictError("Email is already registered")

        # Public registration always creates a regular user; admins are created by seed script.
        user = User(email=email, password_hash=hash_password(password), role=UserRole.USER)
        try:
            self.users.add(user)
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise ConflictError("Email is already registered") from None
        logger.info("user registered user_id=%s", user.id)
        return user

    def login(self, email: str, password: str) -> str:
        user = self.users.get_by_email(email.lower())
        # Same message for unknown email and wrong password so emails can't be probed.
        if user is None or not verify_password(password, user.password_hash):
            logger.info("login failed")
            raise UnauthorizedError("Incorrect email or password")
        logger.info("login ok user_id=%s", user.id)
        return create_access_token(user.id)
