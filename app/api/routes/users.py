from fastapi import APIRouter, Depends

from app.api.dependencies import get_current_user
from app.db.models import User
from app.schemas.user import UserRead

router = APIRouter(prefix="/users", tags=["auth"])


@router.get("/me", response_model=UserRead, summary="Current user")
def read_me(user: User = Depends(get_current_user)):
    return user
