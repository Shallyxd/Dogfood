"""Kingmaker check — does any single judge decide who wins?

For each judge the ranking is refitted without that judge's reviews. A judge is
a "kingmaker" when removing them alone changes which projects hold the prize
places, or who comes first. Projects reviewed only by the removed judge have no
evidence left, so they are excluded from both rankings before comparing; their
disappearance is a coverage problem, not influence.

Ported (for our z-score + shrinkage normaliser) from the idea flagged as worth
stealing from GauravS13/raptor-desk. Organiser-facing only.
"""

from app.judging.normalise import normalise


def _rank(scores: dict[str, dict[str, float]]) -> list[str]:
    """Best-first ordering of project ids under the normalised score."""
    norm = normalise(scores)
    return sorted(norm.keys(), key=lambda p: (norm[p], p), reverse=True)


def kingmakers(
    scores: dict[str, dict[str, float]],
    *,
    places: int = 5,
) -> list[dict]:
    """Judges whose removal alone changes the prize places or the winner."""
    if not scores:
        return []

    base = _rank(scores)
    found = []
    for judge in sorted(scores):
        rest = {j: p for j, p in scores.items() if j != judge}
        if not rest:
            continue
        after = _rank(rest)
        still_ranked = set(after)
        before = [p for p in base if p in still_ranked]
        if not before or not after:
            continue

        top_before, top_after = set(before[:places]), set(after[:places])
        if top_before != top_after or before[0] != after[0]:
            found.append(
                {
                    "judge": judge,
                    "reviews": len(scores[judge]),
                    "entered": sorted(top_after - top_before),
                    "left": sorted(top_before - top_after),
                    "winner_before": before[0],
                    "winner_after": after[0],
                    "changes_winner": before[0] != after[0],
                }
            )
    return found
