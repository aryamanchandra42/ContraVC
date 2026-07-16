# PULSE: The LP Intelligence System — End-to-End Guide for General Partners

**Audience:** A GP at a VC fund who is smart but not a developer.  
**Source of truth:** Live codebase under `contra/`. Every logic claim traces to code, not memory.  
**Last synthesized:** 2026-06-29

---

## 1. What PULSE Is (and Why It Exists)

Fundraising is not a list problem. It is a **judgment problem under incomplete information**. Contra VC already has prospect spreadsheets, syndicate history, call notes, benchmarks, and a CRM — but those live in separate places and do not, by themselves, answer: *Who should we talk to first, why, who is structurally wrong, and who looks cold but is warm through our network?*

**PULSE** (Private-market Unified LP Signal Engine) is Contra’s institutional memory layer for that problem. It ingests immutable source files from `raw_data/` (prospect Excel workbooks, LP scoping rules, AngelList syndicate exports, PDFs/DOCX, LinkedIn CSVs, NFX Signal batches, Contra Top-200 benchmark files) and turns them into:

- A **ranked, tiered prospect view** aligned to the LP Scoping workbook  
- A **relationship graph** (who co-invested with whom, and bridge paths into syndicate capital)  
- **Weak signals** per allocator (text, investment patterns, graph connectivity)  
- A **live LP Gate** that screens individual names with web research + AI  
- A **CRM + outreach layer** that turns gate YES/REVIEW into leads and draft emails  

The batch pipeline (`contra refresh` / `run_refresh` in `contra/orchestrator.py`) runs **without requiring an LLM** for ingestion, normalization, graph building, and ICP scoring. LLMs are used for live gate screening, outreach drafting, and optional enrichment — always with structured outputs and deterministic guardrails on top.

PULSE is designed for **explainability**: every relationship edge should have evidence rows; human review overrides are append-only and applied at read time via `_effective` SQL views, not by silently rewriting source data.

---

## 2. The Data Foundation

### Where LP data comes from and how it enters

The orchestrator runs eight stages on each refresh:

1. **Ingest** — `agents/ingestion/registry.py` dispatches files by extension/type to adapters (xlsx, pdf, docx, pptx, LinkedIn CSV, NFX xlsx). Each row/chunk lands in `entities_raw` with `source_record_id`, `source_file`, `source_type`, `content_hash`, and `raw_content` (JSON). Ingestion is idempotent: same content hash → skip re-insert.

2. **Normalize** — Entity resolution creates/updates `allocators`, `funds`, `investments`, `interactions`, `entity_aliases`. The syndicate integrator (`agents/normalization/syndicate_normalizer.py`) loads AngelList syndicate roster + LP investment transactions and the Contra Top-200 benchmark. CRM export goes to `crm_contacts` via `crm_normalizer.py` (not `entities_raw`).

3. **Extract** — Ontology terms from documents (`agents/ontology/pipeline.py`).

4. **Derive** — Uncertainty columns (`confidence`, `evidence_count`, `contradiction_score`, `source_agreement_score`) and temporal decay on relationships/signals.

5. **Graph** — Co-invested edges, invested-with edges, prospect inference (warm paths), graph persist to parquet/pickle.

6. **Score** — ICP scoring, rejection extraction, signal extraction, latent signals, syndicate signals, contradiction detection.

7. **Calibrate** — Join PULSE tiers to Contra Top-200; auto-tune tier thresholds into `prompts/icp_calibration.yaml`.

8. **Catalog** — Refresh data catalog for navigator queries.

After scoring, `schema/views.sql` is re-applied so query surfaces stay current.

### What the syndicate co-investment graph is and why it matters

The syndicate data comes from **“Syndicate LPs - MyAsiaVC”** xlsx: ~5,900 individual LPs (`population = 'syndicate_lp'`) and ~16,800 LP→deal transactions in `investments`. Two LPs who backed the same SPV/fund get a **`co_invested`** edge in `relationships`, but only if they share **≥3 deals** (`MIN_SHARED_DEALS = 3` in `syndicate_normalizer.py`) — this keeps the graph signal-rich rather than noise-dense.

`prospect_inference.py` then walks **2-hop paths**: institutional prospect → bridge syndicate LP → another node, writing **`mutual_connection`** edges with `graph_path_inference` evidence. That is how a family office that never appeared on your prospect sheet can still show a **warm intro route** through someone who co-invested with your syndicate.

### What lives in the database (main tables)

