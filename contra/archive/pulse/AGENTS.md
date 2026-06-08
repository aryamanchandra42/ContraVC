# PULSE — Agent Operating Doctrine

**PULSE** (Private-market Unified LP Signal Engine) is an institutional intelligence system for private-market allocator inference and fundraising alpha discovery.

## What PULSE Is

- A probabilistic reasoning system
- A relationship graph engine
- An institutional memory system
- A weak-signal inference engine
- A capital-network intelligence platform

## What PULSE Is NOT

- A CRM or spreadsheet wrapper
- A chatbot or generic SaaS dashboard
- A CRUD application

---

## Core Architectural Invariants

Every agent operating in this repo MUST honour these invariants. Violating them invalidates the institutional memory:

### 1. Database = Memory. Inference = Intelligence.
Schemas capture provenance and weak signals as first-class data. Scoring lives in a layer above storage. Never conflate.

### 2. Evidence Is First-Class
Every relationship edge MUST be backed by ≥1 row in `relationship_evidence`. An edge with zero evidence rows is a schema violation. Signals emitted through the scoring layer MUST have matching `signal_evidence` rows when written via `signal_evidence_writer`.

### 3. Provenance Is Non-Negotiable
Every normalized row carries `source_file`, `source_offset`, `content_hash`, `ingested_at`, and a foreign key back to `entities_raw`. Never write a canonical row without provenance.

### 4. Soft-Truth, Not Binary Truth
Every probabilistic assertion carries `confidence`, `evidence_count`, `contradiction_score`, `source_agreement_score`. These are derived deterministically from evidence — counts and ratios — never from ML or hand-written values.

### 5. Time Is First-Class
Relationships and signals carry `effective_date`, `first_seen`, `last_seen`, `last_active`, `relationship_decay_score`, `temporal_confidence`. The decay is a parameterized exp-decay function defined in `prompts/uncertainty.yaml`. It is NOT learned.

### 6. Human-in-the-Loop Is First-Class
Reviewer overrides live in the append-only `human_reviews` table. Normalized rows are NEVER mutated by a review. Effective state is exposed through `_effective` SQL views. Revisions are new `human_reviews` rows with a `supersedes` pointer.

### 7. Deterministic-First, LLM-Enrichment-Second
The full pipeline MUST run without any LLM. LLM extractors are pluggable enrichers, not source of truth. The heuristic extractor is the default v0.

### 8. Idempotent and Replayable
Every pipeline stage must be safe to re-run. Same inputs → byte-identical outputs. Content hashes are the idempotency keys.

---

## Repository Map

```
raw_data/           immutable source files (never edit)
processed_data/     generated artifacts (gitignored)
  review_queues/    jsonl queues per target_type for human review
  ontology_cache/   content-hash-keyed extractor results
  research_cache/   web search cache for research agent
schema/             pydantic models + DuckDB DDL + Postgres DDL + views
agents/
  ingestion/        pluggable adapters (xlsx, pdf, docx, linkedin)
  normalization/    entity resolver, taxonomies, syndicate/fund normalizers, linkedin enricher
  ontology/         layered extractor pipeline + cache
  uncertainty/      deterministic derivation (evidence counts, decay, views)
  reviews/          append-only review queue writer + override applier
  graph/            NetworkX builder, edge writers, prospect inference, invested_with
  scoring/          ICP v4.1, calibration, signal extractors, latent signals, contradictions
  research/         optional LLM + web enrichment (enrich, Q&A, brief, ontology)
pulse/              typer CLI entrypoint
prompts/            heuristic keywords, uncertainty, ICP calibration, graph inference, linkedin export
notebooks/          substrate audit, resolution walkthrough, graph topology, review queue
graphs/             serialized graph artifacts
evals/              invariant checks + gold-set extraction + signal backtests
docs/               architecture, ontology dictionary, decision archive, GP brief, reading guide
logs/               structured run logs
```

## CLI Quick Reference

```
pulse refresh         — full pipeline + exports (same as UI "Refresh PULSE" button)
pulse ingest          — ingest all adapters → entities_raw
pulse normalize       — entity resolution + taxonomy mapping + LinkedIn enricher
pulse extract         — ontology extraction pipeline
pulse derive          — recompute uncertainty + temporal derivations (relationships + signals)
pulse graph           — build graph (co_invested, invested_with, prospect inference) + persist
pulse score           — ICP v4.1 + latent signals + contradiction detection
pulse calibrate       — ContraVC overlay + grid-search auto-tune
pulse run-all         — full pipeline (ingest→normalize→extract→derive→graph→score→calibrate)
pulse status          — last run summary per stage (--verbose for live DB snapshot)
pulse review list     — show pending review queue items
pulse review ingest   — append reviewer decisions
pulse review status   — queue counts by target_type
pulse research enrich | ask | brief | ontology  — optional LLM + web research
pulse explore         — local read-only LP viewer (Streamlit; pip install -e ".[explore]")
```

**`run-all` stage order:** `ingest → normalize → extract → derive → graph → score → calibrate`

Graph runs before score so network signals (`bridge_strength`, `warm_path_count`, `network_density`, `social_proximity`) feed ICP scoring.

## Signal Layer (Phase 4b–4f)

**16 canonical signal types** (`agents/scoring/signal_types.py`):

Original 8: `response_speed`, `exploratory_check`, `operator_background`, `em_participation`, `geography_overlap`, `social_proximity`, `network_density`, `deployment_velocity`

Expansion 8: `bridge_strength`, `warm_path_count`, `coinvest_intensity`, `recent_activity_recency`, `stage_alignment`, `proxy_fund_overlap`, `clean_profile`, `shared_deal_count`

**Writers:**
- `signal_extractor.py` — heuristic / ICP text signals at score stage
- `latent_signal_extractor.py` — investment-pattern + ICP S5–S7 mirror signals
- `prospect_inference.py` — graph connectivity signals
- `contradiction_detector.py` — `contradicts_value` evidence in `signal_evidence`

**Uncertainty:** `pulse derive` runs `derive_signal_uncertainty()` alongside relationship derivation.

## Evals

```
python evals/run_evals.py          — schema invariants, evidence-per-edge, graph persist sync
python evals/backtest_signals.py   — signal coverage, tier discrimination, connectivity lift, C2 strict gate
```

## Do NOT

- Write ML scoring logic beyond deterministic ICP v4.1 in this phase (Phase 5-6 reserved)
- Mutate rows in `relationships`, `allocators`, `ontology_terms`, etc. via human review — use the append-only `human_reviews` table instead
- Hard-code any LLM dependency — keep the full pipeline runnable without LLM access
- Store confidence/decay/agreement values by hand — only `pulse derive` writes them
- Add production UI, API servers, or frontend code beyond the local `pulse explore` viewer
