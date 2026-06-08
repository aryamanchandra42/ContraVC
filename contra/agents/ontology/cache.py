"""
Content-hash-keyed idempotent cache for extractor results.

Cache key = SHA-256 of (extractor_name + extractor_version + content_hash + prompt_hash).
Cache files live at processed_data/ontology_cache/{cache_key}.json.
Re-running the pipeline with the same inputs returns the cached result without re-running.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from agents.ontology.base import ExtractionResult, ExtractedTerm, ExtractedRelationshipHint

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = ROOT / "processed_data" / "ontology_cache"


def make_cache_key(
    extractor_name: str,
    extractor_version: str,
    content_hash: str,
    prompt_hash: str = "none",
) -> str:
    raw = json.dumps({
        "extractor": extractor_name,
        "version": extractor_version,
        "content": content_hash,
        "prompt": prompt_hash,
    }, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def load_cached(cache_key: str) -> Optional[ExtractionResult]:
    """Return cached ExtractionResult if it exists, else None."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{cache_key}.json"
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return _deserialize(data)
    except Exception:
        return None


def save_cached(cache_key: str, result: ExtractionResult) -> None:
    """Serialize and save an ExtractionResult to cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{cache_key}.json"
    data = _serialize(result)
    cache_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _serialize(result: ExtractionResult) -> dict:
    return {
        "extractor_name": result.extractor_name,
        "extractor_version": result.extractor_version,
        "source_record_id": result.source_record_id,
        "terms": [
            {
                "term": t.term,
                "category": t.category,
                "canonical_label": t.canonical_label,
                "confidence": t.confidence,
                "evidence_type": t.evidence_type,
                "source_record_id": t.source_record_id,
                "provenance_pointer": t.provenance_pointer,
                "matched_pattern": t.matched_pattern,
                "notes": t.notes,
            }
            for t in result.terms
        ],
        "relationship_hints": [
            {
                "source_entity_name": h.source_entity_name,
                "target_entity_name": h.target_entity_name,
                "edge_type": h.edge_type,
                "evidence_type": h.evidence_type,
                "evidence_strength": h.evidence_strength,
                "confidence": h.confidence,
                "source_record_id": h.source_record_id,
                "provenance_pointer": h.provenance_pointer,
                "notes": h.notes,
            }
            for h in result.relationship_hints
        ],
        "metadata": result.metadata,
        "extracted_at": result.extracted_at.isoformat(),
    }


def _deserialize(data: dict) -> ExtractionResult:
    result = ExtractionResult(
        extractor_name=data["extractor_name"],
        extractor_version=data["extractor_version"],
        source_record_id=data["source_record_id"],
        metadata=data.get("metadata", {}),
    )
    for t in data.get("terms", []):
        result.terms.append(ExtractedTerm(**t))
    for h in data.get("relationship_hints", []):
        result.relationship_hints.append(ExtractedRelationshipHint(**h))
    return result
