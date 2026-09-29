"""Uncertainty for the published ranking.

A single ranking hides how stable each place is. We resample the *judges* with
replacement (judges are the unit of variation), refit the z-score + shrinkage
normaliser, and count how often each project lands in the prize places. The
result is a probability per project: "chance of top N".

Deterministic: a fixed seed means the same data always yields the same numbers,
so the demo is reproducible and the output can be checked.
"""

import random

from app.judging.normalise import normalise


def bootstrap_top_probabilities(
    scores: dict[str, dict[str, float]],
    *,
    places: int = 3,
    iterations: int = 200,
    seed: int = 20260301,
) -> dict[str, float]:
    """Probability each project finishes in the top ``places`` under judge resampling."""
    if not scores or iterations <= 0:
        return {}

    judges = list(scores.keys())
    projects: set[str] = set()
    for p_map in scores.values():
        projects.update(p_map.keys())
    counts = {p: 0 for p in projects}

    rng = random.Random(seed)
    for _ in range(iterations):
        resampled: dict[str, dict[str, float]] = {}
        for i in range(len(judges)):
            judge = judges[rng.randrange(len(judges))]
            resampled[f"{judge}#{i}"] = scores[judge]
        norm = normalise(resampled)
        ranked = sorted(norm.keys(), key=lambda p: (norm[p], p), reverse=True)
        for p in ranked[:places]:
            counts[p] += 1

    return {p: counts[p] / iterations for p in counts}
