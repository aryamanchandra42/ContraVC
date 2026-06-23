"""
Generate outreach drafts via the running API for a set of lead IDs (in parallel)
and print the subject, resolved archetype, the hook (everything before the
factsheet line) with its character count, and the personalization hooks used.
"""

import sys
import json
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "http://localhost:8000"
FACTSHEET = "Our Fund I factsheet"
DIV = "-" * 72

# lead_id -> friendly label
LEADS = {
    "792503e7-21a4-4d4c-9ec0-1c94cc6b7fa6": "Henry McNamara (Fund of Funds)",
    "8bdc2394-310b-4f93-b74c-66d6d91577d0": "Kenneth Ballenegger (Family Office)",
    "8c377a0d-51f2-4001-954e-6a81eb5cd5cd": "Ilina Rai-Sia (Individual)",
}

def gen(lead_id: str):
    r = requests.post(
        f"{BASE}/api/crm/leads/{lead_id}/outreach",
        json={"tone": "warm"},
        timeout=600,
    )
    r.raise_for_status()
    return lead_id, r.json()

def show(label: str, d: dict):
    body = d.get("body", "")
    idx = body.find(FACTSHEET)
    hook = body[:idx].strip() if idx != -1 else body[:400]
    # strip the greeting line for an accurate hook char count
    hook_only = hook.split("\n", 1)[1].strip() if hook.lower().startswith("hi ") else hook
    print(f"\n{DIV}\n{label}")
    print(f"  archetype: {d.get('archetype')}   subject_format: {d.get('subject_format')}   "
          f"deep_research_used: {d.get('deep_research_used')}")
    print(f"  SUBJECT: {d.get('subject')}")
    print(f"{DIV}")
    print(hook)
    print(f"\n  [hook length before factsheet, excl. greeting: {len(hook_only)} chars]")
    print("  PERSONALIZATION:")
    for p in d.get("personalization_points", []):
        print(f"    - {p}")

def main():
    flt = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    leads = {k: v for k, v in LEADS.items() if flt in v.lower()} if flt else LEADS
    print("Generating sequentially (deep research + opus per lead, ~2-4 min each)...")
    for lid, label in leads.items():
        try:
            print(f"\n>>> {label} ...", flush=True)
            _, d = gen(lid)
            show(label, d)
        except Exception as e:
            print(f"\n{label}: FAILED - {e}", flush=True)

if __name__ == "__main__":
    main()
