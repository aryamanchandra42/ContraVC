"""
PULSE Research Agent — agents/research/

Provides four capabilities on top of the PULSE graph + DB:
  1. Enrichment    — web-research unknown allocator fields (type, geo, appetite)
  2. Q&A           — natural-language analyst queries via text-to-SQL
  3. Briefs        — per-LP outreach brief with warm-path + talking points
  4. Ontology      — LLM-backed enrichment of low-confidence ontology terms

Design invariants:
  - Deterministic-first: web fetch + search are deterministic/cached; LLM is
    used only for structured Pydantic extraction (instructor).
  - Provenance: every external fact is written to entities_raw first.
  - Append-only: enrichment fills NULLs only; uncertain facts go to review queue.
  - Graceful degradation: all agents work (in local-only mode) without API keys.
"""
