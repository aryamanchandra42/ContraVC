# Airtable Setup Guide — Contra CRM

This guide walks you through creating the Airtable base that receives live data from the Contra backend.

---

## Step 1 — Create a Personal Access Token

1. Go to [airtable.com/create/tokens](https://airtable.com/create/tokens)
2. Click **+ Create new token**
3. Name it: `contra-crm`
4. Scopes: add `data.records:read`, `data.records:write`, `schema:bases:read`, `schema:bases:write`
5. Access: select the base you're about to create (or grant All bases)
6. Copy the token — starts with `pat...`

---

## Step 2 — Create the Base

1. Go to [airtable.com](https://airtable.com) → **Add a base** → **Start from scratch**
2. Name it: `Contra CRM`
3. Note the **Base ID** from the URL: `https://airtable.com/appXXXXXXXXXXXXXX/...`
   - The Base ID is the part starting with `app`

---

## Step 3 — Add Environment Variables

Add to your `.env` (or Render/deployment env):

```
AIRTABLE_API_KEY=pat...your_token_here...
AIRTABLE_BASE_ID=appXXXXXXXXXXXXXX
```

Optional overrides (only needed if you rename tables):
```
AIRTABLE_LEADS_TABLE=LP Leads
AIRTABLE_DRAFTS_TABLE=Outreach Drafts
AIRTABLE_DOSSIERS_TABLE=LP Dossiers
```

---

## Step 4 — Create Table 1: "LP Leads"

Rename the default table to **`LP Leads`**.

| Field Name | Field Type | Notes |
|---|---|---|
| Investor Name | Single line text | **Primary field** — used as unique key |
| Lead ID | Single line text | Internal DB UUID |
| Investor Type | Single select | Options: `fund_of_funds`, `family_office`, `founder_lp`, `corporate_investor`, `endowment`, `pension`, `insurance`, `sovereign_wealth`, `other` |
| Location | Single line text | |
| Pipeline Stage | Single select | Options: `Prospect`, `Outreach Sent`, `Replied`, `Closed` |
| Status | Single select | Options: `active`, `contacted`, `excluded`, `paused` |
| Gate Verdict | Single select | Options: `yes`, `review`, `no` |
| Gate Confidence | Single select | Options: `high`, `medium`, `low` |
| Gate Summary | Long text | |
| ICP Tier | Single line text | e.g. `tier_1`, `tier_2` |
| Fit Score | Number | 0–100 |
| Computed Score | Number | 0–100 |
| Contact Email | Email | |
| Latest Email Subject | Single line text | Auto-updated when a draft is generated |
| Latest Email Body | Long text | Auto-updated when a draft is generated |
| Last Outreach At | Date | ISO date |
| Needs Enrichment | Checkbox | |

---

## Step 5 — Create Table 2: "Outreach Drafts"

Click **+** to add a new table, name it **`Outreach Drafts`**.

| Field Name | Field Type | Notes |
|---|---|---|
| Draft ID | Single line text | **Primary field** — UUID |
| Investor Name | Single line text | Matches LP Leads → Investor Name |
| Subject | Single line text | Email subject line |
| Body | Long text | Full email body |
| Status | Single select | Options: `draft`, `approved`, `sent`, `discarded` |
| Tone | Single line text | e.g. `professional`, `warm` |
| Archetype | Single line text | e.g. `fund_of_funds`, `family_office` |
| Model | Single line text | LLM model used |
| Deep Research Used | Checkbox | |
| Personalization Points | Long text | Bulleted list of personalization hooks |

---

## Step 6 — Create Table 3: "LP Dossiers"

Add another table, name it **`LP Dossiers`**.

| Field Name | Field Type | Notes |
|---|---|---|
| Name Key | Single line text | **Primary field** — normalized slug e.g. `sequoia_capital` |
| Investor Name | Single line text | |
| Latest Verdict | Single select | Options: `yes`, `review`, `no` |
| LP Commitments | Long text | Known LP fund commitments |
| Appetite | Long text | JSON: check size, stage preferences |
| Sources | Long text | Research source URLs (newline-separated) |
| Research Notes | Long text | Gate session research (up to 10k chars) |
| Analyst Notes | Long text | Free-text notes from the team |
| Outreach Summary | Long text | Last 10 outreach events timeline |
| Rejection Reason | Single select | Options: `fund_size`, `geo_mandate`, `deployment_pause`, `placement_agent`, `other` |
| Revisit Date | Date | When to re-engage a paused LP |

---

## Step 7 — Install the dependency

```bash
pip install "pyairtable>=2.3"
```

Or install the full Contra package with the new extra:

```bash
pip install -e ".[gate,api,airtable]"
```

---

## How data flows

```
Gate screen (YES/REVIEW)
    └─► upsert_dossier_from_gate()
            └─► airtable_sync.push_dossier()  ──► LP Dossiers table

Generate outreach email
    └─► generate_outreach_draft()
            ├─► airtable_sync.push_outreach_draft()      ──► Outreach Drafts table
            └─► airtable_sync.update_lead_latest_email() ──► LP Leads (inline)

Mark email as sent
    └─► update_draft_status(status="sent")
            ├─► airtable_sync.update_draft_status_airtable() ──► Outreach Drafts
            └─► airtable_sync.update_lead_latest_email()     ──► LP Leads (stage → Outreach Sent)

Tag rejection
    └─► tag_rejection()
            └─► airtable_sync.push_dossier()  ──► LP Dossiers (rejection_reason + revisit_date)
```

All syncs are non-blocking (background thread). A broken Airtable token never crashes the backend.

---

## Useful views to set up in Airtable

**LP Leads:**
- `Pipeline Board` — Group by **Pipeline Stage**, Kanban-style
- `To Outreach` — Filter: Pipeline Stage = Prospect, Gate Verdict = yes
- `Needs Enrichment` — Filter: Needs Enrichment = checked

**Outreach Drafts:**
- `Pending Approval` — Filter: Status = draft, Sort by created newest first
- `Sent` — Filter: Status = sent

**LP Dossiers:**
- `Revisit Queue` — Filter: Revisit Date is within next 30 days
- `Top Prospects` — Filter: Latest Verdict = yes, Sort by Investor Name
