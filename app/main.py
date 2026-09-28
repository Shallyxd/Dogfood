import csv
import io
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import engine, Base, SessionLocal, get_db
from app.models import Event, Track, Team, TeamMember, Project, User, Judge, Assignment, Score
from app.seed import run_seed, print_seed_header
from app.auth import (
    SessionAuthMiddleware,
    get_current_user,
    require_judge,
    require_organizer,
    require_participant,
)

JUDGE_ALIASES = {"judge_a": "jdg_01", "judge_b": "jdg_02"}


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


# --- Public Gallery & Projects ---


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
        db.add(TeamMember(team_id=new_team_id, user_email=user.email))
        team_id = new_team_id

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


# --- Teams ---


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


# --- T2 Judging API & Routes ---


@app.get("/api/judge/scores")
def judge_scores(
    judge: str | None = None,
    user: User = Depends(require_judge),
    db: Session = Depends(get_db),
):
    me = user.judge_profile.fixture_judge_id
    target = JUDGE_ALIASES.get(judge, judge) if judge else me
    if target != me:
        raise HTTPException(status_code=403, detail="Judges can only read their own scores")
    rows = db.query(Score).filter(Score.judge_fixture_id == me).all()
    return [
        {
            "project_id": r.project_id,
            "criterion": r.criterion,
            "value": r.value,
            "comment": r.comment,
        }
        for r in rows
    ]


@app.get("/judge", response_class=HTMLResponse)
async def judge_dashboard(
    request: Request,
    user: User = Depends(require_judge),
    db: Session = Depends(get_db),
):
    me = user.judge_profile.fixture_judge_id
    event = db.query(Event).first()
    rubric = (event.rubric if event and event.rubric else None) or {
        "functionality": 1.0 / 3.0,
        "quality": 1.0 / 3.0,
        "innovation": 1.0 / 3.0,
    }

    assignments = (
        db.query(Assignment)
        .options(
            joinedload(Assignment.project).joinedload(Project.track),
            joinedload(Assignment.project).joinedload(Project.team),
        )
        .filter(Assignment.judge_fixture_id == me)
        .all()
    )

    my_scores = db.query(Score).filter(Score.judge_fixture_id == me).all()
    score_map = {}
    comment_map = {}
    for s in my_scores:
        score_map.setdefault(s.project_id, {})[s.criterion] = s.value
        if s.comment:
            comment_map[s.project_id] = s.comment

    assignment_data = []
    for a in assignments:
        assignment_data.append({
            "assignment": a,
            "project": a.project,
            "scores": score_map.get(a.project_id, {}),
            "comment": comment_map.get(a.project_id, ""),
        })

    return templates.TemplateResponse(
        request=request,
        name="judge.html",
        context={
            "fixture_id": me,
            "assignments": assignment_data,
            "rubric": rubric,
            "user": request.state.user,
        },
    )


@app.post("/judge/score")
async def judge_score_submit(
    request: Request,
    user: User = Depends(require_judge),
    db: Session = Depends(get_db),
):
    me = user.judge_profile.fixture_judge_id

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id required")

    # Verify an Assignment exists for (my fixture id, project_id) — else 403
    assignment = (
        db.query(Assignment)
        .filter(
            Assignment.judge_fixture_id == me,
            Assignment.project_id == project_id,
        )
        .first()
    )
    if not assignment:
        raise HTTPException(status_code=403, detail="No assignment for this project")

    comment = body.get("comment") or None
    now = datetime.now(timezone.utc)

    for crit in ["functionality", "quality", "innovation"]:
        val = body.get(crit)
        if val is not None:
            try:
                val_int = int(val)
            except (ValueError, TypeError):
                continue
            existing_score = (
                db.query(Score)
                .filter(
                    Score.judge_fixture_id == me,
                    Score.project_id == project_id,
                    Score.criterion == crit,
                )
                .first()
            )
            if existing_score:
                existing_score.value = val_int
                existing_score.comment = comment
                existing_score.submitted_at = now
            else:
                db.add(
                    Score(
                        judge_fixture_id=me,
                        project_id=project_id,
                        criterion=crit,
                        value=val_int,
                        comment=comment,
                        submitted_at=now,
                    )
                )

    assignment.status = "done"
    db.commit()

    if "application/json" in request.headers.get("accept", "") or "application/json" in content_type:
        return JSONResponse(status_code=200, content={"status": "ok"})
    return RedirectResponse(url="/judge", status_code=303)


# --- Organizer Console & Export ---


