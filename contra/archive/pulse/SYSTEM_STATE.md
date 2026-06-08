# PULSE — SYSTEM_STATE

**Milestone:** Phase 1–4 Complete + ICP Scoring v4.1 + Syndicate Integration + Fund Normalizer + **Signal Layer (16 types)** + Contradiction Detection + LinkedIn Ingestion + Calibration Layer + Research Agent v1.0
**Generated:** 2026-06-06 (live DB snapshot, read-only query)
**Last pipeline runs:** graph (14:22, completed) · derive (13:13, completed) · score (13:13, **failed** — DB lock / permission denied; last successful score 12:50)
**Schema version:** 1.3 (+ `signal_evidence`; expanded `signals` types; `invested_with` edge writer)
**Research agent version:** 1.0 (`agents/research/` — enrich, Q&A, brief, ontology)
**Ontology dictionary version:** 1.0
**Uncertainty params version:** 1.0 (`prompts/uncertainty.yaml`)
**ICP scorer version:** 4.1 (`agents/scoring/icp_spec.py`) — **C2 strict gate active**

> Regenerate this file after every major milestone. It is the single operational snapshot for agents and humans entering the repo cold.
> Run `python -m pulse status --verbose` and `python -m pulse review status` for live counts (Windows: `pulse` may not be on PATH).

---

## Viewing data (LP Explorer)

| Method | When to use |
|--------|-------------|
| **`pulse explore`** | Interactive view on live `pulse.duckdb`: outreach, funnel, detail, graph. Read-only. |
| **`processed_data/*.csv`** | Static exports for mail merge (`First_LPs_Ready.csv`, `First_LPs_Outreach_Pack.csv`, `LP_Ranked_List.csv`) — may be stale vs DB. |
| **`notebooks/`** | Review queue, graph topology, substrate audit workflows. |
| **`pulse status -v`** | Quick row counts and invariant checks in the terminal. |
| **`docs/pulse_for_general_partners.md`** | GP-facing qualitative brief (no engineering jargon). |
| **`docs/reading_guide.md`** | Curated reading map by role (partners, engineers, operators). |

**Setup (one-time):** `pip install -e ".[explore]"` then `pulse explore` → http://localhost:8501

While the explorer is open, avoid running write pipeline stages on the same DB file (or close Streamlit first).

---

## 1. Current Architecture

### What PULSE Is

Private-market Unified LP Signal Engine — institutional intelligence for allocator inference and fundraising alpha discovery. Probabilistic reasoning system + relationship graph + institutional memory + weak-signal inference. **Not** a CRM, spreadsheet wrapper, or chatbot.

### Phase Status

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Ingestion (6 source files + **LinkedIn CSV**) | **Complete** |
| 2 | Normalization (entity resolver, taxonomies, syndicate/benchmark, **funds**, **LinkedIn enricher**) | **Complete** — 6,156 allocators; **377 funds** |
| 3 | Ontology extraction (heuristic + optional LLM) | **Complete** — 15 unique ontology terms in DB |
| 4 | Relationship graph (NetworkX, 4-format persist) | **Complete** — **28,941 edges** |
| 4b | ICP Scoring v4.1 | **Complete** — **443 scored**; institutional **109 tier_1** (strict C2) |
| 4c | Calibration overlay (ContraVC benchmark) | **Wired** — benchmark rows linked (200/200); **0 overlap with `icp_scores`** (population mismatch — see Blockers) |
| 4d | Syndicate connectivity inference | **Complete** — 50 `mutual_connection` edges |
| 4e | Signal layer expansion | **Complete** — **16 signal types**; **11,202 signals**; **11,204 `signal_evidence` rows** |
| 4f | Contradiction detection + LinkedIn ingestion + calibration fuzzy join | **Complete** — 2 contradictions detected; adapter ready (no LinkedIn CSV ingested yet) |
| 5 | Probabilistic allocator scoring (ML) | Planned |
| 6 | Inference engine (archetype probs, conversion probs) | Planned |
| 7 | Automation / scheduling | Planned |

### Eight Architectural Invariants

