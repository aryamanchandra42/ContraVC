# PULSE Ontology Dictionary

Canonical taxonomy for institutional intelligence terms discovered from the 6 source files.
Version: 1.0. Last updated: June 2026.

Update this document alongside `prompts/heuristic_keywords.yaml` and `agents/normalization/taxonomies.py`.
Document any additions in `docs/decision_archive.md`.

---

## Allocator Archetypes

| Term | Canonical Label | Key Patterns | Evidence Source |
|------|----------------|--------------|-----------------|
| `family_office_single` | Single Family Office | "family office", "SFO", "single family" | LP Scoping, ICP List |
| `family_office_multi` | Multi Family Office | "multi family office", "MFO" | LP Scoping |
| `fund_of_funds` | Fund of Funds | "fund of funds", "FoF", "FOF" | LP Scoping, ICP List |
| `pension_fund` | Pension Fund | "pension fund", "superannuation", "CPF", "GPIF" | Rating Guide, ICP List |
| `sovereign_wealth_fund` | Sovereign Wealth Fund | "SWF", "GIC", "Temasek", "Mubadala", "ADIA" | LP Scoping |
| `endowment` | Endowment | "endowment", "university endowment" | LP Scoping |
| `development_finance_institution` | DFI | "DFI", "IFC", "ADB", "AIIB" | LP Scoping |
| `insurance` | Insurance / Reinsurance | "insurance", "insurer", "reinsurance" | Rating Guide |
| `fund_of_funds` | Fund of Funds | "FoF", "fund of funds" | LP Scoping, ICP List |
| `asset_manager` | Asset Manager | "asset manager", "investment manager" | ICP List |

---

## EM Appetite Signals

| Term | Canonical Label | Interpretation |
|------|----------------|---------------|
| `em_appetite_high` | High EM Appetite | Explicit EM mandate, Asia focus, frontier interest |
| `em_appetite_low` | Low EM Appetite | DM-only mandate, US/Europe-only language |
| `asia_exposure` | Asia Exposure | APAC allocation, pan-Asia, SEA, India |

---

## Rejection Patterns

| Term | Canonical Label | Language Signals |
|------|----------------|-----------------|
| `mandate_mismatch` | Mandate Mismatch | "outside mandate", "IPS constraint", "policy restriction" |
| `geography_constraint` | Geography Constraint | "no Asia allocation", "EM not approved", "country restriction" |
| `size_constraint` | Fund Size Constraint | "too small", "below our minimum", "fund too small" |
| `committee_constraint` | IC Constraint | "committee approval", "IC sign-off", "trustee approval" |
| `timing_constraint` | Timing / Deployment Constraint | "not deploying now", "fully deployed", "next vintage" |

---

## Geography Clusters

| Term | Canonical Label | Key Identifiers |
|------|----------------|----------------|
| `singapore_hq` | Singapore HQ | "Singapore", "MAS regulated", "ACRA" |
| `hong_kong_hq` | Hong Kong HQ | "Hong Kong", "HK", "SFC", "HKMA" |
| `middle_east_hub` | Middle East Hub | "Dubai", "ADGM", "DIFC", "Abu Dhabi", "Saudi Arabia" |
| `southeast_asia` | Southeast Asia | "SEA", "ASEAN", "Indonesia", "Vietnam", "Thailand" |
| `south_asia` | South Asia | "India", "SAARC", "Bangladesh" |
| `emerging_markets` | Emerging Markets | "EM", "emerging market", "frontier" |

---

## Committee / Institutional Constraints

| Term | Canonical Label | Typical Language |
|------|----------------|-----------------|
| `long_approval_cycle` | Long Approval Cycle | "6 month process", "annual review", "board quarterly" |
| `co_investment_requirement` | Co-Investment Requirement | "co-invest required", "side car", "direct co-invest" |

---

## Weak Signal Taxonomy

| Signal Type | Description | Data Source |
|-------------|-------------|-------------|
| `response_speed` | How quickly the LP responds to outreach | Interaction timestamps |
| `exploratory_check` | Small exploratory commitment before full | LP Scoping patterns |
| `operator_background` | LP has operator / founder background (more risk-tolerant) | ICP notes |
| `em_participation` | Historical EM fund participation | Investment records |
| `geography_overlap` | LP geography overlaps with fund focus | Allocator geography field |
| `social_proximity` | Shared network connections | Relationship graph |
| `network_density` | LP appears in many syndicate clusters | Graph metrics |
| `deployment_velocity` | How fast LP deploys capital | Investment timestamps |

---

## Confidence Interpretation Guide

| Range | Interpretation |
|-------|---------------|
| 0.90 – 1.00 | Near-certain: strong multi-source corroboration |
| 0.75 – 0.90 | High: single strong source, or multiple moderate sources |
| 0.60 – 0.75 | Medium: one moderate source, or heuristic match |
| 0.40 – 0.60 | Low: weak evidence, surfaces for human review |
| 0.00 – 0.40 | Very low: contradicted or insufficient evidence |

---

## Source Agreement Interpretation

| `source_agreement_score` | Interpretation |
|--------------------------|---------------|
| ≥ 0.80 | Strong agreement across sources |
| 0.50 – 0.80 | Partial agreement; some sources missing data |
| < 0.50 | Disagreement or sparse observation |

## Contradiction Score Interpretation

| `contradiction_score` | Interpretation |
|-----------------------|---------------|
| ≥ 0.30 | High contradiction — surface for human review |
| 0.10 – 0.30 | Some tension — monitor |
| < 0.10 | Low contradiction |

---

## Related: Signal Layer (not ontology terms)

Weak signals are separate from ontology terms but use the same uncertainty columns. Sixteen canonical types are defined in `agents/scoring/signal_types.py` and documented in `docs/architecture.md` (Signal Layer section). Signals carry first-class `signal_evidence` rows; contradiction evidence uses `evidence_type='contradicts_value'`.

See also: `docs/pulse_for_general_partners.md` (partner-facing signal summary).
