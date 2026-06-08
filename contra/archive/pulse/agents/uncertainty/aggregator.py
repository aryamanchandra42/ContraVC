"""
Deterministic uncertainty aggregator.

Reads relationship_evidence and recomputes uncertainty columns
(confidence, evidence_count, contradiction_score, source_agreement_score)
for relationships, signals, and ontology_terms.

All computations are deterministic functions of the evidence table.
No ML. No hand-written values.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
UNCERTAINTY_PARAMS = ROOT / "prompts" / "uncertainty.yaml"


def _load_params() -> Dict[str, Any]:
    with open(UNCERTAINTY_PARAMS, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _noisy_or(strengths: List[float]) -> float:
    """1 - ∏(1 - s_i)  — noisy-OR combinator over pre-computed s_i values."""
    if not strengths:
        return 0.0
    product = 1.0
    for s in strengths:
        product *= 1.0 - s
    return 1.0 - product


def _compute_confidence(evidence_rows: List[Dict], params: Dict) -> float:
    """Combine evidence_strength × confidence per row using configured combinator."""
    combinator = params["confidence"]["combinator"]
    min_strength = params["confidence"]["min_evidence_strength"]

    combined = [
        row["evidence_strength"] * row["confidence"]
        for row in evidence_rows
        if row["evidence_strength"] >= min_strength
    ]
    if not combined:
        return 0.0

    if combinator == "noisy_or":
        return _noisy_or(combined)
    elif combinator == "min":
        return min(combined)
    elif combinator == "mean":
        return sum(combined) / len(combined)
    else:
        raise ValueError(f"Unknown combinator: {combinator}")


def _compute_source_agreement(
    evidence_rows: List[Dict], params: Dict
) -> tuple[float, float]:
    """Returns (source_agreement_score, contradiction_score)."""
    agree_prefixes = tuple(params["source_agreement"]["agreeing_evidence_prefixes"])
    contra_prefixes = tuple(params["source_agreement"]["contradicting_evidence_prefixes"])

    observing = set()
    agreeing = set()
    contradicting = set()

    for row in evidence_rows:
        ptr = row.get("provenance_pointer") or {}
        src = ptr.get("source_file", f"unknown_{row.get('evidence_id', '')}")
        observing.add(src)
        ev_type = row.get("evidence_type", "")
        if ev_type.startswith(contra_prefixes):
            contradicting.add(src)
        elif ev_type.startswith(agree_prefixes):
            agreeing.add(src)

    n = len(observing)
    if n == 0:
        return 0.0, 0.0
    return len(agreeing) / n, len(contradicting) / n


def derive_relationship_uncertainty(con, params: Dict | None = None) -> int:
    """
    Recompute uncertainty columns for all relationships from relationship_evidence.
    Returns number of rows updated.
    """
    if params is None:
        params = _load_params()

    # Fetch all evidence grouped by edge_id
    rows = con.execute(
        """
        SELECT
            edge_id,
            evidence_id,
            evidence_type,
            evidence_strength,
            confidence,
            provenance_pointer
        FROM relationship_evidence
        ORDER BY edge_id, timestamp
        """
    ).fetchall()

    cols = ["edge_id", "evidence_id", "evidence_type", "evidence_strength", "confidence", "provenance_pointer"]
    import json
    evidence_by_edge: Dict[str, List[Dict]] = {}
    for row in rows:
        d = dict(zip(cols, row))
        if isinstance(d["provenance_pointer"], str):
            try:
                d["provenance_pointer"] = json.loads(d["provenance_pointer"])
            except Exception:
                d["provenance_pointer"] = {}
        evidence_by_edge.setdefault(str(d["edge_id"]), []).append(d)

    # Compute derived values per edge in Python, then apply with a single set-based
    # UPDATE...FROM join. A per-edge UPDATE loop does a full-table scan each time and
    # does not scale once the graph has tens of thousands of co-investment edges.
    derived = []
    for edge_id, ev_rows in evidence_by_edge.items():
        confidence = _compute_confidence(ev_rows, params)
        agreement, contradiction = _compute_source_agreement(ev_rows, params)
        derived.append((edge_id, confidence, len(ev_rows), contradiction, agreement))

    if not derived:
        return 0

    con.execute("DROP TABLE IF EXISTS _derive_rel")
    con.execute(
        """
        CREATE TEMP TABLE _derive_rel (
            edge_id VARCHAR, confidence DOUBLE, evidence_count INTEGER,
            contradiction DOUBLE, agreement DOUBLE
        )
        """
    )
    con.executemany(
        "INSERT INTO _derive_rel VALUES (?, ?, ?, ?, ?)", derived
    )
    con.execute(
        """
        UPDATE relationships AS r
        SET confidence = d.confidence,
            evidence_count = d.evidence_count,
            contradiction_score = d.contradiction,
            source_agreement_score = d.agreement,
            updated_at = NOW()
        FROM _derive_rel AS d
        WHERE CAST(r.edge_id AS VARCHAR) = d.edge_id
        """
    )
    con.execute("DROP TABLE IF EXISTS _derive_rel")
    return len(derived)


def derive_signal_uncertainty(con, params: Dict | None = None) -> int:
    """Recompute uncertainty columns for signals from signal_evidence."""
    if params is None:
        params = _load_params()

    try:
        rows = con.execute(
            """
            SELECT
                signal_id,
                evidence_id,
                evidence_type,
                evidence_strength,
                confidence,
                provenance_pointer
            FROM signal_evidence
            ORDER BY signal_id, timestamp
            """
        ).fetchall()
    except Exception:
        return 0

    if not rows:
        return 0

    import json
    cols = [
        "signal_id", "evidence_id", "evidence_type",
        "evidence_strength", "confidence", "provenance_pointer",
    ]
    evidence_by_signal: Dict[str, List[Dict]] = {}
    for row in rows:
        d = dict(zip(cols, row))
        if isinstance(d["provenance_pointer"], str):
            try:
                d["provenance_pointer"] = json.loads(d["provenance_pointer"])
            except Exception:
                d["provenance_pointer"] = {}
        evidence_by_signal.setdefault(str(d["signal_id"]), []).append(d)

    derived = []
    for signal_id, ev_rows in evidence_by_signal.items():
        confidence = _compute_confidence(ev_rows, params)
        agreement, contradiction = _compute_source_agreement(ev_rows, params)
        derived.append((signal_id, confidence, len(ev_rows), contradiction, agreement))

    con.execute("DROP TABLE IF EXISTS _derive_sig")
    con.execute(
        """
        CREATE TEMP TABLE _derive_sig (
            signal_id VARCHAR, confidence DOUBLE, evidence_count INTEGER,
            contradiction DOUBLE, agreement DOUBLE
        )
        """
    )
    con.executemany("INSERT INTO _derive_sig VALUES (?, ?, ?, ?, ?)", derived)
    con.execute(
        """
        UPDATE signals AS s
        SET confidence = d.confidence,
            evidence_count = d.evidence_count,
            contradiction_score = d.contradiction,
            source_agreement_score = d.agreement
        FROM _derive_sig AS d
        WHERE CAST(s.signal_id AS VARCHAR) = d.signal_id
        """
    )
    con.execute("DROP TABLE IF EXISTS _derive_sig")
    return len(derived)


def derive_all(con, params: Dict | None = None) -> Dict[str, int]:
    """Run all uncertainty derivations. Called by `pulse derive`."""
    if params is None:
        params = _load_params()
    rel_updated = derive_relationship_uncertainty(con, params)
    sig_updated = derive_signal_uncertainty(con, params)
    return {
        "relationships_updated": rel_updated,
        "signals_updated": sig_updated,
    }
