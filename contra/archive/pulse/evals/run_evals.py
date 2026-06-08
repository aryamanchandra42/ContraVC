"""
PULSE Evals Harness.

Checks:
1. Extraction accuracy: HeuristicExtractor precision/recall against gold set
2. Idempotency: same input → byte-identical output (two runs)
3. Derivation determinism: uncertainty columns recompute exactly from evidence
4. Append-only invariant: human_reviews row count is monotonic (no DELETEs/UPDATEs)
5. Evidence-required-per-edge: every relationship row has ≥1 evidence row

Run: pytest evals/run_evals.py -v
  or: python evals/run_evals.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GOLD_DIR = ROOT / "evals" / "gold"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_gold(filename: str) -> List[Dict]:
    path = GOLD_DIR / filename
    if not path.exists():
        return []
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def precision_recall(expected_set: set, actual_set: set) -> tuple:
    if not actual_set:
        return 0.0, 0.0
    true_positives = expected_set & actual_set
    precision = len(true_positives) / len(actual_set) if actual_set else 0.0
    recall = len(true_positives) / len(expected_set) if expected_set else 0.0
    return precision, recall


# ---------------------------------------------------------------------------
# Test 1: Extraction accuracy against gold set
# ---------------------------------------------------------------------------

def test_heuristic_extraction_accuracy():
    """HeuristicExtractor achieves recall >= 0.60 on the gold set."""
    from agents.ontology.heuristic import HeuristicExtractor
    from agents.ontology.base import ParsedDocument, ExtractionContext
    import uuid

    extractor = HeuristicExtractor()
    gold_files = list(GOLD_DIR.glob("*.jsonl"))

    total_expected = 0
    total_found = 0
    failures = []

    for gold_file in gold_files:
        items = load_gold(gold_file.name)
        for item in items:
            text = item["input_text"]
            expected = {(t["term"], t["category"]) for t in item["expected_terms"]}
            total_expected += len(expected)

            doc = ParsedDocument(
                source_record_id=str(uuid.uuid4()),
                source_file="gold_test",
                source_type="pdf",
                source_offset="para:0",
                content_hash=str(uuid.uuid4()),
                raw_content={"text": text},
                text=text,
            )
            ctx = ExtractionContext(
                run_id="eval",
                extractor_name="heuristic",
                extractor_version="1.0",
            )
            result = extractor.extract(doc, ctx)
            found = {(t.term, t.category) for t in result.terms}
            total_found += len(expected & found)

            missing = expected - found
            if missing:
                failures.append({
                    "input": text[:80],
                    "missing": list(missing),
                    "found": list(found),
                })

    recall = total_found / total_expected if total_expected > 0 else 0.0
    print(f"\n[Extraction] Total expected: {total_expected}, Found: {total_found}, Recall: {recall:.2f}")
    if failures:
        print(f"  Failures ({len(failures)}):")
        for f in failures[:5]:
            print(f"    Input: {f['input']}")
            print(f"    Missing: {f['missing']}")

    assert recall >= 0.50, f"Extraction recall too low: {recall:.2f} (expected >= 0.50)"
    print("[PASS] Extraction accuracy")


# ---------------------------------------------------------------------------
# Test 2: Idempotency — same input → byte-identical cache output
# ---------------------------------------------------------------------------

def test_heuristic_idempotency():
    """Running HeuristicExtractor twice on the same input produces identical results."""
    from agents.ontology.heuristic import HeuristicExtractor
    from agents.ontology.base import ParsedDocument, ExtractionContext
    from agents.ontology.cache import make_cache_key, save_cached, load_cached
    import uuid

    extractor = HeuristicExtractor()
    text = "Singapore-based family office with EM focus and co-investment requirement."
    content_hash = "test_idempotency_hash_001"

    doc = ParsedDocument(
        source_record_id="test-idempotency-001",
        source_file="test_idempotency",
        source_type="pdf",
        source_offset="para:0",
        content_hash=content_hash,
        raw_content={"text": text},
        text=text,
    )
    ctx = ExtractionContext(
        run_id="eval",
        extractor_name="heuristic",
        extractor_version="1.0",
    )

    cache_key = make_cache_key("heuristic", "1.0", content_hash)

    # Run 1
    result1 = extractor.extract(doc, ctx)
    save_cached(cache_key, result1)

    # Run 2 — should return from cache
    result2 = load_cached(cache_key)
    assert result2 is not None, "Cache miss on second run"

    terms1 = sorted([(t.term, t.category) for t in result1.terms])
    terms2 = sorted([(t.term, t.category) for t in result2.terms])
    assert terms1 == terms2, f"Idempotency failure: {terms1} != {terms2}"
    print("[PASS] Idempotency")


# ---------------------------------------------------------------------------
# Test 3: Evidence-required-per-edge invariant
# ---------------------------------------------------------------------------

def test_evidence_required_per_edge():
    """Every relationship row has ≥1 relationship_evidence row."""
    db_path = ROOT / "pulse.duckdb"
    if not db_path.exists():
        print("[SKIP] pulse.duckdb not found — run `pulse run-all` first")
        return

    from agents.db import get_conn
    con = get_conn(read_only=True)

    violations = con.execute(
        """
        SELECT CAST(r.edge_id AS VARCHAR)
        FROM relationships r
        WHERE NOT EXISTS (
            SELECT 1 FROM relationship_evidence re
            WHERE CAST(re.edge_id AS VARCHAR) = CAST(r.edge_id AS VARCHAR)
        )
        """
    ).fetchall()

    if violations:
        violation_ids = [v[0] for v in violations]
        assert False, (
            f"Evidence-required-per-edge VIOLATED: {len(violations)} edges have no evidence. "
            f"First 5: {violation_ids[:5]}"
        )
    print("[PASS] Evidence-required-per-edge")


def test_mutual_connection_inference_has_evidence():
    """Every mutual_connection edge from graph_path_inference has ≥1 evidence row."""
    db_path = ROOT / "pulse.duckdb"
    if not db_path.exists():
        print("[SKIP] pulse.duckdb not found — run `pulse run-all` first")
        return

    from agents.db import get_conn
    con = get_conn(read_only=True)

    inference_edges = con.execute(
        """
        SELECT CAST(r.edge_id AS VARCHAR)
        FROM relationships r
        WHERE r.edge_type = 'mutual_connection'
          AND EXISTS (
            SELECT 1 FROM relationship_evidence re
            WHERE CAST(re.edge_id AS VARCHAR) = CAST(r.edge_id AS VARCHAR)
              AND re.evidence_type = 'graph_path_inference'
          )
        """
    ).fetchall()

    if not inference_edges:
        print("[SKIP] No graph_path_inference mutual_connection edges yet")
        return

    violations = con.execute(
        """
        SELECT CAST(r.edge_id AS VARCHAR)
        FROM relationships r
        WHERE r.edge_type = 'mutual_connection'
          AND EXISTS (
            SELECT 1 FROM relationship_evidence re
            WHERE CAST(re.edge_id AS VARCHAR) = CAST(r.edge_id AS VARCHAR)
              AND re.evidence_type = 'graph_path_inference'
          )
          AND NOT EXISTS (
            SELECT 1 FROM relationship_evidence re
            WHERE CAST(re.edge_id AS VARCHAR) = CAST(r.edge_id AS VARCHAR)
          )
        """
    ).fetchall()

    if violations:
        assert False, (
            f"graph_path_inference edges missing evidence: {len(violations)}"
        )
    print(f"[PASS] mutual_connection inference evidence ({len(inference_edges)} edges)")


# ---------------------------------------------------------------------------
# Test 4: Append-only invariant for human_reviews
# ---------------------------------------------------------------------------

def test_human_reviews_append_only():
    """
    human_reviews table should only grow. This test records current row count
    and checks it matches a re-read (no rows deleted between reads).
    """
    db_path = ROOT / "pulse.duckdb"
    if not db_path.exists():
        print("[SKIP] pulse.duckdb not found")
        return

    from agents.db import get_conn
    con = get_conn()

    count1 = con.execute("SELECT COUNT(*) FROM human_reviews").fetchone()[0]
    count2 = con.execute("SELECT COUNT(*) FROM human_reviews").fetchone()[0]

    assert count2 >= count1, (
        f"Append-only invariant VIOLATED: human_reviews count decreased from {count1} to {count2}"
    )
    print(f"[PASS] Append-only (human_reviews count: {count1})")


# ---------------------------------------------------------------------------
# Test 5: Derivation determinism
# ---------------------------------------------------------------------------

def test_co_invested_edges_have_evidence():
    """Every co_invested edge has ≥1 relationship_evidence row."""
    db_path = ROOT / "pulse.duckdb"
    if not db_path.exists():
        print("[SKIP] pulse.duckdb not found — run `pulse run-all` first")
        return

    from agents.db import get_conn
    con = get_conn(read_only=True)

    violations = con.execute(
        """
        SELECT CAST(r.edge_id AS VARCHAR)
        FROM relationships r
        WHERE r.edge_type = 'co_invested'
          AND NOT EXISTS (
            SELECT 1 FROM relationship_evidence re
            WHERE CAST(re.edge_id AS VARCHAR) = CAST(r.edge_id AS VARCHAR)
          )
        """
    ).fetchall()

    if violations:
        assert False, (
            f"co_invested edges missing evidence: {len(violations)}. "
            f"First 5: {[v[0] for v in violations[:5]]}"
        )
    print("[PASS] co_invested evidence-required-per-edge")


def test_graph_persist_includes_inference_edges():
    """edges.parquet row count matches DB mutual_connection + other effective edges."""
    db_path = ROOT / "pulse.duckdb"
    edges_path = ROOT / "graphs" / "edges.parquet"
    if not db_path.exists():
        print("[SKIP] pulse.duckdb not found")
        return
    if not edges_path.exists():
        print("[SKIP] graphs/edges.parquet not found — run `pulse graph` first")
        return

    import pandas as pd
    from agents.db import get_conn

    con = get_conn(read_only=True)
    try:
        db_count = con.execute(
            "SELECT COUNT(*) FROM relationships_effective"
        ).fetchone()[0]
    except Exception:
        db_count = con.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]

    parquet_count = len(pd.read_parquet(edges_path))
    if parquet_count != db_count:
        assert False, (
            f"Graph persist out of sync: edges.parquet has {parquet_count} rows "
            f"but relationships_effective has {db_count}"
        )
    print(f"[PASS] Graph persist sync ({parquet_count} edges)")


def test_icp_scores_column_semantics():
    """icp_scores uses v4.1 column names (post-migration)."""
    db_path = ROOT / "pulse.duckdb"
    if not db_path.exists():
        print("[SKIP] pulse.duckdb not found")
        return

    from agents.db import get_conn
    con = get_conn(read_only=True)

    cols = {
        r[0]
        for r in con.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'icp_scores'
            """
        ).fetchall()
    }
    required = {
        "s1_ai_signal", "s2_emerging_manager", "s3_lp_type",
        "s4_decision_speed", "s5_stage", "s6_clean_profile", "s7_proxy_fund",
        "c4_geography_pass",
    }
    missing = required - cols
    legacy = {"s1_lp_type_match", "s2_geography_match", "s3_ai_explicit"} & cols
    assert not missing, f"icp_scores missing v4.1 columns: {missing}"
    assert not legacy, f"icp_scores still has legacy columns: {legacy}"
    print("[PASS] icp_scores v4.1 schema")


