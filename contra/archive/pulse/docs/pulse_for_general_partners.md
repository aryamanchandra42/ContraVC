# PULSE — A Brief for General Partners

**What this is:** A plain-language overview of how PULSE supports MyAsiaVC’s fundraise — who it prioritizes, why, and how you can use it without reading code or database schemas.

**Last updated:** 2026-06-06 · Foundation + ICP v4.1 + syndicate graph + signal layer (live on your source files)

---

## The problem we are solving

Fundraising in private markets is not a list problem. It is a **judgment problem under incomplete information**.

You already have:

- A long prospect list and scoping criteria (who fits, who does not)
- Syndicate history (who has co-invested with whom)
- Call notes, PDFs, and strategy documents full of soft signals
- External benchmarks (e.g. how others rank LPs)
- A CRM that holds contacts but not **explainable** conviction

What you typically lack is a single place that answers, with traceable reasoning:

1. **Who should we talk to first — and why?**
2. **Who is structurally wrong for this fund — so we stop wasting partner time?**
3. **Who is “cold” on paper but warm through our syndicate network?**
4. **When two sources disagree, what do we believe — and can we override it without losing the audit trail?**

PULSE exists to hold that institutional memory: not as another spreadsheet, but as a **repeatable scoring and relationship layer** built on your actual documents and syndicate data.

---

## What PULSE is (in one paragraph)

**PULSE** (Private-market Unified LP Signal Engine) ingests your immutable source files — prospect lists, LP scoping rules, syndicate rosters, call-prep PDFs, benchmarks, optional LinkedIn exports — and turns them into:

- A **ranked, tiered prospect view** aligned to your ICP scoping workbook
- A **relationship map** (co-investors, shared fund vehicles, and bridge paths into syndicate capital)
- **Sixteen weak signals per prospect** — from text, investment patterns, and graph connectivity
- **Export packs** ready for outreach (names, contacts, tier, fit, bridge LP, warm-path counts)

It is designed for **explainability and review**, not black-box AI. The full pipeline runs without depending on an LLM. Human partners can reject bad matches or revise labels; those decisions are kept forever and applied at read time — the underlying source rows are not silently rewritten.

---

## What PULSE is not

- **Not a CRM replacement** — your CRM remains where you work contacts day to day; PULSE ingests spreadsheet and document exports.
- **Not a chatbot or dashboard product** — it is an intelligence layer behind CLI exports and an optional local explorer (`pulse explore`).
- **Not an outreach automation tool** — LinkedIn CSV ingestion is read-only enrichment; PULSE does not send messages or scrape live profiles.
- **Not claiming certainty** — scores and tiers are **conviction helpers**, grounded in text and structure from your files, with known gaps called out below.

---

## How we think about “fit” — your scoping doc is the constitution

The authoritative business logic is **`MyAsiaVC LP Scoping.xlsx`**: Core Filters, Exclusion Filters, Soft Filters, and LP Type Priority.

PULSE encodes that logic as **ICP v4.1**. Every institutional prospect is evaluated in three passes:

### 1. Core gates (must all pass)

These are binary qualification tests — fail any one and the prospect is **Tier 4 (out of scope)** regardless of how interesting they sound:

| Gate | Question we ask of the record |
|------|-------------------------------|
| **C1 — VC as LP** | Do they invest **into VC funds** as a limited partner (not direct-only, not PE-only)? |
| **C2 — Emerging manager appetite** | Is there **positive evidence** they back first-/second-/third-time managers (not only brand-name GPs)? |
| **C3 — AI / tech alignment** | Does their thesis or portfolio language touch AI, deep tech, or adjacent sectors you care about? |
| **C4 — Geography** | Do they invest in North America, Asia, Middle East, or run a **global** mandate? |

**C2 update (June 2026):** The emerging-manager gate no longer passes by default when notes are thin. Prospects without EM language fail C2 and land in Tier 4 — this tightened Tier 1 from an inflated set to a smaller, higher-conviction cohort.

### 2. Hard exclusions (any one disqualifies)

Examples encoded today: PE/buyout-primary, VC secondaries-only, real estate-primary, crypto-only, healthcare-only, geography-locked non-qualifying regions, explicit “no emerging managers,” direct-only (no fund LP), prop trading / non-VC financial profiles, sanctioned geographies, and client blacklist statuses from your prospect sheet.

This is how PULSE **protects partner calendar** — it surfaces structural mismatches early.

### 3. Soft signals (conviction ranking among qualifiers)

Among prospects that pass core and exclusions, PULSE computes a **fit score (0–1)** from weighted soft signals, including:

