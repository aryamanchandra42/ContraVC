# PULSE Decision Archive

Institutional memory for non-obvious architectural choices.
Every significant design decision is recorded here with its rationale.
Future agents entering this repo should read this before making changes.

---

## DA-001: Why `relationship_evidence` is a normalized table, not inline JSONB

**Decision**: Evidence is stored in a normalized `relationship_evidence` table (one row per supporting source record per edge), not as a JSON array inside `relationships.evidence`.

**Rationale**:
- Inline JSONB cannot be queried as rows: you can't write `SELECT COUNT(*) FROM evidence WHERE edge_id = X` without unnesting
- The `evidence_summary` SQL view requires GROUP BY over evidence rows, which is impossible if evidence is a blob
- The evals invariant "every edge has ≥1 evidence row" is a SQL `NOT EXISTS` constraint — infeasible with inline JSON
- Future contradiction detection requires comparing individual evidence items across sources — impossible from a blob
- Cross-file match evidence (from entity resolution) and ontology extraction evidence differ in structure; a typed row handles this cleanly

**Trade-off accepted**: slightly more storage, slightly more complex inserts (must write edge + evidence atomically). The explainability and query power are worth it.

**Date**: May 2026

---

## DA-002: Why temporal decay is a deterministic recency function, not a learned weight

**Decision**: `relationship_decay_score = exp(-Δt / half_life_days)` where `half_life_days` is a YAML config parameter. No ML.

**Rationale**:
- We have no labeled training data for "how much has this relationship decayed" in Phase 1-4
- A learned weight would require a labeled dataset that doesn't exist yet
- The exp-decay function is interpretable: an LP relationship last active 365 days ago with `half_life_days=365` has a decay score of `exp(-1) ≈ 0.37`. This is explainable to a non-technical stakeholder
- The formula is replayable: given the same `last_active` and the same `half_life_days`, you always get the same score
- Operators can tune `half_life_days` based on their judgment of relationship durability without touching code
- Phase 5-6 may introduce a learned decay if training data becomes available; the config-driven architecture makes it easy to swap in

**Date**: May 2026

---

## DA-003: Why `human_reviews` is append-only and applied via views

**Decision**: Reviewer overrides are inserted as new rows in `human_reviews`. Normalized rows (`relationships`, `allocators`, `ontology_terms`) are never mutated. Effective state is exposed via `relationships_effective`, `allocators_effective`, `ontology_terms_effective` views.

**Rationale**:
- Private markets require an audit trail: who changed what, when, and why
- Mutating a row loses the history of the original derived value, making the override irreversible and unauditable
- Append-only `human_reviews` means: (1) the original derivation is always recoverable by ignoring reviews, (2) every override has a timestamp and reviewer identity, (3) you can replay the full history of an entity's classification
- The `supersedes` column enables corrections-of-corrections while preserving the full chain
- This is the same design used in financial systems for ledger entries: you never delete a ledger row, you write a reversal

**Consequence**: the _effective views are the canonical query surface. Production code (graph builder, notebooks) must read from views, not raw tables.

**Date**: May 2026

---

## DA-004: Why uncertainty columns are derived, not stored by ingestion adapters

**Decision**: `confidence`, `evidence_count`, `contradiction_score`, `source_agreement_score`, `relationship_decay_score`, `temporal_confidence` are computed by `pulse derive` from `relationship_evidence`. Ingestion adapters and normalizers never write these columns.

**Rationale**:
- If adapters wrote confidence scores at ingestion time, those scores would reflect only the view of one source at one moment. Re-ingesting the same file would overwrite values derived from later evidence
- Evidence accumulates over time: a relationship observed in 3 different files should have higher confidence than one from a single file. This can only be computed after all evidence exists
- Derivation is idempotent: running `pulse derive` twice with the same evidence table produces byte-identical column values. This is verifiable by the evals harness
- Future phases can add new evidence types (e.g., from a new data source), and running `pulse derive` automatically propagates their effect to all downstream confidence scores without touching the adapter code

**Date**: May 2026

---

## DA-005: Why DuckDB is used locally instead of SQLite or Postgres directly

**Decision**: Local development uses DuckDB with `schema/duckdb.sql`. Production migration targets Postgres/Supabase via `schema/postgres.sql`.

