# settings.py
import os
import json
from pathlib import Path
from typing import Any, Dict

# === PROJECT ROOT ===
BASE_DIR = Path(__file__).resolve().parent  # starbridge/
SETTINGS_FILE = BASE_DIR / "settings.json"

# === DEFAULTS (safe fallbacks) ===
DEFAULT_SETTINGS = {
    "git_executable": "git",
    "repositories": [],
    "stargit_url": "https://stargit.com",
    "server_name": "StarBridge Server",
    "log_level": "INFO",
    "log_file": str(BASE_DIR / "starbridge.log"),
    "live_sync": {
        "enabled": True,
        "debounce_seconds": 0.8,
        "report_ignored_every": 1000
    },
    "api": {
        "live_update_endpoint": "https://stargit.com/api/servers/live-update",
        "poll_interval_seconds": 30
    }
}

# === LOAD SETTINGS ===
_settings: Dict[str, Any] = {}

def load_settings(reload: bool = False) -> Dict[str, Any]:
    """Load settings.json with defaults and validation"""
    global _settings

    if _settings and not reload:
        return _settings  # Already loaded

    user_settings = {}

    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                user_settings = json.load(f)
            # Merge: user overrides defaults
            _settings = {**DEFAULT_SETTINGS, **user_settings}
            print(f"Settings loaded from {SETTINGS_FILE}")
        except Exception as e:
            print(f"Failed to load settings.json: {e} — using defaults")
            _settings = DEFAULT_SETTINGS.copy()
    else:
        print(f"settings.json not found — using defaults")
        _settings = DEFAULT_SETTINGS.copy()

    # Deep merge for nested dicts (like live_sync)
    for key, default_val in DEFAULT_SETTINGS.items():
        if isinstance(default_val, dict) and key in user_settings:
            _settings[key] = {**default_val, **user_settings.get(key, {})}

    return _settings

# === PUBLIC ACCESSORS ===
def get(key: str, default: Any = None) -> Any:
    """Get a setting: settings.get('git_executable')"""
    load_settings()  # Ensure loaded
    return _settings.get(key, default)

def get_nested(section: str, key: str, default: Any = None) -> Any:
    """Get nested: settings.get_nested('live_sync', 'debounce_seconds')"""
    load_settings()
    return _settings.get(section, {}).get(key, default)

def add_repository(repo_path: str, reload_settings: bool = True) -> None:
    """Add a repository path to settings.json if not already present."""
    load_settings(reload=reload_settings)
    repo_path = str(repo_path)

    if repo_path not in _settings.get("repositories", []):
        _settings["repositories"].append(repo_path)
        _save_settings()
        print(f"Repository added to settings: {repo_path}")


def remove_repository(repo_path: str, reload_settings: bool = True) -> None:
    """Remove a repository path from settings.json."""
    load_settings(reload=reload_settings)
    repo_path = str(repo_path)

    if repo_path in _settings.get("repositories", []):
        _settings["repositories"].remove(repo_path)
        _save_settings()
        print(f"Repository removed from settings: {repo_path}")


def _save_settings() -> None:
    """Save current _settings dict to settings.json"""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(_settings, f, indent=2)
        print(f"Settings saved to {SETTINGS_FILE}")
    except Exception as e:
        print(f"Failed to save settings.json: {e}")
    
# Auto-load on import
load_settings()

# === Convenience exports ===
GIT_EXECUTABLE = get("git_executable")
REPOSITORIES = get("repositories", [])
STARGIT_URL = get("stargit_url")
SERVER_NAME = get("server_name")
LOG_LEVEL = get("log_level")
LOG_FILE = get("log_file")
LIVE_SYNC_ENABLED = get_nested("live_sync", "enabled", True)
DEBOUNCE_SECONDS = get_nested("live_sync", "debounce_seconds", 0.8)
REPORT_IGNORED_EVERY = get_nested("live_sync", "report_ignored_every", 1000)
LIVE_UPDATE_ENDPOINT = get_nested("api", "live_update_endpoint")

CERT_PATH = get("ssl.cert_path", "certs/cert.pem")      # dot notation
KEY_PATH = get("ssl.key_path", "certs/key.pem")