| Table | What it stores |
|-------|----------------|
| **`entities_raw`** | Immutable ingested rows — the audit substrate. Every normalized record traces back here. |
| **`allocators`** | Canonical LP/allocator entities: name, type, geography, appetite fields, `population` (`institutional_prospect` vs `syndicate_lp`), provenance columns. |
| **`entity_aliases`** | Alternate names → canonical allocator IDs for name matching. |
| **`funds`** | Fund/SPV vehicles syndicate LPs invested in. |
| **`investments`** | LP commitments: `lp_id`, fund, `commitment_usd`, `investment_date`, `notes` (e.g. `'venture fund'`, `'spv'`). |
| **`relationships`** | Graph edges: `edge_type` (`co_invested`, `mutual_connection`, etc.), `weight`, `confidence`, temporal fields. |
| **`relationship_evidence`** | Provenance for each edge — required by architecture; edges without evidence are invalid. |
| **`signals`** | Per-allocator weak signals (19 canonical types in `signal_types.py`; see Appendix A). |
| **`signal_evidence`** | Provenance for each signal value. |
| **`icp_scores`** | Batch ICP evaluation per allocator: C1–C4 pass/fail, S1–S7 scores, `fit_score`, `tier`, client decision, exclusion reason. |
| **`rejections`** | Extracted rejection reasons from outreach history. |
| **`benchmark_rankings`** | External Contra Top-200 rankings for calibration. |
| **`human_reviews`** | Append-only partner overrides (confirm/reject/revise/defer). |
| **`crm_contacts`** | Legacy import from FundingStack `export.csv` — separate from live pipeline. |
| **`crm_leads`** | Active CRM workspace: gate verdict, ICP tier, contacts, pipeline stage, computed score. |
| **`crm_gate_reviews`** | Latest gate verdict per name (for ICP queue tracking). |
| **`lp_dossiers`** | Durable gate memory: research notes, appetite, LP commitments, verdict history. |
| **`crm_outreach_drafts`** | Generated emails: `draft` → `approved` → `sent`. |

Query production analytics through **`_effective` views** (`relationships_effective`, `allocators_effective`) and convenience views like `v_lp_profile`, `v_warm_paths`, `v_crm_icp_queue` — not raw `relationships` when human overrides matter.

---

## 3. Pipeline One — ICP Scoring (Offline Batch)

**Source files:** `agents/scoring/icp_scorer.py`, `agents/scoring/icp_spec.py`

### What triggers it and what it produces

**Trigger:** Stage `score` in `run_refresh()`, calling `run_icp_scoring(con)`.

**Idempotency:** Deletes all `icp_scores` rows where `icp_version = ICP_VERSION`, then rewrites.

**Input rows:** Prospect sheet rows from `entities_raw` where `source_type = 'xlsx'` and sheet name matches `Prospects_m*`, `Prospects_Hong Kong`, or `Prospects_London`, with row number > header row 9 and a non-empty name column (`Unnamed: 1`).

**Output:** One `icp_scores` row per matched allocator, plus tier counts (`scored`, `unmatched_rows`, `tier_1`…`tier_4`).

**ICP version:** `ICP_VERSION = "4.2"` in `icp_spec.py` (added E13/E14). Many SQL views still filter `icp_version = '4.1'` — see Section 10.

### The four hard gates (C1–C4)

All four must pass (`core_pass = True`) or the allocator is Tier 4. Logic in `_score_c1`…`_score_c4` in `icp_scorer.py`; keywords in `icp_spec.py`.

**C1 — VC fund LP**  
Scans prospect scoring text. Must hit at least one `C1_KEYWORDS` entry **and** at least one `C1_REQUIRED_ANY` (`"fund"`, `"vc"`, or `"venture"`).

**C2 — Emerging manager appetite**  
Scans scoring text + client comments for `C2_EMERGING_MANAGER_POSITIVE` phrases (e.g. `"emerging manager"`, `"fund i"`, `"first-time fund"`, `"dedicated emerging"`).

**C3 — AI / tech thesis**  
Scans scoring text for `C3_SECTORS` (AI, robotics, deep tech, named AI companies, etc.).

**C4 — Geography**  
Scans scoring text for `C4_REGIONS` (Asia/APAC, North America, Middle East/GCC, or `"global"` / `"worldwide"`).

### Exclusion rules (E1–E14)

Any exclusion sets `excluded = True` → Tier 4. Full phrase lists in **Appendix B**. Summary:

