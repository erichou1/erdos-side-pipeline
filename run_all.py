#!/usr/bin/env python3
"""Supervise the side pipeline AND the website publisher together.

One command to run everything on the Mac Mini for days unattended. It launches
both child processes and restarts either one if it exits (with a progressive
backoff so a child that keeps dying immediately doesn't spin). Ctrl-C (or SIGTERM)
stops both cleanly.

    python run_all.py --workers 20

Children (run from this repo directory, unbuffered):
  * side_pipeline.py        — the research pipeline. It already has its own
                              internal browser-relaunch supervisor and honors the
                              remote stop/start/restart control, so this outer
                              supervisor only needs to handle a full process exit.
  * side_status_publish.py  — keeps the website's side-pipeline section live.

Options:
  --workers N     workers to pass to the pipeline (default 20)
  --no-publish    run only the pipeline (don't publish the website)
  --no-pipeline   run only the publisher
Any extra args after the known flags are passed through to side_pipeline.py,
e.g.  python run_all.py --workers 12 -- --max-rounds 30
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

_DIR = Path(__file__).resolve().parent
PY = sys.executable
MIN_ALIVE_SECS = 30      # a child dying faster than this is "flapping" → back off
BACKOFF_START = 5
BACKOFF_MAX = 300

# Remote 'update' command (from the website's Update button): the control file
# carries an integer 'update' nonce; a new value triggers one git pull + restart.
CONTROL_URL = os.environ.get(
    "SIDE_PIPELINE_CONTROL_URL",
    "https://egmra-status.vercel.app/api/control")
CONTROL_POLL_SECONDS = 10.0
_UPDATE_STATE = _DIR / "erdos_problems" / "side_pipeline_runs" / "_run_all_state.json"


def _spawn(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen([PY, "-u", *args], cwd=str(_DIR))


def _poll_control() -> Optional[dict]:
    """Return the control document (dict), or None on any error."""
    try:
        sep = "&" if "?" in CONTROL_URL else "?"
        req = urllib.request.Request(f"{CONTROL_URL}{sep}cb={int(time.time())}",
                                     headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _load_handled_update() -> int:
    try:
        return int(json.loads(_UPDATE_STATE.read_text(encoding="utf-8")).get("update", 0))
    except Exception:
        return 0


def _save_handled_update(nonce: int) -> None:
    try:
        _UPDATE_STATE.parent.mkdir(parents=True, exist_ok=True)
        _UPDATE_STATE.write_text(json.dumps({"update": int(nonce)}), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--no-publish", action="store_true", help="run only the pipeline")
    ap.add_argument("--no-pipeline", action="store_true", help="run only the publisher")
    args, extra = ap.parse_known_args()
    extra = [a for a in extra if a != "--"]

    specs: dict[str, list[str]] = {}
    if not args.no_pipeline:
        specs["pipeline"] = ["side_pipeline.py", "--workers", str(args.workers), *extra]
    if not args.no_publish:
        specs["publisher"] = ["side_status_publish.py"]
    if not specs:
        print("[run_all] nothing to run (both --no-pipeline and --no-publish)", flush=True)
        return 2

    procs: dict[str, subprocess.Popen] = {}
    backoff: dict[str, int] = {}
    last_start: dict[str, float] = {}

    def _log(msg: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} [run_all] {msg}", flush=True)

    for name, a in specs.items():
        procs[name] = _spawn(a)
        last_start[name] = time.time()
        backoff[name] = BACKOFF_START
        _log(f"started {name} (pid {procs[name].pid})")

    stopping = False

    def _stop(*_a) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    handled_update = _load_handled_update()
    current_workers = args.workers
    control_at = 0.0
    try:
        while not stopping:
            # Remote 'update': git pull + restart children when the nonce changes.
            now = time.time()
            if now - control_at >= CONTROL_POLL_SECONDS:
                control_at = now
                ctl = _poll_control()
                if ctl is not None:
                    # Worker-count change from the website → restart the pipeline child.
                    w = ctl.get("workers")
                    if (isinstance(w, int) and 1 <= w <= 40 and w != current_workers
                            and "pipeline" in specs):
                        current_workers = w
                        specs["pipeline"] = ["side_pipeline.py", "--workers", str(w), *extra]
                        _log(f"worker count → {w} (from web); restarting pipeline")
                        if procs.get("pipeline"):
                            try:
                                procs["pipeline"].terminate()
                            except Exception:
                                pass
                    # One-shot 'update' → git pull + restart all children.
                    nonce = int(ctl.get("update") or 0)
                    if nonce and nonce != handled_update:
                        handled_update = nonce
                        _save_handled_update(nonce)
                        _log("update requested via control — running git pull")
                        r = subprocess.run(["git", "-C", str(_DIR), "pull", "--ff-only"],
                                           text=True, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT)
                        _log(f"git pull ({r.returncode}): {(r.stdout or '').strip()[-300:]}")
                        if r.returncode == 0:
                            _log("restarting children to apply the update")
                            for nm in list(procs):
                                try:
                                    procs[nm].terminate()
                                except Exception:
                                    pass
                        else:
                            _log("update skipped — git pull failed; fix the repo on this machine")
            for name, a in specs.items():
                if stopping:
                    break
                ret = procs[name].poll()
                if ret is None:
                    continue
                alive = time.time() - last_start[name]
                backoff[name] = (min(BACKOFF_MAX, backoff[name] * 2)
                                 if alive < MIN_ALIVE_SECS else BACKOFF_START)
                _log(f"{name} exited (code {ret}) after {alive:.0f}s; "
                     f"restarting in {backoff[name]}s")
                for _ in range(backoff[name]):
                    if stopping:
                        break
                    time.sleep(1)
                if stopping:
                    break
                procs[name] = _spawn(a)
                last_start[name] = time.time()
                _log(f"restarted {name} (pid {procs[name].pid})")
            time.sleep(2)
    finally:
        _log("stopping children…")
        for p in procs.values():
            try:
                p.terminate()
            except Exception:
                pass
        deadline = time.time() + 12
        for p in procs.values():
            try:
                p.wait(timeout=max(0.0, deadline - time.time()))
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        _log("stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
