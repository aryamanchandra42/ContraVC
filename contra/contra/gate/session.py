"""
In-memory TTL session store for gate chat sessions.

Sessions expire after SESSION_TTL seconds (default 30 min). Each session
carries the full context needed to answer follow-up questions and re-run
the evaluator when the analyst provides new facts.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SESSION_TTL = 1800  # 30 minutes


@dataclass
class GateSession:
    session_id: str
    lp_name: str
    # Serialised dicts (not live objects) so the session is self-contained
    brief_dict: Dict[str, Any]
    web_context: str
    assessment_dict: Dict[str, Any]
    result_dict: Dict[str, Any]
    explanation_dict: Dict[str, Any] = field(default_factory=dict)
    # Grows through chat
    analyst_facts: List[str] = field(default_factory=list)
    message_history: List[Dict[str, str]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > SESSION_TTL


_store: Dict[str, GateSession] = {}
_lock = threading.Lock()


def _purge_expired() -> None:
    expired = [sid for sid, s in _store.items() if s.is_expired()]
    for sid in expired:
        del _store[sid]


def create_session(session: GateSession) -> None:
    with _lock:
        _purge_expired()
        _store[session.session_id] = session


def get_session(session_id: str) -> Optional[GateSession]:
    with _lock:
        _purge_expired()
        return _store.get(session_id)


def update_session(
    session_id: str,
    analyst_facts: Optional[List[str]] = None,
    new_message: Optional[Dict[str, str]] = None,
    result_dict: Optional[Dict[str, Any]] = None,
    assessment_dict: Optional[Dict[str, Any]] = None,
) -> None:
    with _lock:
        session = _store.get(session_id)
        if session is None:
            return
        if analyst_facts is not None:
            session.analyst_facts = analyst_facts
        if new_message is not None:
            session.message_history.append(new_message)
        if result_dict is not None:
            session.result_dict = result_dict
        if assessment_dict is not None:
            session.assessment_dict = assessment_dict
