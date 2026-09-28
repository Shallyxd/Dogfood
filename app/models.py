from sqlalchemy import (
    Column,
    String,
    Integer,
    Boolean,
    DateTime,
    Float,
    Text,
    ForeignKey,
    UniqueConstraint,
    JSON,
)
from sqlalchemy.orm import relationship

from app.database import Base


class Event(Base):
    __tablename__ = "event"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    submissions_close = Column(DateTime, nullable=False)
    judging_open = Column(DateTime, nullable=True)
    results_public = Column(Boolean, default=False, nullable=False)
    rubric = Column(JSON, nullable=True)

    tracks = relationship("Track", back_populates="event")
    teams = relationship("Team", back_populates="event")


class Track(Base):
    __tablename__ = "track"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    event_id = Column(String, ForeignKey("event.id"), nullable=False)

    event = relationship("Event", back_populates="tracks")
    projects = relationship("Project", back_populates="track")


class User(Base):
    __tablename__ = "user"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False, index=True)
    role = Column(String, nullable=False)  # organizer | judge | participant | admin
    session_token = Column(String, unique=True, nullable=True, index=True)

    judge_profile = relationship("JudgeProfile", back_populates="user", uselist=False)


class Team(Base):
    __tablename__ = "team"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    event_id = Column(String, ForeignKey("event.id"), nullable=False)
    invite_code = Column(String, unique=True, nullable=False, index=True)

    event = relationship("Event", back_populates="teams")
    members = relationship("TeamMember", back_populates="team")
    projects = relationship("Project", back_populates="team")


class TeamMember(Base):
    __tablename__ = "team_member"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(String, ForeignKey("team.id"), nullable=False)
    user_email = Column(String, nullable=False)

    team = relationship("Team", back_populates="members")


class Project(Base):
    __tablename__ = "project"

    id = Column(String, primary_key=True)
    team_id = Column(String, ForeignKey("team.id"), nullable=False)
    track_id = Column(String, ForeignKey("track.id"), nullable=False)
    title = Column(String, nullable=False)
    summary = Column(Text, nullable=False)
    repo_url = Column(String, nullable=True)
    submitted_at = Column(DateTime, nullable=False)
    status = Column(String, default="submitted", nullable=False)  # draft | submitted

    team = relationship("Team", back_populates="projects")
    track = relationship("Track", back_populates="projects")
    scores = relationship("Score", back_populates="project")
    assignments = relationship("Assignment", back_populates="project")
    result = relationship("Result", back_populates="project", uselist=False)


class Judge(Base):
    __tablename__ = "judge"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    tracks = Column(JSON, default=list)


class JudgeProfile(Base):
    __tablename__ = "judge_profile"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("user.id"), unique=True, nullable=False)
    fixture_judge_id = Column(String, nullable=False)

    user = relationship("User", back_populates="judge_profile")


class Assignment(Base):
    __tablename__ = "assignment"

    id = Column(Integer, primary_key=True, autoincrement=True)
    judge_fixture_id = Column(String, nullable=False)
    project_id = Column(String, ForeignKey("project.id"), nullable=False)
    status = Column(String, default="pending", nullable=False)  # pending | done

    project = relationship("Project", back_populates="assignments")

    __table_args__ = (
        UniqueConstraint("judge_fixture_id", "project_id", name="uq_assignment_judge_project"),
    )


class Score(Base):
    __tablename__ = "score"

    id = Column(Integer, primary_key=True, autoincrement=True)
    judge_fixture_id = Column(String, nullable=False)
    project_id = Column(String, ForeignKey("project.id"), nullable=False)
    criterion = Column(String, nullable=False)
    value = Column(Integer, nullable=False)
    comment = Column(Text, nullable=True)
    submitted_at = Column(DateTime, nullable=False)

    project = relationship("Project", back_populates="scores")

    __table_args__ = (
        UniqueConstraint(
            "judge_fixture_id", "project_id", "criterion", name="uq_score_judge_project_criterion"
        ),
    )


class Result(Base):
    __tablename__ = "result"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(String, ForeignKey("project.id"), unique=True, nullable=False)
    raw_score = Column(Float, nullable=False)
    normalised_score = Column(Float, nullable=False)
    rank = Column(Integer, nullable=False)
    snapshot_at = Column(DateTime, nullable=False)

    project = relationship("Project", back_populates="result")
