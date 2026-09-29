# DOGFOOD 2026 — Submission & Judging Portal

An **offline-first, single-command, self-hostable** hackathon portal: a public project gallery,
deadline-enforced submissions, backend-enforced judge isolation, documented score
normalisation, community voting, and a **verifiable** published ranking.

Built for the DOGFOOD 2026 hackathon (72-hour build). Machine acceptance: **7/7 PASS**
(`python3 run.py .dogfood.toml`, see [`acceptance-report.txt`](acceptance-report.txt)).

> No cloud. No API keys. No external services. One `docker compose up`, a seeded SQLite
> database, and four deterministic test logins.

---

## Table of contents

1. [What it is](#what-it-is)
2. [Quickstart](#quickstart)
3. [Demo logins](#demo-logins)
4. [A five-minute tour](#a-five-minute-tour)
5. [Features](#features)
6. [How judging works](#how-judging-works)
7. [Innovation features](#innovation-features)
8. [Architecture](#architecture)
9. [Data model](#data-model)
10. [Route reference](#route-reference)
11. [UI / UX](#ui--ux)
12. [Security & threat model](#security--threat-model)
13. [Configuration & fixtures](#configuration--fixtures)
14. [Verify it yourself](#verify-it-yourself)
15. [Repo layout](#repo-layout)
16. [Honest limits](#honest-limits)
17. [Docs index](#docs-index)
18. [License](#license)

---

## What it is

DOGFOOD 2026 is a complete event lifecycle in one container: participants submit projects,
judges score their assigned projects under enforced isolation, the organiser normalises the
scores and publishes an immutable snapshot, and the public can browse the gallery, vote, and
verify the results.

It is designed for the operator who will **fork it and run a real event on Monday**: boring
where it can be, honest about what it does and does not do, and documented to the last number.

The three things that make it stand out:

| | What | Why it matters |
|---|---|---|
| **1** | **Documented normalisation** — per-judge z-scores + shrinkage, with measured before/after numbers | No commercial platform publishes its method; we do, and we prove it on the fixtures |
| **2** | **Judge integrity instrumented** — calibration mirror, review receipts, kingmaker check | Answers the loudest real complaints: inconsistent scales, entries never reviewed, one judge deciding the winner |
| **3** | **Verifiable results** — canonical SHA-256 bundle anyone can recompute | "Immutable" stops being a claim and becomes something you can check |

---

## Quickstart

```bash
git clone https://github.com/Shallyxd/Dogfood.git
cd Dogfood
docker compose up --build
```

Open **http://localhost:8080**.

On first boot the container creates and seeds `portal.db` (SQLite) and prints the four test
logins to the logs. Nothing else is required — no `.env`, no database service, no network.

```text
seeded. test logins:
  organizer    Cookie: session=org_7f2a
  judge_a      Cookie: session=jdg_a_91bc
  judge_b      Cookie: session=jdg_b_44de
  participant  Cookie: session=prt_2e88
```

### Run without Docker (optional)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

The app seeds `./portal.db` on startup either way. Restart after code edits (uvicorn runs
without `--reload`).

---

## Demo logins

Authentication is **deterministic and demo-grade by design** — the acceptance checker requires
fixed tokens. Visit [`/login`](http://localhost:8080/login) and click a role, or hit
`/login/<token>` directly.

| Role | Token | Fixture judge | What they can do |
|---|---|---|---|
| Organizer | `org_7f2a` | — | Assign judges, tune the rubric, provision users, publish, export CSV |
| Judge A | `jdg_a_91bc` | `jdg_01` | Score only their own assigned projects |
| Judge B | `jdg_b_44de` | `jdg_02` | Score only their own assigned projects |
| Participant | `prt_2e88` | — | Create a team, submit/edit projects, vote |

> These tokens are **not secrets** and must never be treated as such. See
> [`THREAT-MODEL.md`](THREAT-MODEL.md).

---

## A five-minute tour

1. **Gallery (public).** Open `/projects` — 41 fixture projects across 8 tracks, searchable by
   title. No login needed.
2. **Deadline enforcement.** As a participant, open `/projects/new`. The event closed
   `2026-03-01T18:00:00Z`, so the server refuses with **HTTP 422** — the deadline is real, not
   cosmetic.
3. **Judge queue.** Log in as Judge B (`jdg_b_44de`) and open `/judge`. See the **calibration
   mirror** ("you score Nσ harsher/softer than the pool") and your assigned projects. Score
   one on functionality / quality / innovation (2–5) with an optional comment.
4. **Review receipts.** Open a project's repo link as a judge — the organiser console records
   *which judge opened which artefact, and when*.
5. **Organiser console.** Log in as organiser and open `/organize`. See scoring progress, the
   append-only audit trail, and the **kingmaker check**.
6. **Publish.** Click *Publish Results Snapshot*. One immutable `Result` row per project is
   written and `results_public` flips to true. Re-publishing is blocked with **HTTP 409**.
7. **Results.** Open `/results` — ranked table with raw average, normalised score,
   **chance of top 3**, and review counts. A **SHA-256 digest** is shown at the bottom.
8. **Verify.** Open `/results/verify` — the machine-readable bundle with the recomputed digest,
   the stored digest, and `"verified": true`.
9. **Vote.** Open `/vote` as a participant — quadratic voting on a fixed credit budget; tallies
   stay hidden until results are published.
10. **Acceptance.** Run `python3 run.py .dogfood.toml` — all checks green.

---

## Features

### T1 — Core submissions

- **Public gallery** of all submitted projects, searchable by title, with track and team.
- **Teams with invite links** — create a team, share `/join/{invite_code}`.
- **Deadline enforcement** server-side in UTC: `POST /projects/new` returns **422** after
  `submissions_close`. Edits are refused the same way.
- **Repository URL validation** — must be `http(s)://`, blocking `javascript:` and stored XSS.

### T2 — Judging integrity

- **Backend-enforced isolation.** `GET /api/judge/scores` filters by the caller's own fixture id;
  requesting another judge's rows returns **403** — enforced in the route, not the UI. `curl`
  gets the same 403.
- **Role separation.** Every protected route uses `require_organizer` / `require_judge` /
  `require_participant`.
- **Score validation.** Every criterion must be an integer 2–5, else **422**.
- **Post-publish freeze.** Score submission 403s once results exist; rubric updates 409.
- **CSV export at every stage** — results, raw scores, and submissions (organiser-only).
- **Audit trail** — publish, rubric changes, and batch assignments are appended to `audit_event`.

### T3 — Community vote

- **Quadratic voting.** Each participant gets a fixed budget (**25 credits**). Voting `v` times
  on one project costs `v²` credits (1 / 4 / 9), up to **3 votes per project**. Strong
  preferences cost more, so everyone gets a fair say without letting whales dominate.
- **Hidden until publish.** Tallies are secret during judging and become public once results are
  published.
- **Budget enforced server-side** — an over-budget allocation returns 422; anonymous voting 403.

### T4 — Stretch (partial)

- **Verifiable results** (see below) — canonical bundle + `/results/verify`.
- Not built: comments, certificates, webhooks, bulk import, embeddable gallery.

---

## How judging works

Full derivation, guards, measured numbers, and limits live in **[`JUDGING.md`](JUDGING.md)**.
The short version:

```text
1. Weighted total   raw = Σ criterion_score × rubric_weight      (default ⅓ each)
2. Per-judge z      z   = (raw − judge_mean) / judge_std          (removes harsh/lenient scale)
3. Aggregate        mean_z = mean of the project's reviewers' z
4. Shrinkage        shrunk = (n·mean_z + 1·global_mean) / (n + 1)  (thin evidence → global mean)
5. Rank             by shrunk score, descending
```

**Guards**

- **Flat-rater** (a judge with zero variance, e.g. all 4s): mean-centred to zeros — contributes
  no signal and no noise, instead of dividing by ~zero.
- **Single-sample judges** (< 2 reviews): excluded from the normalised ranking — one score cannot
  separate judge harshness from project quality. Their raw scores stay in the CSV.

**Measured on the fixture data**

- Judge harshness spread (std of per-judge means): **0.42 → 0.00** across 28 calibrated judges.
- **Natural experiment:** team `tm_07` submitted the same project twice (`prj_07`, `prj_41`,
  both "Dry Harbour"). Raw ranking put the copies at **#32 and #9**; after normalisation they
  converge to **#8 and #9** — two independent judge sets agreeing on where the project belongs.

Reproduce every number:

```bash
docker exec dogfood-app-1 python -m app.judging.report
```

---

## Innovation features

- **Pre-scoring calibration mirror** (judge dashboard) — your running mean ± std versus the pool
  ("you score 0.8σ harsher than average"), plus highest/lowest consensus anchor cards as
  reference points. Read-only; it changes no score.
- **Evidence-of-review receipts** (organiser console) — which judge opened which artefact
  (repo / description), and when. Answers "entries never actually reviewed" with a receipt.
- **Kingmaker check** (organiser console) — leave-one-out refit of the whole ranking. A judge is
  flagged when removing *them alone* changes the prize places or the winner. Informational only.
  → `app/judging/influence.py`
- **Chance of top N** (results) — deterministic bootstrap (200 resamples of the judges,
  seed `20260301`) of how often each project finishes in the top 3. High = stable winner;
  middling = the place depends on who reviewed it. → `app/judging/uncertainty.py`
- **Verifiable snapshot** (results) — the published ranking is serialised canonically and hashed
  with SHA-256. `/results/verify` returns the recomputed digest, the digest stored at publish
  time, and `"verified": true|false`. → `app/main.py`

---

## Architecture

### Stack

| Layer | Choice |
|---|---|
| HTTP | **FastAPI** + **uvicorn** |
| Persistence | **SQLAlchemy** + **SQLite** (`portal.db`, WAL mode) |
| Templates | **Jinja2**, server-rendered, **no JavaScript** |
| Container | Single **Docker** image via `docker-compose.yml` |

No Redis, no Celery, no OAuth, no email, no external services. Dependencies are intentionally
small and unpinned (`requirements.txt`).

### Request flow

```text
request
  │
  ├─ SessionAuthMiddleware        app/auth.py — reads the `session` cookie, loads User
  │                               (+ judge_profile) into request.state.user
  │
  ├─ route + role dependency      require_organizer / require_judge / require_participant
  │                               → 403 on mismatch
  │
  ├─ handler                      app/main.py — SQLAlchemy session via get_db
  │
  └─ Jinja2 template | JSON       HTML pages or API responses
```

### Auth

Cookie sessions with deterministic tokens. Judge users link to fixture judges via
`JudgeProfile.fixture_judge_id` (`judge_a → jdg_01`, `judge_b → jdg_02`, resolved by sorting
fixture judges that have scores).

### Isolation enforcement point

Judge isolation is enforced **inside the route handler** (`GET /api/judge/scores`): the `judge`
query parameter is compared to the caller's own fixture id; mismatches return **403**. There is
no UI-only hiding. CSV export is organiser-only.

### Publish flow

1. Organiser clicks *Publish* (`POST /organize/publish`).
2. The route computes normalised scores, writes one immutable `Result` row per project, sets
   `Event.results_public = True`, and stores a `ResultBundle` (canonical JSON + SHA-256).
3. Any further publish attempt returns **409** — results are a snapshot, never recomputed in
   place.
4. `/results` shows a "not published yet" notice until publishing; afterwards it is public.

---

## Data model

SQLite via SQLAlchemy — **15 tables**. Full field list in [`DATA-MODEL.md`](DATA-MODEL.md).

| Group | Tables |
|---|---|
| Core | `event`, `track`, `user`, `team`, `team_member`, `project` |
| Judging | `judge`, `judge_profile`, `assignment`, `score`, `result` |
| Review evidence | `review_event` |
| Community vote | `vote` |
| Audit & proof | `audit_event`, `result_bundle` |

Notable relations: `Project.scores`, `Project.assignments`, `Project.review_events`,
`Project.votes`, and one-to-one `Project.result`. `User.judge_profile` is eagerly loaded by the
auth middleware so role handlers resolve the fixture id without extra queries.

---

## Route reference

### Public

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Landing page with event summary and stats |
| GET | `/projects` | Public gallery (`?q=` title search) |
| GET | `/projects/{id}` | Project detail |
| GET | `/results` | Published ranking (notice until published) |
| GET | `/results/verify` | Machine-readable snapshot bundle + digest |
| GET | `/vote` | Community vote (public; voting requires login) |
| GET | `/login`, `/login/{token}` | Demo role picker / direct token login |
| GET | `/logout` | Clear the session cookie |

### Participant

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/projects/new` | Submit a project (422 after deadline) |
| GET/POST | `/projects/{id}/edit` | Edit own submission (422 after deadline) |
| GET/POST | `/teams/new`, `/teams` | Create a team |
| GET | `/join/{invite_code}` | Join a team by invite |
| POST | `/vote` | Cast/update a quadratic vote |

### Judge

| Method | Path | Purpose |
|---|---|---|
| GET | `/judge` | Calibration mirror + assigned projects |
| POST | `/judge/score` | Submit scores (2–5 per criterion) |
| GET | `/api/judge/scores` | Own scores only; peer ids → 403 |
| GET | `/go/{project_id}/repo` | Receipted redirect to a project repo |

### Organizer

| Method | Path | Purpose |
|---|---|---|
| GET | `/organize` | Console: progress, receipts, kingmaker check, audit |
| POST | `/organize/assign` | Batch-assign judges in a track |
| GET/POST | `/organize/rubric` | Rubric weights (must sum to 1) |
| POST | `/organize/publish` | Publish immutable snapshot (409 if done) |
| POST | `/organize/users` | Provision a user + login link |
| POST | `/organize/event` | Rename the event |
| GET | `/api/export.csv` | Results CSV (raw + normalised + rank) |
| GET | `/api/export/scores.csv` | Raw per-criterion scores CSV |
| GET | `/api/export/submissions.csv` | Submissions CSV |

---

## UI / UX

- **Self-contained design system** in `templates/base.html` — CSS custom properties, a sticky
  blurred header with active-nav pills, cards, badges, alerts, forms, tables, and utility
  classes. **No CDN, no web fonts, no JavaScript** — it renders identically offline.
- **Role-aware navigation** — links appear only for the current role, with the active page
  highlighted.
- **Honest empty states** everywhere (no assignments, nothing to vote on, results not yet
  published) instead of blank tables.
- **Progressive disclosure** — the maths is summarised in a "method card" on `/results`, with
  the full derivation one click away in `JUDGING.md`.

---

## Security & threat model

A full accounting is in **[`THREAT-MODEL.md`](THREAT-MODEL.md)**. Summary:

**Stopped, with mechanism**

- Peer-score reads → 403 in the route.
- Role escalation → role dependencies on every protected route.
- Out-of-range scores, empty submissions, malformed rubric → 422.
- Post-publish score/rubric changes → 403 / 409.
- Stored XSS via repo URL → `http(s)` scheme validation + Jinja2 autoescaping.
- Client-clock deadline gaming → server-side UTC comparison.
- CSV formula injection → `_csv_safe` prefixes `= + - @` cells.
- Silent organiser actions → append-only `audit_event` trail.

**Known gaps (named, not hidden)**

- Deterministic token auth is demo-grade; `/login/{token}` puts the token in the URL.
- Logout clears the client cookie only.
- No rate limiting, no CSRF tokens (SameSite=Lax only), no CSP.
- `/go/{id}/repo` can redirect to any external `http(s)` domain.
- No conflict-of-interest guard on assignments; no collusion resistance.
- Single-node SQLite — the DB file is a single point of failure.

---

## Configuration & fixtures

| File | Role |
|---|---|
| `.dogfood.toml` | Portal descriptor consumed by `run.py`: base URL, claimed tiers, auth cookies, route map |
| `fixtures.json` | Official fixtures (organiser-supplied) — read by `run.py` |
| `seed/fixtures.json` | Same data, read by the app's idempotent seed (`app/seed.py`) |

The fixtures contain **1 event, 8 tracks, 30 judges, 40 teams, 41 projects, 126 score records**;
criteria are exactly `functionality` / `quality` / `innovation` with values 2–5; submissions
close `2026-03-01T18:00:00Z`.

---

## Verify it yourself

```bash
# Official acceptance checker (10s timeout per request; PASS/FAIL text is authoritative)
python3 run.py .dogfood.toml

# Normalisation report with measured before/after numbers
docker exec dogfood-app-1 python -m app.judging.report
```

Expected acceptance output:

```text
T1  gallery is public ................. PASS
T1  project from fixtures shown ....... PASS
T1  closed event refuses submissions .. PASS
T2  judge sees own scores ............. PASS
T2  judge cannot see peer scores ...... PASS
T2  participant blocked ............... PASS
T2  csv export works .................. PASS

claimed T1 T2, verified T1 T2
```

---

## Repo layout

```text
app/                    FastAPI app
  main.py                 routes, publish, exports, verification
  auth.py                 session middleware + role dependencies
  database.py             engine, WAL pragmas, session factory
  models.py               SQLAlchemy models (15 tables)
  seed.py                 idempotent fixture seed
  judging/
    normalise.py            per-judge z-score + shrinkage (pure functions)
    influence.py            kingmaker (leave-one-out) check
    uncertainty.py          bootstrap chance-of-top-N
    report.py               CLI normalisation report
templates/              Jinja2 templates + self-contained design system (no JS, no CDN)
seed/fixtures.json      seed data for the app
fixtures.json           official fixtures for run.py
run.py                  official acceptance checker (organiser-supplied)
.dogfood.toml           portal descriptor for run.py
acceptance-report.txt   last checker run (7/7 PASS)
ARCHITECTURE.md         stack, request flow, auth, isolation, publish
DATA-MODEL.md           the 15 tables and their relations
JUDGING.md              normalisation method, guards, measured numbers, limits
THREAT-MODEL.md         what is defended, how, and what is not
LICENSE                 MIT
```

`dossier/` (private research) and `ref/` (cloned reference repos) are gitignored and never
shipped. `portal.db*` are runtime SQLite artefacts, recreated on boot.

---

## Honest limits

- **T4 is partial:** no comments, certificates, webhooks, bulk import, or embeddable gallery.
- **No pairwise judging mode** (bonus not attempted — rationale in `JUDGING.md`).
- **Demo-grade auth** by design (deterministic tokens; the checker requires them).
- **No rate limiting, no CSRF tokens, no conflict-of-interest guard.**
- **Single-node SQLite**; judges cannot be invited through the UI (seed only); submissions have
  no draft/edit cycle.
- **Shrinkage weight is a chosen constant**, not fitted; per-judge SDs from 2-review judges are
  noisy. Both are documented rather than hidden.

---

## Docs index

| Document | Contents |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Stack, request flow, auth, isolation enforcement point, publish flow |
| [`DATA-MODEL.md`](DATA-MODEL.md) | The 15 tables and their relations |
| [`JUDGING.md`](JUDGING.md) | Normalisation method, guards, measured results, kingmaker, uncertainty, verification, limits |
| [`THREAT-MODEL.md`](THREAT-MODEL.md) | Assets, attackers, mitigations, known gaps |

---

## License

MIT — see [`LICENSE`](LICENSE).
