"""
9router_WatchEdit - Configuration & Constants
"""
import os
import re
from pathlib import Path

# Base Paths
APPDATA_ROUTER = Path(os.environ.get("APPDATA", "")) / "9router"
ROUTER_DB_PATH = APPDATA_ROUTER / "db" / "data.sqlite"
MACHINE_ID_FILE = APPDATA_ROUTER / "machine-id"
CLI_SECRET_FILE = APPDATA_ROUTER / "auth" / "cli-secret"

# Local WatchEdit Data Directory (LOCAL SECRET / RUNTIME LAYER — always OUTSIDE the repository).
# WATCHEDIT_DATA_DIR override exists for hermetic tests and portable installs.
LOCALAPPDATA_DIR = Path(os.environ.get("WATCHEDIT_DATA_DIR") or (Path(os.environ.get("LOCALAPPDATA", "")) / "9router_WatchEdit"))
LOCALAPPDATA_DIR.mkdir(parents=True, exist_ok=True)

HEALTH_CACHE_FILE = LOCALAPPDATA_DIR / "health_cache.json"
PRESETS_FILE = LOCALAPPDATA_DIR / "presets.json"
SETTINGS_FILE = LOCALAPPDATA_DIR / "settings.json"
BACKUP_DIR = LOCALAPPDATA_DIR / "db_backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# Secret isolation layout (task: SECRET ISOLATION + SAFE EXTERNAL DEVELOPMENT MODE)
CONFIG_DIR = LOCALAPPDATA_DIR / "config"
SECURE_DIR = LOCALAPPDATA_DIR / "secure"
PRIVATE_BACKUP_DIR = LOCALAPPDATA_DIR / "backups" / "private"
LEGACY_BACKUP_ROOT = LOCALAPPDATA_DIR / "backups"
DIAGNOSTICS_DIR = LOCALAPPDATA_DIR / "runtime" / "diagnostics"
for _d in (CONFIG_DIR, SECURE_DIR, PRIVATE_BACKUP_DIR, DIAGNOSTICS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Local (machine-private) settings file for the security layer
LOCAL_SETTINGS_FILE = CONFIG_DIR / "settings.json"

# Environment override allowing live provider access without UI unlock.
# Used by integration tests and headless runs on trusted machines.
LIVE_ACCESS_ENV = "WATCHEDIT_LIVE_ACCESS"

# 9Router API Defaults
DEFAULT_ROUTER_HOST = "127.0.0.1"
DEFAULT_ROUTER_PORT = 20128
DEFAULT_ROUTER_BASE_URL = f"http://{DEFAULT_ROUTER_HOST}:{DEFAULT_ROUTER_PORT}"

CLI_TOKEN_SALT = "9r-cli-auth"
CLI_TOKEN_HEADER = "x-9r-cli-token"

# Scanner Limits & Defaults
DEFAULT_GLOBAL_CONCURRENCY = 3
DEFAULT_PER_PROVIDER_CONCURRENCY = 1
DEFAULT_FAST_TIMEOUT_SEC = 4.0
DEFAULT_SLOW_TIMEOUT_SEC = 15.0
PENDING_THRESHOLD_SEC = 3.0
FAILURE_STREAK_FOR_DEAD = 3

# Probe Request Payload Defaults
PROBE_MAX_TOKENS = 1024  # Reasoning models require headroom to avoid false empty choices
PROBE_PROMPT = "hi"

# Secret Redaction Patterns
REDACT_PATTERNS = [
    re.compile(r'(?i)(bearer\s+)[a-zA-Z0-9_\-\.]{8,}', re.IGNORECASE),
    re.compile(r'sk-[a-zA-Z0-9_\-\.]{12,}'),
    re.compile(r'9r-[a-zA-Z0-9_\-\.]{8,}'),
]

def redact_secrets(text: str) -> str:
    """Redacts API keys, tokens, and sensitive headers from log/error text."""
    if not text:
        return ""
    cleaned = str(text)
    for pattern in REDACT_PATTERNS:
        cleaned = pattern.sub('[REDACTED]', cleaned)
    return cleaned
