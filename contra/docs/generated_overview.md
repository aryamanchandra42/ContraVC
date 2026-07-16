# PULSE: The LP Intelligence System
**A complete plain-English guide for General Partners**

---

> **What this document is:** A full walkthrough of how PULSE works, from raw data to outreach draft, written for a GP who is smart but not a developer. Every signal, every gate, every architectural decision is explained at the level you need to use, defend, and build on this system.

---

## 1. What PULSE Is and Why It Exists

Fundraising in private markets is not a list problem. It is a judgment problem under incomplete information. Contra VC already has prospect spreadsheets, syndicate history, call notes, benchmarks, and a CRM. But those live in separate places and do not, by themselves, answer: Who should we talk to first? Who is structurally wrong for us? Who looks cold but is actually warm through our network? When two sources disagree, what do we believe?

PULSE (Private-market Unified LP Signal Engine) is Contra's institutional memory layer for exactly that problem. It ingests raw source files and turns them into:

- A ranked, tiered prospect view aligned to the LP Scoping workbook
- A relationship graph showing who co-invested with whom, and bridge paths into syndicate capital
- 19 signal types per allocator, drawn from text, investment patterns, and graph connectivity
- A live LP Gate that screens individual names using web research and AI
- A CRM and outreach layer that turns Gate YES/REVIEW verdicts into leads and draft emails

The batch pipeline runs without any LLM for ingestion, normalization, graph building, and ICP scoring. LLMs are used only for live gate screening, outreach drafting, and optional enrichment, always with structured outputs and deterministic guardrails on top.

PULSE is designed for explainability. Every relationship edge has evidence rows. Human review overrides are append-only and applied at read time via SQL views, not by silently rewriting source data. If a partner rejects a bad fuzzy match or revises a label, that decision is stored forever and the original derived value is always recoverable.

---

## 2. The Data Foundation

### Where LP data comes from

The orchestrator runs eight stages on every refresh:

| Stage | What it does |
|---|---|
| **Ingest** | Reads files by type (Excel, PDF, Word, LinkedIn CSV, NFX xlsx) and loads every row into `entities_raw` with a content hash. Same hash = skip re-insert. Ingestion is fully idempotent. |
| **Normalize** | Resolves entity names, creates canonical allocator records, loads syndicate rosters and transactions, processes CRM exports. Uses fuzzy matching with a two-threshold system (see below). |
| **Extract** | Pulls structured ontology terms from documents. |
| **Derive** | Computes uncertainty columns and applies time decay on relationships and signals. Run separately from ingestion so evidence from multiple sources combines correctly before confidence is calculated. |
| **Graph** | Builds co-investment edges, infers warm paths, saves graph to disk. |
| **Score** | Runs ICP scoring, signal extraction, latent signals, syndicate signals, contradiction detection. |
| **Calibrate** | Compares PULSE tiers to the Contra Top-200 benchmark and tunes thresholds. |
| **Catalog** | Refreshes data catalog for navigator queries. |

### Fuzzy name matching: how entity resolution works

When PULSE reads "GIC Singapore" in one file and "GIC" in another, it needs to know these are the same entity. It uses rapidfuzz with a two-threshold system:

- **Above 0.90 similarity:** auto-merged. Catches clear name variants without human review.
- **Between 0.70 and 0.90:** written to a review queue for partner judgment.
- **Below 0.70:** not a match. Treated as separate entities.

This two-tier design means the pipeline never blocks waiting for a human, but never silently merges ambiguous names either. Partners have already used this queue to reject hundreds of false-positive alias matches (similar suffixes across unrelated family offices).

### The syndicate co-investment graph

The syndicate data comes from the MyAsiaVC LP roster: roughly 5,900 individual LPs and 16,800 LP-to-deal transactions. Two LPs who backed the same SPV or fund get a `co_invested` edge, but only if they share at least 3 deals. This threshold keeps the graph signal-rich rather than noise-dense.

The prospect inference layer then walks 2-hop paths: institutional prospect → bridge syndicate LP → another node. That is how a family office that never appeared on your prospect sheet can still show a warm intro route through someone who co-invested with your syndicate.

The graph has three relationship layers:

| Layer | What it means | Live count |
|---|---|---|
| `co_invested` | Two LPs backed the same syndicate vehicles repeatedly | ~28,550 edges |
| `invested_with` | Two LPs share fund-vehicle exposure from normalized investments | ~167 edges |
| `mutual_connection` | Institutional prospect connected to syndicate LP via 2-hop bridge | ~50 edges |

> **Why `invested_with` edges are capped:** One syndicate fund had 492 LPs. Naive pairwise combination hung the graph stage for over 5 minutes. The system now caps at 40 LPs per fund and 50,000 total edges. Full clique enumeration belongs in offline analytics, not the default pipeline.

### Why human overrides never destroy source data

When a partner rejects a bad match or revises a label, that decision is stored as a new row in `human_reviews`. The original normalized rows are never mutated. Effective state is exposed via `_effective` SQL views (`relationships_effective`, `allocators_effective`) that apply overrides at read time.

This is the same design used in financial ledgers: you never delete a ledger row, you write a reversal. The result is that every tier, edge, and signal has a traceable history, and partner judgment accumulates without corrupting the underlying source data.

### What lives in the database