| Rule | Trigger |
|------|---------|
| E1 | PE/buyout-primary phrases |
| E2 | VC secondaries-only |
| E3 | Real estate-primary |
| E4 | Web3/crypto-only |
| E5 | Healthcare/life sciences-only |
| E6 | Geography-locked non-qualifying regions **or** sanctioned country (OFAC/MAS list) |
| E7 | Impact/philanthropy-primary |
| E8 | Explicit no-emerging-manager evidence |
| E9 | Check size mismatch (ticket too large/small) |
| E10 | Direct-only (no fund LP) |
| E11 | Client status: `"rejected - blacklist"` or `"rejected - seems to conflict"` |
| E12 | Prop trading / non-VC financial |
| E13 | Fund size minimum disqualifies $30M Fund I (phrases from rejection archive) |
| E14 | Explicit US/Europe-only mandate (no Asia) |

### The seven soft signals (S1–S7) — ICP fit_score only

These are **not** the same as the 19 `signals` table types (see Appendix A). Weights sum to 1.0.

| Signal | Weight | What it measures |
|--------|--------|----------------|
| **S1** AI investment | 0.25 | Portfolio AI cos (OpenAI, Anthropic, etc.) or thesis keyword depth |
| **S2** EM depth | 0.20 | Quality of emerging-manager language beyond binary C2 |
| **S3** LP type | 0.20 | `LP_TYPE_PRIORITY` table score by allocator type |
| **S4** Decision speed | 0.15 | Same table, decision-speed column (HNWI fastest) |
| **S5** Stage | 0.10 | Pre-seed/seed/Series A keyword hits |
| **S6** Clean profile | 0.05 | Absence of conflict phrases (subset of E1/E4/E5/E10) |
| **S7** Proxy fund overlap | 0.05 | Mentions of peer funds (Neon, Better Capital, Mana, etc.) |

**Fit score math:**

```
fit_score = round(s1×0.25 + s2×0.20 + s3×0.20 + s4×0.15 + s5×0.10 + s6×0.05 + s7×0.05, 4)
```

### Tier assignment

Thresholds from `get_tier_thresholds()` — defaults **0.60** (Tier 1) and **0.38** (Tier 2), overridable via `prompts/icp_calibration.yaml`.

| Tier | Logic |
|------|-------|
| **tier_4** | `excluded` OR NOT `core_pass` |
| **tier_1** | NOT excluded, `core_pass`, `fit_score >= tier_1_min`, AND `client_decision == "approved"` |
| **tier_2** | NOT excluded, `core_pass`, and (`fit_score >= tier_1_min` but not approved) OR (`fit_score >= tier_2_min`) |
| **tier_3** | NOT excluded, `core_pass`, below tier_2 threshold |

### What gets written

One row per allocator in `icp_scores`: C1–C4 booleans + evidence (truncated 300 chars), `core_pass`, `excluded` + reason, `s1`…`s7`, `fit_score`, `tier`, client fields, source provenance, `icp_version`.

---

## 4. Pipeline Two — The LP Gate (Live, Per-LP)

**Source files:** `contra/gate/runner.py`, `evaluator.py`, `verdict.py`, `research.py`, `appetite_validator.py`, `evidence_verifier.py`, `prompts/navigator/gate_explain.yaml`

### What triggers a gate run

CLI `contra gate "LP Name"`, API `POST /api/gate`, web UI, or NFX batch with `screening_mode = "nfx_individual"`.

### How web research works (Tavily fan-out)

`contra/gate/research.py` — two paths:

1. **Preferred — OpenAI deep research** when `OPENAI_API_KEY` set: single adaptive call, then NFX direct fetch, supplemental Tavily (2 queries), PitchBook authenticated block.

2. **Fallback — Tavily fan-out** via `build_lp_fit_queries()` (7 queries in `agents/research/web_search.py`):
   - General VC fund LP profile  
   - PitchBook-style fund commitments  
   - Anchor LP / emerging manager / Fund I  
   - Portfolio AI/software/technology  
   - Venture fund LP Asia/SEA/India  
   - **Negative:** PE-only / direct-only / large minimum  
   - Mumbai/India family office venture fund  

Results deduped, reranked (NVIDIA NIM), PitchBook injected at top when cookies available.

### How `gate_explain.yaml` reasons through a prospect

The system prompt instructs the model as **Contra Gate Analyst** for MyAsiaVC / Contra VC ($30M Fund I). Key instruction blocks:

1. NFX Signal = angel network, not LP evidence  
2. Screening mode posture (`nfx_individual` lean NO; `institutional` lean REVIEW)  
3. PitchBook = ground truth when authenticated  
4. **GP ≠ LP** — employer portfolio is never personal LP evidence  
5. Probabilistic C1–C4 (`pass` / `fail` / `unknown`)  
6. **Appetite Engine** — infer from fund LP decisions only (full criteria in **Appendix D**)  
7. Negative inference tags and archetype assignment  
8. Structured output with length limits  

