import statistics


def compute_z_scores(scores: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Compute per-judge z-scores with flat-rater and single-sample guards."""
    z_scores = {}
    for judge_id, project_scores in scores.items():
        vals = list(project_scores.values())
        if len(vals) < 2:
            # Single-sample judges carry no calibration information: one score
            # cannot separate judge harshness from project quality. Exclude them
            # from the normalised ranking (their raw scores stay visible in the
            # CSV and gallery). Keeping a raw 2-5-scale value in a pool of
            # z-scores corrupts both scale and sign, so "keep as-is" is wrong.
            z_scores[judge_id] = {}
        else:
            mean = statistics.mean(vals)
            std = statistics.stdev(vals)
            if std < 0.01:
                # Flat-rater guard: mean-center only
                z_scores[judge_id] = {p: s - mean for p, s in project_scores.items()}
            else:
                z_scores[judge_id] = {p: (s - mean) / std for p, s in project_scores.items()}
    return z_scores


def normalise(scores: dict[str, dict[str, float]]) -> dict[str, float]:
    """scores = {judge_id: {project_id: raw_total}}. Returns {project_id: normalised}."""
    # 1. Per-judge z-score with guards
    z_scores = compute_z_scores(scores)

    # 2. Aggregate per project: mean of z-scores across its judges
    project_z_values: dict[str, list[float]] = {}
    for judge_id, p_map in z_scores.items():
        for project_id, z in p_map.items():
            project_z_values.setdefault(project_id, []).append(z)

    all_z = [z for z_list in project_z_values.values() for z in z_list]
    global_mean = statistics.mean(all_z) if all_z else 0.0

    # 3. Shrinkage: (n * mean_z + 1 * global_mean) / (n + 1)
    normalised = {}
    for project_id, z_list in project_z_values.items():
        n = len(z_list)
        mean_z = statistics.mean(z_list)
        normalised[project_id] = (n * mean_z + 1.0 * global_mean) / (n + 1.0)

    return normalised
