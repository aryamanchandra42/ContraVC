# PULSE Architecture

**Private-market Unified LP Signal Engine — Phase 1–4 Foundation + ICP + Signal Layer**

---

## Doctrine

PULSE operates in private markets characterized by information asymmetry, hidden relationships, fragmented allocator data, and invisible institutional constraints. These conditions require probabilistic reasoning, not deterministic lookup.

Eight invariants govern every design decision:

| # | Invariant | Consequence |
|---|-----------|-------------|
| 1 | Database = Memory. Inference = Intelligence. | Schemas capture provenance and weak signals; scoring lives above storage |
| 2 | Evidence is first-class | Every edge has ≥1 `relationship_evidence` row; signals have `signal_evidence` when written via signal writers |
| 3 | Provenance is non-negotiable | Every row traces to `entities_raw.source_record_id` → source file + byte offset |
| 4 | Soft-truth, not binary truth | `confidence`, `evidence_count`, `contradiction_score`, `source_agreement_score` on every probabilistic table |
| 5 | Time is first-class | `last_active`, `relationship_decay_score`, `temporal_confidence` on all edges and signals |
| 6 | Human-in-the-loop is first-class | `human_reviews` is append-only; overrides applied via `_effective` views, not row mutation |
| 7 | Deterministic-first, LLM-enrichment-second | Full pipeline runs without any LLM; LLMs are optional enrichers |
| 8 | Idempotent and replayable | Same inputs → byte-identical outputs; every stage is safe to re-run |

---

## High-Level Data Flow

```
raw_data/  (xlsx, pdf, docx, linkedin CSV)
    ↓  Ingestion Adapters
entities_raw (append-only, provenance-preserving)
    ↓  Normalization (entity resolver + taxonomies + syndicate/fund normalizers + LinkedIn enricher)
allocators, funds, interactions, investments, entity_aliases
    ↓  Ontology Pipeline (heuristic → optional LLM)
ontology_terms, relationship_evidence
    ↓  Derivation (aggregator + temporal) — relationships AND signals
uncertainty columns on relationships, signals
    ↓  Human Review (append-only + _effective views)
relationships_effective, allocators_effective
    ↓  Graph Builder (NetworkX MultiDiGraph)
         co_invested + invested_with + prospect bridge inference
graphs/pulse.gpickle + edges.parquet + evidence.parquet + pulse.graphml
    ↓  Score (ICP v4.1 + latent signals + contradiction detection)
icp_scores, signals, signal_evidence, rejections
    ↓  Calibrate (ContraVC overlay + grid-search auto-tune)
processed_data/calibration_*.csv/json, LP_Ranked_List.csv
```

---

## Pipeline Stages

### Stage 1: Ingest (`pulse ingest`)

- **Adapters**: xlsx (openpyxl+pandas), pdf (pdfplumber), docx (python-docx), linkedin (Phantombuster exports)
- **Output**: `entities_raw` rows, each with `source_record_id` (deterministic SHA-256 hash)
- **Idempotency**: existing `source_record_id`s are skipped
- **Manifest**: written to `processed_data/` per source file

### Stage 2: Normalize (`pulse normalize`)

- **Entity resolver**: rapidfuzz fuzzy matching across files; conservative thresholds
  - `>= 0.90` → automatic match
  - `[0.70, 0.90)` → written to `review_queues/aliases.jsonl`
- **Taxonomies**: canonical enums for allocator_type, geography, check_size, appetite
- **Normalizers**: allocator, fund, interaction, syndicate — apply taxonomies, preserve provenance
- **LinkedIn enricher**: fuzzy-match Phantombuster CSV rows to existing allocators (`linkedin_enricher.py`)
- **Cross-file matches**: emit `relationship_evidence` rows with `evidence_type='cross_file_match'`

### Stage 3: Extract (`pulse extract`)

- **Layer 1 (Structured-deterministic)**: pandas + regex over xlsx rows
- **Layer 2 (Heuristic-unstructured)**: keyword dicts (`prompts/heuristic_keywords.yaml`) over pdf/docx prose
- **Layer 3 (Ontology tagging)**: normalize extracted terms → `ontology_terms`; low-confidence → review queue
- **Layer 4 (Optional LLM)**: gated by `PULSE_LLM_PROVIDER`; live via `agents/research/ontology_enricher.py`
- **Cache**: `processed_data/ontology_cache/{sha256}.json` — idempotent re-runs

### Stage 4a: Derive (`pulse derive`)

