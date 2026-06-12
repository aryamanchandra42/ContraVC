"""
Ontology extraction pipeline orchestrator.

Stages:
1. Structured-deterministic (xlsx rows) — schema-aware, no LLM
2. Heuristic-unstructured (pdf/docx prose) — keyword dicts, no LLM
3. Ontology tagging — normalize terms against ontology_terms table, grow dictionary
4. Optional LLM enrichment — gated by PULSE_LLM_PROVIDER env var (stub in v0)

Every stage is idempotent via the cache. Running twice = same output.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Set

import structlog

_log = structlog.get_logger("pulse.extract")

from agents.ontology.base import (
    OntologyExtractor, ParsedDocument, ExtractionContext, ExtractionResult,
    ExtractedTerm, ExtractedRelationshipHint,
)
from agents.ontology.heuristic import HeuristicExtractor
from agents.ontology.cache import make_cache_key, load_cached, save_cached
from agents.reviews.queue_writer import write_to_queue

ROOT = Path(__file__).resolve().parent.parent.parent


def run_extraction_pipeline(con, run_id: str) -> Dict[str, int]:
    """
    Main pipeline entry point. Called by `pulse extract`.
    Returns counts: {documents_processed, terms_extracted, relationships_hinted, cached_hits}
    """
    docs = list(_load_documents(con))
    total_docs = len(docs)
    _log.info("Extraction loaded documents", total=total_docs)
    extractors = _build_extractor_chain()

    # Load known entity names for co-occurrence scanning.
    # Include both canonical names and alias texts so we catch all surface forms.
    known_entities = _load_known_entities(con)

    terms_extracted = 0
    relationships_hinted = 0
    cached_hits = 0
    docs_processed = 0

    for doc in docs:
        for extractor in extractors:
            ctx = ExtractionContext(
                run_id=run_id,
                extractor_name=extractor.name,
                extractor_version=extractor.version,
                extra={"known_entities": known_entities},
            )
            # Co-occurrence results depend on known_entities which change between runs;
            # only cache deterministic extractors when known_entities list is stable.
            cache_key = make_cache_key(
                extractor.name, extractor.version, doc.content_hash
            )
            result = load_cached(cache_key)
            if result is not None:
                cached_hits += 1
            else:
                try:
                    result = extractor.extract(doc, ctx)
                except NotImplementedError:
                    continue  # LLM stub not implemented; skip
                except Exception as e:
                    continue  # Don't fail the pipeline on extractor errors
                save_cached(cache_key, result)

            _persist_result(con, result)
            terms_extracted += len(result.terms)
            relationships_hinted += len(result.relationship_hints)

        docs_processed += 1
        if docs_processed % 1000 == 0 or docs_processed == total_docs:
            _log.info(
                "Extraction progress",
                processed=docs_processed,
                total=total_docs,
                cached_hits=cached_hits,
            )

    return {
        "documents_processed": docs_processed,
        "terms_extracted": terms_extracted,
        "relationships_hinted": relationships_hinted,
        "cached_hits": cached_hits,
    }


def _load_known_entities(con) -> List[str]:
    """
    Load all canonical allocator names + their aliases from the DB.
    Returns a deduplicated sorted list of strings (shortest-unique surface forms first
    so the regex engine can bail early on long sentences).
    """
    names: set = set()
    try:
        rows = con.execute(
            "SELECT canonical_name FROM allocators WHERE canonical_name IS NOT NULL"
        ).fetchall()
        for (name,) in rows:
            if name and len(name) > 2:
                names.add(name.strip())

        alias_rows = con.execute(
            "SELECT alias_text FROM entity_aliases WHERE alias_text IS NOT NULL"
        ).fetchall()
        for (alias,) in alias_rows:
            if alias and len(alias) > 2:
                names.add(alias.strip())
    except Exception:
        pass
    return sorted(names, key=lambda n: (len(n), n))


def _load_documents(con) -> Iterator[ParsedDocument]:
    """Load all entities_raw rows as ParsedDocuments."""
    rows = con.execute(
        """
        SELECT source_record_id, source_file, source_type, source_offset,
               content_hash, raw_content, ingested_at
        FROM entities_raw
        ORDER BY source_file, source_offset
        """
    ).fetchall()

    for src_id, src_file, src_type, offset, ch, raw_content, ingested_at in rows:
        if isinstance(raw_content, str):
            try:
                raw_content = json.loads(raw_content)
            except Exception:
                raw_content = {"text": str(raw_content)}
        if not isinstance(raw_content, dict):
            raw_content = {"text": str(raw_content)}

        # Build text field
        text = _build_text(raw_content, src_type)

        yield ParsedDocument(
            source_record_id=src_id,
            source_file=src_file,
            source_type=src_type,
            source_offset=offset,
            content_hash=ch,
            raw_content=raw_content,
            text=text,
        )


def _build_text(raw: Dict, source_type: str) -> str:
    """Build primary text from raw_content for heuristic matching."""
    if source_type in ("pdf", "docx"):
        return raw.get("text", "")
    # xlsx: combine all string values
    parts = [str(v) for k, v in raw.items() if not k.startswith("_") and v and str(v).strip()]
    return " | ".join(parts)


def _build_extractor_chain() -> List[OntologyExtractor]:
    """Build the ordered list of extractors based on config.

    When PULSE_LLM_PROVIDER is set to a supported provider, the real LLM extractor
    from agents/research/ontology_enricher.py is appended after the heuristic pass.
    This replaces the NotImplementedError stubs that previously lived in base.py.
    """
    extractors: List[OntologyExtractor] = [
        HeuristicExtractor(),
    ]

    llm_provider = (
        os.getenv("ENRICH_LLM_PROVIDER", "").strip()
        or os.getenv("PULSE_LLM_PROVIDER", "none")
    ).lower()
    if llm_provider in ("anthropic", "openai", "gemini", "groq", "nvidia"):
        try:
            from agents.research.ontology_enricher import get_llm_ontology_extractor
            llm_extractor = get_llm_ontology_extractor()
            if llm_extractor is not None:
                extractors.append(llm_extractor)
        except ImportError:
            # agents/research not yet installed — fall through silently
            pass
    elif llm_provider == "ollama":
        # Ollama still uses the stub (not yet implemented)
        from agents.ontology.base import OllamaExtractor
        extractors.append(OllamaExtractor())

    return extractors


def _resolve_entity_id(con, name: str) -> Optional[str]:
    """Try to resolve an entity name → allocator_id (canonical name first, then alias)."""
    if not name:
        return None
    row = con.execute(
        "SELECT CAST(allocator_id AS VARCHAR) FROM allocators WHERE canonical_name = ?",
        [name],
    ).fetchone()
    if row:
        return row[0]
    alias_row = con.execute(
        "SELECT canonical_id FROM entity_aliases WHERE alias_text = ? AND entity_type = 'allocator' LIMIT 1",
        [name],
    ).fetchone()
    return alias_row[0] if alias_row else None


def _persist_result(con, result: ExtractionResult) -> None:
    """Write extracted terms and relationship hints to the DB."""
    for term in result.terms:
        _upsert_ontology_term(con, term)
        _maybe_queue_for_review(term, result)

    for hint in result.relationship_hints:
        _persist_relationship_hint(con, hint)


def _upsert_ontology_term(con, term: ExtractedTerm) -> None:
    """Upsert an ontology term; increment evidence_count if exists."""
    existing = con.execute(
        "SELECT CAST(term_id AS VARCHAR), evidence_count FROM ontology_terms WHERE term = ? AND category = ?",
        [term.term, term.category],
    ).fetchone()

    now = datetime.now(timezone.utc).isoformat()

    if existing:
        term_id, ev_count = existing
        con.execute(
            """
            UPDATE ontology_terms
            SET evidence_count = evidence_count + 1, last_seen = ?
            WHERE CAST(term_id AS VARCHAR) = ?
            """,
            [now, term_id],
        )
    else:
        con.execute(
            """
            INSERT INTO ontology_terms
                (term_id, term, category, canonical_label, confidence, evidence_count, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            [
                str(uuid.uuid4()), term.term, term.category,
                term.canonical_label, term.confidence, now, now,
            ],
        )