- Depth of **AI** language (portfolio or thesis)
- Depth of **emerging manager** language
- **LP type priority** from your scoping sheet (e.g. fund-of-funds and multi-family offices ranked above slow institutional types)
- **Decision speed** proxy by allocator type (HNW / single FO faster than pension-style)
- Stage alignment (early / seed / venture)
- Clean profile (absence of conflict phrases)
- Overlap with **proxy peer funds** you named in scoping
- **Network connectivity** — bridge strength, warm-path count, co-invest intensity from syndicate data

### 4. Tiers — how to read them

| Tier | Meaning for partners |
|------|---------------------|
| **Tier 1** | Passes core, not excluded, **strong fit score**, and **client-approved** for campaign |
| **Tier 2** | Qualified and reasonably strong fit; may await explicit approval |
| **Tier 3** | Passes gates but weaker conviction — nurture, research, or wait |
| **Tier 4** | Failed core or hit an exclusion — deprioritize |

**Live counts (June 2026):** ~252 institutional prospects scored; **109 Tier 1** at current thresholds (includes client-approved filter). Use `pulse status --verbose` before IC meetings for fresh numbers.

Thresholds can be calibrated against external benchmarks (ContraVC Top 200) when name linkage is healthy; today that step is wired but **population overlap is still zero** (benchmark links to syndicate LPs, ICP scores institutional prospects — see gaps below).

---

## The second intelligence layer — your syndicate graph

Separate from the ICP list sits a large **syndicate LP universe** (~5,900 co-investors drawn from syndicate spreadsheets).

PULSE builds a **co-investment graph** with three relationship layers:

| Layer | What it means |
|-------|---------------|
| **Co-invested** | Two LPs backed the same syndicate vehicles repeatedly (~28,550 edges) |
| **Invested with** | Two LPs share fund-vehicle exposure from normalized investments (~167 edges) |
| **Mutual connection** | Institutional prospect ↔ syndicate LP via a 2-hop bridge (~50 edges) |

That graph feeds:

- **Warmth without a CRM field** — “who sits near this prospect in co-invest space?”
- **Bridge LP names** in exports — the syndicate member most likely to make an intro
- **Connectivity scores** — `bridge_strength`, `warm_path_count`, `network_density` on outreach CSVs

This is how PULSE answers: *“We have never met them, but are we one introduction away from someone we have?”*

---

## The third layer — weak signals and contradictions

Beyond tiers and graph edges, PULSE tracks **sixteen signal types** per allocator — each backed by traceable evidence:

**From your documents:** response speed, exploratory check-ins, operator background, EM participation, geography overlap, deployment velocity

**From syndicate data:** co-invest intensity, shared deal count, recent activity recency, stage alignment, proxy fund overlap

**From the graph:** bridge strength, warm path count, network density, social proximity

**From ICP logic:** clean profile (no conflict phrases)

When signals **contradict** each other — e.g. “high deployment velocity” in notes but no recent investment activity — PULSE emits contradiction evidence rather than hiding the conflict. Today this is early (a handful of detected cases) but the mechanism is live and will grow as more data sources connect.

---

## What you can look at today (outputs)

| Artifact | What partners use it for |
|----------|--------------------------|
| **`First_LPs_Ready.csv`** | Campaign-ready slice with tier, fit, contacts, **bridge_strength**, **warm_path_count**, **network_density** |
| **`First_LPs_Outreach_Pack.csv`** | Tier 1 approved outreach slice |
| **`LP_Ranked_List.csv`** | Full ranked institutional view with tier and enrichment columns |
| **`Prospect_Syndicate_Connectivity.csv`** | Prospects ordered by syndicate network depth |
| **`pulse explore`** (local) | Interactive browse: funnel, outreach, LP detail, light graph view |
| **Calibration summaries** | How PULSE tiers compare to ContraVC benchmark (when names link) |

Example of what Tier 1 outreach rows carry: name, allocator type, geography, tier, fit score, client approval status, email/LinkedIn, **connectivity score**, **syndicate degree**, **two-hop reach**, **top bridge LP**, and **warm path count**.

---

## Trust model — why this is safe to show in IC

Partners should care about three properties:

### 1. Every strong claim can be traced to a source row

If PULSE asserts a relationship, signal, or keyword hit, there is a path back to **which file, which sheet/row or document passage** produced it. Nothing is “because the model said so” in the default pipeline.

### 2. Disagreement is visible, not hidden

