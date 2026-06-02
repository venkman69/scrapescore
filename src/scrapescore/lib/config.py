"""Package-level config loaded once at import time."""
import importlib.resources as _resources
import yaml
from pathlib import Path


def _load_app_config() -> dict:
    # config.yaml is at the project root: lib/ -> job_score/ -> src/ -> project root
    config_path = Path(__file__).parent.parent.parent.parent / "config.yaml"
    try:
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _load_resource_config() -> dict:
    try:
        p = Path(str(_resources.files("scrapescore.resources").joinpath("job_finder_config.yaml")))
        return yaml.safe_load(p.open()) or {}
    except Exception:
        return {}


APP_CONFIG: dict = _load_app_config()
RESOURCE_CONFIG: dict = _load_resource_config()


def _derive_prefix(cfg: dict) -> str:
    prefix = (cfg.get("server") or {}).get("base_url_prefix", "") or ""
    prefix = prefix.strip().rstrip("/")
    if prefix and not prefix.startswith("/"):
        prefix = "/" + prefix
    return prefix


BASE_PREFIX: str = _derive_prefix(APP_CONFIG)


def get_storage_dir_config(dir_name: str) -> str:
    storage_dirs = APP_CONFIG.get("storage_dirs", {})
    value = storage_dirs.get(dir_name)
    if not value:
        raise KeyError(f"Missing required config 'storage_dirs.{dir_name}' in config.yaml")
    return value
