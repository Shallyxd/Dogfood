# DOGFOOD 2026 — Submission & Judging Portal

Offline-first hackathon portal: public project gallery, deadline-enforced submissions, backend-enforced judge isolation, and documented z-score normalisation with immutable published results.

Built for the DOGFOOD 2026 hackathon (72-hour build). Machine acceptance: **7/7 PASS** (`python3 run.py .dogfood.toml`, see `acceptance-report.txt`).

## Quickstart

```bash
docker compose up --build
```

Open http://localhost:8080. The seed runs automatically and prints four deterministic test logins:

| Role        | Cookie                      |
|-------------|-----------------------------|
| organizer   | `session=org_7f2a`          |
| judge_a     | `session=jdg_a_91bc`        |
| judge_b     | `session=jdg_b_44de`        |
| participant | `session=prt_2e88`          |

No cloud, no API keys, no external services. SQLite database, idempotent seed.

## What it does

- **T1 — Submissions:** public gallery of 41 fixture projects; submission route refuses with 422 after the event deadline (`2026-03-01T18:00:00Z`); teams and invite-code joining.
- **T2 — Judging isolation:** judges see only their own scores via `/api/judge/scores` — requesting another judge's rows returns **403**, enforced in the route, not the UI. Organiser CSV export included.
- **Normalisation:** per-judge z-scores with flat-rater and single-sample guards, shrinkage toward the global mean, one-click immutable publish (`409` on re-publish), public `/results` only after publishing. See `JUDGING.md` for the maths, the measured numbers, and the limits.
- **Innovation:** judge calibration mirror (your scale vs the pool, reference anchor cards) and organiser review receipts (which judge opened which artefact, when).

## Honest limits (what it does not do yet)

- T3/T4 are not built: no community voting, comments, certificates, webhooks, bulk import, or embeddable gallery.
- No pairwise judging mode (bonus not attempted — see `JUDGING.md` for why it was deferred).
- Auth is demo-grade by design: deterministic tokens from `.dogfood.toml` (the acceptance checker requires them), token-in-URL login, logout clears the client cookie only. See `THREAT-MODEL.md`.
- No rate limiting, no CSRF tokens (SameSite=Lax cookies only), no conflict-of-interest guard on assignments.
- Single-node SQLite; judges cannot be invited through the UI (seed only); submissions have no draft/edit cycle.

## Repo layout

```
app/            FastAPI app (routes, auth, models, seed)
app/judging/    normalisation maths + report  (normalise.py, report.py)
templates/      Jinja2 templates (no JavaScript)
seed/           fixture seed data
run.py          official acceptance checker (organiser-supplied)
fixtures.json   official fixtures (organiser-supplied)
.dogfood.toml   portal descriptor for run.py
acceptance-report.txt   last checker run (7/7 PASS)
```

`dossier/` (gitignored) holds private research, prompts, and organiser materials.

## Verify

```bash
python3 run.py .dogfood.toml   # 10s timeout per request, always exits 0; PASS/FAIL text is authoritative
docker exec dogfood-app-1 python -m app.judging.report   # normalisation report with measured numbers
```

## Docs

- `ARCHITECTURE.md` — stack, request flow, auth, isolation enforcement point
- `DATA-MODEL.md` — the 12 tables and their relations
- `JUDGING.md` — normalisation method, guards, measured results, limits
- `LICENSE` — MIT
