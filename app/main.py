import csv
import hashlib
import io
import json
import secrets
import statistics
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import engine, Base, SessionLocal, get_db
from app.models import Event, Track, Team, TeamMember, Project, User, Judge, Assignment, Score, Result, ReviewEvent, AuditEvent, Vote, ResultBundle
from app.seed import run_seed, print_seed_header
from app.judging.normalise import normalise
from app.judging.influence import kingmakers
from app.judging.uncertainty import bootstrap_top_probabilities
from app.auth import (
    SessionAuthMiddleware,
    get_current_user,
    require_judge,
    require_organizer,
    require_participant,
    require_user,
)

JUDGE_ALIASES = {"judge_a": "jdg_01", "judge_b": "jdg_02"}

# T3 — quadratic voting parameters. Cost of v votes on one project is v² credits.
VOTE_BUDGET = 25
MAX_VOTES_PER_PROJECT = 3


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


def _log_audit(db: Session, user: User, action: str, detail: str = "") -> None:
    """Append-only organiser audit trail; committed with the caller's transaction."""
    db.add(AuditEvent(
        actor_email=user.email,
        action=action,
        detail=detail,
        created_at=datetime.now(timezone.utc),
    ))


def _results_published(db: Session) -> bool:
    return db.query(Result).count() > 0


