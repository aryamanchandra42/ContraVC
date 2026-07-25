"""
Evaluate cascade Stage 3 (prerank) against real client decisions.

Run:  python scripts/eval_prerank.py [--db contra.duckdb] [--json out.json]

WHAT THIS MEASURES, AND WHAT IT DELIBERATELY DOES NOT
=====================================================

The only supervised labels in this database are `icp_scores.client_decision` —
112 approved and 46 rejected allocators, deduped to the latest `icp_version`.
It is tempting to calibrate the prerank score against them. That would be
invalid, for a reason worth writing down so nobody tries it again:

`icp_scores.c1_evidence` .. `c4_evidence` are not raw facts about the allocator.
They are the OUTPUT of the ICP keyword scorer, e.g.

    "VC fund evidence: fund, vc, venture capital  No emerging manager evidence
     in scoring text  AI/tech:  ai   Regions:  us , global"

Prerank is a keyword scorer over the same C1-C4 lists. Scoring it against a
record of which of those keywords already matched grades it on reproducing
itself. So Test A below runs exactly that comparison and reports the SEPARATION,
in order to demonstrate the absence of signal rather than to tune anything.

The genuinely useful test is B: the reasons clients actually gave for rejecting
LPs — "They have a private equity focus", "real estate focus", "more focused on
direct investments vs Fund investments" — live in `stated_reason`, and they are
disqualifiers of exactly the kind Stage 3's hard-exclusion list exists to catch.
Test B asks whether the exclusion phrases fire on that real client language, and
Test C asks whether they stay silent on approved LPs.

Caveat on B, stated plainly: several exclusion phrases in `icp_spec.py` were
originally derived from these same rejections, so B is a COVERAGE check on the
phrase list, not an out-of-sample generalisation estimate. It tells you whether
the list still covers known disqualifier language; it cannot tell you how the
list will do on unseen wording.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


LABEL_SQL = """
WITH latest AS (
    SELECT allocator_id, MAX(icp_version) AS v
    FROM icp_scores
    WHERE client_decision IN ('approved', 'rejected')
    GROUP BY allocator_id
)
SELECT a.canonical_name,
       a.allocator_type,
       a.geography,
       s.client_decision,
       COALESCE(s.c1_evidence, '') AS c1,
       COALESCE(s.c2_evidence, '') AS c2,
       COALESCE(s.c3_evidence, '') AS c3,
       COALESCE(s.c4_evidence, '') AS c4,
       COALESCE(s.stated_reason, '') AS stated_reason
FROM icp_scores s
JOIN latest l ON l.allocator_id = s.allocator_id AND l.v = s.icp_version
JOIN allocators a ON a.allocator_id = s.allocator_id
WHERE s.client_decision IN ('approved', 'rejected')
"""


def load_labels(db_path: str) -> List[Dict[str, Any]]:
    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute(LABEL_SQL).fetchall()
        cols = [d[0] for d in con.description]
    finally:
        con.close()
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# Test A — does the prerank score separate approved from rejected?
# ---------------------------------------------------------------------------

def _auc(pos: List[float], neg: List[float]) -> float:
    """
    Probability a random positive outscores a random negative (ties count half).

    0.5 means no separation at all. Computed directly rather than pulled from a
    dependency because the sample is tiny and this is exact.
    """
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / (len(pos) * len(neg))


def test_separation(labels: List[Dict[str, Any]]) -> Dict[str, Any]:
    from contra.prospector.prerank import score_candidate

    scores: Dict[str, List[float]] = {"approved": [], "rejected": []}
    excluded: Dict[str, int] = {"approved": 0, "rejected": 0}

    for row in labels:
        span = " ".join(filter(None, [row["c1"], row["c2"], row["c3"], row["c4"]]))
        res = score_candidate(
            row["canonical_name"], span,
            entity_type=row["allocator_type"] or "",
            geography=row["geography"] or "",
            source_diversity=1,
        )
        decision = row["client_decision"]
        if res.excluded:
            excluded[decision] += 1
            scores[decision].append(0.0)
        else:
            scores[decision].append(float(res.score))

    def _summary(vals: List[float]) -> Dict[str, float]:
        if not vals:
            return {}
        return {
            "n": len(vals),
            "mean": round(statistics.mean(vals), 1),
            "median": round(statistics.median(vals), 1),
            "min": min(vals),
            "max": max(vals),
        }

    return {
        "approved": _summary(scores["approved"]),
        "rejected": _summary(scores["rejected"]),
        "hard_excluded": excluded,
        "auc": round(_auc(scores["approved"], scores["rejected"]), 3),
    }


# ---------------------------------------------------------------------------
# Tests B and C — does the hard-exclusion list fire on real client language?
# ---------------------------------------------------------------------------

def _detect_exclusion(text: str) -> str:
    """Reuse Stage 3's own detector so the test cannot drift from the code."""
    from contra.prospector.prerank import score_candidate

    res = score_candidate("probe entity", text)
    return res.hard_exclusion


