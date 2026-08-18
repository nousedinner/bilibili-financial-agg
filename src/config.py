"""Load config.yaml + environment variables (.env file)."""

import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

_CONFIG = None

def get_config() -> dict:
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG

    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        _CONFIG = yaml.safe_load(f)

    # Overlay env vars for sensitive fields
    _CONFIG.setdefault("database", {})
    _CONFIG["database"]["user"] = os.environ.get("DB_USER", "zzybili")
    _CONFIG["database"]["password"] = os.environ.get("DB_PASSWORD", "")
    _CONFIG["database"]["host"] = os.environ.get("DB_HOST", "mysql")
    _CONFIG["database"]["port"] = int(os.environ.get("DB_PORT", "3306"))
    _CONFIG["database"]["name"] = os.environ.get("DB_NAME", "fin_agg")

    return _CONFIG


def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)
