"""
Contradiction detector — emits contradicts_value signal_evidence when sources disagree.

Runs after signal extraction + latent signals. Does not mutate canonical rows;
only appends signal_evidence rows linked to existing signals.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

SOURCE_FILE = "agents/scoring/contradiction_detector.py"


def _clear_prior_contradictions(con) -> None:
    """Remove prior contradiction evidence from this detector (idempotent re-runs)."""
    con.execute(
        """
        DELETE FROM signal_evidence
        WHERE evidence_type = 'contradicts_value'
          AND json_extract_string(provenance_pointer, '$.source_file') = ?
        """,
        [SOURCE_FILE],
    )


def _get_signal_id(
    con, allocator_id: str, signal_type: str
) -> Optional[str]:
    row = con.execute(
        """
        SELECT CAST(signal_id AS VARCHAR)
        FROM signals
        WHERE CAST(allocator_id AS VARCHAR) = ?
          AND signal_type = ?
        ORDER BY ingested_at DESC
        LIMIT 1
        """,
        [allocator_id, signal_type],
    ).fetchone()
    return row[0] if row else None


def _append_contradiction(
    con,
    signal_id: str,
    source_record_id: str,
    strength: float,
    notes: str,
    field_a: str,
    field_b: str,
) -> None:
    if not source_record_id:
        return

    existing = con.execute(
        """
        SELECT 1 FROM signal_evidence
        WHERE CAST(signal_id AS VARCHAR) = ?
          AND evidence_type = 'contradicts_value'
          AND notes = ?
        LIMIT 1
        """,
        [signal_id, notes],
    ).fetchone()
    if existing:
        return

    ptr = json.dumps({
        "source_file": SOURCE_FILE,
        "source_offset": f"{field_a}_vs_{field_b}",
        "row_id": signal_id,
    })
    con.execute(
        """
        INSERT INTO signal_evidence (
            evidence_id, signal_id, source_record_id, evidence_type,
            evidence_strength, confidence, timestamp, provenance_pointer, notes
        ) VALUES (?, ?, ?, 'contradicts_value', ?, ?, ?, ?, ?)
        """,
        [
            str(uuid.uuid4()), signal_id, source_record_id,
            strength, strength, datetime.now(timezone.utc).isoformat(),
            ptr, notes,
        ],
    )


def run_contradiction_detection(con) -> Dict[str, int]:
    """
    Detect contradictions among icp_scores, signals, and investments.
    Re-runs derive_signal_uncertainty after to refresh contradiction_score.
    """
    _clear_prior_contradictions(con)

    counts = {
        "c2_vs_em_signal": 0,
        "deploy_vs_recency": 0,
        "em_pass_vs_coinvest": 0,
    }

    # 1. C2 pass but weak em_participation / s2 score
    rows = con.execute(
        """
        SELECT
            CAST(i.allocator_id AS VARCHAR),
            i.s2_emerging_manager,
            CAST(s.signal_id AS VARCHAR),
            s.source_record_id
        FROM icp_scores i
        JOIN signals s
          ON CAST(s.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
         AND s.signal_type = 'em_participation'
        WHERE i.icp_version = '4.1'
          AND i.c2_emerging_manager_pass = TRUE
          AND COALESCE(i.s2_emerging_manager, 0) < 0.55
        """
    ).fetchall()
    for aid, s2, sig_id, src_rec in rows:
        if not sig_id:
            continue
        _append_contradiction(
            con, sig_id, src_rec, 0.7,
            f"C2 pass but s2_emerging_manager={s2:.2f}",
            "c2_emerging_manager_pass", "s2_emerging_manager",
        )
        counts["c2_vs_em_signal"] += 1

    # 2. High deployment_velocity but low recent_activity_recency
    rows = con.execute(
        """
        SELECT
            CAST(s1.allocator_id AS VARCHAR),
            s1.normalized_value AS deploy,
            s2.normalized_value AS recency,
            CAST(s1.signal_id AS VARCHAR),
            s1.source_record_id
        FROM signals s1
        JOIN signals s2
          ON CAST(s2.allocator_id AS VARCHAR) = CAST(s1.allocator_id AS VARCHAR)
         AND s2.signal_type = 'recent_activity_recency'
        WHERE s1.signal_type = 'deployment_velocity'
          AND s1.normalized_value >= 0.7
          AND COALESCE(s2.normalized_value, 0) < 0.35
        """
    ).fetchall()
    for aid, deploy, recency, sig_id, src_rec in rows:
        if not sig_id:
            continue
        _append_contradiction(
            con, sig_id, src_rec, 0.65,
            f"deployment_velocity={deploy:.2f} but recent_activity_recency={recency:.2f}",
            "deployment_velocity", "recent_activity_recency",
        )
        counts["deploy_vs_recency"] += 1

    # 3. Strong EM in ICP but stale investment recency (syndicate LPs with investments)
    rows = con.execute(
        """
        SELECT
            CAST(i.allocator_id AS VARCHAR),
            i.s2_emerging_manager,
            COALESCE(s_rec.normalized_value, 0) AS recency,
            CAST(s_em.signal_id AS VARCHAR),
            COALESCE(
                s_em.source_record_id,
                (
                    SELECT CAST(inv.source_record_id AS VARCHAR)
                    FROM investments inv
                    WHERE CAST(inv.lp_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
                    LIMIT 1
                )
            ) AS src_rec
        FROM icp_scores i
        LEFT JOIN signals s_rec
          ON CAST(s_rec.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
         AND s_rec.signal_type = 'recent_activity_recency'
        JOIN signals s_em
          ON CAST(s_em.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
         AND s_em.signal_type = 'em_participation'
        WHERE i.icp_version = '4.1'
          AND i.s2_emerging_manager >= 0.7
          AND COALESCE(s_rec.normalized_value, 0) < 0.2
          AND EXISTS (
              SELECT 1 FROM investments inv
              WHERE CAST(inv.lp_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
          )
        """
    ).fetchall()
    for aid, s2, recency, sig_id, src_rec in rows:
        if not sig_id:
            continue
        _append_contradiction(
            con, sig_id, src_rec, 0.6,
            f"Strong EM signal ({s2:.2f}) but stale investment recency ({recency:.2f})",
            "s2_emerging_manager", "recent_activity_recency",
        )
        counts["em_pass_vs_coinvest"] += 1

    counts["total_contradictions"] = sum(counts[k] for k in counts if k != "total_contradictions")
    return counts