1. **Database = Memory. Inference = Intelligence.** — Schemas capture provenance; scoring lives above storage.
2. **Evidence is first-class** — Every edge requires ≥1 `relationship_evidence` row; signals written via `signal_evidence_writer` require `signal_evidence`.
3. **Provenance is non-negotiable** — Every canonical row traces to `entities_raw`.
4. **Soft-truth, not binary truth** — `confidence`, `evidence_count`, `contradiction_score`, `source_agreement_score` derived deterministically.
5. **Time is first-class** — `last_active`, `relationship_decay_score`, `temporal_confidence` on all edges/signals.
6. **Human-in-the-loop is first-class** — `human_reviews` append-only; overrides via `_effective` views. Graph builder **raises** if `relationships_effective` view is missing (no silent fallback).
7. **Deterministic-first, LLM-enrichment-second** — Full pipeline runs without any LLM.
8. **Idempotent and replayable** — Same inputs → byte-identical outputs.

### Data Flow

```
raw_data/ (immutable, SHA-256 manifest)
    ↓  pulse ingest — xlsx / pdf / docx / linkedin CSV
entities_raw (24,354 rows)
    ↓  pulse normalize — rapidfuzz resolver + taxonomies
         + syndicate_normalizer (roster + investments + co-invest edges)
         + fund_normalizer (Fund_Rating_Guide → funds)
         + ContraVC benchmark ingestion
         + linkedin_enricher (fuzzy match Phantombuster CSV → allocators)
allocators (6,156: 252 institutional_prospect + 5,904 syndicate_lp)
entity_aliases (6,449) | interactions (87) | funds (377) | investments (16,848)
relationships (28,941: co_invested + invested_with + cross_file + mutual_connection + co_mentioned)
    ↓  pulse extract — HeuristicExtractor (keywords + co-occurrence)
ontology_terms (15 unique) · relationship_evidence (60,645)
    ↓  pulse derive — noisy-OR confidence + exp-decay temporal (relationships + signals)
confidence / decay on all edges and signals
    ↓  pulse graph — NetworkX MultiDiGraph from _effective views
         + invested_with_edges (LP pairs sharing fund vehicles, capped)
         + run_prospect_inference → mutual_connection edges + connectivity signals
graphs/pulse.gpickle · edges.parquet · evidence.parquet · pulse.graphml
28,941 edges (167 invested_with · 50 mutual_connection inference)
    ↓  pulse score — ICP v4.1 (strict C2) + latent signals + contradiction detection
icp_scores (443) · signals (11,202) · signal_evidence (11,204) · rejections (98)
    ↓  pulse calibrate — ContraVC overlay + grid-search auto-tune (fuzzy name join)
processed_data/calibration_*.csv/json · LP_Ranked_List.csv · First_LPs_Ready.csv
```

### Repository Map

| Path | Role |
|------|------|
| `raw_data/` | Immutable source files + `manifest.json` |
| `processed_data/review_queues/` | jsonl queues per `target_type` |
| `processed_data/ontology_cache/` | content-hash-keyed extractor results |
| `processed_data/research_cache/` | web search cache for research agent |
| `processed_data/calibration_overlay.csv` | PULSE ICP ↔ ContraVC benchmark join |
| `processed_data/calibration_tier1_vs_contra.csv` | Tier 1 vs ContraVC Top 200 detail |
| `processed_data/calibration_summary.json` | Calibration metrics (**stale** — 0 overlap) |
| `processed_data/Prospect_Syndicate_Connectivity.csv` | Prospects ranked by syndicate network depth |
| `processed_data/First_LPs_Ready.csv` | Tier 1 ready slice with bridge_strength, warm_path_count, network_density |
| `processed_data/First_LPs_Outreach_Pack.csv` | Tier 1 approved outreach slice |
| `processed_data/LP_Ranked_List.csv` | Full ranked list (enriched with connectivity columns) |
| `schema/` | Pydantic models + DuckDB DDL + Postgres DDL + views |
| `agents/ingestion/` | Pluggable adapters (xlsx, pdf, docx, linkedin) |
| `agents/normalization/` | Entity resolver, taxonomies, syndicate/fund normalizers, linkedin_enricher |
| `agents/ontology/` | Layered extractor pipeline + cache |
| `agents/uncertainty/` | Deterministic derivation (aggregator + temporal; relationships + signals) |
| `agents/reviews/` | Append-only review queue writer + override applier |
| `agents/graph/` | NetworkX builder, co_invested, invested_with_edges, prospect_inference, persist |
| `agents/scoring/` | ICP v4.1, calibration, signal_extractor, latent_signal_extractor, contradiction_detector, signal_evidence_writer |
| `agents/research/` | Optional LLM + web enrichment |
| `pulse/` | Typer CLI (all commands below) |
| `prompts/` | `heuristic_keywords.yaml` · `uncertainty.yaml` · `icp_calibration.yaml` · `graph_inference.yaml` · **`linkedin_export.yaml`** |
| `evals/` | `run_evals.py` (9 tests) + `backtest_signals.py` (10 tests) |
| `docs/` | `architecture.md`, `ontology_dictionary.md`, `decision_archive.md`, **`pulse_for_general_partners.md`**, **`reading_guide.md`** |
| `pulse.duckdb` | Local embedded database |