@app.get("/organize", response_class=HTMLResponse)
async def organize_dashboard(
    request: Request,
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    tracks = db.query(Track).all()
    judges = db.query(Judge).all()
    assignments = db.query(Assignment).all()

    judge_stats = {}
    for a in assignments:
        stats = judge_stats.setdefault(a.judge_fixture_id, {"total": 0, "done": 0})
        stats["total"] += 1
        if a.status == "done":
            stats["done"] += 1

    judge_progress = []
    for j in judges:
        st = judge_stats.get(j.id, {"total": 0, "done": 0})
        judge_progress.append({
            "id": j.id,
            "name": j.name,
            "email": j.email,
            "tracks": j.tracks or [],
            "done": st["done"],
            "total": st["total"],
        })

    projects = (
        db.query(Project)
        .options(joinedload(Project.track), joinedload(Project.scores))
        .all()
    )
    project_reviews = []
    for p in projects:
        unique_judges = set(s.judge_fixture_id for s in p.scores)
        project_reviews.append({
            "id": p.id,
            "title": p.title,
            "track": p.track,
            "track_id": p.track_id,
            "reviews_count": len(unique_judges),
        })

    return templates.TemplateResponse(
        request=request,
        name="organize.html",
        context={
            "tracks": tracks,
            "judge_progress": judge_progress,
            "project_reviews": project_reviews,
            "user": request.state.user,
        },
    )


@app.post("/organize/assign")
async def organize_assign(
    request: Request,
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    track_id = body.get("track_id")
    if not track_id:
        raise HTTPException(status_code=422, detail="track_id required")

    projects = db.query(Project).filter(Project.track_id == track_id).all()
    judges = db.query(Judge).all()
    track_judges = [j for j in judges if track_id in (j.tracks or [])]

    created = 0
    for j in track_judges:
        for p in projects:
            existing = (
                db.query(Assignment)
                .filter(
                    Assignment.judge_fixture_id == j.id,
                    Assignment.project_id == p.id,
                )
                .first()
            )
            if not existing:
                db.add(
                    Assignment(
                        judge_fixture_id=j.id,
                        project_id=p.id,
                        status="pending",
                    )
                )
                created += 1
    db.commit()

    if "application/json" in request.headers.get("accept", "") or "application/json" in content_type:
        return JSONResponse(status_code=200, content={"created": created})
    return RedirectResponse(url="/organize", status_code=303)


@app.get("/organize/rubric", response_class=HTMLResponse)
async def organize_rubric_get(
    request: Request,
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    event = db.query(Event).first()
    rubric = (event.rubric if event and event.rubric else None) or {
        "functionality": 1.0 / 3.0,
        "quality": 1.0 / 3.0,
        "innovation": 1.0 / 3.0,
    }
    return templates.TemplateResponse(
        request=request,
        name="rubric.html",
        context={"event": event, "rubric": rubric, "user": request.state.user},
    )


@app.post("/organize/rubric")
async def organize_rubric_post(
    request: Request,
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    f_val = float(body.get("functionality", 1.0 / 3.0))
    q_val = float(body.get("quality", 1.0 / 3.0))
    i_val = float(body.get("innovation", 1.0 / 3.0))

    event = db.query(Event).first()
    if event:
        event.rubric = {
            "functionality": f_val,
            "quality": q_val,
            "innovation": i_val,
        }
        db.commit()

    if "application/json" in request.headers.get("accept", "") or "application/json" in content_type:
        return JSONResponse(status_code=200, content={"rubric": event.rubric if event else {}})
    return RedirectResponse(url="/organize", status_code=303)


@app.get("/api/export.csv")
def export_csv(
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "project_id",
        "title",
        "track",
        "reviews",
        "avg_functionality",
        "avg_quality",
        "avg_innovation",
        "avg_total",
    ])

    event = db.query(Event).first()
    rubric = (event.rubric if event and event.rubric else None) or {
        "functionality": 1.0 / 3.0,
        "quality": 1.0 / 3.0,
        "innovation": 1.0 / 3.0,
    }

    projects = (
        db.query(Project)
        .options(joinedload(Project.track), joinedload(Project.scores))
        .all()
    )

    for p in projects:
        distinct_judges = set()
        crit_scores = {"functionality": [], "quality": [], "innovation": []}
        for s in p.scores:
            distinct_judges.add(s.judge_fixture_id)
            if s.criterion in crit_scores:
                crit_scores[s.criterion].append(s.value)

        reviews_count = len(distinct_judges)
        avg_func = (
            sum(crit_scores["functionality"]) / len(crit_scores["functionality"])
            if crit_scores["functionality"]
            else 0.0
        )
        avg_qual = (
            sum(crit_scores["quality"]) / len(crit_scores["quality"])
            if crit_scores["quality"]
            else 0.0
        )
        avg_innov = (
            sum(crit_scores["innovation"]) / len(crit_scores["innovation"])
            if crit_scores["innovation"]
            else 0.0
        )

        w_func = float(rubric.get("functionality", 1.0 / 3.0))
        w_qual = float(rubric.get("quality", 1.0 / 3.0))
        w_innov = float(rubric.get("innovation", 1.0 / 3.0))
        avg_total = (avg_func * w_func) + (avg_qual * w_qual) + (avg_innov * w_innov)

        writer.writerow([
            p.id,
            p.title,
            p.track.name if p.track else p.track_id,
            reviews_count,
            f"{avg_func:.2f}",
            f"{avg_qual:.2f}",
            f"{avg_innov:.2f}",
            f"{avg_total:.2f}",
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="scores_export.csv"'},
    )


# --- Auth ---


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
