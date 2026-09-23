"""
9router_WatchEdit - Launcher & Entry Point
Supports both native PySide6 desktop GUI and headless CLI execution for automated verification.
"""
import sys
import argparse
from pathlib import Path

# Ensure package root is in sys.path
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from config import DEFAULT_ROUTER_BASE_URL
from core.router_client import RouterClient, validate_router_base_url
from core.history import HealthCache
from core.discovery import ModelDiscovery
from core.probe import ScannerWorker, ScanMode
from core.combo_manager import PresetManager
from core.security import LiveAccessLockedError

# W2-005: authoritative CLI exit codes derived from ScannerWorker's terminal
# status. Only COMPLETED is success; every other terminal state is non-zero and
# distinctly identifiable so a scripted caller cannot read false green.
CLI_EXIT_OK = 0
CLI_EXIT_FAILED = 1
CLI_EXIT_LOCKED = 2
CLI_EXIT_CANCELLED = 3
CLI_EXIT_PERSISTENCE_FAILED = 4
CLI_EXIT_NOT_FOUND = 5
CLI_EXIT_BUSY = 6

_TERMINAL_EXITS = {
    "COMPLETED": CLI_EXIT_OK,
    "FAILED": CLI_EXIT_FAILED,
    "COMPLETED_PERSISTENCE_FAILED": CLI_EXIT_PERSISTENCE_FAILED,
    "CANCELLED": CLI_EXIT_CANCELLED,
    "LOCKED": CLI_EXIT_LOCKED,
}

_TERMINAL_MESSAGES = {
    "COMPLETED": "Scan completed successfully.",
    "FAILED": "Scan FAILED.",
    "COMPLETED_PERSISTENCE_FAILED": "Scan completed but results were NOT persisted.",
    "CANCELLED": "Scan CANCELLED.",
    "LOCKED": "Scan refused: secrets are LOCKED.",
}


def run_cli_mode(args) -> int:
    """Headless scan; returns the process exit code (W2-005).

    Only the COMPLETED terminal status prints success and exits 0. A requested
    combo that is not found, a busy scanner (run_scan returned None), and the
    LOCKED security boundary are all non-zero and clearly labelled.
    """
    print("=" * 60)
    print("9router_WatchEdit - Headless Scanner")
    print("=" * 60)

    client = RouterClient(base_url=args.router_url or DEFAULT_ROUTER_BASE_URL)
    cache = HealthCache()
    discovery = ModelDiscovery(client)

    try:
        reachable = client.is_server_reachable()
    except LiveAccessLockedError:
        print("Scan refused: secrets are LOCKED. Unlock live access first.")
        return CLI_EXIT_LOCKED
    if not reachable:
        print(f"[!] Warning: 9Router not reachable at {client.base_url}. Using SQLite fallback.")

    if args.list_combos:
        try:
            combos = client.get_combos()
        except LiveAccessLockedError:
            print("List combos refused: secrets are LOCKED. Unlock live access first.")
            return CLI_EXIT_LOCKED
        print(f"Found {len(combos)} combos in 9Router:")
        for c in combos:
            models = c.get("models", [])
            print(f"  • {c.get('name')} (ID: {c.get('id')}) — {len(models)} models")
            for idx, m in enumerate(models[:5]):
                rec = cache.get(m)
                st = f"[{rec.state}]" if rec else "[UNTESTED]"
                print(f"      #{idx+1:02d} {m} {st}")
            if len(models) > 5:
                print(f"      ... +{len(models)-5} more")
        return CLI_EXIT_OK

    try:
        models = discovery.discover_all()
    except LiveAccessLockedError:
        print("Scan refused: secrets are LOCKED. Unlock live access first.")
        return CLI_EXIT_LOCKED
    print(f"Discovered {len(models)} models across connected providers.\n")

    mode = ScanMode.QUICK
    if args.scan == "full":
        mode = ScanMode.FULL
    elif args.scan == "failed":
        mode = ScanMode.FAILED_ONLY

    worker = ScannerWorker(client, cache)

    def on_started(cid):
        print(f"  [PROBE] {cid} ...", flush=True)

    def on_pending(cid, elapsed):
        print(f"  [PENDING] {cid} ({elapsed:.0f}s)", flush=True)

    def on_finished(cid, rec):
        st = rec.state
        lat = f"{rec.latency_ms:.0f}ms"
        print(f"  [{st:<10}] {cid:<40} {lat:>8} | {rec.reason}", flush=True)

    worker.on_probe_started = on_started
    worker.on_probe_pending = on_pending
    worker.on_probe_finished = on_finished

    target_combo_models = None
    if args.combo:
        combos = client.get_combos()
        found = [c for c in combos if c.get("name") == args.combo]
        if found:
            target_combo_models = found[0].get("models", [])
            mode = ScanMode.COMBO
            print(f"Scanning combo '{args.combo}' ({len(target_combo_models)} models):")
        else:
            print(f"Combo '{args.combo}' not found.")
            return CLI_EXIT_NOT_FOUND

    print(f"Starting {mode.value} scan...")
    status = worker.run_scan(models, mode=mode, target_combo_models=target_combo_models)
    if status is None:
        # No lease: a session is already active or the scanner is closing.
        print("\nScan not started: another scan is already active.")
        return CLI_EXIT_BUSY

    print(f"\n{_TERMINAL_MESSAGES.get(status, f'Scan ended: {status}')}")
    return _TERMINAL_EXITS.get(status, CLI_EXIT_FAILED)

def run_gui_mode():
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication
    from ui.theme import apply_theme
    from ui.main_window import MainWindow

    if hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.Round
        )

    app = QApplication(sys.argv)
    app.setApplicationName("9router_WatchEdit")
    apply_theme(app)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())

def main():
    parser = argparse.ArgumentParser(description="9router_WatchEdit Companion")
    parser.add_argument("--cli", action="store_true", help="Run in headless CLI mode")
    parser.add_argument("--scan", choices=["quick", "full", "failed"], default="quick", help="Scan mode for CLI")
    parser.add_argument("--combo", type=str, help="Scan only specific combo by name")
    parser.add_argument("--list-combos", action="store_true", help="List all combos from 9Router")
    parser.add_argument(
        "--router-url", type=str, default=DEFAULT_ROUTER_BASE_URL,
        help="Local 9Router URL: explicit loopback IP only (127.0.0.0/8 or [::1]); custom ports allowed",
    )

    args = parser.parse_args()
    try:
        args.router_url = validate_router_base_url(args.router_url)
    except ValueError as ex:
        # argparse's type= error echoes the raw value, which may contain
        # userinfo. Report only our value-independent validation message.
        parser.error(f"--router-url: {ex}")

    if args.cli or args.list_combos or args.combo:
        sys.exit(run_cli_mode(args))
    else:
        run_gui_mode()

if __name__ == "__main__":
    main()
