"""Atomic signal + signal_evidence writes."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from agents.scoring.signal_types import VALID_SIGNAL_EVIDENCE_TYPES, VALID_SIGNAL_TYPES


def delete_signals_cascade(con, where_clause: str, params: Optional[Sequence] = None) -> int:
    """Delete signals matching where_clause and their signal_evidence rows."""
    params = list(params or [])
    ids = con.execute(
        f"SELECT CAST(signal_id AS VARCHAR) FROM signals WHERE {where_clause}",
        params,
    ).fetchall()
    if ids:
        con.executemany(
            "DELETE FROM signal_evidence WHERE CAST(signal_id AS VARCHAR) = ?",
            ids,
        )
    con.execute(f"DELETE FROM signals WHERE {where_clause}", params)
    return len(ids)


def purge_orphan_signal_evidence(con) -> int:
    """Remove signal_evidence rows whose signal_id no longer exists."""
    before = con.execute("SELECT COUNT(*) FROM signal_evidence").fetchone()[0]
    con.execute(
        """
        DELETE FROM signal_evidence
        WHERE NOT EXISTS (
            SELECT 1 FROM signals s
            WHERE CAST(s.signal_id AS VARCHAR) = CAST(signal_evidence.signal_id AS VARCHAR)
        )
        """
    )
    after = con.execute("SELECT COUNT(*) FROM signal_evidence").fetchone()[0]
    return before - after


def insert_signals_batch(
    con,
    rows: List[Tuple],
    evidence_rows: Optional[List[Tuple]] = None,
) -> int:
    """
    Insert signal rows. Each row tuple:
      (signal_id, allocator_id, signal_type, raw_value, normalized_value,
       source_record_id, source_file, content_hash)

    Optional evidence_rows tuples:
      (evidence_id, signal_id, source_record_id, evidence_type,
       evidence_strength, confidence, timestamp, provenance_pointer_json, notes)
    """
    if not rows:
        return 0

    for row in rows:
        if row[2] not in VALID_SIGNAL_TYPES:
            raise ValueError(f"Invalid signal_type '{row[2]}'")

    con.executemany(
        """
        INSERT INTO signals (
            signal_id, allocator_id, signal_type,
            raw_value, normalized_value,
            confidence, evidence_count, contradiction_score, source_agreement_score,
            source_record_id, source_file, content_hash
        ) VALUES (?, ?, ?, ?, ?, NULL, 0, NULL, NULL, ?, ?, ?)
        """,
        rows,
    )

    if evidence_rows:
        for ev in evidence_rows:
            if ev[3] not in VALID_SIGNAL_EVIDENCE_TYPES:
                raise ValueError(f"Invalid signal evidence_type '{ev[3]}'")
        con.executemany(
            """
            INSERT INTO signal_evidence (
                evidence_id, signal_id, source_record_id, evidence_type,
                evidence_strength, confidence, timestamp, provenance_pointer, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            evidence_rows,
        )

    return len(rows)


def make_evidence_row(
    signal_id: str,
    source_record_id: str,
    evidence_type: str,
    strength: float,
    source_file: str,
    notes: Optional[str] = None,
    source_offset: str = "",
) -> Tuple:
    now = datetime.now(timezone.utc).isoformat()
    ptr = json.dumps({
        "source_file": source_file,
        "source_offset": source_offset,
        "row_id": signal_id,
    })
    conf = min(1.0, max(0.0, float(strength)))
    return (
        str(uuid.uuid4()),
        signal_id,
        source_record_id,
        evidence_type,
        conf,
        conf,
        now,
        ptr,
        notes,
    )
