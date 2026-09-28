# Data model

SQLite via SQLAlchemy. Twelve tables.

## Core

- **event** — `id`, `name`, `submissions_close`, `judging_open`, `results_public` (bool), `rubric` (JSON: criterion → weight). One row in fixtures.
- **track** — `id`, `name`, `event_id` → event. 8 rows.
- **user** — `id`, `email` (unique), `role` (`organizer` | `judge` | `participant` | `admin`), `session_token` (unique, nullable). Four seeded users.
- **team** — `id`, `name`, `event_id` → event, `invite_code` (unique). 40 rows.
- **team_member** — `id`, `team_id` → team, `user_email`.
- **project** — `id`, `team_id` → team, `track_id` → track, `title`, `summary`, `repo_url` (nullable), `submitted_at`, `status` (`draft` | `submitted`). 41 rows.

## Judging

- **judge** — `id` (fixture id, e.g. `jdg_01`), `name`, `email`, `tracks` (JSON list). 30 rows.
- **judge_profile** — `id`, `user_id` → user (unique), `fixture_judge_id`. Links login users to fixture judges.
- **assignment** — `id`, `judge_fixture_id`, `project_id` → project, `status` (`pending` | `done`); unique on (`judge_fixture_id`, `project_id`).
- **score** — `id`, `judge_fixture_id`, `project_id` → project, `criterion`, `value` (2–5), `comment` (nullable), `submitted_at`; unique on (`judge_fixture_id`, `project_id`, `criterion`). 126 rows in fixtures.
- **result** — `id`, `project_id` → project (unique), `raw_score`, `normalised_score`, `rank`, `snapshot_at`. Written once by publish; never updated (re-publish → 409).

## Review evidence

- **review_event** — `id`, `judge_fixture_id` (indexed), `project_id` → project (indexed), `artefact` (`repo` | `description`), `opened_at`. Append-only log of judges inspecting entries: a row when a judge opens a project detail page, and when they follow the `/go/{project_id}/repo` redirect.

## Relations of note

- `Project.scores`, `Project.assignments`, `Project.review_events`, `Project.result` (one-to-one).
- `User.judge_profile` is eagerly loaded by the auth middleware (`joinedload`) so role handlers can resolve the fixture id without extra queries.
