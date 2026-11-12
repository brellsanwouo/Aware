"""Environment loading helpers."""

from __future__ import annotations

import os
import threading
from pathlib import Path


RUNTIME_ENV_VARS = frozenset({
    "AWARE_LLM_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "OLLAMA_API_KEY",
    "OLLAMA_BASE_URL",
    "OLLAMA_MODEL",
    "OLLAMA_TIMEOUT_SECONDS",
    "EXECUTOR_MAX_AGENTS",
})
_DOTENV_MANAGED: dict[str, str] = {}
_ENV_LOCK = threading.Lock()


def load_env() -> list[Path]:
    """Load `.env` values without replacing variables set by the shell."""
    loaded: list[Path] = []
    with _ENV_LOCK:
        for path in _candidate_env_paths():
            if not path.exists() or not path.is_file():
                continue
            _load_dotenv_file(path)
            loaded.append(path)
    return loaded


def refresh_env(keys: frozenset[str] = RUNTIME_ENV_VARS) -> None:
    """Refresh file-managed values while preserving explicit shell overrides."""
    file_values: dict[str, str] = {}
    for path in _candidate_env_paths():
        if path.is_file():
            for key, value in _iter_dotenv_values(path):
                if key in keys:
                    file_values.setdefault(key, value)

    with _ENV_LOCK:
        for key in keys:
            previous = _DOTENV_MANAGED.get(key)
            current = os.environ.get(key)
            if previous is not None and current != previous:
                _DOTENV_MANAGED.pop(key, None)
                continue
            if key in file_values:
                if previous is not None or current is None:
                    os.environ[key] = file_values[key]
                    _DOTENV_MANAGED[key] = file_values[key]
            elif previous is not None:
                os.environ.pop(key, None)
                _DOTENV_MANAGED.pop(key, None)


def _candidate_env_paths() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.getenv("AWARE_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.cwd() / ".env")
    # Project root when launched from another current working directory.
    candidates.append(Path(__file__).resolve().parents[1] / ".env")

    seen: set[str] = set()
    unique: list[Path] = []
    for item in candidates:
        try:
            key = str(item.resolve())
        except Exception:
            key = str(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _load_dotenv_file(path: Path) -> None:
    for clean_key, clean_value in _iter_dotenv_values(path):
        if clean_key not in os.environ:
            os.environ[clean_key] = clean_value
            _DOTENV_MANAGED[clean_key] = clean_value


def _iter_dotenv_values(path: Path):
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        clean_key = key.strip()
        clean_value = _clean_value(value)
        if clean_key:
            yield clean_key, clean_value


def _clean_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if (value.startswith("'") and value.endswith("'")) or (
        value.startswith('"') and value.endswith('"')
    ):
        return value[1:-1]
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def env_bool(name: str, default: bool = True) -> bool:
    """Read a boolean env var with tolerant parsing."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on", "y"}:
        return True
    if value in {"0", "false", "no", "off", "n"}:
        return False
    return default


def resolve_max_agents(override: int | None = None) -> int:
    """Maximum total Executor sub-agents instantiated for one assessment."""
    if override is not None:
        return max(1, int(override))
    configured = os.getenv("EXECUTOR_MAX_AGENTS", "5").strip()
    return max(1, int(configured)) if configured.isdigit() else 5