The user template injects backend profile, signal checklist, allocation history, similar LPs, and web research. The LLM returns `GateExplanation` JSON; deterministic layers validate afterward.

### Gate signal checklist (deterministic)

From `evaluator.py`. Need **≥2 signals met** for evaluator recommendation YES (advisory only; LLM verdict is primary).

| ID | Met when |
|----|----------|
| `icp_qualified` | Tier 1/2 + `core_pass` |
| `syndicate_fund_lp` | `is_fund_lp` (≥1 fund deal in syndicate data) |
| `syndicate_upgrade` | ≥1 fund deal AND ≥$5k committed |
| `warm_path` | `warm_path_count > 0` |
| `benchmark_rank` | On Contra Top-200 |
| `appetite_emerging_manager` | `em_appetite` or `fund_i_appetite` moderate/strong |
| `appetite_ai_tech` | `ai_tech_appetite` moderate/strong |
| `appetite_venture_fit` | `venture_appetite` moderate/strong |
| `similar_lp_precedent` | ≥MIN_SIGNAL_COUNT similar LPs above MIN_SIGNAL_SCORE |
| `analyst_fact_*` | Analyst fact contains LP-confirming keywords (max 2) |

**Hard blocks:** already in CRM; ICP excluded; direct/PE-only in exclusion reason.

### Haiku → Sonnet escalation

1. **Triage:** Claude Haiku 4.5 (default when `ANTHROPIC_API_KEY` set)  
2. **Escalation:** If triage returns `yes` or `review`, re-run with **claude-sonnet-4-5** (`GATE_ESCALATION=true`, default). Escalated verdict wins.  
3. Clear NOs stay on cheap tier.

### Quality checks

**`appetite_validator.py`** (downgrade-only): GP title + no LP commits → `no_fund_lp_history`; cap EM/fund-I without evidence; nfx zero-evidence REVIEW → NO; institutional thin NO → REVIEW; strip hedge language from NO summaries.

**`evidence_verifier.py`:** Remove unquotable `lp_commitments_found`; YES with zero verified commits → REVIEW (institutional) or NO (nfx).

### Final verdict

Hard blocks → NO. Else `apply_appetite_adjustments(llm_recommendation, appetite, screening_mode)`. LLM is primary; evaluator signal count is context only.

### Persistence (YES/REVIEW)

`persist_gate_findings`, `upsert_dossier_from_gate`, `record_gate_review`, in-memory `GateSession`. **Does not auto-create CRM lead** — manual Add to CRM required.

---

## 5. The AI Layer in Detail

| Use case | Model (default) | Output schema |
|----------|-----------------|---------------|
| Gate triage | `claude-haiku-4-5` | `GateExplanation` |
| Gate escalation | `claude-sonnet-4-5` | `GateExplanation` |
| Gate knowledge enrich | NVIDIA NIM (task router) | Analyst bullets appended to web context |
| Outreach draft | `gpt-4o` (`OUTREACH_LLM_MODEL`) | `OutreachDraft` |
| Outreach critique | Same | `OutreachCritique` → revise loop |
| Deep web research | OpenAI Responses API | Structured notes + URLs |

**Groq fallback:** `get_llm_client()` wraps `ResilientLLMClient` when `PULSE_LLM_AUTO_SWITCH=true`. On context-size or rate-limit errors, cycles larger models then cross-provider fallbacks (NVIDIA Llama, Anthropic Haiku, OpenAI mini, Groq). Groq rotates `GROQ_API_KEY` through `_9` on TPD exhaustion. Default Groq model: `llama-3.3-70b-versatile`.

**Confidence:** Gate returns `high` / `medium` / `low`. Batch signals get uncertainty columns from `pulse derive`. Temporal decay: `exp(-Δt / 365 days)` per `prompts/uncertainty.yaml`.

---

## 6. The CRM and Outreach Layer

**Source:** `contra/crm/writer.py`, `contra/crm/outreach.py`

### Gate YES → CRM lead (manual)

1. Gate returns `session_id` with `yes=True` or `is_review=True`.  
2. GP clicks **Add to CRM** → `add_lead_from_gate(con, session_id)`.  
3. LLM extracts CRM fields → `crm_leads` row with `source='gate'`.  
4. `persist_from_session()` enriches allocators if needed.

Alternate: `promote_prospect()` from ICP/syndicate queue without full gate.

### Outreach draft generation

