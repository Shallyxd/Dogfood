import json
import statistics
from pathlib import Path

from app.judging.normalise import compute_z_scores, normalise


def load_data():
    try:
        from app.database import SessionLocal
        from app.models import Score, Project, Event

        db = SessionLocal()
        event = db.query(Event).first()
        rubric = (event.rubric if event and event.rubric else None) or {
            "functionality": 1.0 / 3.0,
            "quality": 1.0 / 3.0,
            "innovation": 1.0 / 3.0,
        }

        scores_rows = db.query(Score).all()
        projects_rows = db.query(Project).all()

        if scores_rows:
            judge_proj_crit = {}
            raw_crit_by_judge = {}
            for s in scores_rows:
                judge_proj_crit.setdefault(s.judge_fixture_id, {}).setdefault(
                    s.project_id, {}
                )[s.criterion] = s.value
                raw_crit_by_judge.setdefault(s.judge_fixture_id, []).append(s.value)

            scores = {}
            for j_id, p_map in judge_proj_crit.items():
                for p_id, crits in p_map.items():
                    w_sum = sum(crits.get(c, 0) * rubric.get(c, 1.0 / 3.0) for c in crits)
                    scores.setdefault(j_id, {})[p_id] = w_sum

            project_titles = {p.id: p.title for p in projects_rows}
            db.close()
            return scores, raw_crit_by_judge, project_titles
        db.close()
    except Exception:
        pass

    candidates = [
        Path("seed/fixtures.json"),
        Path("/app/seed/fixtures.json"),
        Path("dossier/Dogfood/fixtures.json"),
    ]
    fpath = None
    for c in candidates:
        if c.is_file():
            fpath = c
            break
    if not fpath:
        raise FileNotFoundError("Could not find fixtures data")

    with open(fpath, "r", encoding="utf-8") as f:
        data = json.load(f)

    scores = {}
    raw_crit_by_judge = {}
    for s in data["scores"]:
        j = s["judge"]
        p = s["project"]
        crits = s["criteria"]
        total = (crits["functionality"] + crits["quality"] + crits["innovation"]) / 3.0
        scores.setdefault(j, {})[p] = total
        for v in crits.values():
            raw_crit_by_judge.setdefault(j, []).append(v)

    project_titles = {p["id"]: p["title"] for p in data["projects"]}
    return scores, raw_crit_by_judge, project_titles


def generate_report():
    scores, _crit_vals, project_titles = load_data()
    z_scores = compute_z_scores(scores)
    normalised_scores = normalise(scores)

    sorted_judges = sorted(scores.keys())

    print("Normalisation report (fixture data)")
    print("Judge harshness (raw mean ± std -> normalised):")

    judge_raw_means = []
    judge_norm_means = []

    for j in sorted_judges:
        raw_vals = list(scores[j].values())
        raw_mean = statistics.mean(raw_vals)
        raw_std = statistics.stdev(raw_vals) if len(raw_vals) > 1 else 0.0

        z_vals = list(z_scores[j].values())
        if not z_vals:
            print(f"  {j}: {raw_mean:.2f} ± {raw_std:.2f} -> excluded (<2 reviews, cannot calibrate)")
            judge_raw_means.append(raw_mean)
            continue

        norm_mean = statistics.mean(z_vals)
        norm_std = statistics.stdev(z_vals) if len(z_vals) > 1 else 0.0

        judge_raw_means.append(raw_mean)
        judge_norm_means.append(norm_mean)

        print(
            f"  {j}: {raw_mean:.2f} ± {raw_std:.2f} -> mean {norm_mean:.2f} ± {norm_std:.2f}"
        )

    # Honest one-number proof: z-scoring forces every judge's mean to 0,
    # so the spread of per-judge means collapses. No scaling fudge factors.
    before_spread = round(statistics.stdev(judge_raw_means), 2) if len(judge_raw_means) > 1 else 0.0
    after_spread = round(statistics.stdev(judge_norm_means), 2) if len(judge_norm_means) > 1 else 0.0

    print(
        f"Judge harshness spread (std of per-judge means): before={before_spread:.2f} after={after_spread:.2f}   <- the \"one number\" for the demo"
    )

    proj_raw = {}
    for j, p_scores in scores.items():
        for p, s in p_scores.items():
            proj_raw.setdefault(p, []).append(s)

    raw_scores = {p: statistics.mean(vals) for p, vals in proj_raw.items()}

    ranked_raw_ids = sorted(raw_scores.keys(), key=lambda p: (raw_scores[p], p), reverse=True)
    ranked_norm_ids = sorted(
        normalised_scores.keys(), key=lambda p: (normalised_scores[p], p), reverse=True
    )

    raw_ranks = {p: i + 1 for i, p in enumerate(ranked_raw_ids)}
    norm_ranks = {p: i + 1 for i, p in enumerate(ranked_norm_ids)}

    print("Top 10 rank movement (raw rank -> normalised rank):")
    for i, p in enumerate(ranked_norm_ids[:10]):
        delta = raw_ranks[p] - norm_ranks[p]
        sign = f"+{delta}" if delta > 0 else str(delta)
        title = project_titles.get(p, p)
        print(f"  {i+1:>2}. {title:<20} {raw_ranks[p]:>2} -> {norm_ranks[p]:>2} ({sign})")

    return before_spread, after_spread


if __name__ == "__main__":
    generate_report()
