from pathlib import Path

# Load .env from project root before any CLI command runs.
_root = Path(__file__).resolve().parent.parent
_env_file = _root / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file, override=False)
    except ImportError:
        pass

from contra.cli import app

if __name__ == "__main__":
    app()