`generate_outreach_draft()`: load lead + dossier → resolve archetype → fresh deep research → extract 3 insight angles → build prompt with ranked signals → LLM draft → critique-revise loop (max 2) → `crm_outreach_drafts` + Airtable sync.

Blocked if no research and no signals ("run gate first").

### Hook rules, archetypes, critique

Full detail in **Appendix C**.

### Static pitch block

LLM instructed to include verbatim after CTA. Mandatory facts in `_CONTRA_STORY_INGREDIENTS` (`outreach.py`): $30M Fund I, $500–750K checks, ~30 companies, 50% Asian founders thesis, B2B AI focus, $70M+/300+ companies/6,000+ LPs via MyAsiaVC, institutional form of community.

### Email send state

Drafts: `draft` → `approved` → `sent`. `update_draft_status("sent")` updates DB + Airtable only — **no SMTP/mail transport**. GP sends manually.

---

## 7. Warm Paths

**Graph term:** A `mutual_connection` edge linking an `institutional_prospect` to a bridge allocator (usually syndicate LP) via 2-hop co-invest inference.

**View:** `v_warm_paths` in `schema/views.sql` — joins `relationships_effective`, `relationship_evidence` (`graph_path_inference`), prospect + bridge allocators.

**Gate use:** `warm_path` signal; outreach mentions warm paths when `warm_path_count > 0`.

**Compounding:** Each syndicate deal adds co-invest edges (≥3 shared deals threshold) → more bridges → more warm paths.

---

## 8. Signal Accumulation and Scoring History

**Batch signals:** Idempotent rewrite per extractor; `signal_evidence` rows with provenance; `pulse derive` writes uncertainty columns.

**Gate signals:** Persisted to `lp_dossiers.appetite_json` and allocator appetite fields on YES/REVIEW.

**ICP scores:** Versioned by `icp_version`; full delete-and-rewrite per refresh for current version.

**ICP version mismatch:** Scoring writes `4.2`; views/graph queries often filter `4.1` — operational gap until aligned.

---

## 9. What PULSE Produces — Outputs Summary

| Output | What it is | Who acts |
|--------|------------|----------|
| **ICP score** | `icp_scores` row: gates, S1–S7, fit, tier | GP prioritizes via `v_crm_icp_queue` |
| **Gate verdict** | YES/REVIEW/NO + appetite + LP commitments | GP decides Add to CRM |
| **Dossier** | `lp_dossiers`: research, history, appetite | GP + outreach agent |
| **Outreach draft** | Personalized email in `crm_outreach_drafts` | GP reviews, sends manually |
| **Warm path flag** | Bridge LP + strength via `v_warm_paths` | GP requests intro via bridge |

---

## 10. Known Limitations

- **ICP version mismatch** — Scoring at 4.2; views at 4.1  
- **No live email send** — Draft/approved/sent status only  
- **Empty `crm_contacts`** — Unless `export.csv` imported; truth in `crm_leads`  
- **Calibration noise** — Fuzzy benchmark name joins can mis-tune thresholds  
- **Silent failure handling** — `record_gate_review`, dossier upsert, contact extract can fail without blocking gate  

---

## 11. The Moat

1. **Syndicate graph** — Thousands of evidenced co-invest edges + warm-path inference  
2. **Scoring history** — Versioned ICP, client decisions, rejection-driven exclusions (E13/E14)  
3. **Signal accumulation** — 19 signal types + evidence + human-review overlays  
4. **Gate + dossier loop** — Live screening teaches what batch text scoring missed  

The moat is provenance-linked memory: sources → graph → scores → gate → CRM → outcomes → tighter rules.

---

# Appendices (Full Reference Data from Code)

## Appendix A — All 19 Signal Types (`agents/scoring/signal_types.py`)

**Note:** Older docs say "16 types." The live catalog has **19** — 16 from Phase 4b expansion plus 3 Contra syndicate extensions. These populate the `signals` table and are **separate from ICP soft signals S1–S7** (which only affect `icp_scores.fit_score`).

### Signal evidence types

| `evidence_type` | Typical source |
|-----------------|----------------|
| `signal_heuristic` | Keyword / text extractor |
| `signal_investment_pattern` | Latent extractor from investments |
| `signal_graph_metric` | Graph topology metrics |
| `signal_icp_mirror` | ICP soft-signal mirror |
| `signal_connectivity` | Prospect inference |
| `contradicts_value` | Contradiction detector |

### The 19 signal types

