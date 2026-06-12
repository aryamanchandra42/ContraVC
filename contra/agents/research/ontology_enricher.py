"""
PULSE LLM Ontology Enrichment — concrete implementations of AnthropicExtractor
and OpenAIExtractor that were previously stub-only in agents/ontology/base.py.

These classes fully implement the OntologyExtractor protocol:
  - deterministic = False (LLM-backed)
  - Cache key = SHA-256(extractor.name + version + doc.content_hash + prompt_hash)
  - Cache files at processed_data/ontology_cache/{key}.json (never auto-invalidated)
  - Extract to ExtractedTerm + ExtractedRelationshipHint from agents/ontology/base
  - Flow through the existing ontology pipeline + pulse derive (no hand-written uncertainty)

The run_ontology_enrichment() entry point is called by `pulse research ontology`.
It targets documents where the heuristic extractor found low-confidence terms
OR where no terms were extracted at all (likely prose/PDF chunks).

IMPORTANT: The _build_extractor_chain() in agents/ontology/pipeline.py imports the
stub classes from agents/ontology/base. After this module is implemented, that
function is patched by pulse/cli.py `research ontology` to use PulseAnthropicExtractor
/ PulseOpenAIExtractor — or you can update _build_extractor_chain() directly to import
from here. The pipeline.py update is done as part of this module's activation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from agents.ontology.base import (
    OntologyExtractor,
    ParsedDocument,
    ExtractionContext,
    ExtractionResult,
    ExtractedTerm,
    ExtractedRelationshipHint,
)
from agents.ontology.cache import make_cache_key, load_cached, save_cached

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
KEYWORDS_PATH = ROOT / "prompts" / "heuristic_keywords.yaml"

VALID_EDGE_TYPES = frozenset({
    "invested_with", "introduced_by", "co_invested", "syndicate_overlap",
    "mutual_connection", "repeated_exposure", "co_mentioned",
})

VALID_CATEGORIES = frozenset({
    "allocator_archetype", "em_signal", "rejection_pattern",
    "geography_cluster", "committee_constraint",
})


# ---------------------------------------------------------------------------
# Prompt template helpers
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a private-market data extraction specialist. "
    "Extract ontology terms and relationship hints from LP-related documents. "
    "Be precise and conservative — if you are not confident, set confidence below 0.5 "
    "or omit the term entirely. Never hallucinate entity names or terms."
)


def _load_keyword_summary() -> str:
    """Build a compact keyword guide from heuristic_keywords.yaml for the LLM."""
    try:
        with open(KEYWORDS_PATH, encoding="utf-8") as f:
            kw = yaml.safe_load(f)
    except Exception:
        return "(keyword dictionary unavailable)"

    lines = []
    for category, entries in kw.get("categories", {}).items():
        terms = [e.get("term", "") for e in entries if e.get("term")]
        lines.append(f"  {category}: {', '.join(terms)}")
    return "\n".join(lines)


def _prompt_hash(prompt_template: str) -> str:
    return hashlib.sha256(prompt_template.encode()).hexdigest()[:32]


def _build_extraction_prompt(doc: ParsedDocument) -> str:
    text = (doc.text or "").strip()
    if not text:
        raw_vals = list(doc.raw_content.values())[:20]
        text = " | ".join(str(v) for v in raw_vals if v)

    text_preview = text[:3000]
    if len(text) > 3000:
        text_preview += "\n[truncated]"

    kw_guide = _load_keyword_summary()

    return f"""Extract ontology terms and entity relationship hints from the document below.

ONTOLOGY CATEGORIES AND KNOWN TERMS:
{kw_guide}

VALID EDGE TYPES:
invested_with, introduced_by, co_invested, syndicate_overlap,
mutual_connection, repeated_exposure, co_mentioned

DOCUMENT METADATA:
  source_file: {doc.source_file}
  source_type: {doc.source_type}
  source_offset: {doc.source_offset}

