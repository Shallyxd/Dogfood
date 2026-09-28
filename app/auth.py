from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request, HTTPException, Depends
from sqlalchemy.orm import Session, joinedload

from app.database import SessionLocal, get_db
from app.models import User


class SessionAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        session_token = request.cookies.get("session")
        user = None
        if session_token:
            db = SessionLocal()
            try:
                user = (
                    db.query(User)
                    .options(joinedload(User.judge_profile))
                    .filter(User.session_token == session_token)
                    .first()
                )
                if user:
                    db.expunge_all()
            finally:
                db.close()
        request.state.user = user
        response = await call_next(request)
        return response


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    return getattr(request.state, "user", None)


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if not user:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def require_organizer(user: User | None = Depends(get_current_user)) -> User:
    if not user or user.role != "organizer":
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def require_judge(user: User | None = Depends(get_current_user)) -> User:
    if not user or user.role != "judge":
        raise HTTPException(status_code=403, detail="Forbidden")
    if getattr(user, "judge_profile", None) is None:
        raise HTTPException(status_code=403, detail="Judge account is not linked to a judge profile")
    return user


def require_participant(user: User | None = Depends(get_current_user)) -> User:
    if not user or user.role != "participant":
        raise HTTPException(status_code=403, detail="Forbidden")
    return user