| Table | What it stores |
|---|---|
| `entities_raw` | Immutable ingested rows. Every normalized record traces back here. |
| `allocators` | Canonical LP entities: name, type, geography, appetite fields, population tag. |
| `entity_aliases` | Alternate names mapped to canonical allocator IDs for name matching. |
| `funds` | Fund and SPV vehicles that syndicate LPs invested in. |
| `investments` | LP commitments: who invested in what, how much, and when. |
| `relationships` | Graph edges with type, weight, confidence, and temporal fields. |
| `relationship_evidence` | Provenance for each edge. Edges without evidence are invalid by design. |
| `signals` | Per-allocator signals across 19 canonical types. |
| `signal_evidence` | Provenance for each signal value. |
| `icp_scores` | Batch ICP evaluation per allocator: gates, soft signals, fit score, tier, client decision. |
| `rejections` | Extracted rejection reasons from outreach history. |
| `benchmark_rankings` | External Contra Top-200 rankings for calibration. |
| `human_reviews` | Append-only partner overrides. Never destroys history. |
| `crm_contacts` | Legacy import from FundingStack export. Separate from the live pipeline. |
| `crm_leads` | Active CRM workspace: gate verdict, ICP tier, contacts, pipeline stage, score. |
| `crm_gate_reviews` | Latest gate verdict per name, for ICP queue tracking. |
| `lp_dossiers` | Durable gate memory: research notes, appetite, LP commitments, verdict history. |
| `crm_outreach_drafts` | Generated emails in draft, approved, or sent status. |

> Always query through `_effective` views and convenience views like `v_lp_profile`, `v_warm_paths`, `v_crm_icp_queue`. Raw tables do not reflect human overrides.

---

## 3. Allocator Types: The Full Taxonomy

PULSE recognizes the following canonical allocator archetypes. These drive the S3 (LP type priority) and S4 (decision speed) soft signal scores in ICP scoring, and the outreach archetype selection in the CRM layer.

| Archetype | What it means | Key identifiers |
|---|---|---|
| `fund_of_funds` | Invests exclusively into other funds; highest ICP priority | "fund of funds," "FoF," "FOF" |
| `family_office_multi` | Multi-family office managing capital for several families | "multi family office," "MFO" |
| `family_office_single` | Single family office for one family; faster decisions | "family office," "SFO," "single family" |
| `asset_manager` | Professional investment manager deploying institutional capital | "asset manager," "investment manager" |
| `sovereign_wealth_fund` | State-owned investment fund | "SWF," "GIC," "Temasek," "Mubadala," "ADIA" |
| `endowment` | University or institutional endowment | "endowment," "university endowment" |
| `pension_fund` | Pension or superannuation fund; slowest decisions | "pension fund," "superannuation," "CPF," "GPIF" |
| `development_finance_institution` | Development bank or multilateral with impact mandate | "DFI," "IFC," "ADB," "AIIB" |
| `insurance` | Insurance or reinsurance firm deploying alternatives | "insurance," "insurer," "reinsurance" |

---

## 4. Geography Clusters

PULSE maps allocator geography to the following canonical clusters, which feed C4 gate scoring and inform outreach context.

| Cluster | Key identifiers |
|---|---|
| `singapore_hq` | "Singapore," "MAS regulated," "ACRA" |
| `hong_kong_hq` | "Hong Kong," "HK," "SFC," "HKMA" |
| `middle_east_hub` | "Dubai," "ADGM," "DIFC," "Abu Dhabi," "Saudi Arabia" |
| `southeast_asia` | "SEA," "ASEAN," "Indonesia," "Vietnam," "Thailand" |
| `south_asia` | "India," "SAARC," "Bangladesh" |
| `emerging_markets` | "EM," "emerging market," "frontier" |

---

## 5. Signal Extraction: Before Scoring Begins

Before any ICP scoring happens, four extraction processes run over raw entity data and write structured signals to the database. Each signal row links to a `signal_evidence` row, which links back to `entities_raw`. Nothing is an opaque float; every value can be traced to a source passage or investment record.

### signal_extractor.py
Reads raw allocator text and investment history. Derives signals from document language: what the prospect has said about themselves, their portfolio, their thesis.

### latent_signal_extractor.py
Derives signals that are not directly stated but can be inferred. If an allocator's portfolio companies are all AI-native, that is a latent AI thesis signal even if the word "AI" never appears in their profile. Reads from investment records, not text.

### prospect_inference.py
Handles allocators with thin or missing profiles by inferring signals from graph connectivity. If a prospect co-invested with three LPs who are confirmed AI-focused, that pattern is itself a signal. Also writes the `mutual_connection` edges that power warm paths.

### contradiction_detector.py
Scans for conflicting signals on the same allocator. If one source says "emerging manager focus" and another says "minimum $50M fund size," that is flagged. Rather than hiding the conflict, PULSE emits a `contradicts_value` evidence row. The contradiction score and source agreement score surface in ICP scoring and gate prompts.

### Why evidence is a normalized table, not inline JSON

Evidence is stored in a separate `signal_evidence` table (one row per supporting source per signal), not as a JSON blob on the signal row. This means:

- You can count evidence rows with a SQL query
- Contradiction detection can compare individual assertions across sources
- `pulse derive` can recompute confidence from evidence without touching the original signal value
- Every enrichment is auditable and reversible

---

## 6. The 19 Canonical Signal Types

These are defined in `signal_types.py` and represent the complete set of things PULSE tracks per allocator. Each has an evidence type that describes how it was derived.

### Original 8 (from text and interaction data)

| Signal | What it measures | Evidence type |
|---|---|---|
| `response_speed` | How quickly the LP responds to outreach. Fast responders are higher priority for first-close. | `signal_heuristic` from interaction timestamps |
| `exploratory_check` | Whether the LP has made small exploratory commitments before going full size. Signals willingness to test before committing. | `signal_investment_pattern` |
| `operator_background` | Whether the LP has a founder or operator background. Operators tend to be more risk-tolerant and conviction-driven than institutional committee LPs. | `signal_heuristic` from ICP notes |
| `em_participation` | Historical participation in emerging manager funds. Direct evidence of EM appetite beyond what they say they do. | `signal_investment_pattern` from investment records |
| `geography_overlap` | Whether the LP's existing geography overlaps with Contra's focus (SE Asia, NA, ME). | `signal_heuristic` from allocator geography field |
| `social_proximity` | Shared network connections between the LP and Contra's GP network or syndicate. | `signal_connectivity` from relationship graph |
| `network_density` | How many syndicate co-investment clusters the LP appears in. High density means they are a central node in private markets capital flow. | `signal_graph_metric` |
| `deployment_velocity` | How fast the LP deploys capital across their investment history. Relevant for first-close timing pressure. | `signal_investment_pattern` from investment timestamps |

