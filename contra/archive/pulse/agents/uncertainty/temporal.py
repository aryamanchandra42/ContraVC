"""
Deterministic temporal derivation.

Computes relationship_decay_score and temporal_confidence from:
- last_active (max timestamp across evidence + interactions)
- half_life_days from prompts/uncertainty.yaml

This is a parameterized recency function. It is NOT learned. It is NOT ML.
Changing half_life_days in uncertainty.yaml is the only valid way to change decay behaviour.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
UNCERTAINTY_PARAMS = ROOT / "prompts" / "uncertainty.yaml"


def _load_params() -> Dict[str, Any]:
    with open(UNCERTAINTY_PARAMS, encoding="utf-8") as f:
        return yaml.safe_load(f)


def decay_score(last_active: Optional[datetime], half_life_days: float, now: Optional[datetime] = None) -> Optional[float]:
    """
    exp(-Δt / half_life_days) where Δt is days since last_active.

    Returns None if last_active is None (no temporal data available).
    Returns 1.0 if last_active is in the future (data error: clamped).
    """
    if last_active is None:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    if last_active.tzinfo is None:
        last_active = last_active.replace(tzinfo=timezone.utc)
    delta_days = max(0.0, (now - last_active).total_seconds() / 86400.0)
    return math.exp(-delta_days / half_life_days)


_VIEW_HARDCODED_HALF_LIFE = 365.0  # must match the constant in schema/views.sql


def _check_decay_view_sync(half_life: float) -> None:
    """Warn if relationship_decay_view would disagree with our yaml half-life."""
    if abs(half_life - _VIEW_HARDCODED_HALF_LIFE) > 0.01:
        import warnings
        warnings.warn(
            f"prompts/uncertainty.yaml half_life_days={half_life} differs from the "
            f"hardcoded {_VIEW_HARDCODED_HALF_LIFE} in schema/views.sql "
            f"relationship_decay_view. Update the SQL constant to match, then "
            f"re-run `pulse derive` so the view stays accurate for analytics.",
            stacklevel=3,
        )


def derive_temporal(con, params: Dict | None = None) -> int:
    """
    Recompute temporal columns for all relationships.
    last_active = max(relationship_evidence.timestamp, interactions.occurred_at) per edge.
    Returns number of rows updated.
    """
    if params is None:
        params = _load_params()

    half_life = params["temporal"]["half_life_days"]
    _check_decay_view_sync(half_life)
    now = datetime.now(timezone.utc)

    # last_active + current confidence per edge, in one query
    rows = con.execute(
        """
        SELECT CAST(r.edge_id AS VARCHAR), MAX(re.timestamp), r.confidence
        FROM relationships r
        JOIN relationship_evidence re ON re.edge_id = r.edge_id
        GROUP BY r.edge_id, r.confidence
        """
    ).fetchall()

    # Compute decay in Python, then apply with one set-based UPDATE...FROM join
    # (a per-edge UPDATE loop does not scale to tens of thousands of edges).
    derived = []
    for edge_id_str, max_ts, confidence in rows:
        if max_ts is None:
            continue
        if hasattr(max_ts, "tzinfo") and max_ts.tzinfo is None:
            max_ts = max_ts.replace(tzinfo=timezone.utc)
        decay = decay_score(max_ts, half_life, now)
        temporal_conf = (confidence * decay) if (confidence is not None and decay is not None) else None
        derived.append((edge_id_str, max_ts, decay, temporal_conf))

    if not derived:
        return 0

    con.execute("DROP TABLE IF EXISTS _derive_temporal")
    con.execute(
        """
        CREATE TEMP TABLE _derive_temporal (
            edge_id VARCHAR, last_active TIMESTAMP WITH TIME ZONE,
            decay DOUBLE, temporal_conf DOUBLE
        )
        """
    )
    con.executemany("INSERT INTO _derive_temporal VALUES (?, ?, ?, ?)", derived)
    con.execute(
        """
        UPDATE relationships AS r
        SET last_active = d.last_active,
            relationship_decay_score = d.decay,
            temporal_confidence = d.temporal_conf,
            updated_at = NOW()
        FROM _derive_temporal AS d
        WHERE CAST(r.edge_id AS VARCHAR) = d.edge_id
        """
    )
    con.execute("DROP TABLE IF EXISTS _derive_temporal")
    return len(derived)


def params_hash() -> str:
    """SHA-256 of uncertainty.yaml content — used as derivation_params_hash in pipeline_runs."""
    import hashlib
    content = UNCERTAINTY_PARAMS.read_bytes()
    return hashlib.sha256(content).hexdigest()