@app.get("/", response_class=HTMLResponse)
async def root(request: Request, db: Session = Depends(get_db)):
    event = db.query(Event).first()
    project_count = db.query(Project).count()
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={"event": event, "project_count": project_count, "user": request.state.user},
    )


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
        new_team_id = f"tm_{secrets.token_hex(8)}"
        invite_code = secrets.token_hex(8)
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

    repo_url = str(body.get("repo_url", "") or "").strip() or None
    if repo_url and not repo_url.lower().startswith(("http://", "https://")):
        return JSONResponse(
            status_code=422,
            content={"detail": "Repository URL must start with http:// or https://"},
        )

    new_project_id = f"prj_{secrets.token_hex(8)}"
    project = Project(
        id=new_project_id,
        team_id=team_id,
        track_id=track_id,
        title=title,
        summary=body.get("summary", ""),
        repo_url=repo_url,
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

    user = request.state.user
    if user and user.role == "judge" and getattr(user, "judge_profile", None):
        me = user.judge_profile.fixture_judge_id
        db.add(
            ReviewEvent(
                judge_fixture_id=me,
                project_id=project.id,
                artefact="description",
                opened_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

    return templates.TemplateResponse(
        request=request,
        name="project_detail.html",
        context={"project": project, "user": request.state.user},
    )


@app.get("/go/{project_id}/repo")
async def go_project_repo(
    project_id: str,
    user: User = Depends(require_judge),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    me = user.judge_profile.fixture_judge_id
    db.add(
        ReviewEvent(
            judge_fixture_id=me,
            project_id=project.id,
            artefact="repo",
            opened_at=datetime.now(timezone.utc),
        )
    )
    db.commit()

    if not project.repo_url:
        return RedirectResponse(url=f"/projects/{project.id}", status_code=303)
    return RedirectResponse(url=project.repo_url, status_code=303)


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
    invite_code = secrets.token_hex(8)
    team_id = f"tm_{secrets.token_hex(8)}"

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


def compute_judge_calibration(db: Session, fixture_id: str, rubric: dict) -> dict:
    scores_rows = db.query(Score).all()
    if not scores_rows:
        return {
            "has_data": False,
            "message": "Not enough data yet — anchors will appear after your first reviews.",
        }

    judge_proj_crit = {}
    for s in scores_rows:
        judge_proj_crit.setdefault(s.judge_fixture_id, {}).setdefault(
            s.project_id, {}
        )[s.criterion] = s.value

    scores = {}
    for j_id, p_map in judge_proj_crit.items():
        for p_id, crits in p_map.items():
            w_sum = sum(crits.get(c, 0) * rubric.get(c, 1.0 / 3.0) for c in crits)
            scores.setdefault(j_id, {})[p_id] = w_sum

    my_scores = scores.get(fixture_id, {})
    if len(my_scores) < 2:
        return {
            "has_data": False,
            "message": "Not enough data yet — anchors will appear after your first reviews.",
        }

    my_vals = list(my_scores.values())
    judge_mean = statistics.mean(my_vals)
    judge_std = statistics.stdev(my_vals)
    n_reviews = len(my_vals)

    all_scores = [s for j, p_scores in scores.items() for s in p_scores.values()]
    pool_mean = statistics.mean(all_scores)
    pool_std = statistics.stdev(all_scores) if len(all_scores) > 1 else 0.0

    diff = judge_mean - pool_mean
    if diff < -0.05:
        comp_text = f"You score ~{abs(diff):.1f} harsher than average."
    elif diff > 0.05:
        comp_text = f"You score ~{abs(diff):.1f} more lenient than average."
    else:
        comp_text = "You score ~aligned with average."

    scale_text = f"Your scale: mean {judge_mean:.1f} ± {judge_std:.1f} across {n_reviews} reviews — pool: {pool_mean:.1f} ± {pool_std:.1f}. {comp_text}"

    # Anchor cards: min 3 reviews across pool
    proj_totals = {}
    for j, p_map in scores.items():
        for p, total in p_map.items():
            proj_totals.setdefault(p, []).append(total)

    eligible = {}
    for p, vals in proj_totals.items():
        if len(vals) >= 3:
            eligible[p] = statistics.mean(vals)

    anchors = None
    if eligible:
        projects_dict = {p.id: p.title for p in db.query(Project).all()}
        sorted_by_mean = sorted(eligible.items(), key=lambda x: (x[1], x[0]), reverse=True)
        high_id, high_mean = sorted_by_mean[0]
        low_id, low_mean = sorted_by_mean[-1]
        anchors = {
            "highest": {
                "id": high_id,
                "title": projects_dict.get(high_id, high_id),
                "mean": high_mean,
            },
            "lowest": {
                "id": low_id,
                "title": projects_dict.get(low_id, low_id),
                "mean": low_mean,
            },
        }

    return {
        "has_data": True,
        "scale_text": scale_text,
        "anchors": anchors,
    }


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

    calibration = compute_judge_calibration(db, me, rubric)

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
            "calibration": calibration,
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

    # Scores freeze once results are published (immutable snapshot).
    if _results_published(db):
        raise HTTPException(status_code=403, detail="Results are published — scores are frozen")

    comment = body.get("comment") or None
    now = datetime.now(timezone.utc)

    scored_any = False
    for crit in ["functionality", "quality", "innovation"]:
        val = body.get(crit)
        if val is None or (isinstance(val, str) and val.strip() == ""):
            continue  # not provided (e.g. untouched placeholder option)
        if isinstance(val, bool) or (isinstance(val, float) and not val.is_integer()):
            raise HTTPException(status_code=422, detail=f"{crit} must be an integer 2-5")
        try:
            val_int = int(val)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail=f"{crit} must be an integer 2-5")
        if not 2 <= val_int <= 5:
            raise HTTPException(status_code=422, detail=f"{crit} must be between 2 and 5")
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
        scored_any = True

    if not scored_any:
        raise HTTPException(status_code=422, detail="At least one criterion score (2-5) is required")

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
        .options(
            joinedload(Project.track),
            joinedload(Project.scores),
            joinedload(Project.assignments),
            joinedload(Project.review_events),
        )
        .all()
    )
    project_reviews = []
    for p in projects:
        unique_judges = set(s.judge_fixture_id for s in p.scores)
        assigned_count = len(p.assignments)
        reviewed_count = len(unique_judges)

        # Per-project receipt line (which artefacts each judge opened, when)
        judge_events = {}
        for ev in sorted(p.review_events, key=lambda x: x.opened_at):
            t_str = ev.opened_at.strftime("%H:%M:%S UTC")
            judge_events.setdefault(ev.judge_fixture_id, []).append(f"{ev.artefact} ({t_str})")

        receipt_parts = [f"{j}: {', '.join(items)}" for j, items in judge_events.items()]
        receipt_line = " | ".join(receipt_parts) if receipt_parts else ""

        project_reviews.append({
            "id": p.id,
            "title": p.title,
            "track": p.track,
            "track_id": p.track_id,
            "reviewed_count": reviewed_count,
            "assigned_count": assigned_count,
            "reviews_count": reviewed_count,
            "receipt_line": receipt_line,
        })

    audit_events = (
        db.query(AuditEvent)
        .order_by(AuditEvent.id.desc())
        .limit(200)
        .all()
    )

    # Kingmaker check: whose single removal alone moves the prize places?
    event = db.query(Event).first()
    rubric = (event.rubric if event and event.rubric else None) or {
        "functionality": 1.0 / 3.0,
        "quality": 1.0 / 3.0,
        "innovation": 1.0 / 3.0,
    }
    score_rows = db.query(Score).all()
    crit_scores: dict[str, dict[str, dict[str, int]]] = {}
    for s in score_rows:
        crit_scores.setdefault(s.judge_fixture_id, {}).setdefault(s.project_id, {})[
            s.criterion
        ] = s.value
    weighted_scores: dict[str, dict[str, float]] = {}
    for j_id, p_map in crit_scores.items():
        for p_id, crits in p_map.items():
            weighted_scores.setdefault(j_id, {})[p_id] = sum(
                crits.get(c, 0) * rubric.get(c, 1.0 / 3.0) for c in crits
            )
    title_by_id = {p.id: p.title for p in projects}
    kingmaker_findings = []
    for km in kingmakers(weighted_scores):
        kingmaker_findings.append(
            {
                **km,
                "entered_titles": [title_by_id.get(i, i) for i in km["entered"]],
                "left_titles": [title_by_id.get(i, i) for i in km["left"]],
                "winner_before_title": title_by_id.get(km["winner_before"], km["winner_before"]),
                "winner_after_title": title_by_id.get(km["winner_after"], km["winner_after"]),
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="organize.html",
        context={
            "tracks": tracks,
            "judge_progress": judge_progress,
            "project_reviews": project_reviews,
            "audit_events": audit_events,
            "kingmaker_findings": kingmaker_findings,
            "kingmaker_places": 5,
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

    existing_assignments = set(
        (a.judge_fixture_id, a.project_id)
        for a in db.query(Assignment.judge_fixture_id, Assignment.project_id).filter(
            Assignment.project_id.in_([p.id for p in projects])
        ).all()
    )
    created = 0
    for j in track_judges:
        for p in projects:
            if (j.id, p.id) not in existing_assignments:
                db.add(
                    Assignment(
                        judge_fixture_id=j.id,
                        project_id=p.id,
                        status="pending",
                    )
                )
                created += 1
    if created:
        _log_audit(db, user, "batch_assign", f"track {track_id}: {created} assignments created")
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

    if _results_published(db):
        raise HTTPException(status_code=409, detail="Results already published — rubric is frozen")

    def _parse_weight(name: str, raw: Any) -> float:
        try:
            w = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail=f"{name} must be a number between 0 and 1")
        if not 0.0 <= w <= 1.0:
            raise HTTPException(status_code=422, detail=f"{name} must be a number between 0 and 1")
        return w

    f_val = _parse_weight("functionality", body.get("functionality", 1.0 / 3.0))
    q_val = _parse_weight("quality", body.get("quality", 1.0 / 3.0))
    i_val = _parse_weight("innovation", body.get("innovation", 1.0 / 3.0))
    if abs((f_val + q_val + i_val) - 1.0) > 1e-6:
        raise HTTPException(status_code=422, detail="Rubric weights must sum to 1")

    event = db.query(Event).first()
    if event:
        event.rubric = {
            "functionality": f_val,
            "quality": q_val,
            "innovation": i_val,
        }
        _log_audit(db, user, "rubric_update",
                   f"weights set to functionality={f_val} quality={q_val} innovation={i_val}")
        db.commit()

    if "application/json" in request.headers.get("accept", "") or "application/json" in content_type:
        return JSONResponse(status_code=200, content={"rubric": event.rubric if event else {}})
    return RedirectResponse(url="/organize", status_code=303)


@app.post("/organize/publish")
async def publish_results(
    request: Request,
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    event = db.query(Event).first()
    # Immutable: if a Result row exists for a project, never overwrite it (skip or 409)
    existing_count = db.query(Result).count()
    if existing_count > 0:
        if "application/json" in request.headers.get("accept", "") or "application/json" in request.headers.get("content-type", ""):
            return JSONResponse(status_code=409, content={"detail": "Results already published"})
        return RedirectResponse(url="/results", status_code=303)

    rubric = (event.rubric if event and event.rubric else None) or {
        "functionality": 1.0 / 3.0,
        "quality": 1.0 / 3.0,
        "innovation": 1.0 / 3.0,
    }

    scores_rows = db.query(Score).all()
    projects_rows = db.query(Project).all()

    judge_proj_crit = {}
    for s in scores_rows:
        judge_proj_crit.setdefault(s.judge_fixture_id, {}).setdefault(
            s.project_id, {}
        )[s.criterion] = s.value

    scores = {}
    for j_id, p_map in judge_proj_crit.items():
        for p_id, crits in p_map.items():
            w_sum = sum(crits.get(c, 0) * rubric.get(c, 1.0 / 3.0) for c in crits)
            scores.setdefault(j_id, {})[p_id] = w_sum

    proj_raw = {}
    for j_id, p_scores in scores.items():
        for p_id, s in p_scores.items():
            proj_raw.setdefault(p_id, []).append(s)

    raw_scores = {
        p.id: (statistics.mean(proj_raw[p.id]) if p.id in proj_raw else 0.0)
        for p in projects_rows
    }

    norm_scores = normalise(scores)

    # Rank projects: higher normalised_score is better (1 is top)
    ranked_projects = sorted(
        projects_rows,
        key=lambda p: (norm_scores.get(p.id, 0.0), raw_scores.get(p.id, 0.0), p.id),
        reverse=True,
    )

    now = datetime.now(timezone.utc)
    for rank_idx, p in enumerate(ranked_projects, start=1):
        # Double check no existing result
        existing = db.query(Result).filter(Result.project_id == p.id).first()
        if not existing:
            db.add(
                Result(
                    project_id=p.id,
                    raw_score=raw_scores.get(p.id, 0.0),
                    normalised_score=norm_scores.get(p.id, 0.0),
                    rank=rank_idx,
                    snapshot_at=now,
                )
            )

    if event:
        event.results_public = True
    _log_audit(db, user, "publish_results", f"{len(ranked_projects)} projects ranked, snapshot immutable")
    db.commit()

    # Persist the signed, recomputable bundle once, at publish time.
    if db.query(ResultBundle).count() == 0:
        published_rows = db.query(Result).order_by(Result.rank.asc()).all()
        digest = _result_snapshot_digest(published_rows)
        canonical = [
            {
                "project_id": r.project_id,
                "rank": r.rank,
                "raw_score": round(float(r.raw_score), 6),
                "normalised_score": round(float(r.normalised_score), 6),
                "snapshot_at": r.snapshot_at.isoformat() if r.snapshot_at else None,
            }
            for r in published_rows
        ]
        db.add(
            ResultBundle(
                bundle_json=json.dumps(canonical, sort_keys=True, separators=(",", ":")),
                digest=digest or "",
                created_at=now,
            )
        )
        db.commit()

    if "application/json" in request.headers.get("accept", "") or "application/json" in request.headers.get("content-type", ""):
        return JSONResponse(status_code=200, content={"status": "published", "count": len(ranked_projects)})
    return RedirectResponse(url="/results", status_code=303)


def _result_snapshot_digest(results: list) -> str | None:
    """Canonical, recomputable SHA-256 of the published ranking.

    Anyone can rebuild the same bytes from the exported CSV and check the digest;
    it turns 'immutable snapshot' from a claim into something verifiable.
    """
    if not results:
        return None
    canonical = [
        {
            "project_id": r.project_id,
            "rank": r.rank,
            "raw_score": round(float(r.raw_score), 6),
            "normalised_score": round(float(r.normalised_score), 6),
            "snapshot_at": r.snapshot_at.isoformat() if r.snapshot_at else None,
        }
        for r in sorted(results, key=lambda x: (x.rank, x.project_id))
    ]
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@app.get("/results", response_class=HTMLResponse)
async def results_page(request: Request, db: Session = Depends(get_db)):
    event = db.query(Event).first()
    results = (
        db.query(Result)
        .options(
            joinedload(Result.project).joinedload(Project.track),
            joinedload(Result.project).joinedload(Project.scores),
        )
        .order_by(Result.rank.asc())
        .all()
    )

    is_published = len(results) > 0 and (event.results_public if event else True)

    results_data = []
    if is_published:
        for r in results:
            distinct_judges = (
                set(s.judge_fixture_id for s in r.project.scores) if r.project else set()
            )
            results_data.append({
                "rank": r.rank,
                "project": r.project,
                "normalised_score": r.normalised_score,
                "raw_score": r.raw_score,
                "reviews_count": len(distinct_judges),
                "snapshot_at": r.snapshot_at,
                "top_prob": None,
            })

        # Chance-of-top-N: resample judges, refit, count prize-place finishes.
        rubric = (event.rubric if event and event.rubric else None) or {
            "functionality": 1.0 / 3.0,
            "quality": 1.0 / 3.0,
            "innovation": 1.0 / 3.0,
        }
        crit_scores: dict[str, dict[str, dict[str, int]]] = {}
        for s in db.query(Score).all():
            crit_scores.setdefault(s.judge_fixture_id, {}).setdefault(s.project_id, {})[
                s.criterion
            ] = s.value
        weighted_scores: dict[str, dict[str, float]] = {}
        for j_id, p_map in crit_scores.items():
            for p_id, crits in p_map.items():
                weighted_scores.setdefault(j_id, {})[p_id] = sum(
                    crits.get(c, 0) * rubric.get(c, 1.0 / 3.0) for c in crits
                )
        top_probs = bootstrap_top_probabilities(weighted_scores, places=3)
        for row in results_data:
            row["top_prob"] = top_probs.get(row["project"].id)

    return templates.TemplateResponse(
        request=request,
        name="results.html",
        context={
            "published": is_published,
            "results": results_data,
            "top_places": 3,
            "snapshot_digest": _result_snapshot_digest(results) if is_published else None,
            "user": request.state.user,
        },
    )


@app.get("/results/verify")
def verify_results(db: Session = Depends(get_db)):
    """Public, recomputable proof of the published snapshot."""
    event = db.query(Event).first()
    results = db.query(Result).order_by(Result.rank.asc()).all()
    if not results:
        return JSONResponse(status_code=404, content={"detail": "Results not published"})
    digest = _result_snapshot_digest(results)
    stored = db.query(ResultBundle).order_by(ResultBundle.id.desc()).first()
    return JSONResponse(
        status_code=200,
        content={
            "algorithm": "sha256",
            "canonical": "json.dumps(rows, sort_keys=True, separators=(',', ':'))",
            "fields": [
                "project_id",
                "rank",
                "raw_score",
                "normalised_score",
                "snapshot_at",
            ],
            "rows": len(results),
            "snapshot_at": results[0].snapshot_at.isoformat() if results[0].snapshot_at else None,
            "event": event.name if event else None,
            "digest": digest,
            "stored_bundle_digest": stored.digest if stored else None,
            "verified": bool(stored and stored.digest == digest),
        },
    )


def _csv_safe(value: str) -> str:
    """
    Neutralise CSV formula injection (OWASP: 'Injection in Exported CSVs').
    Spreadsheet applications interpret cells starting with =, +, -, or @ as
    formulas. Prefixing with a tab makes the value text-only while keeping it
    human-readable. Applied to every string cell in the export.
    """
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@"):
        return "\t" + s
    return s


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
        "normalised_score",
        "rank",
    ])

    event = db.query(Event).first()
    rubric = (event.rubric if event and event.rubric else None) or {
        "functionality": 1.0 / 3.0,
        "quality": 1.0 / 3.0,
        "innovation": 1.0 / 3.0,
    }

    results_map = {r.project_id: r for r in db.query(Result).all()}
    is_published = len(results_map) > 0 and (event.results_public if event else True)

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

        norm_score_str = ""
        rank_str = ""
        if is_published and p.id in results_map:
            res = results_map[p.id]
            norm_score_str = f"{res.normalised_score:.2f}"
            rank_str = str(res.rank)

        writer.writerow([
            _csv_safe(p.id),
            _csv_safe(p.title),
            _csv_safe(p.track.name if p.track else p.track_id),
            reviews_count,
            f"{avg_func:.2f}",
            f"{avg_qual:.2f}",
            f"{avg_innov:.2f}",
            f"{avg_total:.2f}",
            norm_score_str,
            rank_str,
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
    response.set_cookie("session", token, path="/", httponly=True, samesite="lax")
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/projects", status_code=303)
    response.delete_cookie("session", path="/")
    return response

@app.get("/api/export/submissions.csv")
def export_submissions_csv(
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "project_id",
        "title",
        "track",
        "team_id",
        "repo_url",
        "status",
        "submitted_at"
    ])
    
    projects = (
        db.query(Project)
        .options(joinedload(Project.track))
        .all()
    )
    for p in projects:
        writer.writerow([
            _csv_safe(p.id),
            _csv_safe(p.title),
            _csv_safe(p.track.name if p.track else p.track_id),
            _csv_safe(p.team_id),
            _csv_safe(p.repo_url or ""),
            _csv_safe(p.status),
            p.submitted_at.isoformat()
        ])
        
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="submissions_export.csv"'},
    )

@app.get("/api/export/scores.csv")
def export_scores_csv(
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "project_id",
        "judge_fixture_id",
        "criterion",
        "value",
        "comment",
        "submitted_at"
    ])
    
    scores = db.query(Score).all()
    for s in scores:
        writer.writerow([
            _csv_safe(s.project_id),
            _csv_safe(s.judge_fixture_id),
            _csv_safe(s.criterion),
            s.value,
            _csv_safe(s.comment or ""),
            s.submitted_at.isoformat()
        ])
        
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="raw_scores_export.csv"'},
    )

@app.post("/organize/users")
async def create_user(
    request: Request,
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    form = await request.form()
    body = dict(form)
            
    email = str(body.get("email", "")).strip()
    role = str(body.get("role", "")).strip()
    
    if not email or role not in ["judge", "participant", "organizer"]:
        return RedirectResponse(url="/organize", status_code=303)
        
    token = secrets.token_hex(8)
    new_user = User(
        email=email,
        role=role,
        session_token=token
    )
    db.add(new_user)
    db.commit()
    magic_link = f"http://localhost:8080/login/{token}"
    return HTMLResponse(
        content=f"<!DOCTYPE html><html><body><h2>User Created</h2><p>Email: {email}</p><p>Role: {role}</p><p>Magic Link: <a href='{magic_link}'>{magic_link}</a></p><br><a href='/organize'>Back to Organizer Dashboard</a></body></html>",
        status_code=201
    )

@app.post("/organize/event")
async def update_event(
    request: Request,
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    form = await request.form()
    body = dict(form)
    
    name = str(body.get("name", "")).strip()
    if name:
        event = db.query(Event).first()
        if event:
            event.name = name
            db.commit()
            
    return RedirectResponse(url="/organize", status_code=303)

@app.get("/projects/{project_id}/edit", response_class=HTMLResponse)
async def edit_project_form(
    project_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_participant),
):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Not found")
        
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
        name="project_edit.html",
        context={"closed": is_closed, "tracks": tracks, "project": project, "user": request.state.user},
    )

@app.post("/projects/{project_id}/edit")
async def edit_project(
    project_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_participant),
):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Not found")
        
    event = db.query(Event).first()
    now = datetime.now(timezone.utc)
    if event:
        close_dt = event.submissions_close
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=timezone.utc)
        if now > close_dt:
            return JSONResponse(status_code=422, content={"detail": "Submissions are closed"})

    form = await request.form()
    body = dict(form)
    
    title = str(body.get("title", "")).strip()
    if title:
        project.title = title
    project.summary = body.get("summary", "")
    project.repo_url = body.get("repo_url") or None
    if body.get("track_id"):
        project.track_id = body.get("track_id")
        
    db.commit()
    return RedirectResponse(url=f"/projects/{project.id}", status_code=303)