### Phase 4b expansion: latent and connectivity signals (8 additional)

| Signal | What it measures | Evidence type |
|---|---|---|
| `bridge_strength` | The quality of the warm intro path between a prospect and Contra's network. High bridge strength means the connecting LP is itself well-connected and credible. | `signal_graph_metric` |
| `warm_path_count` | Number of distinct warm intro paths to this prospect through the syndicate graph. More paths = more intro options. | `signal_graph_metric` |
| `coinvest_intensity` | How frequently the LP co-invests alongside others in the same vehicles. High intensity suggests they are an active syndicate participant, not a passive LP. | `signal_graph_metric` |
| `recent_activity_recency` | How recently the LP made an investment. Stale activity may indicate they are between deployment cycles or winding down. | `signal_investment_pattern` |
| `stage_alignment` | Whether the LP's historical investments match pre-seed to Series A. Derived from investment records, not just stated thesis. | `signal_investment_pattern` |
| `proxy_fund_overlap` | Whether the LP has backed peer funds named in the ICP spec (Neon, Better Capital, Mana, Afore, 20VC, etc.). Backing a peer fund is strong evidence of EM appetite and stage fit. | `signal_icp_mirror` |
| `clean_profile` | Absence of conflict phrases (web3-only, PE focus, healthcare-only, direct-only). A high clean_profile score means nothing in the record contradicts the thesis. | `signal_icp_mirror` |
| `shared_deal_count` | Number of deals this LP shares with other LPs in the syndicate universe. Feeds co-invest graph edge weight. | `signal_graph_metric` |

### Contra syndicate extension (3 additional)

| Signal | What it measures | Evidence type |
|---|---|---|
| `fund_lp_behavior` | Whether the LP has demonstrably acted as a fund LP (not just a direct investor). The most important C1 evidence signal. | `signal_investment_pattern` |
| `syndicate_depth` | How deeply embedded the LP is in the MyAsiaVC syndicate specifically, measured by deal count and relationship density. | `signal_graph_metric` |
| `syndicate_recency` | How recently the LP was active in the MyAsiaVC syndicate. Recent activity signals maintained relationship and current deployment. | `signal_graph_metric` |

### Signal evidence types

| Evidence type | What it means |
|---|---|
| `signal_heuristic` | Derived from text keywords or stated profile language |
| `signal_investment_pattern` | Derived from actual investment records and transactions |
| `signal_graph_metric` | Computed from the co-investment graph structure |
| `signal_icp_mirror` | Reflected directly from ICP scoring logic |
| `signal_connectivity` | Derived from relationship graph proximity |
| `contradicts_value` | This evidence contradicts another signal value on the same allocator |

---

## 7. Confidence, Uncertainty, and Time Decay

### How confidence is calculated

Confidence is not set at ingestion. It is derived by `pulse derive` after all evidence is collected, using a noisy-OR combinator across evidence rows. This means:

- A signal observed in 3 different files has higher confidence than one from a single file
- Re-running `pulse derive` with the same evidence always produces byte-identical confidence values
- Adding a new data source automatically propagates its effect to all downstream confidence scores

### Confidence interpretation

| Range | What it means |
|---|---|
| 0.90 to 1.00 | Near-certain: strong multi-source corroboration |
| 0.75 to 0.90 | High: single strong source, or multiple moderate sources |
| 0.60 to 0.75 | Medium: one moderate source, or heuristic match |
| 0.40 to 0.60 | Low: weak evidence, surfaces for human review |
| 0.00 to 0.40 | Very low: contradicted or insufficient evidence |

### Source agreement interpretation

| Score | What it means |
|---|---|
| Above 0.80 | Strong agreement across sources |
| 0.50 to 0.80 | Partial agreement; some sources missing data |
| Below 0.50 | Disagreement or sparse observation |

### Contradiction score interpretation

| Score | What it means |
|---|---|
| Above 0.30 | High contradiction: surface for human review |
| 0.10 to 0.30 | Some tension: monitor |
| Below 0.10 | Low contradiction |

### Time decay

Signals and relationships decay in confidence over time. The formula is:

```
temporal_confidence = confidence x exp(-days_since_last_active / 365)
```

A signal from 18 months ago carries roughly 22% of its original confidence weight. A signal from exactly 365 days ago carries about 37%.

This is a deliberate, deterministic formula rather than a learned ML weight. The reasons:

- No labeled training data exists for "how much has this relationship decayed"
- The formula is fully interpretable: given any two dates and the half-life config, you can calculate the exact decay manually
- Operators can tune `half_life_days` in `prompts/uncertainty.yaml` without touching code
- The formula is replayable: same inputs always produce the same decay score

---

## 8. Pipeline One: ICP Scoring (Offline Batch)

### What triggers it and what it produces

**Trigger:** The `score` stage of `contra refresh`, or `POST /api/refresh`.

**Idempotency:** Deletes all `icp_scores` rows for the current ICP version, then rewrites from scratch. Running it twice with the same data produces identical results.

**Input:** Rows from `entities_raw` where the source is the Prospects Excel workbook, sheet names matching `Prospects_m*`, `Prospects_Hong Kong`, or `Prospects_London`, with a non-empty name column.

**Output:** One `icp_scores` row per matched allocator, containing all gate results, soft signal scores, fit score, tier, and full provenance back to the source Excel row.

**Live counts (June 2026):** ~252 institutional prospects scored, 109 Tier 1 at current thresholds.

---

### The four hard gates (C1 to C4)

All four must pass. Fail any one and the allocator is Tier 4 regardless of how strong their soft signals are.

**C1: Must invest in VC funds as an LP**

