"""
HeuristicExtractor — deterministic keyword-based ontology extraction.

Driven by prompts/heuristic_keywords.yaml. No LLM dependency.
Same input → byte-identical output. Fully replayable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from agents.ontology.base import (
    OntologyExtractor, ParsedDocument, ExtractionContext, ExtractionResult,
    ExtractedTerm, ExtractedRelationshipHint,
)

ROOT = Path(__file__).resolve().parent.parent.parent
KEYWORDS_PATH = ROOT / "prompts" / "heuristic_keywords.yaml"


class HeuristicExtractor(OntologyExtractor):
    """
    Deterministic keyword extractor. Reads prompts/heuristic_keywords.yaml.
    deterministic=True: same inputs → byte-identical outputs guaranteed.
    """

    name = "heuristic"
    version = "1.0"
    deterministic = True

    def __init__(self, keywords_path: Path = KEYWORDS_PATH) -> None:
        self._keywords_path = keywords_path
        self._keywords: Optional[Dict] = None

    @property
    def keywords(self) -> Dict:
        if self._keywords is None:
            with open(self._keywords_path, encoding="utf-8") as f:
                self._keywords = yaml.safe_load(f)
        return self._keywords

    def extract(self, doc: ParsedDocument, ctx: ExtractionContext) -> ExtractionResult:
        result = ExtractionResult(
            extractor_name=self.name,
            extractor_version=self.version,
            source_record_id=doc.source_record_id,
        )

        # Determine text to search
        text = self._get_searchable_text(doc)
        if not text:
            return result

        # Run keyword matching over all categories
        for category_name, entries in self.keywords.get("categories", {}).items():
            for entry in entries:
                term_result = self._match_entry(entry, text, doc)
                if term_result:
                    result.terms.append(term_result)

        # Extract relationship hints from explicit patterns + entity co-occurrence
        result.relationship_hints.extend(self._extract_relationship_hints(text, doc, ctx))

        return result

    def _get_searchable_text(self, doc: ParsedDocument) -> str:
        """Build searchable text from document."""
        parts = []
        raw = doc.raw_content or {}

        # Direct text fields
        for field in ("text", "notes", "summary", "comments", "description"):
            val = raw.get(field, "")
            if val and isinstance(val, str):
                parts.append(val)

        # For xlsx rows, combine all string values
        if doc.source_type == "xlsx":
            for k, v in raw.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, str) and v.strip():
                    parts.append(v)

        combined = " ".join(parts)
        return combined

    def _match_entry(
        self, entry: Dict, text: str, doc: ParsedDocument
    ) -> Optional[ExtractedTerm]:
        """Return ExtractedTerm if any pattern matches in text."""
        term = entry["term"]
        patterns = entry.get("patterns", [])
        confidence = entry.get("confidence", 0.70)
        canonical_label = entry.get("canonical_label", term)
        category = entry.get("category", "allocator_archetype")

        for pattern in patterns:
            # Case-insensitive whole-word-ish match
            regex = re.compile(r'\b' + re.escape(pattern) + r'\b', re.IGNORECASE)
            if regex.search(text):
                return ExtractedTerm(
                    term=term,
                    category=category,
                    canonical_label=canonical_label,
                    confidence=confidence,
                    evidence_type="heuristic_keyword_match",
                    source_record_id=doc.source_record_id,
                    provenance_pointer={
                        "source_file": doc.source_file,
                        "source_offset": doc.source_offset,
                        "row_id": doc.source_record_id,
                    },
                    matched_pattern=pattern,
                )
        return None

    def _extract_relationship_hints(
        self, text: str, doc: ParsedDocument, ctx=None
    ) -> List[ExtractedRelationshipHint]:
        """
        Extract relationship hints from:
        1. Explicit co-investment / introduction linguistic patterns.
        2. Co-occurrence of two or more known entity names in the same sentence.
           Known entities are passed via ctx.extra['known_entities'] by the pipeline.
        """
        hints: List[ExtractedRelationshipHint] = []

        provenance_base = {
            "source_file": doc.source_file,
            "source_offset": doc.source_offset,
            "row_id": doc.source_record_id,
        }

        # ------------------------------------------------------------------
        # 1. Explicit co-investment patterns
        # ------------------------------------------------------------------
        co_invest_patterns = [
            r"co[- ]invest(?:ed|ing|ment)? (?:with|alongside|in) ([A-Z][A-Za-z0-9 &\.\'-]{2,50}?)(?=\s*[,\.;]|\s+and\b|\s*$)",
            r"invested alongside ([A-Z][A-Za-z0-9 &\.\'-]{2,50}?)(?=\s*[,\.;]|\s+and\b|\s*$)",
            r"backed by ([A-Z][A-Za-z0-9 &\.\'-]{2,50}?)(?=\s*[,\.\(;]|\s+and\b|\s*$)",
        ]
        for pat in co_invest_patterns:
            for match in re.finditer(pat, text, re.IGNORECASE):
                entity_name = match.group(1).strip().rstrip(".,;")
                hints.append(ExtractedRelationshipHint(
                    source_entity_name="",
                    target_entity_name=entity_name,
                    edge_type="co_invested",
                    evidence_type="heuristic_keyword_match",
                    evidence_strength=0.60,
                    confidence=0.60,
                    source_record_id=doc.source_record_id,
                    provenance_pointer={**provenance_base, "matched_text": match.group(0)},
                ))

        # ------------------------------------------------------------------
        # 2. Introduction patterns
        # ------------------------------------------------------------------
        intro_patterns = [
            r"introduced (?:by|through|via) ([A-Z][^\.,\n]{2,60})",
            r"warm intro (?:from|via|through) ([A-Z][^\.,\n]{2,60})",
            r"referred (?:by|through) ([A-Z][^\.,\n]{2,60})",
        ]
        for pat in intro_patterns:
            for match in re.finditer(pat, text, re.IGNORECASE):
                entity_name = match.group(1).strip().rstrip(".,;")
                hints.append(ExtractedRelationshipHint(
                    source_entity_name="",
                    target_entity_name=entity_name,
                    edge_type="introduced_by",
                    evidence_type="heuristic_keyword_match",
                    evidence_strength=0.65,
                    confidence=0.65,
                    source_record_id=doc.source_record_id,
                    provenance_pointer={**provenance_base, "matched_text": match.group(0)},
                ))

        # ------------------------------------------------------------------
        # 3. Known-entity co-occurrence scanning
        #    Emit a "co_mentioned" hint when 2+ known entities appear in the
        #    same sentence.  Strength is deliberately low (0.40) because
        #    co-mention alone is weak evidence; it seeds the graph for later
        #    enrichment.
        # ------------------------------------------------------------------
        known_entities: List[str] = []
        if ctx is not None:
            known_entities = ctx.extra.get("known_entities", [])

        # Co-occurrence is O(sentences × |known_entities|). Skip for xlsx/csv rows
        # (syndicate roster cells); prose sources (pdf/docx) benefit from co_mentioned hints.
        if doc.source_type not in ("pdf", "docx"):
            return hints

        if known_entities and len(text) > 10:
            sentences = re.split(r"(?<=[.!?\n])\s+", text)
            for sentence in sentences:
                if len(sentence) < 10:
                    continue
                found_in_sentence = [
                    name for name in known_entities
                    if re.search(r'\b' + re.escape(name) + r'\b', sentence, re.IGNORECASE)
                ]
                if len(found_in_sentence) < 2:
                    continue
                # Emit one hint per ordered pair (avoid duplicates via sorted order)
                seen_pairs: set = set()
                for i, name_a in enumerate(found_in_sentence):
                    for name_b in found_in_sentence[i + 1:]:
                        pair_key = tuple(sorted([name_a.lower(), name_b.lower()]))
                        if pair_key in seen_pairs:
                            continue
                        seen_pairs.add(pair_key)
                        hints.append(ExtractedRelationshipHint(
                            source_entity_name=name_a,
                            target_entity_name=name_b,
                            edge_type="co_mentioned",
                            evidence_type="heuristic_co_occurrence",
                            evidence_strength=0.40,
                            confidence=0.40,
                            source_record_id=doc.source_record_id,
                            provenance_pointer={
                                **provenance_base,
                                "sentence_snippet": sentence[:120],
                                "entities_found": found_in_sentence,
                            },
                        ))

        return hints