### CLI Surface

```
pulse refresh | run-all   # full pipeline (refresh = same as UI button)
pulse ingest | normalize | extract | derive | graph | score | calibrate | status [--verbose]
pulse review list | ingest | status
pulse research enrich | ask | brief | ontology
pulse explore
```

### Research Agent (`agents/research/`)

| Module | Role |
|--------|------|
| `llm_client.py` | Provider-agnostic `instructor` wrapper (Anthropic/OpenAI/Gemini); raises `LLMUnavailable` gracefully |
| `web_search.py` | Tavily search + page-fetch; deterministic cache at `processed_data/research_cache/` |
| `schemas.py` | Strict Pydantic v2 output schemas: `EnrichmentResult`, `QAAnswer`, `BriefSections`, `OntologyEnrichment` |
| `enrichment_agent.py` | Web research → `EnrichmentResult` → `entities_raw` (source_type='api') + COALESCE allocator update |
| `qa_agent.py` | NL → SQL → result → narrative (SELECT-only validator, read-only DB) |
| `brief_agent.py` | Per-LP outreach brief: warm-path + talking points + risks → `processed_data/briefs/{id}.md` |
| `ontology_enricher.py` | Real LLM extractors implementing `OntologyExtractor` protocol |

**Env vars required:**

| Var | Purpose |
|-----|---------|
| `PULSE_LLM_PROVIDER` | `anthropic` \| `openai` \| `gemini` \| `none` (default) |
| `ANTHROPIC_API_KEY` | Required when provider=anthropic |
| `OPENAI_API_KEY` | Required when provider=openai |
| `GEMINI_API_KEY` | Required when provider=gemini |
| `PULSE_LLM_MODEL` | Override default model per provider (optional) |
| `PULSE_SEARCH_PROVIDER` | `tavily` \| `none` (default) |
| `TAVILY_API_KEY` | Required when PULSE_SEARCH_PROVIDER=tavily |

**Install:** `pip install -e ".[research]"` or `pip install -r requirements-llm.txt`

**`pulse status --verbose`** — live DB row counts, edge distribution, allocator population split, ICP tier distribution, invariant checks (evidence-per-edge, view exists, orphan evidence, unknown allocator types).

**`run-all` stage order:** `ingest → normalize → extract → derive → graph → score → calibrate`
(graph runs before score so network signals feed into ICP scoring)

### Technology Stack

- **Storage:** DuckDB local (`pulse.duckdb`); Postgres/Supabase portable via `schema/postgres.sql`
- **Validation:** Pydantic v2 (`schema/models.py`)
- **Graph:** NetworkX MultiDiGraph
- **Fuzzy match:** rapidfuzz (0.90 auto / 0.70 review thresholds; 0.85 calibration join)
- **CLI:** Typer + Rich (`python -m pulse` on Windows if entrypoint not installed)
- **Serialization:** gpickle, parquet (pyarrow), graphml

---

## 2. Schemas

### Core Tables (`schema/duckdb.sql` · `schema/postgres.sql` · `schema/models.py`)