| Group | Type | Writer module | What it measures |
|-------|------|---------------|------------------|
| **Text / heuristic (original 8)** | `response_speed` | `signal_extractor.py` | Decision speed proxy from LP type |
| | `exploratory_check` | `signal_extractor.py` | Contact data richness (email, LinkedIn, validated QA) |
| | `operator_background` | `signal_extractor.py` | Contact title suggests operator/GP vs admin |
| | `em_participation` | `signal_extractor.py` | Emerging-manager phrase depth in prospect text |
| | `geography_overlap` | `signal_extractor.py` | Allocator geography vs fund target regions |
| | `social_proximity` | `prospect_inference.py` | Graph social proximity metric |
| | `network_density` | `prospect_inference.py` | Syndicate network density |
| | `deployment_velocity` | `signal_extractor.py` | Active vs paused investing from status + text |
| **Graph / connectivity** | `bridge_strength` | `prospect_inference.py` | Strength of bridge to syndicate |
| | `warm_path_count` | `prospect_inference.py` | Count of mutual_connection paths |
| **Investment latent** | `coinvest_intensity` | `latent_signal_extractor.py` | Co-investment pattern intensity |
| | `recent_activity_recency` | `latent_signal_extractor.py` | Recency of investment activity |
| | `shared_deal_count` | `latent_signal_extractor.py` | Shared deals with network |
| | `stage_alignment` | `latent_signal_extractor.py` | Mirrors ICP S5 stage keywords |
| | `proxy_fund_overlap` | `latent_signal_extractor.py` | Mirrors ICP S7 proxy funds |
| | `clean_profile` | `latent_signal_extractor.py` | Mirrors ICP S6 conflict absence |
| **Syndicate (Contra extension)** | `fund_lp_behavior` | `syndicate_signal_extractor.py` | Ratio of fund vs SPV/direct deals |
| | `syndicate_depth` | `syndicate_signal_extractor.py` | Depth of syndicate participation |
| | `syndicate_recency` | `syndicate_signal_extractor.py` | Recency of syndicate activity |

---

## Appendix B — Full E1–E14 Exclusion Phrase Lists (`agents/scoring/icp_spec.py`)

Applied in `_score_exclusions()` in `icp_scorer.py` against scoring text + client comments (+ country for sanctions, + client_status for E11).

### E1 — PE/Buyout Primary
```
pe focus, private equity focus, pe primary, buyout focus, buyout only, pe/buyout,
private equity only, pe only, private equity is the dominant, primarily pe
```

### E2 — VC Secondaries Only
```
vc secondaries, secondaries only, secondary focus, secondary vc, vc secondary,
secondaries per website
```

### E3 — Real Estate Primary
```
real estate focus, real estate primary, real estate only, primarily real estate,
real estate is the dominant, real estate investment trust, reit focus
```

### E4 — Web3/Crypto Only
```
web3 focus, blockchain focus, crypto focus, crypto only, crypto-native, web3-native,
nft focus, defi focus, blockchain primary, crypto primary
```

### E5 — Healthcare/Life Sciences Only
```
healthcare only, healthcare focus, life sciences only, lifesciences only,
life science focus, biotech only, biotech focus, medical only, pharma focus
```

### E6 — Geography-Locked Non-Qualifying (+ sanctioned countries)
**Phrases:**
```
alberta only, alberta-only, hk only, hong kong only, dc only, domestic only,
latam only, latin america only, europe only, africa only, australia only,
single region mandate, local mandate
```
**Sanctioned countries (separate check on HQ/country field):**
```
iran, north korea, dprk, myanmar, burma, cuba, venezuela, belarus, russia, syria, sudan
```

### E7 — Impact/Philanthropy Primary
```
impact investing focus, philanthropy focus, philanthropic mandate, esg-screened,
impact only, blended finance only, social impact primary, non-profit focus
```

### E8 — Does Not Back Emerging Managers
```
does not invest in emerging, no emerging manager, only established managers,
only tier 1 managers, sequoia only, andreessen only, top-tier only,
dont see any emerging managers, not emerging managers,
they dont seem to invest in emerging managers, no evidence of emerging manager,
established track record only, proven track record required
```

### E9 — Check Size Mismatch
```
write larger checks, larger checks, minimum ticket, we do not fit bucket,
not fit bucket, check size too small, below minimum, ticket too small
```

### E10 — Direct Only (No Fund LP)
```
does not invest in funds, direct investments only, no fund investments,
exclusively direct, direct investments vs fund, does not take lp positions,
does not invest in vc funds
```

### E11 — Blacklist / Prior Contact (client_status, not phrase scan)
```
rejected - blacklist
rejected - seems to conflict
```

### E12 — Prop Trading / Non-VC Financial
```
prop trading firm, proprietary trading, hedge fund without vc, broker-dealer,
market maker, algo trading
```