Scans the prospect scoring text for two keyword groups. Must hit at least one from each:

- Group 1 (what they do): "fund," "vc," "venture capital," "lp in," "backs funds"
- Group 2 (required confirmation): "fund," "vc," "venture"

Critical rule: angel check sizes on NFX Signal are explicitly not C1 evidence. A person who writes $5-25K checks into startups directly is an angel, not a fund LP. NFX presence alone fails C1.

**C2: Must have emerging manager appetite**

Scans for phrases like "emerging manager," "fund i," "first-time fund," "dedicated emerging," "ILP program."

> **Important update (June 2026):** This gate no longer passes by default when notes are thin. Prospects without positive EM language fail C2 and land in Tier 4. This was a deliberate tightening: default-pass C2 was inflating Tier 1 with names that looked qualified on paper but had no evidence of actually backing first-time managers. Strict C2 is now backtested in automated evals.

**C3: Must have an AI or tech thesis**

Scans for phrases like "artificial intelligence," "robotics," "deep tech," "generative ai," or named portfolio companies like "OpenAI" or "Anthropic." Any single sector hit passes.

**C4: Must cover the right geographies**

Scans for Asia/APAC country names, "north america," "united states," Middle East/GCC terms, or "global"/"worldwide." Any single region hit passes.

---

### Exclusion rules (E1 to E14)

Separate from the four gates. Any exclusion forces Tier 4 regardless of gate results or fit score. Checked before soft signal scoring runs.

| Exclusion | What triggers it |
|---|---|
| E1: PE/buyout primary | Language indicating primary focus is private equity buyouts, not venture |
| E2: Secondaries only | "VC secondaries," "secondary fund," "liquidity solutions" |
| E3: Real estate primary | "real estate," "property fund," "REIT focus" |
| E4: Crypto only | "crypto-only," "web3-only," "blockchain exclusively" |
| E5: Healthcare only | "healthcare-only," "life sciences only," "biopharma focus" |
| E6: Sanctioned country | HQ in Iran, North Korea, Myanmar, Cuba, Venezuela, Belarus, Russia, Syria, or Sudan |
| E7: Geography locked (non-qualifying) | Mandate explicitly excludes Asia, emerging markets, or Contra's target regions |
| E8: Impact only | "impact-only," "SDG mandate," "concessional returns" |
| E9: Check size mismatch | "write larger checks," "minimum ticket," "not fit bucket" |
| E10: Direct only | "direct-only," "no fund LP," "only co-invest" |
| E11: Client blacklist | Client status contains "rejected - blacklist" or "rejected - seems to conflict" |
| E12: Prop trading / non-VC | Profile is a trading firm, hedge fund, or financial profile with no venture history |
| E13: Fund minimum above buffer | Fund size minimum exceeds the $35M buffer threshold |
| E14: US/Europe-only mandate | Mandate is explicitly limited to US or European venture only, no EM/Asia |

> E13 and E14 were added in ICP v4.2, derived from analysis of 949 historical outreach emails where these patterns were the most common rejection reasons.

---

### The seven soft signals (S1 to S7)

Among prospects that pass all four gates and no exclusions, PULSE computes a fit score from seven weighted soft signals.

**S1: AI investment signal (weight 25%)**

Measures depth of AI evidence in the scoring text. The highest-weight signal because AI/robotics is Contra's core thesis.

- Named AI portfolio companies (OpenAI, Anthropic, Cohere, xAI, Gemini): 2 or more = 1.0; 1 = 0.90
- Thesis keyword hits: 4 or more = 0.85; 2 or more = 0.70; 1 = 0.50; none = 0.0

**S2: Emerging manager depth (weight 20%)**

Measures quality of EM appetite beyond the binary C2 gate.

- High-confidence phrases ("emerging manager program," "fund I and fund II," dedicated allocation): any = 1.0
- Medium phrases ("emerging manager," "fund i"): 3 or more = 0.85; 2 = 0.70; 1 = 0.55; none = 0.20 (neutral-low, not a denial)

**S3: LP type priority (weight 20%)**

Lookup by allocator archetype against the scoping priority table.

| Archetype | Score |
|---|---|
| `fund_of_funds` | 1.00 |
| `family_office_multi` | 0.90 |
| `family_office_single` | 0.80 |
| `asset_manager` | 0.60 |
| `sovereign_wealth_fund` | 0.40 |
| `endowment` | 0.30 |
| `development_finance_institution` | 0.20 |
| `insurance` | 0.20 |
| `pension_fund` | 0.15 |

**S4: Decision speed (weight 15%)**

Lookup by allocator type. Weighted for Fund I first-close urgency: slow institutions that need IC approval cycles are de-prioritized even if they are structurally a fit.

| Archetype | Score |
|---|---|
| `family_office_single` (HNWI) | 1.00 |
| `family_office_multi` | 0.85 |
| `asset_manager` | 0.70 |
| `fund_of_funds` | 0.65 |
| `endowment` | 0.40 |
| `sovereign_wealth_fund` | 0.30 |
| `development_finance_institution` | 0.20 |
| `insurance` | 0.15 |
| `pension_fund` | 0.10 |

**S5: Stage alignment (weight 10%)**

Keyword hits for pre-seed, seed, Series A, early stage, and venture language in the scoring text.

- 3 or more hits = 1.0; 2 hits = 0.75; 1 hit = 0.50; none = 0.10

**S6: Clean profile (weight 5%)**

Absence of conflict phrases (web3-only, PE focus, healthcare-only, direct-only).

- No conflict hits = 1.0; 1 hit = 0.50; 2 or more = 0.10

**S7: Proxy fund overlap (weight 5%)**

Mentions of peer funds named in the ICP spec: Neon, Better Capital, Mana, Afore, 20VC, and others.

- 2 or more mentions = 1.0; 1 mention = 0.70; none = 0.0

---

### The fit score formula