| Table | Purpose | Rows (2026-06-06 snapshot) |
|-------|---------|------------------------------|
| `entities_raw` | Append-only provenance anchor | 24,354 |
| `allocators` | Normalized LP entities | 6,156 (252 institutional_prospect + 5,904 syndicate_lp) |
| `funds` | GP fund / vehicle entities | **377** |
| `interactions` | LP touchpoints | 87 |
| `investments` | LP→Fund commitment records | 16,848 (syndicate deal txns) |
| `relationships` | Graph edges (uncertainty + temporal derived) | **28,941** |
| `relationship_evidence` | First-class evidence per edge | **60,645** |
| `signals` | Weak signals per allocator (16 types) | **11,202** |
| `signal_evidence` | First-class evidence per signal | **11,204** |
| `rejections` | Stated / inferred / structural rejections | 98 |
| `ontology_terms` | Discovered archetypes, patterns, clusters | 15 unique terms |
| `entity_aliases` | Fuzzy-resolution alias mappings | 6,449 |
| `human_reviews` | Append-only reviewer overrides | 672 (all `reject` — false-positive fuzzy aliases) |
| `icp_scores` | ICP v4.1 scores per allocator | **443** |
| `benchmark_rankings` | ContraVC Top 200 external benchmark | 200 (all `allocator_id` linked) |
| `pipeline_runs` | Per-stage run tracking | tracked |

### ICP tier distribution (live — all scored rows)

| Tier | Count |
|------|-------|
| tier_1 | 109 |
| tier_2 | 23 |
| tier_3 | 6 |
| tier_4 | 305 |

**Institutional prospects only (`population='institutional_prospect'`):** tier_1=109 · tier_2=23 · tier_3=6 · tier_4=304 (252 scored + legacy rows)

### Signal type distribution (live)

| `signal_type` | Count | Source |
|--------------|-------|--------|
| `recent_activity_recency` | 3,405 | latent_signal_extractor |
| `coinvest_intensity` | 3,405 | latent_signal_extractor |
| `shared_deal_count` | 1,478 | latent_signal_extractor |
| `warm_path_count` | 253 | prospect_inference |
| `network_density` | 253 | prospect_inference |
| `bridge_strength` | 253 | prospect_inference |
| `social_proximity` | 253 | prospect_inference |
| `clean_profile` | 252 | latent_signal_extractor (ICP mirror) |
| `proxy_fund_overlap` | 252 | latent_signal_extractor |
| `stage_alignment` | 252 | latent_signal_extractor |
| `geography_overlap` | 191 | signal_extractor |
| `em_participation` | 191 | signal_extractor |
| `response_speed` | 191 | signal_extractor |
| `operator_background` | 191 | signal_extractor |
| `deployment_velocity` | 191 | signal_extractor |
| `exploratory_check` | 191 | signal_extractor |

**Contradiction evidence:** 2 rows (`evidence_type='contradicts_value'`) — deploy_vs_recency rule

### `entities_raw` breakdown by source

| Source file | Type | Rows | Notes |
|-------------|------|------|-------|
| `Syndicate LPs - MyAsiaVC*.xlsx` | xlsx | ~22,848 | Syndicate LP roster + LP investments |
| `ContraVC_Top200_LP_Outreach copy.xlsx` | xlsx | ~200 | External benchmark (Top 200 LP Rankings sheet) |
| `MyAsiaVC_ICP_4.0_Prospect_List_External.xlsx` | xlsx | 614 | Institutional ICP prospects |
| `AI_Native_VC_Fund_Strategy.docx` | docx | 207 | Strategy / doctrine text |
| `Fund_Rating_Guide.xlsx` | xlsx | 72 | **377 funds** normalized from guide rows |
| `MyAsiaVC LP Scoping.xlsx` | xlsx | 63 | ICP rule source of truth |
| `LP Side Plan Draft 1.pdf` | pdf | 34 | LP workflow prose |
| `Fund_Pre Screening Briefing_Call_Prep.pdf` | pdf | 26 | Call-prep / evaluation prose |

### `allocators` type distribution (institutional only, `population='institutional_prospect'`)