### E13 — Fund Size Minimum Exceeds $35M (v4.2)
```
minimum fund size, minimum commitment size, fund size minimum, fund size below,
funds below $50m, funds below $75m, funds below $100m, minimum fund,
fund must be at least, won't consider funds below, does not invest in funds under,
only funds over, only funds above, fund size too small, not the right size, size is too small
```

### E14 — US/Europe-Only Mandate (v4.2)
```
us and europe only, us/europe only, us & europe only,
invest only in the us and europe, only us and europe,
north america and europe only, us and european markets only,
restricted to us and europe, us-europe mandate, no asia mandate, no asia exposure,
not investing in asia, western markets only, developed markets only
```

`ALL_HARD_EXCLUSION_PHRASES` concatenates E1–E8, E10, E12–E14 (E9 scanned separately; E11 via status).

---

## Appendix C — Outreach Archetypes and Critique Rules (`contra/crm/outreach.py`)

### Archetype playbooks (`_ARCHETYPE_PLAYBOOKS`, lines 71–153)

Keyed to gate `Archetype` enum (`contra/gate/models.py`). Each playbook defines how to open the email for that allocator type.

| Archetype | Opening strategy |
|-----------|------------------|
| `fund_of_funds` | Lead with manager-selection thesis; sentence 3 names a fund they backed |
| `family_office` | Human, legacy identity; specific deal or focus area as evidence |
| `founder_lp` | Peer tone; what they built or angel thesis; named company as evidence |
| `corporate_investor` | Org mission/thesis; specific program or cohort |
| `institutional_lp` | Institution mandate; specific program or allocation |
| `asia_specialist` | Regional conviction about Asia-origin global founders |
| `technology_specialist` | AI/tech infrastructure thesis; named portfolio co |
| `emerging_manager_specialist` | Conviction about Fund I managers; named anchored fund |
| `generalist` / `unknown` | Clearest thesis signal available, then bridge, then specifics |

### CRM `investor_type` → archetype map (`_TYPE_TO_ARCHETYPE`)

Maps messy CRM strings when gate has not assigned archetype: e.g. `fof` → `fund_of_funds`, `family office` → `family_office`, `angel`/`gp`/`founder` → `founder_lp`, `pension`/`endowment` → `institutional_lp`, etc.

### Email structure rules (`_SYSTEM`, lines 202–341)

**Decision tree:** Identify (A) their thesis and (B) a specific recent fact. If both → 3-sentence hook. If only thesis → 2 sentences. If neither → NO-HOOK template (no fabrication).

**WITH HOOK opening order:**
1. Sentence 1 — THEIR thesis (no "Contra" mention)  
2. Sentence 2 — Bridge to Contra's shared conviction  
3. Sentence 3 — Specific research fact as corroborating evidence  
4. Verbatim CTA: `Our Fund I factsheet is here: https://contravcfactsheet.netlify.app/ and I'd love to find time for a call if it sparks any questions.`  
5. `*Here's some more context on what we're building:*`  
6. Static pitch (verbatim, 5 paragraphs)  
7. Sign-off: `{sender}` + `General Partner, Contra VC`

**Non-negotiable rules:**
1. No em dashes  
2. No "Contra" in sentence 1  
3. No bullet points in body  
4. No dry research fact as sentence 1; no famous/generic hooks  
5. Prolific angels: famous portfolio cos are NOT specific facts  
6. Static pitch verbatim — do not rewrite  
7. No paragraph between CTA and static pitch  
8. Sign-off format fixed  
9. Max 3 sentences before CTA  
10. Do not fabricate — use NO-HOOK if uncertain  

### Critique-revise loop (`_critique_and_revise`, lines 364–425)

Max 2 iterations. `OutreachCritique` returns PASS or REVISE.

**Six critique criteria:**
1. Sentence 1 opens with THEIR thesis, not a dry data point  
2. Sentence 2 bridges their thesis to Contra's explicitly  
3. Sentence 3 has specific research as evidence (not in sentence 1)  
4. Static pitch includes core metrics ($30M, $500–750K, $70M MyAsiaVC, 50% Asian founders)  
5. No em-dashes in personalized opening  
6. No jargon: `"alpha"`, `"lens"`, `"deal flow"`, `"archetype"`

On REVISE: regenerate with critique reasoning appended to prompt.

---

## Appendix D — Appetite Grading Criteria (`prompts/navigator/gate_explain.yaml`)

**Source:** APPETITE ENGINE section (system prompt lines 169–213). Enforced in code by `appetite_validator.py` and `evaluator.py` (`strong`/`moderate` count toward gate signals).