When sources conflict, PULSE tracks **agreement vs contradiction** among evidence — and can emit explicit `contradicts_value` evidence when stated behavior and revealed behavior diverge.

### 3. Partner overrides are permanent and honest

When reviewers **reject** a bad fuzzy name match or **revise** a label, that decision is stored as a new review record. Queries use **effective** views that respect overrides **without deleting or rewriting** the original ingested data. You can always see what the machine thought *and* what the partnership decided.

In practice, hundreds of alias false-positives (similar suffixes across unrelated family offices) were already rejected through this queue — a real example of human judgment disciplining automation.

---

## Day-to-day workflow for partners

No command line required. The full workflow is:

```text
1. Refine criteria in LP Scoping workbook (business source of truth)
2. Drop updated prospect / syndicate / CRM / LinkedIn exports into raw_data/
3. Double-click Launch_PULSE.bat (Windows) — or run: pulse explore
4. Click "Refresh PULSE" in the left sidebar
5. Wait for the progress panel (1–3 minutes)
6. Download First_LPs_Outreach_Pack.csv from the Outreach tab
```

**Outreach pack layout:**
- **ICP Tier 1 Client Approved** — names from your prospect spreadsheet, ranked by fit score, with bridge LP and warm-path counts

**LinkedIn (optional):** export Phantombuster / Sales Nav CSVs to `raw_data/linkedin_*.csv`, then click Refresh PULSE. PULSE fuzzy-matches profiles to existing allocators — no live scraping, no automated outreach. See `prompts/linkedin_export.yaml`.

---

## What is working well vs still maturing

**Working today**

- End-to-end pipeline: ingest → normalize → extract → derive → graph → score → calibrate
- ICP tiers driven by your scoping workbook, with **strict C2 emerging-manager gate**
- Large syndicate co-invest graph (~29k relationship edges)
- **Fund-vehicle edges** (`invested_with`) connecting LPs through shared investments
- Prospect-to-syndicate bridge inference and connectivity exports
- **16 signal types** with evidence-backed scoring
- Human review discipline on bad entity merges
- Tier 1 outreach packs with bridge LP and warm-path columns
- **19/19 automated evals** passing (invariants + signal backtests)

**Still maturing (be explicit in partner meetings)**

- **ContraVC benchmark calibration** — built and fuzzy name join wired, but institutional prospects and ContraVC syndicate LPs are largely disjoint populations; auto-tune still skipped
- **Contradiction detection** — live but early (handful of cases); more rules as data grows
- **LinkedIn enrichment** — adapter ready; no LinkedIn CSV in corpus yet
- **Institutional relationship graph** is thinner than the syndicate graph; most network mass is co-investor fabric, not call-note edges
- **PDF/DOCX names** are extracted for signals but not always merged into the same entity resolution path as Excel prospects

We state these gaps openly so partners treat tiers as **prioritization**, not **autopilot**.

---

## How this supports the fundraise narrative

For IC or partner meetings, you can frame PULSE as four commitments:

1. **Criteria consistency** — The same Core / Exclusion / Soft rules apply to every prospect, every re-run.
2. **Network-aware prioritization** — Syndicate history lifts prospects who are structurally adjacent to capital you already touch.
3. **Calendar protection** — Exclusions, strict C2, and Tier 4 funnel remove structural mismatches before partner calls.
4. **Institutional memory** — Overrides, sources, signals, and review history accumulate; the system does not “forget” why a name was rejected.

PULSE does not replace partner judgment on relationship quality, reference checks, or final ticket size. It **front-loads structure** so judgment is spent on the right names, with **explainable** reasons and **warm-path** hints where the data supports them.

---

## Suggested talking points (60 seconds)

> “We built PULSE to stop re-deriving the same LP logic from scattered files. It reads our scoping rules and prospect data, ranks who passes hard gates — including a strict emerging-manager test — scores conviction among qualifiers, and maps our syndicate co-investors to see bridge paths into cold institutional names. Exports now carry bridge strength and warm-path counts, not just tier labels. Every tier, edge, and signal traces to a source row; partner rejections stick without corrupting the underlying data. ContraVC benchmark calibration and LinkedIn enrichment are the next tightening steps.”

---

## Where to go deeper

| Audience | Document |
|----------|----------|
| Everyone (start here) | `docs/reading_guide.md` |
| Partners (this doc) | `docs/pulse_for_general_partners.md` |
| Operators | `SYSTEM_STATE.md`, outreach CSVs in `processed_data/` |
| Technical / engineering | `docs/architecture.md`, `AGENTS.md` |

To refresh live numbers before a partner meeting: `pulse status --verbose`