# --- T3 Community Voting (quadratic) ---


def _voting_closed(db: Session) -> bool:
    """Voting closes once the organiser publishes the immutable results snapshot."""
    event = db.query(Event).first()
    return bool(event and event.results_public)


@app.get("/vote", response_class=HTMLResponse)
async def vote_page(request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    is_closed = _voting_closed(db)
    projects = (
        db.query(Project)
        .options(joinedload(Project.track))
        .order_by(Project.title.asc())
        .all()
    )
    votes = db.query(Vote).all()

    my_votes: dict[str, int] = {}
    used_credits = 0
    if user:
        for v in votes:
            if v.voter_email == user.email:
                used_credits += v.credits
                my_votes[v.project_id] = int(round(v.credits ** 0.5))

    public_totals: dict[str, int] = {}
    if is_closed:
        for v in votes:
            public_totals[v.project_id] = public_totals.get(v.project_id, 0) + v.credits

    return templates.TemplateResponse(
        request=request,
        name="vote.html",
        context={
            "projects": projects,
            "user": user,
            "budget": VOTE_BUDGET,
            "max_votes_per_project": MAX_VOTES_PER_PROJECT,
            "used_credits": used_credits,
            "remaining": max(VOTE_BUDGET - used_credits, 0),
            "my_votes": my_votes,
            "is_closed": is_closed,
            "public_totals": public_totals,
        },
    )


@app.post("/vote")
async def submit_vote(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    if _voting_closed(db):
        return JSONResponse(status_code=422, content={"detail": "Voting is closed"})

    form = await request.form()
    project_id = str(form.get("project_id", "")).strip()
    try:
        votes_cast = int(str(form.get("votes", "0")))
    except (TypeError, ValueError):
        votes_cast = 0
    votes_cast = max(0, min(votes_cast, MAX_VOTES_PER_PROJECT))

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    existing = (
        db.query(Vote)
        .filter(Vote.voter_email == user.email, Vote.project_id == project_id)
        .first()
    )
    other_credits = sum(
        v.credits
        for v in db.query(Vote).filter(Vote.voter_email == user.email).all()
        if v.project_id != project_id
    )
    new_cost = votes_cast * votes_cast
    if other_credits + new_cost > VOTE_BUDGET:
        return JSONResponse(
            status_code=422,
            content={"detail": "Not enough voting credits for that allocation"},
        )

    now = datetime.now(timezone.utc)
    if votes_cast == 0:
        if existing:
            db.delete(existing)
    elif existing:
        existing.credits = new_cost
        existing.cast_at = now
    else:
        db.add(
            Vote(
                voter_email=user.email,
                project_id=project_id,
                credits=new_cost,
                cast_at=now,
            )
        )
    db.commit()
    return RedirectResponse(url="/vote", status_code=303)