**Rationale**:
- DuckDB provides a Postgres-compatible SQL dialect, making the DDL nearly identical between local and production
- DuckDB has excellent Pandas/PyArrow integration (`.fetchdf()`, `.from_df()`) which is critical for the normalization and derivation pipelines
- DuckDB is embedded (no server process), making local development and CI simple
- The schema is kept strictly portable: JSON (not JSONB) in duckdb.sql, JSONB in postgres.sql — this is the only meaningful syntactic difference
- Migration path: `pg_dump` + `COPY` of the DuckDB-generated parquet files into Postgres, then run `schema/postgres.sql` views on top

**Date**: May 2026

---

## DA-006: FundingStack integration removed

**Decision**: FundingStack CRM ingestion (CSV adapter, API stub, pre-screener, explore tab, and outreach pack Section B) was removed. PULSE now ingests xlsx/pdf/docx/LinkedIn sources only; outreach exports are institutional Tier 1 approved prospects from the prospect spreadsheet.

**Status**: Removed (2026-06).

**Date**: June 2026

---

## DA-007: Why rapidfuzz uses a two-threshold system (0.90 auto / 0.70 review)

**Decision**: Fuzzy entity matches at similarity >= 0.90 are automatically merged (auto-accept); matches [0.70, 0.90) are written to the review queue; below 0.70 are not matches.

**Rationale**:
- A single threshold creates either too many false positives (low threshold) or too many false negatives (high threshold)
- The two-tier system gives the pipeline a high-confidence path (proceed automatically) and a review path (surface for human judgment), without blocking the pipeline
- 0.90 was chosen as the auto-accept threshold because it catches clear name variants ("GIC Singapore" vs "GIC") while rejecting ambiguous cases ("ABC Capital" vs "ABC Holdings")
- 0.70 as the review floor was calibrated against the LP Scoping file, which has the most name variation; below 0.70 produces mostly noise

**This threshold can be adjusted** in `agents/normalization/entity_resolver.py` constants `AUTO_MATCH_THRESHOLD` and `REVIEW_MATCH_THRESHOLD`.

**Date**: May 2026

---

## DA-008: Why the ontology pipeline uses a cache keyed by (extractor + version + content_hash)

**Decision**: Cache key = SHA-256 of `{extractor_name, extractor_version, content_hash, prompt_hash}`. Changing any of these invalidates the cache.

**Rationale**:
- Including `content_hash` means: if a source file changes (new ingestion), all documents get re-extracted automatically
- Including `extractor_version` means: upgrading the heuristic extractor (e.g., new keywords) triggers re-extraction of all documents by default
- Including `prompt_hash` means: for LLM extractors, changing the prompt template triggers re-extraction
- The cache is not keyed by run_id, so multiple runs with the same inputs share the cache — the pipeline is O(N documents) on first run, O(1) on subsequent identical runs
- Deterministic extractors (`deterministic=True`) must produce byte-identical output given the same inputs, so their cache files never become stale

**Date**: May 2026

---

## DA-016: LLM enrichment is wired as an optional research-agent layer (resolves UD-001)

**Decision**: LLM enrichment is implemented in `agents/research/` as a pluggable, opt-in layer above the deterministic pipeline. Three providers are supported (Anthropic, OpenAI, Gemini), selected by `PULSE_LLM_PROVIDER`. The full pipeline continues to run without any LLM.

**Rationale**:
- UD-001 was deferred because Phase 1-4 required a deterministic baseline before adding non-deterministic enrichment
- Implementing LLM as a layer in `agents/research/` (not inside ingestion or normalization) preserves the deterministic-first doctrine: all existing stages are unchanged
- `instructor` enforces strict Pydantic v2 schemas on every LLM response — no free-form text enters the DB
- The enrichment agent writes provenance to `entities_raw` first (source_type='api'), then COALESCE-updates allocators — no history is lost and the step is idempotent
- Web search results are cached at `processed_data/research_cache/` using the same SHA-256 key convention as the ontology cache — identical queries never re-hit the network
- Graceful degradation: if `PULSE_LLM_PROVIDER` is unset or API key is missing, all research subcommands log a warning and exit cleanly with zero writes

