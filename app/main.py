import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import engine, Base, SessionLocal, get_db
from app.models import Event, Track, Team, TeamMember, Project, User
from app.seed import run_seed, print_seed_header
from app.auth import (
    SessionAuthMiddleware,
    get_current_user,
    require_participant,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        run_seed(db)
    finally:
        db.close()
    print_seed_header()
    yield


app = FastAPI(title="DOGFOOD 2026 Portal", lifespan=lifespan)
app.add_middleware(SessionAuthMiddleware)

templates = Jinja2Templates(directory="templates")


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: Exception):
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    return templates.TemplateResponse(
        request=request,
        name="404.html",
        context={"user": request.state.user, "detail": "Page not found"},
        status_code=404,
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return HTMLResponse(
        status_code=exc.status_code,
        content=f"<!DOCTYPE html><html><body><h1>{exc.status_code} {exc.detail}</h1><p><a href='/login'>Login</a> | <a href='/projects'>Gallery</a></p></body></html>",
    )


@app.get("/")
async def root():
    return RedirectResponse(url="/projects", status_code=303)


@app.get("/projects", response_class=HTMLResponse)
async def gallery(request: Request, q: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Project).options(joinedload(Project.track), joinedload(Project.team))
    if q and q.strip():
        query = query.filter(Project.title.ilike(f"%{q.strip()}%"))
    projects = query.all()
    return templates.TemplateResponse(
        request=request,
        name="gallery.html",
        context={"projects": projects, "q": q, "user": request.state.user},
    )


@app.get("/projects/new", response_class=HTMLResponse)
async def new_project_form(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_participant),
):
    event = db.query(Event).first()
    is_closed = False
    if event:
        close_dt = event.submissions_close
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > close_dt:
            is_closed = True

    tracks = db.query(Track).all()
    return templates.TemplateResponse(
        request=request,
        name="project_new.html",
        context={"closed": is_closed, "tracks": tracks, "user": request.state.user},
    )


@app.post("/projects/new")
async def create_project(request: Request, db: Session = Depends(get_db)):
    # (1) Check closed FIRST before auth role, before parsing body
    event = db.query(Event).first()
    now = datetime.now(timezone.utc)
    if event:
        close_dt = event.submissions_close
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=timezone.utc)
        if now > close_dt:
            return JSONResponse(
                status_code=422,
                content={"detail": "Submissions are closed"},
            )

    # (2) Require participant (else 403)
    user = get_current_user(request, db)
    if not user or user.role != "participant":
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})

    # (3) Parse body (accepts JSON and form data)
    body = {}
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:
            body = {}
    else:
        try:
            form = await request.form()
            body = dict(form)
        except Exception:
            body = {}

    title = str(body.get("title", "")).strip() if body else ""
    if not title:
        return JSONResponse(status_code=422, content={"detail": "Title is required"})

    # (4) Create Project with status 'submitted', team = participant's team (create if none)
    tm = db.query(TeamMember).filter(TeamMember.user_email == user.email).first()
    if tm:
        team_id = tm.team_id
    else:
        new_team_id = f"tm_{secrets.token_hex(4)}"
        invite_code = secrets.token_hex(4)
        new_team = Team(
            id=new_team_id,
            name=f"Team {user.email.split('@')[0]}",
            event_id=event.id if event else "evt_01",
            invite_code=invite_code,
        )
        db.add(new_team)
        db.flush()
        db.add(TeamMember(team_id=new_team.id, user_email=user.email))
        team_id = new_team.id

    track_id = body.get("track_id")
    track = db.query(Track).filter(Track.id == track_id).first() if track_id else None
    if not track:
        first_track = db.query(Track).first()
        track_id = first_track.id if first_track else "trk_01"

    new_project_id = f"prj_{secrets.token_hex(4)}"
    project = Project(
        id=new_project_id,
        team_id=team_id,
        track_id=track_id,
        title=title,
        summary=body.get("summary", ""),
        repo_url=body.get("repo_url") or None,
        submitted_at=now,
        status="submitted",
    )
    db.add(project)
    db.commit()

    if "application/json" in request.headers.get("accept", "") or "application/json" in content_type:
        return JSONResponse(
            status_code=201,
            content={
                "id": project.id,
                "title": project.title,
                "team_id": project.team_id,
                "track_id": project.track_id,
                "status": project.status,
            },
        )
    return RedirectResponse(url=f"/projects/{project.id}", status_code=303)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(project_id: str, request: Request, db: Session = Depends(get_db)):
    project = (
        db.query(Project)
        .options(joinedload(Project.track), joinedload(Project.team))
        .filter(Project.id == project_id)
        .first()
    )
    if not project:
        return templates.TemplateResponse(
            request=request,
            name="404.html",
            context={"user": request.state.user, "detail": "Project not found"},
            status_code=404,
        )
    return templates.TemplateResponse(
        request=request,
        name="project_detail.html",
        context={"project": project, "user": request.state.user},
    )


@app.get("/teams/new", response_class=HTMLResponse)
async def new_team_form(
    request: Request,
    user: User = Depends(require_participant),
):
    return templates.TemplateResponse(
        request=request,
        name="team_new.html",
        context={"user": request.state.user},
    )


@app.post("/teams")
async def create_team(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != "participant":
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:
            body = {}
    else:
        try:
            form = await request.form()
            body = dict(form)
        except Exception:
            body = {}

    name = str(body.get("name", "")).strip() or f"Team {user.email.split('@')[0]}"
    event = db.query(Event).first()
    invite_code = secrets.token_hex(4)  # 8 chars
    team_id = f"tm_{secrets.token_hex(4)}"

    team = Team(
        id=team_id,
        name=name,
        event_id=event.id if event else "evt_01",
        invite_code=invite_code,
    )
    db.add(team)
    db.flush()
    db.add(TeamMember(team_id=team.id, user_email=user.email))
    db.commit()

    invite_url = f"/join/{invite_code}"
    if "application/json" in request.headers.get("accept", "") or "application/json" in content_type:
        return JSONResponse(
            status_code=201,
            content={
                "team_id": team.id,
                "name": team.name,
                "invite_code": invite_code,
                "invite_url": invite_url,
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="team_created.html",
        context={"team": team, "invite_url": invite_url, "user": request.state.user},
    )


@app.get("/join/{invite_code}")
async def join_team(invite_code: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != "participant":
        raise HTTPException(status_code=403, detail="Forbidden")

    team = db.query(Team).filter(Team.invite_code == invite_code).first()
    if not team:
        return templates.TemplateResponse(
            request=request,
            name="404.html",
            context={"user": request.state.user, "detail": "Invalid invite code"},
            status_code=404,
        )

    existing = (
        db.query(TeamMember)
        .filter(TeamMember.team_id == team.id, TeamMember.user_email == user.email)
        .first()
    )
    if not existing:
        db.add(TeamMember(team_id=team.id, user_email=user.email))
        db.commit()

    return RedirectResponse(url="/projects", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"user": request.state.user},
    )


@app.get("/login/{token}")
async def login_with_token(token: str):
    response = RedirectResponse(url="/projects", status_code=303)
    response.set_cookie("session", token, path="/", httponly=False)
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/projects", status_code=303)
    response.delete_cookie("session", path="/")
    return response