Recomputes uncertainty and temporal columns deterministically from evidence for **relationships and signals**:

| Column | Formula | Location |
|--------|---------|----------|
| `evidence_count` | `COUNT(evidence per entity)` | `aggregator.py` |
| `confidence` | `1 - ∏(1 - sᵢ·cᵢ)` (noisy-OR combinator, configurable) | `aggregator.py` |
| `source_agreement_score` | `agreeing_sources / observing_sources` | `aggregator.py` |
| `contradiction_score` | `contradicting_sources / observing_sources` | `aggregator.py` |
| `last_active` | `max(evidence.timestamp, interactions.occurred_at)` | `temporal.py` |
| `relationship_decay_score` | `exp(-Δt / half_life_days)` | `temporal.py` |
| `temporal_confidence` | `confidence × relationship_decay_score` | `temporal.py` |

All parameters in `prompts/uncertainty.yaml`. `derive_all()` returns counts for both `relationships_updated` and `signals_updated`.

**Note:** `ontology_terms` uncertainty is not yet in derive scope — relationships + signals only.

### Stage 4b: Human Review (`pulse review`)

- **Queue writer**: surfaces uncertain assertions to `processed_data/review_queues/{target_type}.jsonl`
- **Override applier**: reads reviewer decisions from jsonl → appends to `human_reviews`
- **Views**: `relationships_effective`, `allocators_effective`, `ontology_terms_effective` apply overrides at query time
- **Append-only**: `human_reviews` is never updated or deleted; revisions reference prior `review_id` via `supersedes`

### Stage 5: Graph (`pulse graph`)

- Reads from `relationships_effective` (post-review); **raises** if view missing
- Constructs `networkx.MultiDiGraph`
- **Edge writers**: `co_invested` (syndicate SPV overlap), `invested_with` (LP pairs sharing fund vehicles, capped), cross-file corroboration, co_mentioned
- **Prospect inference** (`prospect_inference.py`): 2-hop BFS → `mutual_connection` edges + connectivity signals
- Persists all four formats atomically: gpickle + edges.parquet + evidence.parquet + graphml

### Stage 6: Score (`pulse score`)

- **ICP v4.1** (`icp_scorer.py`): Core gates C1–C4, hard exclusions, weighted soft signals S1–S7
- **C2 strict gate**: emerging-manager appetite requires positive evidence (no longer passes by default)
- **Signal extractor**: heuristic text signals from prospect notes
- **Latent signal extractor**: investment-pattern signals (`coinvest_intensity`, `shared_deal_count`, `recent_activity_recency`, ICP mirrors)
- **Contradiction detector**: emits `contradicts_value` evidence (e.g. deployment_velocity vs recent_activity_recency)
- Writes `icp_scores`, `signals`, `signal_evidence`, `rejections` atomically

### Stage 7: Calibrate (`pulse calibrate`)

- Joins `icp_scores` ↔ `benchmark_rankings` (ContraVC Top 200)
- Fuzzy name join via rapidfuzz (`calibration.py`) for institutional ↔ benchmark overlap
- Grid-search auto-tune on tier thresholds when overlap exists
- Re-exports ranked CSVs with connectivity columns

### Research Agent (`pulse research …`) — optional

- **enrich**: web research → `entities_raw` + COALESCE allocator update
- **ask**: NL → SQL → narrative (read-only)
- **brief**: per-LP outreach brief → `processed_data/briefs/{id}.md`
- **ontology**: LLM enrichment of low-confidence ontology terms

Requires `pip install -e ".[research]"` and API keys. Pipeline runs fully without it.

---

## Signal Layer

### 16 canonical signal types

| Group | Types |
|-------|-------|
| Text / heuristic | `response_speed`, `exploratory_check`, `operator_background`, `em_participation`, `geography_overlap`, `deployment_velocity` |
| Graph / connectivity | `social_proximity`, `network_density`, `bridge_strength`, `warm_path_count` |
| Investment latent | `coinvest_intensity`, `recent_activity_recency`, `shared_deal_count`, `stage_alignment`, `proxy_fund_overlap` |
| ICP mirror | `clean_profile` |

### `signal_evidence` table

Every signal row written through `signal_evidence_writer.py` has ≥1 evidence row:

| `evidence_type` | Source |
|-----------------|--------|
| `signal_heuristic` | Keyword / text extractor |
| `signal_investment_pattern` | Latent extractor from investments |
| `signal_graph_metric` | Graph topology metrics |
| `signal_icp_mirror` | ICP soft-signal mirror |
| `signal_connectivity` | Prospect inference |
| `contradicts_value` | Contradiction detector |