def test_exclusion_coverage(labels: List[Dict[str, Any]]) -> Dict[str, Any]:
    caught: List[Tuple[str, str, str]] = []
    missed: List[Tuple[str, str]] = []
    false_fires: List[Tuple[str, str, str]] = []

    for row in labels:
        reason = (row["stated_reason"] or "").strip()
        if not reason:
            continue
        hit = _detect_exclusion(reason)
        name = row["canonical_name"]
        if row["client_decision"] == "rejected":
            if hit:
                caught.append((name, reason, hit))
            else:
                missed.append((name, reason))
        elif hit:
            false_fires.append((name, reason, hit))

    total_rejected_with_reason = len(caught) + len(missed)
    recall = (len(caught) / total_rejected_with_reason) if total_rejected_with_reason else 0.0

    return {
        "rejected_with_stated_reason": total_rejected_with_reason,
        "caught": len(caught),
        "missed": len(missed),
        "recall": round(recall, 3),
        "false_fires_on_approved": len(false_fires),
        "caught_examples": [
            {"name": n, "reason": r[:120], "rule": h} for n, r, h in caught[:8]
        ],
        "missed_examples": [
            {"name": n, "reason": r[:120]} for n, r in missed[:12]
        ],
        "false_fire_examples": [
            {"name": n, "reason": r[:120], "rule": h} for n, r, h in false_fires[:8]
        ],
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("PULSE_DB", "contra.duckdb"))
    parser.add_argument("--json", default="", help="also write the report as JSON")
    args = parser.parse_args()

    db_path = args.db if os.path.isabs(args.db) else str(ROOT / args.db)
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    labels = load_labels(db_path)
    if not labels:
        print("No labelled rows found (icp_scores.client_decision).", file=sys.stderr)
        return 1

    n_app = sum(1 for r in labels if r["client_decision"] == "approved")
    n_rej = len(labels) - n_app

    print("=" * 72)
    print("Stage 3 (prerank) evaluation against client decisions")
    print("=" * 72)
    print(f"labelled allocators : {len(labels)}  ({n_app} approved / {n_rej} rejected)")
    print()

    sep = test_separation(labels)
    print("-" * 72)
    print("TEST A — score separation (EXPECTED TO SHOW NO SIGNAL)")
    print("-" * 72)
    print("Features are the ICP scorer's own keyword-match output, so this is a")
    print("circularity check, not a calibration. AUC near 0.5 = no separation.")
    print()
    for decision in ("approved", "rejected"):
        s = sep[decision]
        if s:
            print(f"  {decision:9} n={s['n']:4}  mean={s['mean']:5}  "
                  f"median={s['median']:5}  range={s['min']:.0f}-{s['max']:.0f}")
    print(f"  hard-excluded: {sep['hard_excluded']}")
    print(f"  AUC: {sep['auc']}")
    verdict = (
        "no usable signal — do NOT treat prerank as a conversion predictor"
        if abs(sep["auc"] - 0.5) < 0.1 else
        "some separation present — investigate before trusting it"
    )
    print(f"  -> {verdict}")
    print()

    cov = test_exclusion_coverage(labels)
    print("-" * 72)
    print("TEST B/C — hard-exclusion list vs real client rejection language")
    print("-" * 72)
    print(f"  rejected LPs with a stated reason : {cov['rejected_with_stated_reason']}")
    print(f"  caught by exclusion phrases       : {cov['caught']}  "
          f"(recall {cov['recall']:.0%})")
    print(f"  missed                            : {cov['missed']}")
    print(f"  false fires on APPROVED LPs       : {cov['false_fires_on_approved']}")
    print()
    if cov["caught_examples"]:
        print("  caught:")
        for ex in cov["caught_examples"]:
            print(f"    {ex['name'][:32]:34} {ex['rule']}")
            print(f"      \"{ex['reason']}\"")
    if cov["missed_examples"]:
        print()
        print("  missed (candidate phrases to add to icp_spec.py):")
        for ex in cov["missed_examples"]:
            print(f"    {ex['name'][:32]:34} \"{ex['reason']}\"")
    if cov["false_fire_examples"]:
        print()
        print("  FALSE FIRES — these would wrongly kill a good LP:")
        for ex in cov["false_fire_examples"]:
            print(f"    {ex['name'][:32]:34} {ex['rule']}")
            print(f"      \"{ex['reason']}\"")
    print()

    print("-" * 72)
    print("CONCLUSION")
    print("-" * 72)
    print("Stage 3's defensible job is hard-exclusion detection plus cheap ranking")
    print("to order the paid stages — not predicting client conversion. The real")
    print("precision signal for the cascade is Stage 4's corroboration rate and")
    print("the eventual gate verdicts, both recorded per run in prospector_runs.")
    print()

    if args.json:
        out = {
            "labels": {"total": len(labels), "approved": n_app, "rejected": n_rej},
            "separation": sep,
            "exclusion_coverage": cov,
        }
        Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"JSON written to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