| `allocator_type` | Count | Source |
|-----------------|-------|--------|
| `family_office_single` | 100 | xlsx / name inference / scoring text |
| `unknown` | 55 | unresolved type — review or enrich pass |
| `high_net_worth` | 48 | xlsx / name inference |
| `fund_of_funds` | 30 | xlsx / name inference |
| `asset_manager` | 10 | xlsx / name inference |
| `family_office_multi` | 6 | name inference |
| `corporate` | 2 | name inference |
| `pension_fund` | 1 | scoring text |

### `relationships` edge type distribution (live)

| `edge_type` | Count | Meaning |
|------------|-------|---------|
| `co_invested` | 28,550 | Two LPs co-backed ≥3 syndicate SPVs/funds |
| `invested_with` | **167** | Two LPs share fund-vehicle exposure (capped writer) |
| `cross_file_corroboration` | 69 | Same LP in multiple sheets/files |
| `co_mentioned` | 105 | Two LP names co-occur in same sentence |
| `mutual_connection` | 50 | Institutional prospect ↔ syndicate LP via 2-hop bridge |

### Canonical enums (`schema/duckdb.sql` · `schema/postgres.sql`)

**Edge types (v1.3):** `invested_with` · `introduced_by` · `co_invested` · `syndicate_overlap` · `mutual_connection` · `repeated_exposure` · `co_mentioned` · `cross_file_corroboration`

**Node types:** `lp` · `fund` · `syndicate` · `founder` · `advisor` · `geography`

**Signal types (16):** `response_speed` · `exploratory_check` · `operator_background` · `em_participation` · `geography_overlap` · `social_proximity` · `network_density` · `deployment_velocity` · `bridge_strength` · `warm_path_count` · `coinvest_intensity` · `recent_activity_recency` · `stage_alignment` · `proxy_fund_overlap` · `clean_profile` · `shared_deal_count`

**Signal evidence types:** `signal_heuristic` · `signal_investment_pattern` · `signal_graph_metric` · `signal_icp_mirror` · `signal_connectivity` · `contradicts_value`

**Ontology categories:** `allocator_archetype` · `em_signal` · `rejection_pattern` · `geography_cluster` · `committee_constraint`

**Evidence types (v1.3):** `cross_file_match` · `structured_xlsx_match` · `heuristic_keyword_match` · `heuristic_co_occurrence` · `llm_enriched` · `contradicts_edge` · `contradicts_value` · `co_investment_pattern` · **`graph_path_inference`**

**Review target types:** `alias` · `allocator_archetype` · `ontology_term` · `signal` · `relationship_edge` · `rejection`

**Review decisions:** `confirm` · `reject` · `revise` · `defer`

### Derived columns (written only by `pulse derive`)

| Column | Formula | Config |
|--------|---------|--------|
| `evidence_count` | COUNT(evidence per edge/signal) | — |
| `confidence` | Noisy-OR: 1 - PRODUCT(1 - s_i * c_i) | `prompts/uncertainty.yaml` |
| `source_agreement_score` | agreeing_sources / observing_sources | prefix lists in yaml |
| `contradiction_score` | contradicting_sources / observing_sources | prefix lists in yaml |
| `last_active` | max(evidence.timestamp, interactions.occurred_at) | — |
| `relationship_decay_score` | exp(-delta_t / half_life_days) | `half_life_days: 365` |
| `temporal_confidence` | confidence * relationship_decay_score | — |

**Note on `relationship_decay_view`:** The SQL view hardcodes `365.0`; this matches the yaml default. If `half_life_days` is changed in `prompts/uncertainty.yaml`, update the constant in `schema/views.sql` too.

**Derive scope:** relationships + signals (not `ontology_terms` yet).

### Query surface (always use `_effective` views in production)

- `relationships_effective` — human review overrides on edges (**required** — graph builder raises if missing)
- `allocators_effective` — archetype overrides
- `ontology_terms_effective` — label overrides
- `relationship_decay_view` — recomputable analytics decay
- `evidence_summary` — per-edge evidence aggregation (feeds aggregator)
- `calibration_overlay` — joins `icp_scores` ↔ `benchmark_rankings` for analytics

