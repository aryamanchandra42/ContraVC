# PULSE

**Private-market Unified LP Signal Engine**

An institutional intelligence system for private-market allocator inference and fundraising alpha discovery.

---

## What This Is

PULSE is a probabilistic reasoning system, relationship graph engine, institutional memory system, and weak-signal inference engine for capital-network intelligence. It is not a CRM, spreadsheet wrapper, or generic SaaS dashboard.

## Current Phase: 1–4 Foundation + ICP Scoring + Signal Layer

Beyond the core foundation pipeline, PULSE now includes:

- **ICP v4.1 scoring** — Core / exclusion / soft-signal tiers aligned to `MyAsiaVC LP Scoping.xlsx`
- **Syndicate co-invest graph** — ~29k edges including `co_invested`, `invested_with`, and prospect bridge inference
- **16 signal types** with first-class `signal_evidence` (connectivity, latent investment patterns, contradiction detection)
- **Calibration overlay** — ContraVC Top 200 benchmark join + grid-search auto-tune (when populations overlap)
- **Research agent v1.0** — optional LLM + web enrichment (`pulse research …`)
- **LinkedIn CSV ingestion** — Phantombuster / Sales Nav exports via `linkedin_csv_adapter`
- **Local LP explorer** — `pulse explore` (Streamlit, read-only)

Phase 5–6 (ML allocator scoring, inference engine) remain planned.

## Quick Start — Partner Workflow (no command line required)

**Windows:** Double-click `Launch_PULSE.bat` at the repo root. The app opens in your browser at http://localhost:8501.

**Mac / Linux:**
```bash
pip install -e ".[explore]"
pulse explore
```

Inside the app, click **Refresh PULSE** in the left sidebar. That single button runs the full pipeline (ingest → score → calibrate → exports) and regenerates the outreach CSVs. No commands needed.

**Outreach pack output:**
- `processed_data/First_LPs_Outreach_Pack.csv` — ICP Tier 1, client approved (from your prospect spreadsheet)

---

## Developer CLI reference

For debugging or running individual stages:

```bash
# Full autonomous refresh (same as the UI button)
pulse refresh

# Individual stages
pulse ingest
pulse normalize
pulse extract
pulse derive
pulse graph      # includes invested_with + prospect inference
pulse score      # ICP + latent signals + contradiction detection
pulse calibrate

# Review queue
pulse review list
pulse review status

# Live snapshot
pulse status --verbose
```

Optional research layer (requires LLM API key):
```bash
pip install -e ".[research]"
pulse research enrich --research-fit
pulse research brief <allocator_id>
pulse research ask "Which Tier 1 LPs have warm paths?"
```

## Understanding PULSE

| Audience | Start here |
|----------|------------|
| Everyone | [docs/reading_guide.md](docs/reading_guide.md) — curated reading map by role |
| Partners / GPs | [docs/pulse_for_general_partners.md](docs/pulse_for_general_partners.md) |
| Engineers | [docs/architecture.md](docs/architecture.md) · [AGENTS.md](AGENTS.md) |
| Live counts & blockers | [SYSTEM_STATE.md](SYSTEM_STATE.md) |

## Source Files

Raw source files live in `raw_data/` (immutable). Their SHA-256 hashes are recorded in `raw_data/manifest.json`.

| File | Description |
|------|-------------|
| `AI_Native_VC_Fund_Strategy.docx` | Operating doctrine, institutional memory philosophy, AI-native VC architecture |
| `Fund_Pre Screening Briefing_Call_Prep.pdf` | Allocator evaluation logic, FoF scoring, LP psychology |
| `Fund_Rating_Guide.xlsx` | Allocator scoring logic, evaluation systems, ranking heuristics |
| `LP Side Plan Draft 1.pdf` | LP-side workflows, fundraising architecture, allocator interactions |
| `MyAsiaVC LP Scoping.xlsx` | Allocator datasets, LP lists, relationship intelligence, co-investment patterns |
| `MyAsiaVC_ICP_4.0_Prospect_List_External.xlsx` | Allocator prospects, ICP segmentation, outreach targets |
| `Syndicate LPs - MyAsiaVC*.xlsx` | Syndicate LP roster + co-investment transactions |
| `ContraVC_Top200_LP_Outreach copy.xlsx` | External LP benchmark (Top 200) |
| `linkedin_*.csv` (optional) | Phantombuster / Sales Nav exports — see `prompts/linkedin_export.yaml` |

## Key Invariants

1. Every edge in the relationship graph has ≥1 row in `relationship_evidence`
2. Every signal has ≥1 row in `signal_evidence` (when emitted via the signal writers)
3. Uncertainty columns (`confidence`, `evidence_count`, `contradiction_score`, `source_agreement_score`, `temporal_confidence`) are derived deterministically — never hand-written
4. Human reviews are append-only; normalized rows are never mutated
5. Full pipeline runs without LLM access (LLMs are optional enrichers)
6. All pipeline stages are idempotent: same inputs → byte-identical outputs

## Evals

```bash
python evals/run_evals.py          # 9 invariant checks
python evals/backtest_signals.py   # 10 signal-layer backtests
```

Last verified: **19/19 pass** (2026-06-06).

**Operational snapshot:** [SYSTEM_STATE.md](SYSTEM_STATE.md)
