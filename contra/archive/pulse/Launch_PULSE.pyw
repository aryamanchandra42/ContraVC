"""
PULSE Launcher — double-click this file to open the LP Explorer in your browser.

No terminal window. No commands. Just double-click.

Requires Python to be installed (which it already is if PULSE runs on this machine).
.pyw files run with pythonw.exe — the windowless Python interpreter.
"""

import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8501
URL = f"http://localhost:{PORT}"


def _kill_existing_port(port: int) -> None:
    """Kill any process already listening on the port so we always start fresh."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for line in result.stdout.splitlines():
            if f":{port} " in line and ("LISTENING" in line or "LISTEN" in line):
                parts = line.strip().split()
                pid = parts[-1]
                if pid.isdigit():
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    time.sleep(0.5)
    except Exception:
        pass


def _install_explore_extras() -> None:
    """Install Streamlit extras silently if not yet present."""
    try:
        import streamlit  # noqa: F401
    except ImportError:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".[explore]", "-q"],
            cwd=str(ROOT),
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def _wait_for_server(url: str, timeout: int = 30) -> bool:
    """Poll until Streamlit is accepting connections, then open the browser."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> None:
    _kill_existing_port(PORT)
    _install_explore_extras()

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(ROOT / "pulse" / "explore" / "app.py"),
            "--server.port",
            str(PORT),
            "--server.headless",
            "true",
            "--server.runOnSave",
            "false",
            "--global.developmentMode",
            "false",
        ],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    # Wait for the server to be ready, then open the browser
    if _wait_for_server(URL, timeout=30):
        webbrowser.open(URL)
    else:
        # Fallback: open anyway after 5 s even if the health check timed out
        webbrowser.open(URL)

    # Keep this process alive so the Streamlit child doesn't get orphaned
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


if __name__ == "__main__":
    main()