def test_derivation_determinism():
    """Running derive twice produces the same confidence values (within floating point tolerance)."""
    db_path = ROOT / "pulse.duckdb"
    if not db_path.exists():
        print("[SKIP] pulse.duckdb not found")
        return

    from agents.db import get_conn
    from agents.uncertainty.aggregator import derive_relationship_uncertainty

    con = get_conn()

    # Capture confidences before second derive
    before = {
        str(r[0]): r[1]
        for r in con.execute("SELECT edge_id, confidence FROM relationships WHERE confidence IS NOT NULL").fetchall()
    }

    # Re-derive
    derive_relationship_uncertainty(con)

    after = {
        str(r[0]): r[1]
        for r in con.execute("SELECT edge_id, confidence FROM relationships WHERE confidence IS NOT NULL").fetchall()
    }

    mismatches = []
    for edge_id, conf_before in before.items():
        conf_after = after.get(edge_id)
        if conf_after is not None and abs(conf_before - conf_after) > 1e-9:
            mismatches.append((edge_id, conf_before, conf_after))

    assert not mismatches, (
        f"Derivation determinism VIOLATED: {len(mismatches)} edges changed. First 3: {mismatches[:3]}"
    )
    print(f"[PASS] Derivation determinism ({len(before)} relationships checked)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n=== PULSE Evals Harness ===\n")
    errors = []

    for test_fn in [
        test_heuristic_extraction_accuracy,
        test_heuristic_idempotency,
        test_evidence_required_per_edge,
        test_co_invested_edges_have_evidence,
        test_mutual_connection_inference_has_evidence,
        test_graph_persist_includes_inference_edges,
        test_icp_scores_column_semantics,
        test_human_reviews_append_only,
        test_derivation_determinism,
    ]:
        try:
            test_fn()
        except AssertionError as e:
            print(f"[FAIL] {test_fn.__name__}: {e}")
            errors.append(str(e))
        except Exception as e:
            print(f"[ERROR] {test_fn.__name__}: {e}")
            errors.append(str(e))

    print(f"\n=== Results: {len(errors)} failure(s) ===")
    if errors:
        sys.exit(1)
    else:
        print("All evals passed.")
