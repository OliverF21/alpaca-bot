"""
run_all.py — Alpaca Bot orchestrator
=====================================
Starts all three services and keeps them running forever.

    python run_all.py                   # equity + crypto + web dashboard
    python run_all.py --no-web          # scanners only
    python run_all.py --no-equity       # crypto + web (weekend mode)
    python run_all.py --no-crypto       # equity + web only

Logs are written to  logs/<service>_YYYYMMDD.log  (rotates at midnight).
Press Ctrl-C to stop everything cleanly.
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import namedtuple
from datetime import date, datetime
from pathlib import Path

# ── Service definitions ────────────────────────────────────────────────────────

ServiceConfig = namedtuple("ServiceConfig", ["name", "cmd", "log_stem", "legacy_log"])

_REPO = Path(__file__).resolve().parent

ALL_SERVICES = [
    ServiceConfig(
        name="equity",
        cmd=[sys.executable, "scanner/run_scanner.py"],
        log_stem="equity_scanner",
        legacy_log=Path("/tmp/bot_logs/equity_scanner.log"),
    ),
    ServiceConfig(
        name="crypto",
        cmd=[sys.executable, "scanner/run_crypto_scanner.py"],
        log_stem="crypto_scanner",
        legacy_log=Path("/tmp/bot_logs/crypto_scanner.log"),
    ),
    ServiceConfig(
        name="web",
        cmd=[sys.executable, "webapp/server.py"],
        log_stem="web_server",
        legacy_log=None,
    ),
]

_BACKOFF_MIN = 5.0
_BACKOFF_MAX = 60.0
_WEB_PORT    = 8000


def _free_port(port: int):
    """Kill any process listening on *port* so the web service can bind."""
    import signal as _signal
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True,
        )
        pids = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
        for pid in pids:
            try:
                os.kill(pid, _signal.SIGTERM)
                print(f"  Freed port {port}: killed PID {pid}")
            except ProcessLookupError:
                pass
        if pids:
            time.sleep(1)   # brief grace period for SIGTERM
    except Exception as e:
        print(f"  Warning: could not free port {port}: {e}")

# ── ServiceProcess ─────────────────────────────────────────────────────────────

class ServiceProcess:
    def __init__(self, config: ServiceConfig, log_dir: Path):
        self.config = config
        self.log_dir = log_dir
        self.proc: subprocess.Popen | None = None
        self.state = "stopped"
        self.restart_count = 0
        self.next_restart_at = 0.0
        self._backoff = _BACKOFF_MIN
        self._log_file = None
        self._log_date: date | None = None

    def start(self):
        log_f = self._open_log()
        log_f.write(f"\n--- started {datetime.now().isoformat()} ---\n")
        log_f.flush()

        # Truncate legacy log on first start so dashboard shows fresh output
        if self.config.legacy_log and not self.config.legacy_log.exists():
            self.config.legacy_log.write_text("")

        self.proc = subprocess.Popen(
            self.config.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO),
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.state = "running"

        t = threading.Thread(target=self._log_reader, daemon=True)
        t.start()

    def stop(self):
        self.state = "stopped"
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        except OSError:
            pass
        finally:
            self.proc = None
            if self._log_file and not self._log_file.closed:
                self._log_file.write(f"--- stopped {datetime.now().isoformat()} ---\n")
                self._log_file.close()

    def poll_and_maybe_restart(self, now: float) -> str | None:
        """Return an event message if a crash or restart occurred, else None."""
        if self.proc is None or self.state == "stopped":
            return None

        ret = self.proc.poll()
        if ret is None:
            self.state = "running"
            return None

        # Process has exited
        msg = f"[{self.config.name}] exited (code {ret})"
        if ret == 0:
            self._backoff = _BACKOFF_MIN      # clean exit — reset backoff
        else:
            self._backoff = min(self._backoff * 2, _BACKOFF_MAX)

        self.state = "crashed"
        self.next_restart_at = now + self._backoff
        self.restart_count += 1
        msg += f" — restart in {self._backoff:.0f}s (#{self.restart_count})"

        if now >= self.next_restart_at:
            self.start()
            self.state = "running"

        return msg

    def maybe_do_restart(self, now: float) -> str | None:
        """After backoff delay, actually relaunch a crashed service."""
        if self.state == "crashed" and now >= self.next_restart_at:
            if self.config.name == "web":
                _free_port(_WEB_PORT)
            self.start()
            self.state = "running"
            return f"[{self.config.name}] restarted (#{self.restart_count})"
        return None

    # ── internal ────────────────────────────────────────────────────────────────

    def _open_log(self):
        today = date.today()
        if self._log_date != today or self._log_file is None or self._log_file.closed:
            if self._log_file and not self._log_file.closed:
                self._log_file.close()
            fname = self.log_dir / f"{self.config.log_stem}_{today.strftime('%Y%m%d')}.log"
            self._log_file = open(fname, "a", buffering=1, encoding="utf-8")
            self._log_date = today
        return self._log_file

    def _log_reader(self):
        legacy = None
        if self.config.legacy_log:
            try:
                legacy = open(self.config.legacy_log, "a", buffering=1, encoding="utf-8")
            except OSError:
                pass

        try:
            for line in self.proc.stdout:
                log_f = self._open_log()
                log_f.write(line)
                if legacy:
                    legacy.write(line)
        finally:
            if legacy:
                legacy.close()

# ── Terminal output ────────────────────────────────────────────────────────────

_STATE_LABEL = {
    "running": "RUNNING ",
    "crashed": "CRASHED ",
    "stopped": "STOPPED ",
}

def print_status(services: list[ServiceProcess]):
    ts = datetime.now().strftime("%H:%M:%S")
    parts = [f"{s.config.name}={_STATE_LABEL.get(s.state, s.state.upper()[:8])}"
             for s in services]
    line = f"[{ts}]  " + "  ".join(parts)
    print(f"\r{line:<79}", end="", flush=True)

def log_event(msg: str):
    print(f"\n{msg}", flush=True)

# ── Shutdown ───────────────────────────────────────────────────────────────────

def shutdown(services: list[ServiceProcess]):
    print("\nShutting down...", flush=True)
    for svc in services:
        svc.stop()
    print("All services stopped.")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Alpaca Bot — perpetual service runner")
    parser.add_argument("--no-equity", action="store_true", help="Skip equity scanner")
    parser.add_argument("--no-crypto", action="store_true", help="Skip crypto scanner")
    parser.add_argument("--no-web",    action="store_true", help="Skip web dashboard")
    args = parser.parse_args()

    active_cfgs = [
        s for s in ALL_SERVICES if not (
            (s.name == "equity" and args.no_equity) or
            (s.name == "crypto" and args.no_crypto) or
            (s.name == "web"    and args.no_web)
        )
    ]
    if not active_cfgs:
        print("Nothing to run (all services disabled).")
        sys.exit(1)

    # Create directories
    log_dir = _REPO / "logs"
    log_dir.mkdir(exist_ok=True)
    Path("/tmp/bot_logs").mkdir(parents=True, exist_ok=True)
    # Truncate legacy logs so the dashboard shows fresh output
    for cfg in active_cfgs:
        if cfg.legacy_log:
            try:
                cfg.legacy_log.write_text("")
            except OSError:
                pass

    # Signal handling
    shutdown_requested = False
    services = [ServiceProcess(cfg, log_dir) for cfg in active_cfgs]

    def _handle_signal(signum, frame):
        nonlocal shutdown_requested
        shutdown_requested = True

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Banner
    names = ", ".join(s.config.name for s in services)
    print(f"\nAlpaca Bot  —  starting: {names}")
    print(f"Logs → {log_dir}/\n")

    # Start all services (clear port first for web)
    for svc in services:
        if svc.config.name == "web":
            _free_port(_WEB_PORT)
        svc.start()

    # Monitor loop
    while not shutdown_requested:
        now = time.monotonic()
        for svc in services:
            event = svc.poll_and_maybe_restart(now)
            if event:
                log_event(event)
            else:
                restart_msg = svc.maybe_do_restart(now)
                if restart_msg:
                    log_event(restart_msg)
        print_status(services)
        time.sleep(2)

    shutdown(services)


if __name__ == "__main__":
    main()