**LLM Extractor registration**: `agents/ontology/pipeline._build_extractor_chain()` was updated to import from `agents/research/ontology_enricher` instead of the stub classes in `agents/ontology/base`. The stubs remain for Ollama (not yet implemented).

**New CLI surface**: `pulse research enrich | ask | brief | ontology`

**Date**: June 2026

---

## DA-017: Research agent write-back follows a strict three-lane protocol

**Decision**: External research facts are written to PULSE through exactly three lanes, in order: (1) `entities_raw` provenance row, (2) COALESCE-only allocator update, (3) review queue for low-confidence assertions.

**Rationale**:
- Lane 1 (raw provenance): every externally-researched fact is anchored in `entities_raw` with `source_type='api'` before any canonical row is touched. This preserves the provenance invariant and makes every enrichment auditable and reversible.
- Lane 2 (COALESCE update): `UPDATE allocators SET col = COALESCE(col, ?) WHERE ...` ensures that enrichment never overwrites existing non-null values. If PULSE already knows the allocator type, web research cannot change it — human review is required for that.
- Lane 3 (review queue): any enrichment where LLM confidence < `review_queue.low_confidence_threshold` (default 0.40 from `prompts/uncertainty.yaml`) is written to `processed_data/review_queues/allocator_types.jsonl` for human review before a decision is applied to the canonical table.
- This three-lane design means the research agent can run fully autonomously without any human oversight for high-confidence enrichments, while surfacing uncertain cases for review — matching the Phase 6 preparation goal.

**Date**: June 2026

---

## DA-018: Why signals have a normalized `signal_evidence` table (mirroring edges)

**Decision**: Signals use the same evidence-first pattern as relationships: a `signals` row plus ≥1 `signal_evidence` row written atomically via `signal_evidence_writer.py`. Signal types expanded from 8 to 16.

**Rationale**:
- ICP scoring and graph inference produce heterogeneous signal sources (text heuristics, investment patterns, graph metrics, ICP mirrors, connectivity). A typed evidence row per source preserves provenance and enables contradiction detection.
- `contradicts_value` evidence requires comparing individual signal assertions — impossible if signals are opaque floats on `allocators`.
- `pulse derive` can recompute `confidence`, `contradiction_score`, and temporal columns on signals from evidence rows using the same noisy-OR combinator as edges.
- The 8 new signal types (`bridge_strength`, `warm_path_count`, `coinvest_intensity`, etc.) capture latent and connectivity information that text extraction alone cannot surface.

**Writers**: `signal_extractor.py` (text), `latent_signal_extractor.py` (investments), `prospect_inference.py` (graph), `contradiction_detector.py` (contradictions).

**Date**: June 2026

---

## DA-019: Why `invested_with` edges are capped (MAX_LPS_PER_FUND=40, MAX_EDGES=50_000)

**Decision**: The `invested_with` edge writer (`agents/graph/invested_with_edges.py`) emits LP-pair edges for allocators sharing fund vehicles, but caps combinatorial explosion on large syndicate funds.

**Rationale**:
- One syndicate fund had 492 LPs → naive pairwise combination hung the graph stage for 5+ minutes.
- PULSE prioritizes institutional prospect connectivity over exhaustive syndicate clique enumeration.
- Caps (40 LPs per fund, 50k total edges, priority-LP scoping) keep graph builds deterministic and fast while still surfacing meaningful `invested_with` signal (167 edges live).
- Full clique enumeration belongs in offline analytics, not the default pipeline.

**Date**: June 2026

---

## DA-020: Why C2 (emerging manager gate) no longer passes by default

**Decision**: `_score_c2_emerging_manager` in `icp_scorer.py` requires positive EM evidence. Prospects with thin or absent EM language fail C2 and land in Tier 4.

**Rationale**:
- Default-pass C2 inflated Tier 1 before human review would catch thin records.
- The scoping workbook treats emerging-manager appetite as a **core gate**, not a soft signal — permissive default violated the business constitution.
- Strict C2 is backtested in `evals/backtest_signals.py::test_c2_strict_tier1` — all Tier 1 institutional rows must have EM participation evidence.
- Partners still review Tier 1 names, but the machine no longer assumes EM appetite from silence.

**Date**: June 2026