def _maybe_queue_for_review(term: ExtractedTerm, result: ExtractionResult) -> None:
    """Surface low-confidence terms for human review."""
    if term.confidence < 0.60:
        write_to_queue(
            target_type="ontology_terms",
            entity_id=term.term,
            current_value={"term": term.term, "category": term.category, "confidence": term.confidence},
            evidence_pointers=[term.provenance_pointer],
            confidence=term.confidence,
            reason=f"low_confidence_term ({term.confidence:.2f})",
            metadata={"matched_pattern": term.matched_pattern},
        )


def _persist_relationship_hint(con, hint: ExtractedRelationshipHint) -> None:
    """
    Persist a relationship hint as a relationship_evidence row.
    If the edge doesn't exist yet, create it first.

    Source entity resolution order:
    1. hint.source_entity_name by canonical_name (co-occurrence hints carry the name directly)
    2. hint.source_entity_name via entity_aliases
    3. hint.source_record_id via allocators.source_record_id (legacy path)
    """
    if not hint.target_entity_name:
        return

    # --- Resolve target ---
    target_id = _resolve_entity_id(con, hint.target_entity_name)
    if not target_id:
        return

    # --- Resolve source ---
    source_id: Optional[str] = None
    if hint.source_entity_name:
        source_id = _resolve_entity_id(con, hint.source_entity_name)
    if not source_id:
        # Fall back to source_record_id → allocator lookup
        source_row = con.execute(
            "SELECT CAST(allocator_id AS VARCHAR) FROM allocators WHERE source_record_id = ?",
            [hint.source_record_id],
        ).fetchone()
        if source_row:
            source_id = source_row[0]
    if not source_id:
        return

    if source_id == target_id:
        return  # Self-loop; skip

    # Find or create relationship edge
    edge_row = con.execute(
        """
        SELECT CAST(edge_id AS VARCHAR) FROM relationships
        WHERE source_node_id = ? AND target_node_id = ? AND edge_type = ?
        LIMIT 1
        """,
        [source_id, target_id, hint.edge_type],
    ).fetchone()

    if edge_row:
        edge_id = edge_row[0]
    else:
        edge_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        con.execute(
            """
            INSERT INTO relationships
                (edge_id, source_node_id, source_node_type, target_node_id, target_node_type,
                 edge_type, weight, first_seen, last_seen)
            VALUES (?, ?, 'lp', ?, 'lp', ?, 1.0, ?, ?)
            """,
            [edge_id, source_id, target_id, hint.edge_type, now, now],
        )

    # Write evidence row
    con.execute(
        """
        INSERT INTO relationship_evidence
            (evidence_id, edge_id, source_record_id, evidence_type,
             evidence_strength, confidence, provenance_pointer)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            str(uuid.uuid4()), edge_id, hint.source_record_id,
            hint.evidence_type, hint.evidence_strength, hint.confidence,
            json.dumps(hint.provenance_pointer),
        ],
    )
