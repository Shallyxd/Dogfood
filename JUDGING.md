# Judging normalisation

How raw judge scores become the published ranking. The maths is small, documented, and reproducible: `app/judging/normalise.py` (pure functions) and `app/judging/report.py` (report). Run `docker exec dogfood-app-1 python -m app.judging.report` to reproduce every number below on the fixture data.

## Why this exists

No commercial hackathon platform publishes its normalisation method. Devfolio states scores are "automatically normalized" in one sentence with zero methodology; Devpost offers no normalisation at all. MLH's official organiser guide names the harsh/lenient judge problem as a key judging risk — yet proposes only manual stack-ranking as the mitigation.

The NIH explicitly percentile-ranks grant applications "to permit comparison across review panels that may have different scoring behaviours" ([NIH Grants Policy Statement, 2024](https://grants.nih.gov/grants/policy/nihgps/HTML5/section_14/14.4_peer_review_guidelines.htm)). Percentile-ranking is a form of per-committee z-scoring. We do the same thing, more explicitly.

One additional industry fact: HackerEarth's published hackathon rules state "JUDGES ARE NOT REQUIRED TO TEST THE APPLICATION AND MAY CHOOSE TO JUDGE BASED SOLELY ON THE TEXT DESCRIPTION, IMAGES AND VIDEO PROVIDED." Our evidence-of-review receipts answer this structurally.

---

## Method

1. **Weighted totals.** Each judge's per-criterion scores (2–5) for a project are combined into one total using the event rubric weights (default: ⅓ each for `functionality`, `quality`, `innovation`).
2. **Per-judge z-scores.** For each judge, subtract their own mean and divide by their own standard deviation across the projects they reviewed. A harsh judge's 3 and a lenient judge's 3 stop being the same number. This is equivalent to — and independently motivated by — the DECA ICDC method that reports scores "as if participants were all evaluated by the same judge, when in fact they were not."
3. **Aggregate.** Each project's normalised score is the mean of its reviewers' z-scores.
4. **Shrinkage.** Projects with few reviews are pulled toward the global mean:

   ```
   shrunk = (n * mean_z + 1 * global_mean) / (n + 1)
   ```

   where `n` is the project's review count and `global_mean` is the mean of all z-values. One pseudo-observation of weight 1 — a fixed, stated choice, not an estimated parameter.
5. **Rank** by shrunk score, descending. Ties share no special handling; they are ordered by project id.

---

## Guards

- **Flat-rater** (std < 0.01 across a judge's reviews, e.g. all 4s): z-scoring would divide by ~zero. Instead the judge's scores are mean-centered to all-zeros — they contribute no signal, and no noise. In fixtures this is `jdg_07`. **Basis:** ISO 11132:2012 (sensory science) defines a non-discriminating assessor as one who assigns uniform scores and prescribes their removal from the calibrated pool. We apply the same principle.
- **Single-sample judges** (< 2 reviewed projects): **excluded from the normalised ranking.** One score cannot separate judge harshness from project quality, so there is nothing to calibrate. Their raw scores remain visible in the CSV export and the gallery; they just don't enter the z-score pool. In fixtures this is `jdg_01` and `jdg_23`.

### Why exclusion, not "keep as-is"

An earlier version kept single-sample judges' raw totals in the pool. That mixed 2–5-scale raw values with z-scores (mean 0, std 1): a raw 2.00 — the worst possible score — read as +2σ, an excellent one. `jdg_01`'s all-2 review of `prj_07` (Dry Harbour) catapulted it from raw rank 32 to normalised rank 6 on a scale/sign artifact. The honest one-number metric caught it (see below), and the guard was changed to exclusion. Shrinkage does **not** fix this — it pulls toward a global mean already contaminated by the bad values.

---

## Measured results (fixture data, 28 Sep 2026)

- **Judge harshness spread** (std of per-judge means): **before = 0.42 → after = 0.00** across 28 calibrated judges (`jdg_01`, `jdg_23` excluded, labelled in the report). Z-scoring forces every calibrated judge's mean to 0 by construction; the metric confirms the implementation does what it claims.
- **Rank movements** are small and sane: mostly 0, ±1, ±4 (e.g. Iron Switch 1→1, Slow Trail 6→2, Still Beacon 3→7).
- **Natural experiment — the duplicate project.** Team `tm_07` submitted the same project twice: `prj_07` and `prj_41`, both titled "Dry Harbour". Raw rankings put the copies at **#32 and #9** — the raw average of `prj_07` was dragged down by the single uncalibrated 2.00 it drew. After normalisation (excluding that judge), the copies converge to **#8 and #9**: two independent judge sets, working separately, agree on where the project belongs. This is a natural experiment in the fixture data: the two copies act as replicated treatments. Convergence is the validation signal.

---

## Limits

- Z-scoring assumes a judge's scale is roughly an affine transform of a shared scale. It corrects harshness/generosity, not strategic or correlated voting.
- Excluding single-sample judges discards data. A project reviewed *only* by excluded judges gets no normalised score (defaults to 0.0 at publish); with 2–5 reviews per project and 2 excluded judges out of 30, this does not occur in fixtures, but it is a real edge.
- **The shrinkage weight (1 pseudo-observation) is a chosen constant, not estimated from data.** Stronger shrinkage would compress the ranking; weaker would let thinly-reviewed projects swing. A data-driven weight (method-of-moments on between-project vs within-project variance) would give λ fitted to this event's noise — we chose the simpler auditable form.
- Mean-centering a flat-rater assumes their uniform scores carry no information. If a flat-rater genuinely believes every project is a 4, that belief is discarded.
- **Per-judge SD estimates are noisy.** Piech et al. (2013, *Tuned Models of Peer Assessment in MOOCs*, 63,000 peer grades) found that estimating per-grader precision reliably required substantially more reviews than the 1–11 available here. Our z-scores from judges with 2 reviews rest on fragile SD estimates. This is the weakest link in the pipeline, and it is documented rather than hidden.

---

## Why this is not called "James–Stein"

It isn't the James–Stein estimator. James–Stein shrinks group means toward the grand mean with a **data-driven** shrinkage factor derived from estimated variances. Ours is a fixed-weight pseudo-observation pull — simpler, fully auditable, and honestly named as such. Borrowing the fancier name would be false precision.

For completeness: the BLUP (Best Linear Unbiased Predictor) formulation under an additive Gaussian judge-bias model is the minimum-MSE linear estimator in that model class. Our stdlib implementation is a method-of-moments approximation — not exact REML, not hierarchical EM — and is labelled as such.

---

## Method note

A 2025 peer-reviewed paper (*Entropy*, MDPI) describes a normalisation pipeline for educational peer review that is structurally similar to ours: per-rater z-scoring followed by shrinkage toward the global mean. Our method is not improvised — it sits in a documented line of published practice.

---

## Kingmaker detection

The organiser console (`/organize`) runs a leave-one-out sensitivity check for every judge:

1. Remove judge j's scores from the pool.
2. Re-run the full normalisation pipeline on the reduced data.
3. Compare the top-5 prize set before and after removal, and the winner.
4. If the set or the winner changes, judge j is flagged as a **kingmaker**.

This check is stricter than ranking stability alone: it finds cases where a single judge's leniency estimate (which rests on a small number of reviews) is the decisive factor in which projects hold prize positions. On the official fixtures, several judges qualify as kingmakers — expected given each judge reviews only 2–11 projects. The flag is informational: an organiser can request additional reviews for contested borderline projects before making the final call.

**Implementation:** `app/judging/influence.py::kingmakers`, called from the `/organize` route. Each project is reviewed only by the removed judge is dropped from both rankings before comparison (its disappearance is a coverage problem, not influence). Complexity is O(J × normalise) — fast enough to run on every organiser page load at fixture scale.

---

## Uncertainty: chance of top N

A single ranking hides how stable each place is. The results page (`/results`) reports a bootstrap probability per project: the fraction of resamples in which the project finishes in the top N (default N = 3).

1. Resample the **judges** with replacement (judges are the unit of variation).
2. Refit the z-score + shrinkage normaliser on the resampled pool.
3. Rank, and count how often each project lands in the prize places.

**Implementation:** `app/judging/uncertainty.py::bootstrap_top_probabilities`, 200 resamples, `places=3`. Seeded with `20260301`, so the same data always yields the same numbers — reproducible and checkable. A high percentage is a stable winner; a middling one means the place rests on which judges happened to review the project.

---

## Signed immutable result bundle

At publish time the platform stores a canonical record of the ranking in the `result_bundle` table:

```json
[
  { "project_id": "prj_34", "rank": 1, "raw_score": 4.25, "normalised_score": 1.42, "snapshot_at": "2026-09-29T..." },
  ...
]
```

The rows are serialised with `json.dumps(rows, sort_keys=True, separators=(',', ':'))` and hashed with SHA-256. The digest is shown on `/results` and served, alongside the recomputed value and a stored-vs-recomputed check, at:

```
GET /results/verify
```

```json
{
  "algorithm": "sha256",
  "rows": 41,
  "digest": "…",
  "stored_bundle_digest": "…",
  "verified": true
}
```

**What this gives:** anyone can retrieve the bundle, recompute the SHA-256, and confirm it matches the digest stored at publish time — proving the ranking has not been altered since. No private key is required. This turns "we normalised the scores" into "here is the record, verify it yourself."

**Limitation:** the digest confirms the published ranking has not changed. It does not prove the input scores are authentic — that requires trusting the server's audit trail (`AuditEvent` table, `/api/export/scores.csv`). A full cryptographic chain would sign input data at submission time, which is out of scope for this build.