### Reserved for Phase 5–6 (null today)

- `allocators.inferred_scores` (JSON)
- `allocators.confidences` (JSON)
- `rejections.future_conversion_prob`

---

## 3. Unresolved Decisions

| ID | Question | Options | Notes |
|----|----------|---------|-------|
| UD-001 | ~~LLM enrichment provider~~ | **RESOLVED — DA-016** | Research agent wires anthropic/openai/gemini; pipeline deterministic without keys. |
| UD-002 | **Postgres migration timing** | Migrate now vs. after graph populated | DuckDB works locally; `postgres.sql` ready. |
| UD-005 | ~~Fund normalizer scope~~ | **RESOLVED — DA-019** | 377 funds + 167 `invested_with` edges live. |
| UD-007 | **Scoring combinator for Phase 5** | Keep noisy-OR vs. add Bayesian layer | Only `noisy_or` implemented. |
| UD-008 | **Learned temporal decay** | Keep config exp-decay vs. learned weights | DA-002 defers ML until labels exist. |
| UD-009 | **FundingStack adapter** | Implement API vs. remove stub | **Removed** (2026-06). |
| UD-011 | **Benchmark ↔ ICP join strategy** | Link Contra to institutional IDs vs. score syndicate_lp rows | Fuzzy join wired; **0 overlap** — populations disjoint (family office prospects vs syndicate angels) |

*Resolved since June 3 snapshot:* UD-005 (invested_with); C2 permissive gate (DA-020); partial pipeline in-flight.

---

## 4. Blockers

| Severity | Blocker | Impact | Status |
|----------|---------|--------|--------|
| **P0** | ~~Zero relationship edges~~ | ~~Graph empty~~ | **RESOLVED** — 28,941 edges |
| **P0** | ~~Cross-file evidence discarded~~ | ~~Provenance lost~~ | **RESOLVED** |
| **P0** | ~~Allocator archetypes unset~~ | ~~All unknown~~ | **RESOLVED** (55 `unknown` institutional remain) |
| **P0** | ~~Review queue never processed~~ | ~~False-positive aliases~~ | **RESOLVED** — 672 decisions ingested |
| **P0** | ~~Zero funds normalized~~ | ~~No fund nodes~~ | **RESOLVED** — **377 funds** |
| **P0** | ~~`invested_with` edges = 0~~ | ~~No LP↔fund graph~~ | **RESOLVED** — **167 edges** (capped writer) |
| **P1** | **ContraVC ↔ `icp_scores` overlap = 0** | Calibration auto-tune skipped; `calibration_summary.json` stale | Fuzzy join at 85% finds no matches — populations disjoint (UD-011) |
| **P2** | **Institutional subgraph sparse** | 69 cross-file + 105 co_mentioned + 50 mutual_connection vs ~28.5k syndicate edges | Expected until institutional investments linked |
| **P2** | ~~**C2 passes by default**~~ | ~~Inflated tier_1~~ | **RESOLVED — DA-020** — strict EM evidence required |
| **P2** | **55 institutional `unknown` types** | Weaker LP-type soft signal (S3/S4) | Enrichment pass or review queue |
| **P2** | **2,080 pending alias reviews** | New fuzzy candidates since last ingest | Process `review_queues/aliases.jsonl` or tune resolver |
| **P2** | **Score stage fails when DB locked** | `pulse score` / evals error if `pulse explore` holds `pulse.duckdb` | Close Streamlit before refresh or evals |
| **P2** | **LinkedIn CSV not yet ingested** | Adapter + enricher ready; no sample data in corpus | Drop `raw_data/linkedin_*.csv` + re-run ingest/normalize |
| **P3** | **Evals not CI-gated** | Regressions undetected | Gate evals in `run-all` or pre-commit |
| **P3** | **Contradiction rules sparse** | Only deploy_vs_recency live (2 cases) | Add more stated-vs-revealed rules |

---

## 5. Next Priorities

### Immediate (data integrity)

