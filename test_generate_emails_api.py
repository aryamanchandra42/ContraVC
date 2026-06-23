import requests
import time

def main():
    base_url = "http://localhost:8000"
    
    # 1. Fetch all active leads
    print("Fetching active leads...")
    resp = requests.get(f"{base_url}/api/crm/leads?status=active")
    if not resp.ok:
        print(f"Failed to fetch leads: {resp.status_code} {resp.text}")
        return
        
    leads = resp.json()
    print(f"Found {len(leads)} active leads.")
    
    # 2. Generate email for each
    for lead in leads:
        lead_id = lead["lead_id"]
        investor_name = lead["investor_name"]
        print(f"Generating email for {investor_name} ({lead_id})...", flush=True)
        
        try:
            # The endpoint is POST /api/crm/leads/{lead_id}/outreach
            gen_resp = requests.post(f"{base_url}/api/crm/leads/{lead_id}/outreach", json={
                "tone": "warm",
                "sender_name": "Alex"
            })
            
            if gen_resp.ok:
                draft = gen_resp.json()
                print(f"  -> Success! Draft ID: {draft['draft_id']}", flush=True)
            else:
                print(f"  -> Failed: {gen_resp.status_code} {gen_resp.text}", flush=True)
        except Exception as e:
            print(f"  -> Error: {e}", flush=True)
            
        time.sleep(1) # Small delay to not overwhelm the LLM API

if __name__ == "__main__":
    main()
