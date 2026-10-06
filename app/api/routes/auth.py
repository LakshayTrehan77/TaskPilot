from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm

from app.api.dependencies import get_auth_service
from app.schemas.auth import RegisterRequest, Token
from app.schemas.user import UserRead
from app.services.auth import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserRead, status_code=201, summary="Create an account")
def register(payload: RegisterRequest, service: AuthService = Depends(get_auth_service)):
    return service.register(payload.email, payload.password)


@router.post("/login", response_model=Token, summary="Get a JWT access token")
def login(
    form: OAuth2PasswordRequestForm = Depends(), service: AuthService = Depends(get_auth_service)
):
    """Form fields follow the OAuth2 password flow so Swagger's Authorize button works;
    put your email in the `username` field."""
    return Token(access_token=service.login(form.username, form.password))