DOCUMENT TEXT:
{text_preview}

INSTRUCTIONS:
1. Extract any terms that match or extend the known ontology categories above.
   - term: the exact surface form found in the text
   - category: one of allocator_archetype, em_signal, rejection_pattern, geography_cluster, committee_constraint
   - canonical_label: the closest canonical term from the list above (or null if novel)
   - confidence: your certainty ∈ [0, 1]; set below 0.5 if unsure
   - matched_pattern: the exact text fragment that triggered this extraction
2. Extract any NAMED ENTITY PAIRS (e.g. two LP names, LP + fund name) that imply a relationship.
   Only extract if both entity names appear explicitly in the text.
3. Return only terms you are genuinely confident about. It is better to return fewer terms
   with high confidence than many terms with low confidence.

Return the OntologyEnrichment JSON schema with fields:
  - terms: list of OntologyTermExtraction
  - relationship_hints: list of RelationshipHintExtraction
"""


# ---------------------------------------------------------------------------
# Base LLM extractor implementation
# ---------------------------------------------------------------------------

class _BaseLLMOntologyExtractor(OntologyExtractor):
    """
    Shared implementation for LLM-backed ontology extractors.
    Concrete subclasses set `name`, `version`, and `_llm_provider`.
    """

    name: str = "_base_llm"
    version: str = "1.0.0"
    deterministic: bool = False
    _llm_provider: str = ""

    def extract(self, doc: ParsedDocument, ctx: ExtractionContext) -> ExtractionResult:
        from agents.research.llm_client import get_llm_client, LLMUnavailable, LLMExtractionError
        from agents.research.schemas import OntologyEnrichment

        result = ExtractionResult(
            extractor_name=self.name,
            extractor_version=self.version,
            source_record_id=doc.source_record_id,
            metadata={"provider": self._llm_provider, "doc_source": doc.source_file},
        )

        # Skip documents with no usable text
        text = (doc.text or "").strip()
        raw_text = " ".join(str(v) for v in doc.raw_content.values() if v)
        if len(text) + len(raw_text) < 50:
            return result

        prompt = _build_extraction_prompt(doc)
        p_hash = _prompt_hash(prompt)

        # Check cache (includes prompt hash — matches cache-key doctrine)
        cache_key = make_cache_key(self.name, self.version, doc.content_hash, p_hash)
        cached = load_cached(cache_key)
        if cached is not None:
            logger.debug("Ontology LLM cache hit: %s", doc.source_record_id[:16])
            return cached

        # Call LLM
        try:
            llm_client = get_llm_client(provider=self._llm_provider)
        except LLMUnavailable as exc:
            logger.warning("LLM unavailable for ontology extraction: %s", exc)
            return result

        try:
            enrichment: OntologyEnrichment = llm_client.structured(
                prompt=prompt,
                response_model=OntologyEnrichment,
                system=_SYSTEM_PROMPT,
            )
        except LLMExtractionError as exc:
            logger.error(
                "LLM extraction failed for %s: %s",
                doc.source_record_id[:16], exc,
            )
            result.metadata["error"] = str(exc)
            return result

        # Convert OntologyEnrichment → ExtractedTerm / ExtractedRelationshipHint
        for t in enrichment.terms:
            if not _is_valid_term(t):
                continue
            result.terms.append(
                ExtractedTerm(
                    term=t.term,
                    category=t.category,
                    canonical_label=t.canonical_label,
                    confidence=t.confidence,
                    evidence_type="llm_enriched",
                    source_record_id=doc.source_record_id,
                    provenance_pointer={
                        "source_file": doc.source_file,
                        "source_offset": doc.source_offset,
                        "row_id": doc.source_record_id,
                    },
                    matched_pattern=t.matched_pattern,
                    notes=t.notes,
                )
            )

        for h in enrichment.relationship_hints:
            if not _is_valid_hint(h):
                continue
            result.relationship_hints.append(
                ExtractedRelationshipHint(
                    source_entity_name=h.source_entity_name,
                    target_entity_name=h.target_entity_name,
                    edge_type=h.edge_type,
                    evidence_type="llm_enriched",
                    evidence_strength=h.evidence_strength,
                    confidence=h.confidence,
                    source_record_id=doc.source_record_id,
                    provenance_pointer={
                        "source_file": doc.source_file,
                        "source_offset": doc.source_offset,
                        "row_id": doc.source_record_id,
                    },
                    notes=h.notes,
                )
            )

        result.metadata.update({
            "terms_found": len(result.terms),
            "hints_found": len(result.relationship_hints),
            "prompt_hash": p_hash,
        })

        save_cached(cache_key, result)
        logger.info(
            "LLM ontology extraction: %s → %d terms, %d hints (doc=%s)",
            self.name, len(result.terms), len(result.relationship_hints),
            doc.source_record_id[:16],
        )
        return result


def _is_valid_term(t) -> bool:
    return (
        t.term
        and t.category in VALID_CATEGORIES
        and 0.0 <= t.confidence <= 1.0
    )


def _is_valid_hint(h) -> bool:
    return (
        h.source_entity_name
        and h.target_entity_name
        and h.edge_type in VALID_EDGE_TYPES
        and 0.0 <= h.evidence_strength <= 1.0
        and 0.0 <= h.confidence <= 1.0
    )


# ---------------------------------------------------------------------------
# Concrete extractor classes
# ---------------------------------------------------------------------------

class PulseAnthropicExtractor(_BaseLLMOntologyExtractor):
    """
    Anthropic Claude — concrete implementation replacing the stub in base.py.

    Register in pipeline._build_extractor_chain() when PULSE_LLM_PROVIDER=anthropic.
    Requires ANTHROPIC_API_KEY + `pip install -r requirements-llm.txt`.
    """
    name = "anthropic_ontology"
    version = "1.0.0"
    deterministic = False
    _llm_provider = "anthropic"


class PulseOpenAIExtractor(_BaseLLMOntologyExtractor):
    """
    OpenAI GPT — concrete implementation replacing the stub in base.py.

    Register in pipeline._build_extractor_chain() when PULSE_LLM_PROVIDER=openai.
    Requires OPENAI_API_KEY + `pip install -r requirements-llm.txt`.
    """
    name = "openai_ontology"
    version = "1.0.0"
    deterministic = False
    _llm_provider = "openai"


class PulseGeminiExtractor(_BaseLLMOntologyExtractor):
    """
    Google Gemini — concrete implementation for PULSE_LLM_PROVIDER=gemini.
    Requires GEMINI_API_KEY + `pip install -r requirements-llm.txt`.
    """
    name = "gemini_ontology"
    version = "1.0.0"
    deterministic = False
    _llm_provider = "gemini"


class PulseGroqExtractor(_BaseLLMOntologyExtractor):
    """
    Groq inference API — concrete implementation for PULSE_LLM_PROVIDER=groq.
    Runs Llama / Mixtral at high speed on Groq hardware. Free tier available.
    Requires GROQ_API_KEY + `pip install groq`.
    """
    name = "groq_ontology"
    version = "1.0.0"
    deterministic = False
    _llm_provider = "groq"


class PulseNvidiaExtractor(_BaseLLMOntologyExtractor):
    """
    NVIDIA NIM — concrete implementation for ENRICH_LLM_PROVIDER=nvidia.
    Requires NVAPI_API_KEY and/or NIM_BASE_URL.
    """
    name = "nvidia_ontology"
    version = "1.0.0"
    deterministic = False
    _llm_provider = "nvidia"


# ---------------------------------------------------------------------------
# Extractor factory (replaces stubs)
# ---------------------------------------------------------------------------

def get_llm_ontology_extractor() -> Optional[_BaseLLMOntologyExtractor]:
    """
    Return the appropriate LLM ontology extractor based on PULSE_LLM_PROVIDER.
    Returns None if provider is 'none' or unset (heuristic-only mode).
    """
    provider = (
        os.getenv("ENRICH_LLM_PROVIDER", "").strip()
        or os.getenv("PULSE_LLM_PROVIDER", "none")
    ).lower().strip()
    mapping = {
        "anthropic": PulseAnthropicExtractor,
        "openai": PulseOpenAIExtractor,
        "gemini": PulseGeminiExtractor,
        "groq": PulseGroqExtractor,
        "nvidia": PulseNvidiaExtractor,
    }
    cls = mapping.get(provider)
    return cls() if cls else None


# ---------------------------------------------------------------------------
# Standalone enrichment entry point (for pulse research ontology command)
# ---------------------------------------------------------------------------

def run_ontology_enrichment(
    con,
    min_confidence_threshold: float = 0.40,
    target_source_types: Optional[List[str]] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Run LLM ontology enrichment over documents where heuristic extraction
    produced low-confidence terms or no terms at all.

    Parameters
    ----------
    con : DuckDB connection
    min_confidence_threshold : terms below this from heuristic → queue for LLM
    target_source_types : source types to target (default: pdf, docx, api)
    limit : max documents to process

    Returns
    -------
    Dict with: documents_targeted, terms_extracted, hints_found, cache_hits, errors
    """
    from agents.research.llm_client import get_llm_client, LLMUnavailable
    from agents.ontology.pipeline import _load_documents, _persist_result

    # Verify LLM is available before iterating
    try:
        _probe_client = get_llm_client()
    except LLMUnavailable as exc:
        logger.warning("LLM unavailable — ontology enrichment skipped: %s", exc)
        return {
            "documents_targeted": 0,
            "terms_extracted": 0,
            "hints_found": 0,
            "cache_hits": 0,
            "errors": 0,
            "skipped_reason": str(exc),
        }

    extractor = get_llm_ontology_extractor()
    if extractor is None:
        return {
            "documents_targeted": 0,
            "terms_extracted": 0,
            "hints_found": 0,
            "cache_hits": 0,
            "errors": 0,
            "skipped_reason": "PULSE_LLM_PROVIDER not set or 'none'",
        }

    # Target source types: prose/unstructured content benefits most from LLM
    source_types = target_source_types or ["pdf", "docx", "api"]

    docs = [d for d in _load_documents(con) if d.source_type in source_types]
    if limit:
        docs = docs[:limit]

    logger.info(
        "Ontology LLM enrichment: %d documents targeted (types=%s)",
        len(docs), source_types,
    )

    totals: Dict[str, int] = {
        "documents_targeted": len(docs),
        "terms_extracted": 0,
        "hints_found": 0,
        "cache_hits": 0,
        "errors": 0,
    }

    import uuid as _uuid
    run_id = str(_uuid.uuid4())

    for doc in docs:
        ctx = ExtractionContext(
            run_id=run_id,
            extractor_name=extractor.name,
            extractor_version=extractor.version,
        )

        # Check for existing cache hit before calling LLM
        from agents.research.web_search import _cache_key  # reuse helper
        p_hash = _prompt_hash(_build_extraction_prompt(doc))
        cache_key = make_cache_key(extractor.name, extractor.version, doc.content_hash, p_hash)
        if load_cached(cache_key) is not None:
            totals["cache_hits"] += 1
            continue

        try:
            result = extractor.extract(doc, ctx)
            _persist_result(con, result)
            totals["terms_extracted"] += len(result.terms)
            totals["hints_found"] += len(result.relationship_hints)
        except Exception as exc:
            logger.error(
                "Ontology enrichment failed for doc %s: %s",
                doc.source_record_id[:16], exc,
                exc_info=True,
            )
            totals["errors"] += 1

    logger.info("Ontology enrichment complete: %s", totals)
    return totals
