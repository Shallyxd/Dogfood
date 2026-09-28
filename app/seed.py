import json
import os
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy.orm import Session

from app.models import (
    Event,
    Track,
    Team,
    TeamMember,
    Project,
    Judge,
    JudgeProfile,
    Assignment,
    Score,
    User,
)


def find_fixtures_path() -> Path:
    candidates = [
        Path("seed/fixtures.json"),
        Path("/app/seed/fixtures.json"),
        Path("dossier/Dogfood/fixtures.json"),
        Path("/Users/nomrelol/Desktop/dogfood/seed/fixtures.json"),
    ]
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError("Could not locate fixtures.json")


def parse_datetime(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def run_seed(db: Session):
    fixtures_file = find_fixtures_path()
    with open(fixtures_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Resolve judge fixture IDs deterministically
    judges_with_scores = set(s["judge"] for s in data.get("scores", []))
    sorted_judges = sorted(list(judges_with_scores))
    judge_a_fixture_id = sorted_judges[0] if len(sorted_judges) > 0 else "jdg_01"
    judge_b_fixture_id = sorted_judges[1] if len(sorted_judges) > 1 else "jdg_02"

    teams_data = data.get("teams", [])
    first_team_id = teams_data[0]["id"] if teams_data else "tm_01"

    event_row = db.query(Event).first()
    if not event_row:
        # Seed Event
        evt_info = data["event"]
        event = Event(
            id=evt_info["id"],
            name=evt_info["name"],
            submissions_close=parse_datetime(evt_info["submissions_close"]),
            judging_open=None,
            results_public=False,
        )
        db.add(event)

        # Seed Tracks
        for trk in data.get("tracks", []):
            db.add(Track(id=trk["id"], name=trk["name"], event_id=event.id))

        # Seed Judges
        for jdg in data.get("judges", []):
            db.add(
                Judge(
                    id=jdg["id"],
                    name=jdg["name"],
                    email=jdg["email"],
                    tracks=jdg.get("tracks", []),
                )
            )

        # Seed Teams & TeamMembers
        for tm in teams_data:
            db.add(
                Team(
                    id=tm["id"],
                    name=tm["name"],
                    event_id=event.id,
                    invite_code=f"inv_{tm['id']}",
                )
            )
            for member_email in tm.get("members", []):
                db.add(TeamMember(team_id=tm["id"], user_email=member_email))

        # Seed Projects (all 41, preserving multiple projects per team)
        for prj in data.get("projects", []):
            db.add(
                Project(
                    id=prj["id"],
                    team_id=prj["team"],
                    track_id=prj["track"],
                    title=prj["title"],
                    summary=prj.get("summary", ""),
                    repo_url=prj.get("repo_url"),
                    submitted_at=parse_datetime(prj["submitted_at"]),
                    status="submitted",
                )
            )

        # Seed Scores & Assignments
        default_score_dt = datetime(2026, 3, 1, 18, 0, 0, tzinfo=timezone.utc)
        seen_assignments = set()
        for sc in data.get("scores", []):
            j_id = sc["judge"]
            p_id = sc["project"]
            comment = sc.get("comment")

            if (j_id, p_id) not in seen_assignments:
                db.add(Assignment(judge_fixture_id=j_id, project_id=p_id, status="done"))
                seen_assignments.add((j_id, p_id))

            for criterion, val in sc.get("criteria", {}).items():
                db.add(
                    Score(
                        judge_fixture_id=j_id,
                        project_id=p_id,
                        criterion=criterion,
                        value=int(val),
                        comment=comment,
                        submitted_at=default_score_dt,
                    )
                )

        db.flush()

    # Create the 4 deterministic seed users if missing
    # 1. Organizer
    if not db.query(User).filter(User.email == "org@example.org").first():
        db.add(User(email="org@example.org", role="organizer", session_token="org_7f2a"))

    # 2. Judge A
    user_a = db.query(User).filter(User.email == "judge_a@example.org").first()
    if not user_a:
        user_a = User(email="judge_a@example.org", role="judge", session_token="jdg_a_91bc")
        db.add(user_a)
        db.flush()
        db.add(JudgeProfile(user_id=user_a.id, fixture_judge_id=judge_a_fixture_id))
    else:
        profile_a = db.query(JudgeProfile).filter(JudgeProfile.user_id == user_a.id).first()
        if not profile_a:
            db.add(JudgeProfile(user_id=user_a.id, fixture_judge_id=judge_a_fixture_id))

    # 3. Judge B
    user_b = db.query(User).filter(User.email == "judge_b@example.org").first()
    if not user_b:
        user_b = User(email="judge_b@example.org", role="judge", session_token="jdg_b_44de")
        db.add(user_b)
        db.flush()
        db.add(JudgeProfile(user_id=user_b.id, fixture_judge_id=judge_b_fixture_id))
    else:
        profile_b = db.query(JudgeProfile).filter(JudgeProfile.user_id == user_b.id).first()
        if not profile_b:
            db.add(JudgeProfile(user_id=user_b.id, fixture_judge_id=judge_b_fixture_id))

    # 4. Participant
    user_p = db.query(User).filter(User.email == "part@example.org").first()
    if not user_p:
        user_p = User(email="part@example.org", role="participant", session_token="prt_2e88")
        db.add(user_p)
        db.flush()
        if first_team_id:
            existing_tm = (
                db.query(TeamMember)
                .filter(
                    TeamMember.team_id == first_team_id,
                    TeamMember.user_email == "part@example.org",
                )
                .first()
            )
            if not existing_tm:
                db.add(TeamMember(team_id=first_team_id, user_email="part@example.org"))

    db.commit()


def print_seed_header():
    print(
        "seeded. test logins:\n"
        "  organizer    Cookie: session=org_7f2a\n"
        "  judge_a      Cookie: session=jdg_a_91bc\n"
        "  judge_b      Cookie: session=jdg_b_44de\n"
        "  participant  Cookie: session=prt_2e88",
        flush=True,
    )