---

## Schema Overview

```
entities_raw           ← append-only, provenance anchor
allocators             ← normalized LP entities (institutional_prospect + syndicate_lp)
funds                  ← normalized fund entities
interactions           ← LP touchpoints
investments            ← LP→Fund records
relationships          ← graph edges (uncertainty + temporal columns derived)
relationship_evidence  ← first-class evidence rows per edge
signals                ← weak signals per allocator (16 types)
signal_evidence        ← first-class evidence rows per signal
rejections             ← stated/inferred/structural rejections
ontology_terms         ← discovered archetypes, EM signals, rejection patterns
entity_aliases         ← fuzzy resolution alias mappings
human_reviews          ← append-only override log
icp_scores             ← ICP v4.1 tier + fit score per allocator
benchmark_rankings     ← ContraVC Top 200 external benchmark
pipeline_runs          ← run tracking per stage
```

Views (applied at query time):
- `relationships_effective` — applies human review overrides to edges
- `allocators_effective` — applies archetype overrides
- `ontology_terms_effective` — applies label overrides
- `relationship_decay_view` — materializes exp-decay computation
- `evidence_summary` — aggregates evidence per edge
- `calibration_overlay` — joins icp_scores ↔ benchmark_rankings

---

## File Structure

```
raw_data/              immutable source files + manifest.json
processed_data/
  review_queues/       jsonl queues per target_type
  ontology_cache/      content-hash-keyed extractor results
  research_cache/      web search cache
schema/
  models.py            Pydantic v2 models
  duckdb.sql           local DDL
  postgres.sql         Supabase-portable DDL
  views.sql            derived views
agents/
  db.py                shared DuckDB connection + bootstrap + migrations
  ingestion/           adapter framework + linkedin_csv_adapter
  normalization/       entity resolver, taxonomies, linkedin_enricher
  ontology/            layered extractor pipeline
  uncertainty/         deterministic derivation (relationships + signals)
  reviews/             append-only review infra
  graph/               NetworkX builder, invested_with_edges, prospect_inference
  scoring/             ICP, calibration, signals, contradictions
  research/            optional LLM + web enrichment
pulse/                 typer CLI
prompts/               keywords, uncertainty, ICP calibration, graph inference, linkedin export
notebooks/             substrate audit, resolution, graph topology, review queue
graphs/                serialized graph artifacts
evals/                 run_evals.py + backtest_signals.py
docs/                  architecture, ontology dictionary, decision archive, GP brief
.cursor/rules/         persistent agent doctrine rules
```

---

## Phase Roadmap

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Ingestion (xlsx, pdf, docx, linkedin CSV) | **Complete** |
| 2 | Normalization (entity resolver, syndicate, funds, LinkedIn enricher) | **Complete** |
| 3 | Ontology extraction (heuristic + optional LLM) | **Complete** |
| 4 | Relationship graph (NetworkX, 4-format persist) | **Complete** |
| 4b | ICP Scoring v4.1 | **Complete** |
| 4c | Calibration overlay (ContraVC benchmark) | **Wired** — auto-tune blocked on population overlap |
| 4d | Syndicate connectivity inference | **Complete** |
| 4e | Signal layer expansion (16 types + `signal_evidence`) | **Complete** |
| 4f | Contradiction detection, LinkedIn ingestion, calibration fuzzy join | **Complete** |
| 5 | Probabilistic allocator scoring (ML) | Planned |
| 6 | Inference engine (archetype probs, conversion probs) | Planned |
| 7 | Automation / scheduling | Planned |

Phase 5–6 will read from the substrate built in Phases 1–4f. The JSON scoring columns (`allocators.inferred_scores`, `allocators.confidences`) remain null until Phase 5.

---

## Live Snapshot (2026-06-06)

Run `python -m pulse status --verbose` for current counts. Last verified:

| Metric | Value |
|--------|-------|
| `entities_raw` | 24,354 |
| Relationships | 28,941 |
| `relationship_evidence` | 60,645 |
| `invested_with` edges | 167 |
| `mutual_connection` edges | 50 |
| Signals | 11,202 |
| `signal_evidence` rows | 11,204 |
| ICP scores | 443 (institutional tier_1 = 109) |
| Evals | 19/19 pass (2026-06-06) |

See [SYSTEM_STATE.md](../SYSTEM_STATE.md) for the full operational snapshot and [reading_guide.md](reading_guide.md) for where to start reading.