### Core principle

Infer appetite from **historical fund LP decisions as a limited partner**. Angel checks into startups ≠ fund LP appetite. Employment at a VC ≠ fund LP appetite.

### Five appetite dimensions

Each graded: **`strong` | `moderate` | `weak` | `none` | `unknown`**

| Dimension | Definition | STRONG/MODERATE requires |
|-----------|------------|--------------------------|
| `em_appetite` | Backs emerging / first- and second-time managers **as LP** | ≥1 named external LP commitment (default: unknown) |
| `fund_i_appetite` | Writes LP checks into Fund I (anchor / first close) | ≥1 named external LP commitment |
| `ai_tech_appetite` | AI, robotics, deep tech, software (via backed funds or portfolio) | Evidence from allocation behavior |
| `venture_appetite` | Commits to VC funds as LP (vs direct / PE / public) | Evidence from allocation behavior |
| `geography_appetite` | SE Asia, North America, or Middle East exposure | Evidence from backed funds/regions |

### Reasoning examples (from prompt)

| Evidence pattern | Inference |
|------------------|-----------|
| LP in Hustle Fund + Weekend Fund + Conviction | STRONG EM, STRONG Fund-I |
| LP in a16z, Sequoia, Lightspeed only | Established-manager preference; EM WEAK |
| 20 angel deals, no fund LP history | `venture_appetite` UNKNOWN/WEAK |
| GP at small fund, no external LP history | C1 UNKNOWN, `em_appetite` UNKNOWN |
| LP in SE Asia / India seed funds | `geography_appetite` STRONG |

### Recency

Weight last 24 months heavily. If all evidence >5 years old → lower confidence, lean REVIEW.

### Behavioral archetypes (gate-assigned)

`fund_of_funds`, `family_office`, `institutional_lp`, `emerging_manager_specialist`, `asia_specialist`, `technology_specialist`, `founder_lp`, `corporate_investor`, `generalist`, `unknown`

- **Favorable for Fund I:** `emerging_manager_specialist`, `founder_lp`, `fund_of_funds`  
- **Unfavorable:** `corporate_investor`, direct-only angel, pure GP with no LP activity

### Negative flags (active search required)

| Tag | Effect |
|-----|--------|
| `pe_only`, `direct_only`, `no_venture`, `no_fund_lp_history`, `angel_only`, `nfx_angel_only` | Strong → NO (nfx) or review→no (institutional) |
| `established_managers_only`, `min_check_too_large`, `wrong_geography`, `inactive_recent` | Soft → REVIEW |
| `no_fund_lp_history` | Set when web **confirms** GP/direct-only with no external LP commits (not mere DB absence) |

### Code enforcement (`appetite_validator.py`)

- GP title regex + no external LP language → cap `em_appetite`/`fund_i_appetite` to unknown, add `no_fund_lp_history`  
- EM/fund-I moderate/strong without LP commit phrases in `allocation_evidence` → downgrade  
- Strip employer-fund portfolio from `allocation_evidence`  
- nfx_individual + strong negative → force NO  
- institutional + thin NO + C1 unconfirmed → upgrade to REVIEW with flip condition

### Evaluator signal threshold

`evaluator.py`: `em_appetite` OR `fund_i_appetite` in `{strong, moderate}` → `appetite_emerging_manager` signal met. Same pattern for `ai_tech_appetite` and `venture_appetite`.

---

## Source File Index

| Topic | Canonical path |
|-------|----------------|
| ICP spec (C1–C4, E1–E14, S1–S7) | `agents/scoring/icp_spec.py` |
| ICP scorer | `agents/scoring/icp_scorer.py` |
| Signal types (19) | `agents/scoring/signal_types.py` |
| Signal extractors | `agents/scoring/signal_extractor.py`, `latent_signal_extractor.py`, `syndicate_signal_extractor.py` |
| Graph / warm paths | `agents/graph/prospect_inference.py`, `schema/views.sql` |
| Gate runner | `contra/gate/runner.py` |
| Gate evaluator | `contra/gate/evaluator.py` |
| Gate LLM prompt | `prompts/navigator/gate_explain.yaml` |
| Gate validators | `contra/gate/appetite_validator.py`, `evidence_verifier.py` |
| Web research queries | `agents/research/web_search.py` (`build_lp_fit_queries`) |
| CRM writer | `contra/crm/writer.py` |
| Outreach | `contra/crm/outreach.py` |
| Orchestrator | `contra/orchestrator.py` |
| LLM client / Groq fallback | `agents/research/llm_client.py` |
