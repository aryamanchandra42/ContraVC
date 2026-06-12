"""POST /api/gate — LP screening verdict + chat follow-up."""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from api.deps import get_db
from contra.gate import run_gate
from contra.gate.batch_models import BatchGateReport
from contra.gate.models import GateResult

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class GateRequest(BaseModel):
    name: str
    analyst_facts: List[str] = []


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    updated_result: Optional[GateResult] = None
    rescreened: bool = False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/gate", response_model=GateResult)
def gate(req: GateRequest, con=Depends(get_db)) -> GateResult:
    try:
        result = run_gate(con, req.name, analyst_facts=req.analyst_facts or [])
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    try:
        from contra.crm.writer import record_gate_review
        record_gate_review(con, result)
    except Exception:
        pass

    return result


@router.post("/gate/chat", response_model=ChatResponse)
def gate_chat(req: ChatRequest, con=Depends(get_db)) -> ChatResponse:
    from contra.gate.chat import process_message
    try:
        result = process_message(con, req.session_id, req.message)
        return ChatResponse(
            reply=result.reply,
            updated_result=result.updated_result,
            rescreened=result.rescreened,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------------------
# Batch upload endpoints
# ---------------------------------------------------------------------------

_SUPPORTED_SOURCE_TYPES = {"signal-nfx"}


class BatchCrmAddRequest(BaseModel):
    batch_id: str
    investor_name: str
    session_id: str


class BatchCrmAddResponse(BaseModel):
    ok: bool
    investor_name: str


@router.post("/gate/upload", response_model=BatchGateReport)
def gate_upload(
    file: UploadFile = File(...),
    source_type: str = Form("signal-nfx"),
    delay_seconds: float = Form(3.0),
    con=Depends(get_db),
) -> BatchGateReport:
    """
    Upload a CSV/XLSX file to batch-screen investors through GATE.

    Currently supported source types:
      - signal-nfx  (NFX Signal xlsx export)

    The batch runs synchronously (workers blocked until complete). For large files
    (>20 investors) the frontend should poll /api/gate/batch/{batch_id} while
    the upload request is pending, or use a background job for very large batches.

    Rate limiting: delay_seconds sleep between each LLM call (default 3s).
    """
    if source_type not in _SUPPORTED_SOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported source_type '{source_type}'. Supported: {sorted(_SUPPORTED_SOURCE_TYPES)}",
        )

    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        from agents.ingestion.nfx_xlsx_adapter import NfxXlsxAdapter
        adapter = NfxXlsxAdapter()
        records = adapter.extract_records_from_bytes(content, filename=file.filename or "upload.xlsx")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Failed to parse file: {exc}") from exc

    if not records:
        raise HTTPException(status_code=422, detail="No investor rows found in the uploaded file.")

    try:
        from contra.gate.batch import batch_gate_run
        report = batch_gate_run(con, records, source_type=source_type, delay_seconds=delay_seconds)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    return report


@router.get("/gate/batch/{batch_id}", response_model=BatchGateReport)
def gate_batch_status(batch_id: str) -> BatchGateReport:
    """
    Poll the status of a batch gate run by batch_id.

    Returns the current BatchGateReport (partial while running, complete when done).
    """
    from contra.gate.batch import get_batch_report
    report = get_batch_report(batch_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found.")
    return report


@router.post("/gate/batch/crm-add", response_model=BatchCrmAddResponse)
def gate_batch_crm_add(req: BatchCrmAddRequest, con=Depends(get_db)) -> BatchCrmAddResponse:
    """
    Add a specific investor from a batch result to CRM.

    Uses the existing add_lead_from_gate() flow (same as single-gate add-to-crm).
    Flips crm_added=True in the batch checkpoint on success.
    """
    from contra.crm.writer import add_lead_from_gate
    from contra.gate.batch import mark_crm_added
    try:
        add_lead_from_gate(con, req.session_id)
        mark_crm_added(req.batch_id, req.investor_name)
        return BatchCrmAddResponse(ok=True, investor_name=req.investor_name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
