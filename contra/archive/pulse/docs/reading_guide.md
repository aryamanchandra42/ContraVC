# PULSE — Reading Guide

Where to start depending on what you need from the system.

**Last updated:** 2026-06-06

---

## Start here (5–15 minutes)

| File | Best for |
|------|----------|
| [`README.md`](../README.md) | What PULSE is, quick start, CLI commands, key invariants |
| [`docs/pulse_for_general_partners.md`](pulse_for_general_partners.md) | **Non-technical** — who to call, why, outreach workflow |
| [`SYSTEM_STATE.md`](../SYSTEM_STATE.md) | **Live operational snapshot** — counts, phase status, blockers |

If you only read three files, read those three.

---

## Architecture and design (30–60 minutes)

| File | What you learn |
|------|----------------|
| [`docs/architecture.md`](architecture.md) | Full pipeline flow, stage-by-stage behavior, design doctrine |
| [`AGENTS.md`](../AGENTS.md) | Agent/operator doctrine — invariants, repo map, CLI reference |
| [`docs/decision_archive.md`](decision_archive.md) | **Why** things were built this way |

Implementer rules (more prescriptive) live in `.cursor/rules/`:

- `architecture.mdc` — core principles
- `provenance.mdc` — traceability rules
- `uncertainty.mdc` — confidence, decay, human review protocol
- `graph.mdc` — relationship graph rules
- `schemas.mdc` — three-artifact schema invariant
- `extractors.mdc` — deterministic-first extraction

---

## Domain vocabulary

| File | What you learn |
|------|----------------|
| [`docs/ontology_dictionary.md`](ontology_dictionary.md) | Allocator archetypes, EM appetite, geography, signal terms |
| [`prompts/heuristic_keywords.yaml`](../prompts/heuristic_keywords.yaml) | Keyword patterns the heuristic extractor uses |
| [`prompts/uncertainty.yaml`](../prompts/uncertainty.yaml) | Confidence combinator and temporal decay parameters |

---

## How the pipeline runs (code entry points)

Read in pipeline order:

```
ingest → normalize → extract → derive → graph → score → calibrate
```

| Stage | Start here |
|-------|------------|
| **Orchestration** | [`pulse/orchestrator.py`](../pulse/orchestrator.py) — wires `pulse refresh` |
| **CLI** | [`pulse/cli.py`](../pulse/cli.py) — all `pulse …` commands |
| **Ingest** | [`agents/ingestion/registry.py`](../agents/ingestion/registry.py) and adapters |
| **Normalize** | [`agents/normalization/entity_resolver.py`](../agents/normalization/entity_resolver.py) |
| **Extract** | [`agents/ontology/pipeline.py`](../agents/ontology/pipeline.py) |
| **Derive** | [`agents/uncertainty/aggregator.py`](../agents/uncertainty/aggregator.py) |
| **Graph** | [`agents/graph/builder.py`](../agents/graph/builder.py) |
| **Score** | [`agents/scoring/icp_spec.py`](../agents/scoring/icp_spec.py), `icp_scorer.py` |
| **Calibrate** | [`agents/scoring/calibration.py`](../agents/scoring/calibration.py) |
| **Exports / UI** | [`pulse/exports/outreach_pack.py`](../pulse/exports/outreach_pack.py), [`pulse/explore/app.py`](../pulse/explore/app.py) |

---

## Data model

| File | What you learn |
|------|----------------|
| [`schema/models.py`](../schema/models.py) | Pydantic models — runtime contracts |
| [`schema/duckdb.sql`](../schema/duckdb.sql) | Local DB DDL |
| [`schema/views.sql`](../schema/views.sql) | `_effective` views, decay expressions |

**Rule of thumb:** query `relationships_effective`, `allocators_effective`, and `ontology_terms_effective` — not the raw tables.

---

## Interactive walkthroughs

| Notebook | Topic |
|----------|-------|
| [`notebooks/00_source_substrate_audit.ipynb`](../notebooks/00_source_substrate_audit.ipynb) | What is in `raw_data/` |
| [`notebooks/01_entity_resolution_walkthrough.ipynb`](../notebooks/01_entity_resolution_walkthrough.ipynb) | How duplicate names get merged |
| [`notebooks/02_graph_topology_first_look.ipynb`](../notebooks/02_graph_topology_first_look.ipynb) | Relationship graph shape |
| [`notebooks/03_review_queue.ipynb`](../notebooks/03_review_queue.ipynb) | Human review workflow |

---

## Outputs you can inspect without code

| Path | What it is |
|------|------------|
| `processed_data/First_LPs_Outreach_Pack.csv` | Tier 1 outreach list |
| `processed_data/LP_Ranked_List.csv` | Full ranked prospects |
| `graphs/edges.parquet` | Serialized relationship edges |
| `raw_data/manifest.json` | Source file hashes and provenance |

---

## Suggested paths by role

**Fundraising / GP (no engineering):**  
`pulse_for_general_partners.md` → `First_LPs_Outreach_Pack.csv` → `Launch_PULSE.bat` or `pulse explore`

**New engineer onboarding:**  
`README.md` → `architecture.md` → `AGENTS.md` → `pulse/cli.py` + `orchestrator.py` → `schema/models.py` → one `agents/` stage folder

**“Why did they do X?”:**  
`decision_archive.md` → relevant `.cursor/rules/*.mdc`

**Deep dive on scoring:**  
`icp_spec.py` → `icp_scorer.py` → `signal_types.py` → `evals/backtest_signals.py`

---

## Keeping docs current

| Task | Command |
|------|---------|
| Live DB counts | `python -m pulse status --verbose` |
| Review queue sizes | `python -m pulse review status` |
| Invariant checks | `python evals/run_evals.py` |
| Signal backtests | `python evals/backtest_signals.py` |

Then update [`SYSTEM_STATE.md`](../SYSTEM_STATE.md). Close `pulse explore` before running write stages or evals that need exclusive DB access.
