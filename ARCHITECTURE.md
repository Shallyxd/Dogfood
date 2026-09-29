# Architecture

## Stack

- **FastAPI** + **uvicorn** — HTTP layer
- **SQLAlchemy** + **SQLite** — persistence (`portal.db`, created and seeded on startup)
- **Jinja2** — server-rendered templates, no JavaScript
- **Docker** — single container via `docker-compose.yml`; `Dockerfile` installs `requirements.txt` (`fastapi`, `uvicorn[standard]`, `sqlalchemy`, `jinja2`, `python-multipart`, all unpinned)

No Redis, no Celery, no OAuth, no email, no external services.

## Request flow

```
request
  -> SessionAuthMiddleware          (app/auth.py: reads `session` cookie, loads User + judge_profile, sets request.state.user)
  -> route + role dependency        (require_organizer / require_judge / require_participant — 403 otherwise)
  -> handler (app/main.py)          (SQLAlchemy session via get_db)
  -> Jinja2 template  |  JSON       (HTML pages or API responses)
```

Uvicorn runs **without** `--reload`: restart/rebuild the container after code edits.

## Auth

Cookie sessions with deterministic tokens, printed by the seed on startup:

- `org_7f2a` → organizer, `jdg_a_91bc` → judge_a, `jdg_b_44de` → judge_b, `prt_2e88` → participant
- Judge users link to fixture judges via `JudgeProfile.fixture_judge_id` (judge_a → `jdg_01`, judge_b → `jdg_02`; deterministic: sorted fixture judges with scores)

## Isolation enforcement

Judge score isolation is enforced **inside the route handler** (`GET /api/judge/scores` in `app/main.py`): the `judge` query parameter is compared against the caller's own fixture id and mismatches return 403. There is no UI-only hiding — `curl` gets the 403 too. CSV export (`/api/export.csv`) is organizer-only.

## Judging maths

`app/judging/normalise.py` (pure functions, no I/O), `app/judging/influence.py` (kingmaker
leave-one-out check), `app/judging/uncertainty.py` (bootstrap chance-of-top-N), and
`app/judging/report.py` (DB read + printable report). Full method, guards, measured numbers, and
limits: `JUDGING.md`.

## Community vote (T3)

`GET /vote` renders the quadratic ballot (budget 25 credits, ≤ 3 votes per project, cost = votes²);
`POST /vote` validates the budget server-side and upserts one `vote` row per
(`voter_email`, `project_id`). Tallies are hidden until `Event.results_public` is true, then shown
publicly.

## Publish flow

1. Organizer opens `/organize`, clicks Publish (or `POST /organize/publish`).
2. The route computes normalised scores, writes one immutable `Result` row per project, sets
   `Event.results_public = True`, and stores a canonical `ResultBundle` (JSON + SHA-256).
3. Any further publish attempt returns **409** — results are a snapshot, never recomputed in place.
4. `GET /results` shows a "not published yet" notice until publishing; afterwards it renders the
   ranking. `GET /results/verify` returns the recomputed vs stored digest for independent checking.

## Fixtures

`fixtures.json` (organiser-supplied): 1 event, 8 tracks, 30 judges, 40 teams, 41 projects, 126 score records. Criteria are exactly `functionality` / `quality` / `innovation`, values 2–5, submissions closed `2026-03-01T18:00:00Z`. The seed (`app/seed.py`) is idempotent.
