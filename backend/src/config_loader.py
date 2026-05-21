import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"
_ENV_PATH = Path(__file__).parent.parent / "config" / ".env"

load_dotenv(_ENV_PATH)


def load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def get_env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


CONFIG = load_config()