```
fit_score = (S1 x 0.25) + (S2 x 0.20) + (S3 x 0.20) + (S4 x 0.15)
          + (S5 x 0.10) + (S6 x 0.05) + (S7 x 0.05)
```

Result is rounded to 4 decimal places. Maximum possible score is 1.0.

---

### Tier assignment logic

| Tier | Exact condition | What it means operationally |
|---|---|---|
| **Tier 4** | Excluded OR any hard gate failed | Out of scope. Do not pursue. |
| **Tier 1** | All gates pass + fit score above 0.60 + client decision = "approved" | Campaign-ready. Outreach approved. |
| **Tier 2** | All gates pass + fit score above 0.60 but not yet approved, OR fit score above 0.38 | Qualified. Research and gate before outreach. |
| **Tier 3** | All gates pass + fit score below 0.38 | Weak conviction. Nurture or wait. |

Thresholds (0.60 and 0.38) are defaults. They can be tuned by the calibration stage output in `prompts/icp_calibration.yaml` when benchmark name linkage is healthy.

---

### The ICP version system

`ICP_VERSION` in `icp_spec.py` tracks spec evolution:

- v4.0: initial encoding of scoping workbook
- v4.1: scoping alignment, calibration wiring
- v4.2: added E13 (fund minimum) and E14 (US/Europe-only) exclusions from rejection archive analysis

Every scoring run tags rows with the current version. Many SQL views and graph queries still filter on `icp_version = '4.1'`. After a v4.2 run, those views silently skip the new rows. This is a known gap listed in Section 13.

---

## 9. Pipeline Two: The LP Gate (Live, Per-LP)

### What triggers a gate run

- CLI: `contra gate "LP Name"`
- API: `POST /api/gate`
- Web UI gate page
- Batch NFX screening with `screening_mode = "nfx_individual"`

---

### How Tavily web research works

The gate runs one of two research paths:

**Preferred path (if OpenAI key is set):** Deep research via OpenAI Responses API, then optional supplemental Tavily queries (2 queries) and a PitchBook block.

**Fallback path: 7 structured Tavily queries, each targeting a specific question:**

| Query | What it is trying to confirm or rule out |
|---|---|
| General investor profile + VC fund LP | Does this person invest into funds as an LP at all? |
| PitchBook-style fund commitments | Is there a documented record of fund LP commitments? |
| Anchor LP / emerging manager / Fund I | Have they backed a first-time fund or served as anchor? |
| Portfolio companies in AI/software/tech | Does their portfolio match Contra's thesis sectors? |
| Venture fund LP in Asia/SEA/India/ME | Do they operate in Contra's target geographies as a fund LP? |
| Negative check: PE-only / direct-only / large minimums | Are there structural disqualifiers? |
| Mumbai/India family office venture fund | Geography and type disambiguation for South Asian allocators |

Results are deduplicated by URL, capped at 12-14 sources, and optionally reranked by NVIDIA NIM.

---

### How gate_explain.yaml reasons through a prospect

The system prompt instructs the AI to act as a Contra Gate Analyst for a $30M Fund I targeting AI/robotics at pre-seed to Series A across Southeast Asia, North America, and the Middle East.

Step-by-step reasoning the model is instructed to follow:

1. **Read NFX Signal correctly.** Angel check sizes ($5-25K) are for direct startup investing, not fund LP capacity. NFX presence alone does not pass C1.
2. **Apply screening mode.** `nfx_individual` leans NO when C1 is unconfirmed. `institutional` leans REVIEW when uncertain.
3. **Treat PitchBook as ground truth** when authenticated cookies are present.
4. **Apply the GP-is-not-LP rule.** A person's employer fund portfolio is never evidence of their own LP allocation.
5. **Assess C1 to C4 probabilistically** (pass/fail/unknown) with one-line evidence for each.
6. **Infer appetite from fund LP decisions only**, graded across five dimensions: `em_appetite`, `fund_i_appetite`, `ai_tech_appetite`, `venture_appetite`, `geography_appetite`. Each graded as strong/moderate/weak/none/unknown.
7. **Actively search for negative flags:** PE-only, direct-only, no fund LP history, NFX angel-only, large minimum check size.
8. **Return structured output:** recommendation, reasons, two-sentence summary with flip conditions for REVIEW verdicts, LP commitments found, and primary blocker for NO verdicts.

---

### The five appetite dimensions

Every gate run grades the LP across five appetite dimensions. These feed gate signals, outreach copy, and the dossier.

| Dimension | What it measures |
|---|---|
| `em_appetite` | Appetite for emerging/first-time managers specifically |
| `fund_i_appetite` | Appetite for Fund I vehicles (not just general EM) |
| `ai_tech_appetite` | Thesis alignment to AI, robotics, deep tech |
| `venture_appetite` | Appetite for venture fund LP activity generally |
| `geography_appetite` | Appetite for SE Asia, NA, ME geographies |

Each is graded: **strong / moderate / weak / none / unknown**

REVIEW verdicts always state what one verified fact would be needed to flip each unknown grade to confirmed.

---

### The gate signals

The evaluator computes up to 10 signals deterministically before the LLM runs. These inform the prompt context and the final verdict adjustment.

| Signal | When it is met |
|---|---|
| `icp_qualified` | ICP Tier 1 or Tier 2 with all core gates passed (suppressed if match is untrusted) |
| `syndicate_fund_lp` | At least 1 fund deal in syndicate LP history |
| `syndicate_upgrade` | At least 1 fund deal AND at least $5K committed |
| `warm_path` | At least 1 warm intro path in the co-investment graph |
| `benchmark_rank` | Appears in the Contra Top-200 benchmark |
| `appetite_emerging_manager` | EM or Fund I appetite rated moderate or strong (post-LLM) |
| `appetite_ai_tech` | AI/tech appetite rated moderate or strong |
| `appetite_venture_fit` | Venture fund LP appetite rated moderate or strong |
| `similar_lp_precedent` | At least one confirmed similar LP with a high similarity score |
| `analyst_fact` | Up to 2 analyst facts containing LP-confirming keywords (counts as 2 if both met) |

