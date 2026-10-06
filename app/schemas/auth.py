from typing import Annotated

from pydantic import AfterValidator, BaseModel, EmailStr, Field


def _fits_bcrypt(password: str) -> str:
    # bcrypt only looks at the first 72 bytes; refuse instead of silently truncating.
    if len(password.encode()) > 72:
        raise ValueError("password must be at most 72 bytes")
    return password


Password = Annotated[str, Field(min_length=8), AfterValidator(_fits_bcrypt)]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: Password


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
