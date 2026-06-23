import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv

# Add contra to python path
sys.path.insert(0, str(Path(os.getcwd()) / "contra"))
load_dotenv(Path(os.getcwd()) / "contra" / ".env")

from agents.db import get_conn
from contra.crm.outreach import generate_outreach_draft

def main():
    con = get_conn(read_only=False)
    
    # Get all active leads
    cursor = con.execute("SELECT lead_id, investor_name FROM crm_leads WHERE status = 'active'")
    leads = cursor.fetchall()
    
    print(f"Found {len(leads)} active leads.")
    
    for lead_id, investor_name in leads:
        print(f"Generating email for {investor_name} ({lead_id})...")
        try:
            draft = generate_outreach_draft(con, lead_id=str(lead_id))
            print(f"  -> Success! Draft ID: {draft['draft_id']}")
        except Exception as e:
            print(f"  -> Failed: {e}")

if __name__ == "__main__":
    main()
