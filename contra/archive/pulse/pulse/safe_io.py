"""
pulse.safe_io — utilities for writing files safely on Windows.

The core problem: Excel locks CSV files when they are open. A direct
`df.to_csv(path)` raises PermissionError. The safe writer writes to a
temp file in the same directory and then atomically replaces the target.
If even the replacement is blocked (file still open), it writes a
timestamped fallback alongside the target rather than aborting.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Union

import pandas as pd


def safe_write_csv(df: pd.DataFrame, out_path: Union[str, Path], **to_csv_kwargs) -> Path:
    """
    Write *df* to *out_path* as CSV.

    Uses a write-then-rename strategy so the operation succeeds even when
    the target file is open in Excel (which holds a write lock on Windows).
    Falls back to a timestamped sibling file if the rename is also blocked.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    to_csv_kwargs.setdefault("index", False)

    tmp_fd, tmp_path_str = tempfile.mkstemp(
        suffix=".csv",
        dir=out_path.parent,
        prefix="_pulse_tmp_",
    )
    tmp_path = Path(tmp_path_str)
    try:
        os.close(tmp_fd)
        df.to_csv(tmp_path, **to_csv_kwargs)
        try:
            os.replace(tmp_path, out_path)
            return out_path
        except PermissionError:
            # Target is still locked — write alongside with a timestamp suffix
            stamp = datetime.now().strftime("%H%M%S")
            fallback = out_path.with_stem(f"{out_path.stem}_{stamp}")
            shutil.move(str(tmp_path), fallback)
            return fallback
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
