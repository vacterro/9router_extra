"""
9router_WatchEdit - Configuration & Constants
"""
import os
import re
from pathlib import Path

# Source tree root: private runtime state must ALWAYS stay outside this tree
# (audit SRC-001:R0001 / CORE-001). config.py lives at <root>/9router_WatchEdit/.
REPO_ROOT = Path(__file__).resolve().parents[1]

# Unavailable sentinel: os.devnull cannot host a directory tree. Every derived
# path under it is relative-looking, fails .mkdir()/.write_text() with OSError,
# and reports .exists() == False, so read-only consumers degrade cleanly.
_UNAVAILABLE_ROOT = Path(os.devnull)


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolve_appdata_router() -> Path:
    """9Router machine/db root. Fail-closed (CORE-001): without a usable APPDATA
    there is NO router path -- never a CWD-relative one."""
    raw = os.environ.get("APPDATA", "")
    if raw:
        p = Path(raw).expanduser()
        try:
            resolved = p.resolve()
        except OSError:
            return _UNAVAILABLE_ROOT / "9router"
        if p.is_absolute() and not _is_inside(resolved, REPO_ROOT):
            return p / "9router"
    return _UNAVAILABLE_ROOT / "9router"


def _resolve_private_root():
    """Local WatchEdit private root.

    WATCHEDIT_DATA_DIR (hermetic tests / portable installs) is honoured ONLY as
    an absolute path that resolves OUTSIDE the repository; a relative override
    is CWD-dependent by definition and is rejected. Missing LOCALAPPDATA means
    private storage is UNAVAILABLE -- never CWD-relative (CORE-001).
    Returns None when no valid private root exists.
    """
    raw = os.environ.get("WATCHEDIT_DATA_DIR")
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            return None
        try:
            resolved = p.resolve()
        except OSError:
            return None
        if _is_inside(resolved, REPO_ROOT):
            return None
        return p
    raw = os.environ.get("LOCALAPPDATA", "")
    if raw:
        p = Path(raw).expanduser()
        if p.is_absolute():
            try:
                resolved = p.resolve()
            except OSError:
                return None
            if not _is_inside(resolved, REPO_ROOT):
                return p / "9router_WatchEdit"
    return None

# Base Paths (read-only consumers: RouterClient degrades via .exists() checks)
APPDATA_ROUTER = _resolve_appdata_router()
ROUTER_DB_PATH = APPDATA_ROUTER / "db" / "data.sqlite"
MACHINE_ID_FILE = APPDATA_ROUTER / "machine-id"
CLI_SECRET_FILE = APPDATA_ROUTER / "auth" / "cli-secret"


def _ensure_dir(p: Path) -> bool:
    """Best-effort creation. GATE 19 + CORE-001: an unusable private root must
    degrade to PRIVATE STORAGE UNAVAILABLE / SECRETS LOCKED — never crash at import
    and NEVER fall back to writing private state inside the repository (or anywhere
    CWD-relative)."""
    if p is None or not p.is_absolute():
        return False
    try:
        if _is_inside(p.resolve(), REPO_ROOT):
            return False
        p.mkdir(parents=True, exist_ok=True)
        return p.is_dir()
    except OSError:
        return False


# Local WatchEdit Data Directory (LOCAL SECRET / RUNTIME LAYER — always OUTSIDE the repository).
_private_root = _resolve_private_root()
# PRIVATE_STORAGE_AVAILABLE means the root was resolved to a usable directory
# (absolute, outside the repo, creatable/existing) — not merely "a path was chosen".
PRIVATE_STORAGE_CREATED = _ensure_dir(_private_root)
PRIVATE_STORAGE_AVAILABLE = PRIVATE_STORAGE_CREATED
LOCALAPPDATA_DIR = _private_root if PRIVATE_STORAGE_AVAILABLE else _UNAVAILABLE_ROOT

HEALTH_CACHE_FILE = LOCALAPPDATA_DIR / "health_cache.json"
PRESETS_FILE = LOCALAPPDATA_DIR / "presets.json"
SETTINGS_FILE = LOCALAPPDATA_DIR / "settings.json"
BACKUP_DIR = LOCALAPPDATA_DIR / "db_backups"

# Secret isolation layout (task: SECRET ISOLATION + SAFE EXTERNAL DEVELOPMENT MODE)
CONFIG_DIR = LOCALAPPDATA_DIR / "config"
SECURE_DIR = LOCALAPPDATA_DIR / "secure"
PRIVATE_BACKUP_DIR = LOCALAPPDATA_DIR / "backups" / "private"
LEGACY_BACKUP_ROOT = LOCALAPPDATA_DIR / "backups"
DIAGNOSTICS_DIR = LOCALAPPDATA_DIR / "runtime" / "diagnostics"
# OCF-001: local OpenCode Free bridge private runtime root. The official
# `opencode` CLI is executed with this directory as its XDG root, so the
# USER'S global OpenCode config/data is never read for writes and never
# rewritten. Always outside the repository (private storage root).
OPENCODE_BRIDGE_DIR = LOCALAPPDATA_DIR / "runtime" / "opencode_bridge"
if PRIVATE_STORAGE_AVAILABLE:
    for _d in (BACKUP_DIR, CONFIG_DIR, SECURE_DIR, PRIVATE_BACKUP_DIR, DIAGNOSTICS_DIR,
               OPENCODE_BRIDGE_DIR):
        _ensure_dir(_d)

# OCF-001 provider lane constants (prefix is the public 9Router model namespace)
OCF_PROVIDER_PREFIX = "ocf"
OCF_PROVIDER_NAME = "OpenCode Local Free"
OCF_UPSTREAM_PREFIX = "opencode"
# The PUBLIC/catalog OpenCode identity in a live 9Router install: the registry
# entry declares id "opencode" with alias/uiAlias "oc". Direct `oc/*` free
# models stay visible for discovery/history but are NOT routable while the
# upstream rejects arbitrary clients (OCF-001).
OCF_DIRECT_PREFIX = "oc"
OCF_DIRECT_PREFIX_ALIASES = ("oc", "opencode")
OCF_BRIDGE_HOST = "127.0.0.1"
OCF_BRIDGE_DEFAULT_PORT = 20130

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
REASONING_MODEL_TIMEOUT_SEC = 30.0  # Extended timeout for reasoning models like SWE-1.6 Slow
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
    re.compile(r'(?i)kira_[a-zA-Z0-9_\-\.]{8,}'),
]

# Reasoning Model Patterns - models that need extended timeout
REASONING_MODEL_PATTERNS = [
    re.compile(r'swe-1\.6-slow', re.IGNORECASE),
    re.compile(r'reasoning', re.IGNORECASE),
    re.compile(r'thinking', re.IGNORECASE),
]

def redact_secrets(text: str) -> str:
    """Redacts API keys, tokens, and sensitive headers from log/error text."""
    if not text:
        return ""
    cleaned = str(text)
    for pattern in REDACT_PATTERNS:
        cleaned = pattern.sub('[REDACTED]', cleaned)
    return cleaned
