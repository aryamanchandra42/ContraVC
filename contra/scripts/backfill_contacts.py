"""
One-off script to backfill contacts for existing CRM leads.
"""

from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

# Setup paths and environment
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
load_dotenv(".env")

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

def run_backfill():
    from agents.db import get_conn
    from agents.research.contact_hunter import hunt_and_persist_contacts
    
    con = get_conn()
    try:
        # Get all leads from CRM
        logger.info("Fetching existing CRM leads...")
        rows = con.execute(
            """
            SELECT allocator_id, investor_name 
            FROM crm_leads 
            WHERE allocator_id IS NOT NULL
            """
        ).fetchall()
        
        logger.info(f"Found {len(rows)} leads. Starting backfill...")
        
        for idx, (alloc_id, name) in enumerate(rows, 1):
            logger.info(f"[{idx}/{len(rows)}] Hunting for {name} ({alloc_id})...")
            stats = hunt_and_persist_contacts(con, lp_name=name, allocator_id=str(alloc_id))
            logger.info(f"Stats for {name}: {stats}")
            
    except Exception as e:
        logger.error(f"Error during backfill: {e}")
    finally:
        con.close()

if __name__ == "__main__":
    run_backfill()