**Hard blocks (immediate NO, LLM skipped):** Already in CRM as active lead, ICP excluded, or direct/PE-only flagged in exclusion reason.

**Verdict heuristic (pre-LLM, for prompt context only):**
- 2 or more signals met = YES
- Exactly 1 signal, OR syndicate upgrade + 2 or more unknown core gates = REVIEW
- 0 signals = NO

The LLM verdict is the binding decision. The pre-LLM heuristic is context for the prompt, not the final answer.

---

### The Haiku to Sonnet escalation chain

**Step 1 (Triage):** Claude Haiku 4.5 runs the full gate prompt. Fast and cheap. Clear NO verdicts stop here.

**Step 2 (Escalation):** If triage returns YES or REVIEW, and Anthropic is configured, and the escalation model differs from triage, Claude Sonnet 4.5 re-runs the same prompt. The escalated verdict wins.

**Why two models:** NFX batch screening produces many correct NOs. Running Sonnet on every name would be expensive and unnecessary. Haiku handles the bulk; Sonnet handles the names that matter.

---

### The appetite validator and evidence verifier

These run deterministically after the LLM, before the verdict is finalized.

**Appetite validator (downgrade-only, never upgrades):**

- GP title + no external LP commits: forces all appetite grades to unknown; strips employer portfolio from allocation evidence
- EM/Fund I appetite rated moderate or strong without any LP commit language: downgraded to unknown
- `nfx_individual` mode + strong negative signals: verdict forced to NO
- `nfx_individual` mode + zero-evidence REVIEW: forced to NO
- `institutional` mode + NO verdict + unconfirmed C1 with no confirmed misfit: upgraded to REVIEW
- NO summaries: hedge language stripped; must state a clear primary blocker
- REVIEW summaries: must include "Flip to YES if: ..."

**Evidence verifier:**

- Each LP commitment found must be quotable from actual web context or analyst facts
- Unverifiable claims are removed before the verdict is finalized
- YES verdict with zero verified commits and no analyst LP fact: downgraded to REVIEW (institutional) or NO (nfx_individual)

---

### How YES / REVIEW / NO is computed

1. Hard blocks checked first. Any hard block = NO, skip LLM entirely.
2. LLM runs and returns a recommendation with appetite grades and LP commitments.
3. Evidence verifier removes unverifiable claims.
4. Appetite validator applies deterministic adjustments (downgrade-only).
5. Final verdict persisted.

The LLM is primary. The validators can only tighten, never loosen.

---

### What gets persisted after a gate run

For YES or REVIEW verdicts:
- Allocator record created or updated
- Raw research written to database
- Contacts extracted where available
- LP dossier created or updated with verdict, appetite grades, verified LP commitments, research notes, verdict history
- Gate review record written to `crm_gate_reviews`
- In-memory session held for 30-minute chat follow-up

Gate YES does NOT auto-create a CRM lead. That requires a manual Add to CRM step.

**Why manual:** Partners must consciously commit an LP to the active pipeline. The gate screens; the CRM is a commitment to pursue.

---

## 10. The AI Layer in Detail

### Every place Claude and other models are called

| Use case | Default model | What it receives | What it returns |
|---|---|---|---|
| Gate triage | Claude Haiku 4.5 | gate_explain.yaml system prompt + formatted user prompt with all backend data | GateExplanation (structured Pydantic schema) |
| Gate escalation | Claude Sonnet 4.5 | Same prompt, re-run | GateExplanation |
| Gate knowledge enrichment | NVIDIA NIM | Web snippets | Analyst bullets appended to web context |
| Outreach draft | GPT-4o | Archetype playbook + research + dossier | OutreachDraft (subject, body, personalization points) |
| Outreach critique loop | GPT-4o | Draft against rule checklist | OutreachCritique, optional revision |
| Outreach insight extraction | GPT-4o | Raw research | 3 insight angles |
| CRM field extraction | Configured LLM | Gate result + brief | CrmLeadExtraction |
| Deep web research | OpenAI Responses API | LP name + mode | Structured analyst notes + URLs |

### How structured outputs work

All LLM calls use instructor + Pydantic with `extra="forbid"`. This means no free-form text enters the database from an LLM call. Every response must conform to a defined schema or the call fails and is retried or escalated. There are no cases where "because the model said so" enters a canonical record without a schema wrapper.

### Groq fallback

When `PULSE_LLM_AUTO_SWITCH = true` (default), the system cycles through fallbacks on context-size errors or rate-limit exhaustion:

1. Same provider, larger model
2. Cross-provider: NVIDIA LLaMA 3.3 70B, Anthropic Haiku, OpenAI GPT-4o-mini, Groq LLaMA 3.3 70B

Groq rotates across up to 9 API keys on quota errors. Default Groq model is `llama-3.3-70b-versatile`. The compact fallback `llama-3.1-8b-instant` only works with small prompts and is a last resort. The gate defaults to Anthropic when configured. Groq is the typical provider for non-gate batch and optional enrichment paths.

---

## 11. Dossier and Brief Generation

### intelligence/brief.py (alive)

Generates LP dossiers by pulling from the database: allocator record, ICP score, signals, relationships, warm paths, gate review history, and any existing dossier content. Produces a structured brief used by the gate prompt and the outreach layer.

### brief_agent.py (non-functional)

Was intended as an extended dossier agent with richer analytics. Currently broken due to import errors. Not called anywhere in the live system.

### What an lp_dossier contains

Research notes up to 20,000 characters, appetite JSON (all five dimensions and their grades), verified LP commitments with source quotes, verdict history across all gate runs, outreach history, and analyst notes. Synced to Airtable. This is the institutional memory for each LP: it persists across sessions and accumulates over time.

---

## 12. The CRM and Outreach Layer

### How a gate YES becomes a CRM lead

