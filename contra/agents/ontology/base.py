"""
Ontology extractor Protocol and shared data structures.

All extractors implement OntologyExtractor.
Concrete LLM extractors (Ollama, OpenAI, Anthropic) are stubs in v0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ParsedDocument:
    """A parsed chunk/record ready for ontology extraction."""
    source_record_id: str
    source_file: str
    source_type: str           # xlsx | pdf | docx
    source_offset: str
    content_hash: str
    raw_content: Dict[str, Any]
    text: Optional[str] = None  # primary text for heuristic/LLM extraction
    ingested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ExtractionContext:
    """Context passed to every extractor alongside a ParsedDocument."""
    run_id: str
    extractor_name: str
    extractor_version: str
    keyword_version: str = "1.0"
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedTerm:
    """A single ontology term extracted from a document."""
    term: str
    category: str              # allocator_archetype | em_signal | rejection_pattern | geography_cluster | committee_constraint
    canonical_label: Optional[str]
    confidence: float
    evidence_type: str         # structured_xlsx_match | heuristic_keyword_match | llm_enriched
    source_record_id: str
    provenance_pointer: Dict[str, Any]
    matched_pattern: Optional[str] = None
    notes: Optional[str] = None


@dataclass
class ExtractedRelationshipHint:
    """A hint that an edge may exist between two entities — used to emit relationship_evidence."""
    source_entity_name: str
    target_entity_name: str
    edge_type: str             # must be in VALID_EDGE_TYPES
    evidence_type: str
    evidence_strength: float
    confidence: float
    source_record_id: str
    provenance_pointer: Dict[str, Any]
    notes: Optional[str] = None


@dataclass
class ExtractionResult:
    """Output of one extractor run on one document."""
    extractor_name: str
    extractor_version: str
    source_record_id: str
    terms: List[ExtractedTerm] = field(default_factory=list)
    relationship_hints: List[ExtractedRelationshipHint] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    extracted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class OntologyExtractor:
    """
    Base class (duck-typed Protocol) for all ontology extractors.
    Subclass and override extract().
    """
    name: str = "base"
    version: str = "0.0.0"
    deterministic: bool = True

    def extract(self, doc: ParsedDocument, ctx: ExtractionContext) -> ExtractionResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# LLM extractor stubs — interface-complete, no implementation in v0
# ---------------------------------------------------------------------------

class OllamaExtractor(OntologyExtractor):
    """
    Stub — local LLM via Ollama. Not implemented in Phase 1-4.
    Set PULSE_LLM_PROVIDER=ollama and implement this class to activate.
    """
    name = "ollama"
    version = "0.1.0"
    deterministic = False

    def extract(self, doc: ParsedDocument, ctx: ExtractionContext) -> ExtractionResult:
        raise NotImplementedError(
            "OllamaExtractor not implemented in Phase 1-4. "
            "Set PULSE_LLM_PROVIDER=ollama and implement this extractor to activate LLM enrichment."
        )


class OpenAIExtractor(OntologyExtractor):
    """
    Stub — OpenAI API. Not implemented in Phase 1-4.
    Set PULSE_LLM_PROVIDER=openai and OPENAI_API_KEY to activate.
    """
    name = "openai"
    version = "0.1.0"
    deterministic = False

    def extract(self, doc: ParsedDocument, ctx: ExtractionContext) -> ExtractionResult:
        raise NotImplementedError(
            "OpenAIExtractor not implemented in Phase 1-4. "
            "Set PULSE_LLM_PROVIDER=openai and OPENAI_API_KEY to activate."
        )


class AnthropicExtractor(OntologyExtractor):
    """
    Stub — Anthropic API. Not implemented in Phase 1-4.
    Set PULSE_LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY to activate.
    """
    name = "anthropic"
    version = "0.1.0"
    deterministic = False

    def extract(self, doc: ParsedDocument, ctx: ExtractionContext) -> ExtractionResult:
        raise NotImplementedError(
            "AnthropicExtractor not implemented in Phase 1-4. "
            "Set PULSE_LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY to activate."
        )