1. **ContraVC ↔ ICP calibration (UD-011)** — Decide cross-population scoring strategy or name-bridge table; then `pulse calibrate` and refresh `calibration_summary.json`.
2. **Ingest LinkedIn CSV** — Phantombuster export → `raw_data/linkedin_*.csv` → `pulse ingest` → `pulse normalize`.
3. **Expand contradiction rules** — stage_alignment vs notes, EM stated vs em_participation signal, etc.

### Short-term (scoring quality)

4. **Fuzzy resolver suffix tuning** — 672 historical rejects; 2,080 alias queue items.
5. **Reduce `unknown` allocator types** — 55 institutional prospects via `pulse research enrich`.
6. **Re-export outreach CSVs** after threshold changes (`First_LPs_Ready.csv`, `LP_Ranked_List.csv`).
7. **Gate evals in CI** — `run_evals.py` + `backtest_signals.py` (19/19 pass today).

### Research agent follow-ons

8. **Run `pulse research enrich`** — resolves 55 `unknown` institutional types.
9. **Run `pulse research ontology`** — LLM enrichment of low-confidence PDF/docx terms; then `pulse derive`.
10. **Generate outreach briefs** — `pulse research brief <allocator_id>` for Tier 1 LPs.

### Medium-term (Phase 5 prep)

11. **Postgres migration dry-run**
12. **Extend `pulse derive` to `ontology_terms`**

---

## 6. Technical Debt

| Item | Location | Risk |
|------|----------|------|
| Benchmark joins syndicate_lp only; ICP institutional only | `ingest_contra_benchmark` / calibration | Auto-tune unusable despite 200/200 `allocator_id` |
| `invested_with` capped at 50k edges / 40 LPs per fund | `invested_with_edges.py` | Large syndicate funds truncated by design |
| 55 institutional `unknown` types | `allocator_normalizer` | Weak S3/S4 scoring |
| `ontology_terms` updated via SQL UPDATE (evidence_count++) | `pipeline._upsert_ontology_term` | Violates append-only spirit |
| `co_mentioned` edges low-confidence (~0.40) | `heuristic._extract_relationship_hints` | Semantically weak |
| Fuzzy matching over-clusters on shared suffixes | `entity_resolver._cluster_by_name` | 2,080 alias queue items |
| No structured xlsx extractor (Layer 1) | `pipeline._build_extractor_chain` | xlsx relies on text concat |
| Ollama extractor stub-only | `agents/ontology/base.py` | Anthropic/OpenAI/Gemini live via research agent |
| `SYSTEM_STATE` manual | — | Drift risk; hook to milestone CI |
| `calibration_summary.json` stale vs DB | `processed_data/` | Misleading overlap metrics |
| pdf/docx allocators not in fuzzy resolution | `entity_resolver.py` | PDF/DOCX names not canonicalized |
| Review queue target_type mismatch | review CLI vs `human_reviews` | `aliases` vs `alias` naming inconsistency |
| `backtest_signals.py` summary print bug | `evals/backtest_signals.py` | NameError on final line (tests pass) |

---

## 7. Ontology State

### Dictionary versions

| Asset | Version | Path |
|-------|---------|------|
| Heuristic keywords | 1.0 | `prompts/heuristic_keywords.yaml` |
| Uncertainty params | 1.0 | `prompts/uncertainty.yaml` |
| ICP calibration | 1.0 | `prompts/icp_calibration.yaml` |
| Graph inference | 1.0 | `prompts/graph_inference.yaml` |
| LinkedIn export workflow | 1.0 | `prompts/linkedin_export.yaml` |
| Human-readable dictionary | 1.0 | `docs/ontology_dictionary.md` |
| HeuristicExtractor | 1.1 | `agents/ontology/heuristic.py` |

### Terms in database (`ontology_terms`)

| Category | Count | Notes |
|----------|-------|-------|
| `allocator_archetype` | 7 | per-term confidence ~0.88 avg |
| `em_signal` | 1 | |
| `geography_cluster` | 3 | |
| `rejection_pattern` | 4 | |
| **Total unique terms** | **15** | |

### Heuristic keyword coverage (yaml)

| Category | Terms defined |
|----------|---------------|
| `allocator_archetypes` | 7 |
| `em_signals` | 3 |
| `rejection_patterns` | 5 |
| `geography_clusters` | 3 |
| `committee_constraints` | 2 |