1. Gate returns YES or REVIEW with a session ID
2. GP clicks Add to CRM
3. System runs LLM field extraction on the gate result, merges with existing data, inserts into `crm_leads` with `source = 'gate'`
4. Gate findings also persisted to the allocator record if not already done

Alternate paths: promoting directly from ICP/syndicate/benchmark queues without a full gate run, or adding manually for names the GP already knows.

---

### How outreach drafts are generated

`generate_outreach_draft()` in `crm/outreach.py`:

1. Load the CRM lead record and LP dossier
2. Resolve archetype from gate appetite or investor type mapping
3. Run fresh deep research on the LP
4. Extract 3 insight angles from research (Tier A: LP commitments and allocation evidence; Tier B: archetype and gate reasons; Tier C: appetite fields)
5. Build prompt with ranked signals
6. LLM generates draft
7. Critique-revise loop runs up to 2 iterations against hook rules
8. Draft inserted into `crm_outreach_drafts` with `status = draft`; dossier and Airtable synced

Blocked if no research and no signals: system returns "run gate first."

---

### Hook rules: what is allowed and what is forbidden

**Required email structure (in order):**

1. Sentence 1: Their thesis in their own language. No mention of "Contra" in this sentence.
2. Sentence 2: One connecting sentence to Contra's thesis.
3. Sentence 3: One specific, verified research fact as evidence (not the opener).
4. Verbatim CTA: factsheet URL and call offer.
5. Static pitch block verbatim (see below).

**Non-negotiable rules:**

- No em dashes anywhere in the email
- No bullet points in the body
- Do not open with dry data: fund close date, AUM, or a famous portfolio company for a prolific angel
- Maximum 3 sentences before the CTA
- Do not fabricate any fact. If uncertain, use the NO-HOOK template.
- No jargon in the opening: "alpha," "lens," "deal flow," "archetype" are all banned
- AI/tech gate signals must not bleed into the hook unless the LP is classified as a technology specialist

**The critique loop checks for all of the above.** If the draft fails on any rule, the model is asked to revise. Maximum 2 revision iterations.

---

### The outreach archetypes

The archetype assigned at outreach draft time determines the playbook the LLM follows.

| Archetype | Hook direction |
|---|---|
| `fund_of_funds` | Lead with their portfolio construction thesis and the gap Global Asian venture fills in a diversified alternatives book |
| `family_office_single` | Lead with principal's personal investment philosophy or a specific sector bet they have made |
| `family_office_multi` | Lead with their client base's exposure to Asian growth and how that creates a natural portfolio fit |
| `sovereign_wealth_fund` | Lead with national economic mandate and how Global Asian founders serve their strategic interests |
| `endowment` | Lead with long-horizon return profile and the illiquidity premium argument for early-stage Asian venture |
| `pension_fund` | Lead with DPI track record and how the MyAsiaVC foundation de-risks the emerging manager narrative |
| `asset_manager` | Lead with how the fund fills a whitespace in their alternatives allocation |
| `development_finance_institution` | Lead with development impact of backing underrepresented founders and job creation in target geographies |
| `technology_specialist` | Lead with technical thesis and AI/B2B infrastructure angle; AI/tech signals are allowed in the hook for this archetype |

---

### The static pitch block

The LLM is instructed to include this verbatim in every email. It must not be rewritten, summarized, or paraphrased.

**Mandatory facts it contains:**

- $30M Fund I; $500-750K checks; approximately 30 companies
- The founding insight: 50% of new US tech founders are Asian; no institutional fund was built for them
- B2B AI focus; Global Asian founders
- Co-GPs deployed $70M+ across 300+ companies, 6,000+ LPs via MyAsiaVC
- Contra as the institutional form of that community

This block appears after the CTA line, introduced with "Here's some more context on what we're building:" It is five paragraphs covering: data point → thesis → GP track record → founder archetype → investment mechanics.

---

### Current state: emails drafted but not sent

The status workflow is: `draft → approved → sent / discarded`.

When `update_draft_status("sent")` is called, it updates the database record, sets the lead to "contacted," appends a dossier event, and syncs to Airtable. There is no SMTP, Gmail API, or mail transport in the codebase. Sending is a manual GP action outside the system.

---

## 13. Warm Paths

### What a warm path is

A warm path is a `mutual_connection` edge linking an institutional prospect to a bridge allocator (usually a syndicate LP) through shared co-investment history. In plain terms: we may know someone who knows them. Not a confirmed intro, but a ranked route to one.

### How v_warm_paths is built

From `schema/views.sql`:

1. Starts from `relationships_effective` where `edge_type = 'mutual_connection'`
2. Joins `relationship_evidence` with `evidence_type = 'graph_path_inference'`
3. Filters: prospect must be tagged `institutional_prospect`; bridge is the other node
4. Covers both directions (prospect as source or target)
5. Returns: prospect name, bridge name and type, edge strength, temporal confidence, evidence count

### Why NetworkX exists but is not used at runtime

The graph is saved to a pickle file using NetworkX for offline analysis and visualization. At runtime, the gate uses `v_warm_paths` (a SQL view) directly. The SQL path is faster, already filtered to trusted edges, and respects the `_effective` overlay for human overrides. The pickle is not read during any live gate or scoring run.

### Why warm paths compound

Every new syndicate investment adds `co_invested` edges (at the 3-deal minimum threshold). More edges mean more 2-hop bridges, which mean more `mutual_connection` rows, which mean a richer warm path signal for institutional prospects. The graph is a compounding intro map, not a static contact list.

---

## 14. Scheduling and Infrastructure

### GitHub Actions

Cron jobs run batch operations on schedule: `contra refresh` for ingestion, normalization, scoring, and calibration. Schedules and job names are defined in `.github/workflows/`.

### render.yaml

Defines the Render deployment: the FastAPI service, required environment variables (Anthropic, Tavily, MotherDuck, Groq keys), and build/start commands. The system is stateless on Render. All state lives in MotherDuck (production) or `contra.duckdb` (local).

