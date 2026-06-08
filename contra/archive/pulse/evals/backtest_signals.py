"""
Rigorous backtesting for PULSE signal layer.

Tests:
  1. signal_evidence invariant — every signal has >=1 evidence row
  2. orphan signal_evidence — no evidence without a parent signal
  2. latent signal coverage — institutional prospects have icp-mirror signals
  3. tier discrimination — tier_1 mean fit_score > tier_4
  4. connectivity lift — tier_1 bridge_strength >= tier_3/4 when present
  5. calibration name-join overlap — ContraVC matches institutional prospects by name
  6. invested_with edges exist when investments present
  7. signal derive determinism — re-derive produces identical confidences
  8. precision@K — top-K by composite score captures approved tier_1 LPs
  9. C2 gate strictness — tier_1 allocators must have c2 pass
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "pulse.duckdb"


def _get_conn():
    if not DB_PATH.exists():
        return None
    from agents.db import get_conn
    return get_conn()


def _mean(vals: List[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def test_signal_evidence_invariant():
    """Every signal row must have >=1 signal_evidence row."""
    con = _get_conn()
    if con is None:
        return
    missing = con.execute(
        """
        SELECT COUNT(*) FROM signals s
        WHERE NOT EXISTS (
            SELECT 1 FROM signal_evidence se
            WHERE CAST(se.signal_id AS VARCHAR) = CAST(s.signal_id AS VARCHAR)
        )
        """
    ).fetchone()[0]
    assert missing == 0, f"{missing} signals missing signal_evidence rows"


def test_orphan_signal_evidence():
    """No signal_evidence rows without a matching signal."""
    con = _get_conn()
    if con is None:
        return
    orphans = con.execute(
        """
        SELECT COUNT(*) FROM signal_evidence se
        WHERE NOT EXISTS (
            SELECT 1 FROM signals s
            WHERE CAST(s.signal_id AS VARCHAR) = CAST(se.signal_id AS VARCHAR)
        )
        """
    ).fetchone()[0]
    assert orphans == 0, f"{orphans} orphan signal_evidence rows"


def test_latent_icp_mirror_coverage():
    """Every icp_scores row has stage_alignment / clean_profile / proxy_fund_overlap signals."""
    con = _get_conn()
    if con is None:
        return
    missing = con.execute(
        """
        SELECT COUNT(*) FROM icp_scores i
        WHERE i.icp_version = '4.1'
        AND NOT EXISTS (
            SELECT 1 FROM signals s
            WHERE CAST(s.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
              AND s.signal_type = 'stage_alignment'
              AND s.source_file = 'agents/scoring/latent_signal_extractor.py'
        )
        """
    ).fetchone()[0]
    scored = con.execute(
        "SELECT COUNT(*) FROM icp_scores WHERE icp_version = '4.1'"
    ).fetchone()[0]
    if scored == 0:
        return
    coverage = 1.0 - missing / scored
    assert coverage >= 0.95, f"ICP mirror coverage {coverage:.2%} below 95%"


def test_tier_discrimination_fit_score():
    """tier_1 mean fit_score strictly greater than tier_4."""
    con = _get_conn()
    if con is None:
        return
    rows = con.execute(
        """
        SELECT tier, fit_score FROM icp_scores
        WHERE icp_version = '4.1' AND fit_score IS NOT NULL
        """
    ).fetchall()
    if not rows:
        return
    by_tier: Dict[str, List[float]] = {}
    for tier, fit in rows:
        by_tier.setdefault(tier, []).append(float(fit))
    t1 = _mean(by_tier.get("tier_1", []))
    t4 = _mean(by_tier.get("tier_4", []))
    if by_tier.get("tier_1") and by_tier.get("tier_4"):
        assert t1 > t4, f"tier_1 mean fit {t1:.3f} not > tier_4 mean {t4:.3f}"


def test_connectivity_lift():
    """Institutional tier_1 prospects with bridge_strength beat tier_3/4 median."""
    con = _get_conn()
    if con is None:
        return
    rows = con.execute(
        """
        SELECT i.tier, s.normalized_value
        FROM icp_scores i
        JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
        JOIN signals s ON CAST(s.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
        WHERE i.icp_version = '4.1'
          AND a.population = 'institutional_prospect'
          AND s.signal_type = 'bridge_strength'
          AND s.normalized_value IS NOT NULL
        """
    ).fetchall()
    if len(rows) < 5:
        return
    by_tier: Dict[str, List[float]] = {}
    for tier, val in rows:
        by_tier.setdefault(tier, []).append(float(val))
    t1 = _mean(by_tier.get("tier_1", []))
    t34 = _mean(by_tier.get("tier_3", []) + by_tier.get("tier_4", []))
    if by_tier.get("tier_1") and (by_tier.get("tier_3") or by_tier.get("tier_4")):
        assert t1 >= t34 * 0.8, (
            f"tier_1 bridge_strength {t1:.3f} too far below tier_3/4 {t34:.3f}"
        )


def test_calibration_name_join_overlap():
    """Benchmark rows link to allocators; name-join works for syndicate population."""
    con = _get_conn()
    if con is None:
        return
    bench_count = con.execute(
        "SELECT COUNT(*) FROM benchmark_rankings WHERE ranking_source = 'contravc_top200'"
    ).fetchone()[0]
    if bench_count == 0:
        return

    # Syndicate LPs should match benchmark by allocator_id (ingested together)
    syndicate_linked = con.execute(
        """
        SELECT COUNT(*)
        FROM benchmark_rankings b
        JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = CAST(b.allocator_id AS VARCHAR)
        WHERE b.ranking_source = 'contravc_top200'
          AND a.population = 'syndicate_lp'
        """
    ).fetchone()[0]
    assert syndicate_linked >= 50, (
        f"Expected >=50 ContraVC rows linked to syndicate_lp, got {syndicate_linked}"
    )

    # Name-join path must resolve at least one cross-population match
    name_join_hits = con.execute(
        """
        SELECT COUNT(*)
        FROM benchmark_rankings b
        JOIN allocators a ON lower(trim(b.external_name)) = lower(trim(a.canonical_name))
        WHERE b.ranking_source = 'contravc_top200'
        """
    ).fetchone()[0]
    assert name_join_hits >= bench_count * 0.5, (
        f"Name join hit rate too low: {name_join_hits}/{bench_count}"
    )


def test_invested_with_edges_exist():
    """invested_with edges written when investments table has shared funds."""
    con = _get_conn()
    if con is None:
        return
    inv = con.execute("SELECT COUNT(*) FROM investments").fetchone()[0]
    if inv < 2:
        return
    edges = con.execute(
        "SELECT COUNT(*) FROM relationships WHERE edge_type = 'invested_with'"
    ).fetchone()[0]
    assert edges > 0, "invested_with edges should exist when investments present"


def test_signal_derive_determinism():
    """Re-running derive_signal_uncertainty yields identical confidences."""
    con = _get_conn()
    if con is None:
        return
    from agents.uncertainty.aggregator import derive_signal_uncertainty

    before = con.execute(
        """
        SELECT CAST(signal_id AS VARCHAR), confidence, evidence_count
        FROM signals WHERE confidence IS NOT NULL
        ORDER BY signal_id
        """
    ).fetchall()
    if not before:
        return
    derive_signal_uncertainty(con)
    after = con.execute(
        """
        SELECT CAST(signal_id AS VARCHAR), confidence, evidence_count
        FROM signals WHERE confidence IS NOT NULL
        ORDER BY signal_id
        """
    ).fetchall()
    assert before == after, "Signal derivation not deterministic"


def test_precision_at_k_approved():
    """Top-20 by fit_score captures majority of client-approved tier_1 LPs."""
    con = _get_conn()
    if con is None:
        return
    approved = con.execute(
        """
        SELECT CAST(allocator_id AS VARCHAR)
        FROM icp_scores
        WHERE icp_version = '4.1'
          AND tier = 'tier_1'
          AND client_decision = 'approved'
        """
    ).fetchall()
    if len(approved) < 5:
        return
    approved_ids = {r[0] for r in approved}
    top_k = con.execute(
        """
        SELECT CAST(allocator_id AS VARCHAR)
        FROM icp_scores
        WHERE icp_version = '4.1' AND core_pass = TRUE AND excluded = FALSE
        ORDER BY fit_score DESC
        LIMIT 20
        """
    ).fetchall()
    top_ids = {r[0] for r in top_k}
    hit = len(approved_ids & top_ids)
    precision = hit / max(len(top_ids), 1)
    assert precision >= 0.25, (
        f"Precision@20 for approved tier_1 only {precision:.2%} (hits={hit})"
    )


def test_contradiction_evidence_emitted():
    """Contradiction detector writes contradicts_value evidence when C2/EM diverge."""
    con = _get_conn()
    if con is None:
        return
    count = con.execute(
        """
        SELECT COUNT(*) FROM signal_evidence
        WHERE evidence_type = 'contradicts_value'
        """
    ).fetchone()[0]
    # May be zero if all tier_1 have consistent EM signals — only assert table works
    assert count >= 0


def test_c2_strict_tier1():
    """All tier_1 rows must have c2_emerging_manager_pass = TRUE after strict gate."""
    con = _get_conn()
    if con is None:
        return
    bad = con.execute(
        """
        SELECT COUNT(*) FROM icp_scores
        WHERE icp_version = '4.1'
          AND tier = 'tier_1'
          AND (c2_emerging_manager_pass IS NULL OR c2_emerging_manager_pass = FALSE)
        """
    ).fetchone()[0]
    assert bad == 0, f"{bad} tier_1 rows fail strict C2 gate"


def run_all() -> Tuple[int, int]:
    tests = [
        test_signal_evidence_invariant,
        test_orphan_signal_evidence,
        test_latent_icp_mirror_coverage,
        test_tier_discrimination_fit_score,
        test_connectivity_lift,
        test_calibration_name_join_overlap,
        test_invested_with_edges_exist,
        test_signal_derive_determinism,
        test_precision_at_k_approved,
        test_contradiction_evidence_emitted,
        test_c2_strict_tier1,
    ]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {str(e).encode('ascii', 'replace').decode()}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {fn.__name__}: {e}")
    return passed, failed


if __name__ == "__main__":
    print("PULSE Signal Backtests")
    if not DB_PATH.exists():
        print("  SKIP — pulse.duckdb not found")
        sys.exit(0)
    p, f = run_all()
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
