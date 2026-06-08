"""Computed CRM lead ranking score (0–100)."""

from __future__ import annotations

from typing import Optional

_CONFIDENCE_MAP = {"high": 1.0, "medium": 0.6, "low": 0.3}


def _norm_fit(fit_score: Optional[float]) -> float:
    if fit_score is None:
        return 0.0
    if fit_score > 1.0:
        return min(fit_score / 100.0, 1.0)
    return min(max(fit_score, 0.0), 1.0)


def compute_score(
    *,
    fit_score: Optional[float] = None,
    gate_confidence: Optional[str] = None,
    warm_path_count: int = 0,
    contra_rank: Optional[int] = None,
    syndicate_score: Optional[float] = None,
) -> float:
    """
    Weighted score for CRM lead ranking.

    fit_score 35%, gate confidence 20%, warm paths 15%, contra rank 15%, syndicate 15%.
    """
    fit_component = _norm_fit(fit_score) * 35.0

    conf = _CONFIDENCE_MAP.get((gate_confidence or "").lower(), 0.0)
    gate_component = conf * 20.0

    warm_component = min(max(warm_path_count, 0), 5) / 5.0 * 15.0

    rank_component = 0.0
    if contra_rank is not None and 1 <= contra_rank <= 200:
        rank_component = (201 - contra_rank) / 200.0 * 15.0

    syn = syndicate_score or 0.0
    if syn > 1.0:
        syn = min(syn / 100.0, 1.0)
    syndicate_component = min(max(syn, 0.0), 1.0) * 15.0

    return round(
        fit_component + gate_component + warm_component + rank_component + syndicate_component,
        2,
    )