### Local vs MotherDuck

In production (Render), the system connects to `md:contra` (MotherDuck cloud DuckDB). Locally, it uses `contra.duckdb`. The connection string is set via environment variable. Schema and query logic are identical across both.

### Why DuckDB rather than PostgreSQL locally

DuckDB runs embedded with no server process, making local development and CI simple. It has excellent dataframe integration used throughout the normalization and derivation pipelines. The schema is kept strictly portable; the only meaningful difference between local and production DDL is `JSON` vs `JSONB`. Migration path when needed: parquet export from DuckDB, then load into Postgres with the production schema applied on top.

---

## 15. What PULSE Produces: Outputs Summary

**ICP score (batch)**
A row in `icp_scores` per institutional prospect. Contains: four gate booleans, seven soft signal values, weighted fit score from 0 to 1, tier 1 to 4, client decision, exclusion reason, and provenance back to the source Excel row. Used by GPs to prioritize who enters the live gate queue.

**Gate verdict (live)**
YES / REVIEW / NO with confidence level, 3 to 5 reasons, a two-sentence summary, core gate statuses, appetite profile across five dimensions, verified LP commitments found, primary blocker for NO verdicts, source URLs, and escalation metadata. REVIEW verdicts always include flip conditions.

**LP dossier**
Durable record in `lp_dossiers`: research notes up to 20,000 characters, appetite JSON, verified LP commitments, verdict history, and outreach history. Synced to Airtable. Serves as institutional memory across all gate sessions for a given LP.

**Outreach draft**
Personalized subject line and email body in `crm_outreach_drafts`, tagged by archetype, critique-loop validated. Synced to Airtable. GP reviews, approves, and sends manually.

**Warm path flag**
Count and bridge names surfaced via `v_warm_paths` and the gate result. Tells the GP who to approach for a warm intro before sending a cold email.

**Outreach pack CSV**
`First_LPs_Outreach_Pack.csv`: Tier 1 approved names ranked by fit score, with bridge LP name, warm path count, network density, syndicate degree, two-hop reach, and contact details.

**Gate review record**
Latest verdict per LP name in `crm_gate_reviews`. Prevents re-screening the same name and feeds the ICP queue tracking view.

---

## 16. Known Limitations

These are known gaps being tracked as roadmap items.

**ICP version mismatch**
Scoring writes `icp_version = "4.2"` but four files hardcode `'4.1'`: `views.sql`, `signal_extractor.py`, `latent_signal_extractor.py`, and `contradiction_detector.py`. CRM queue and LP profiles show stale tiers after a fresh scoring run. Fix: those four files need to import `ICP_VERSION` from `icp_spec.py` rather than hardcoding the string.

**No live email send**
Pipeline ends at `draft / approved / sent` status in the database and Airtable. No in-app mail delivery. Sending is a manual GP action.

**Empty crm_contacts table**
Legacy import from `export.csv` only. The file was deleted (`export.csv.bak` exists). In-CRM similarity checks currently run against zero rows. Re-ingest needed.

**Calibration noise**
Calibration grid-searches tier thresholds against Contra Top-200 name matches including fuzzy joins. Institutional prospects and ContraVC syndicate LPs are currently largely disjoint populations, so auto-tuning is effectively skipped. The `calibration_overlay` view exposes the disagree buckets.

**brief_agent.py non-functional**
Broken imports. Not called anywhere in the live system.

**Silent failure handling**
Several paths swallow errors by design to avoid blocking the main pipeline: gate review record write, dossier upsert, contact extraction, enrichment agent. Operations can appear successful while subsidiary writes failed. Monitoring requires log checks or row count verification, not just API response inspection.

**PitchBook dependency**
Requires authenticated cookies. Returns "no_cookies" status otherwise. Not available in automated runs.

**Verifalia email validation**
Returns "Unknown" status. Email validation is effectively off.

**OpenAI deep research and NVIDIA NIM**
Off in production (no keys configured). System falls through to Tavily fan-out and Anthropic for all enrichment.

**Batch ICP uses text only**
C1 to C4 gates scan prospect sheet text only. No live web research runs during batch scoring.

**GP brief signal count discrepancy**
The GP brief document says 16 signal types. The ontology dictionary says 18. The actual `signal_types.py` defines 19. The Contra syndicate extension (`fund_lp_behavior`, `syndicate_depth`, `syndicate_recency`) was added after the brief was written and not reflected in documentation yet.

---

## 17. The Moat

PULSE compounds in four ways that are structurally hard to replicate from a spreadsheet.

**The syndicate graph**
Thousands of real co-investment edges with evidence rows, warm-path inference, and temporal decay. Each new deal strengthens bridge routing across the network. The 3-deal minimum threshold keeps it signal-dense. The graph is a compounding intro map, not a static list. Rebuilding this from scratch requires years of actual transaction history.

**Scoring history**
Versioned `icp_scores`, client decisions, rejection archive, and calibration against the Contra Top-200. The spec evolves with real outreach outcomes. Exclusions E13 and E14 came directly from analyzing 949 historical outreach emails. The system gets better at saying no to the right people over time.

**Signal accumulation**
19 signal types with evidence rows, contradiction detection, and human-review overlays. Conviction deepens per allocator across every refresh. Older signals decay in weight but are never deleted. Every gate run enriches the dossier for the next run on the same LP.

**Gate and dossier loop**
Every screened LP leaves structured appetite grades, verified LP commitments, research notes, and outreach history in the dossier. The live gate layer teaches the batch scoring layer what text-only ICP scoring missed. That feedback loop closes with each new calibration run.

The moat is not the LLM prompts. It is the provenance-linked memory: raw sources → graph → scores → gate findings → CRM → outreach outcomes → tighter exclusions and better calibration. Rebuilding that from scratch would require years of syndicate data and labeled partner decisions.