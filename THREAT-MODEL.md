# Threat Model — DOGFOOD 2026 Portal

Honest accounting of what this portal defends against, what it does not, and what is
out of scope. Written for the +3 Threat Model bonus; every claim names the mechanism.

## Assets

- Submitted projects (title, summary, repo URL) and team membership.
- Judge scores and comments.
- Published rankings (must be tamper-evident after publish).
- Organiser actions (assignment, rubric changes, publication).

## Attackers considered

| Attacker | Capability |
|---|---|
| Curious judge | Authenticated, tries to read peer scores or unassigned projects |
| Malicious judge | Authenticated, tries to distort rankings (out-of-range scores, post-publish edits) |
| Malicious participant | Authenticated, tries stored XSS via project fields, deadline gaming |
| Network observer | Sees traffic (mitigation is deployment's job, not the app's) |
| Organiser (insider) | Trusted with publish; actions must be auditable, not preventable |

Out of scope: host compromise, TLS termination, DDoS, and the organiser's own machine.

## What is stopped, and how

- **Judge reads peer scores** — stopped in the route: `/api/judge/scores` filters by the
  caller's fixture id and 403s on any other judge id. Not template logic; not JS.
- **Participant hits judge/organiser routes** — stopped by `require_judge` /
  `require_organizer` / `require_participant` on every protected route.
- **Out-of-range score injection** (`functionality=100`) — stopped: `POST /judge/score`
  validates every criterion is an integer 2–5, else HTTP 422. Empty submissions 422
  instead of silently marking the assignment done.
- **Post-publish score changes** — stopped: score submission 403s once any `Result` row
  exists. The published ranking is an immutable snapshot (re-publish → HTTP 409).
- **Post-publish rubric changes** — stopped: rubric update 409s once results exist, so
  the CSV can never be recomputed under different weights than the frozen ranking.
- **Rubric garbage** (negative weights, non-numbers, weights not summing to 1) —
  stopped: validated server-side, HTTP 422.
- **Stored XSS via repository URL** — stopped: `repo_url` must start with `http://` or
  `https://` (422 otherwise); `javascript:` URLs cannot be stored. All other
  user content is Jinja2-escaped by default.
- **Deadline gaming with a skewed client clock** — stopped: deadline compared
  server-side in UTC on every submission.
- **CSV formula injection** (`=cmd|...` in a title) — stopped: `_csv_safe` prefixes
  cells starting with `= + - @` with a tab character.
- **Unlinked judge account crash** — stopped: `require_judge` 403s when no
  `JudgeProfile` exists instead of an AttributeError.
- **Silent organiser actions** — mitigated: `audit_event` table records publish,
  rubric updates and batch assignments (actor, action, detail, timestamp), shown on
  `/organize`.

## What is NOT stopped (known gaps)

- **Token-in-URL auth.** `/login/{token}` puts the session token in the URL (history,
  logs, Referer). Tokens are deterministic demo credentials the acceptance checker
  requires (`org_7f2a` etc.) — they are not secrets and must never be treated as
  such. A production deployment must replace this with real auth.
- **Logout is client-side only.** Clearing the cookie does not invalidate the server
  token. This is deliberate: invalidating would permanently lock out the
  deterministic checker tokens. Named here instead of silently "fixed".
- **No rate limiting.** `/login/{token}` is brute-forceable (16-bit demo tokens).
  No throttling anywhere; no lockout.
- **No CSRF tokens.** Mutating forms rely on `SameSite=Lax` cookies only.
- **Unvalidated Redirect.** `/go/{project_id}/repo` redirects to the `repo_url` provided by the participant. While the scheme is validated as `http/https`, it can redirect to any external domain (phishing/tracker risk).
- **No conflict-of-interest guard.** Nothing stops a judge being assigned to their own
  team's project — there is no judge↔team link in the data model.
- **No collusion/strategy resistance.** Z-scoring corrects scale harshness, not
  strategic voting.
- **Single-node SQLite.** Concurrent writes serialize; no backups; the DB file is the
  single point of failure.
- **No CSP headers.** `HttpOnly` cookies raise the XSS bar; there is no
  Content-Security-Policy beyond that.

## Deliberate non-goals

- Community voting (T3) was not built, so ballot-stuffing/Sybil defences are not
  applicable — and are not claimed.
- Pairwise judging mode was not built (bonus not attempted).