### Gaps vs. source substrate

- **Missing geography terms in yaml:** `southeast_asia`, `south_asia`, `emerging_markets`
- **Missing archetype:** `asset_manager` in yaml (present in DB via inference)
- **Signals:** 11,202 rows across 16 types; full palette populated by `pulse score` + graph inference + latent extractor

---

## 8. Calibration State

### `prompts/icp_calibration.yaml`

- **Current thresholds:** `TIER_1_FIT_MIN: 0.60`, `TIER_2_FIT_MIN: 0.50`
- **Auto-tune status:** skipped — **0 rows** in `icp_scores` ⋈ `benchmark_rankings` (populations disjoint)
- **Fuzzy name join:** rapidfuzz at 85% threshold wired in `calibration.py` — still 0 overlap
- **Live DB:** `icp_scores` = 443; institutional tier_1 = 109; `benchmark_rankings.allocator_id` = 200/200 filled
- **`--re-score`:** `pulse calibrate --re-score` after threshold or overlap fix

### `prompts/graph_inference.yaml`

- **Prospect inference:** 2-hop BFS on `co_invested`, cap 25 edges/prospect, `min_bridge_strength: 0.15`
- **Live:** 50 `mutual_connection` edges; connectivity signals on 253 institutional prospects

---

## 9. Review Queue State (live)

| Queue | Items |
|-------|-------|
| `aliases` | **2,080** |
| `allocator_types` | 0 |
| `edges` | 0 |
| `ontology_terms` | 0 |
| `signals` | 0 |

**Human reviews in DB:** 672 × `alias` / `reject` (false-positive fuzzy merges processed).

---

## 10. Evals State

| Suite | Tests | Status (2026-06-06) |
|-------|-------|---------------------|
| `evals/run_evals.py` | 9 | **9/9 PASS** |
| `evals/backtest_signals.py` | 10 | **10/10 PASS** |

Key backtests: signal_evidence invariant, latent ICP mirror coverage, tier discrimination, connectivity lift, invested_with exists, C2 strict tier_1, contradiction evidence emitted.

---

## Appendix: Decision Archive Index

Recorded in `docs/decision_archive.md`:

- **DA-001** — `relationship_evidence` normalized table (not JSONB)
- **DA-002** — Deterministic exp-decay (not learned)
- **DA-003** — Append-only `human_reviews` + `_effective` views
- **DA-004** — Uncertainty columns derived only by `pulse derive`
- **DA-005** — DuckDB local / Postgres production
- **DA-006** — FundingStack integration removed
- **DA-007** — rapidfuzz two-threshold (0.90 / 0.70)
- **DA-008** — Ontology cache key includes extractor + content + prompt hash
- **DA-009** — `cross_file_corroboration` + `co_mentioned` edge types
- **DA-010** — Cross-sheet corroboration as cross-context evidence
- **DA-011** — `Unnamed: 1/2/4` columns mapped to LP fields
- **DA-012** — Calibration overlay + grid search on tier thresholds
- **DA-013** — 2-hop prospect inference + graph_path_inference evidence
- **DA-014** — `run-all`: graph before score
- **DA-015** — Anonymized prospect placeholders filtered
- **DA-016** — Research agent LLM provider wiring
- **DA-017** — Research agent three-lane write-back protocol
- **DA-018** — `signal_evidence` table + 16-type signal catalogue
- **DA-019** — `invested_with` edge writer with combinatorial caps
- **DA-020** — C2 emerging-manager strict gate (no default pass)

---

## Regeneration Checklist

After the next milestone, update this file with:

- [x] `python -m pulse status --verbose` (row counts — 2026-06-06)
- [x] Review queue counts via read-only DB query (aliases: 2,080)
- [x] `python evals/run_evals.py` + `python evals/backtest_signals.py` (19/19 pass — 2026-06-06)
- [x] Row counts and blocker status updates
- [x] New decisions → `docs/decision_archive.md` + Section 3 (DA-018–020)
- [ ] Ontology yaml version bump if keywords changed
- [ ] `pulse calibrate` → refresh `calibration_summary.json` after overlap fix
