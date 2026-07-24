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
import signal
import subprocess
import sys
import time
from pathlib import Path

_DIR = Path(__file__).resolve().parent
PY = sys.executable
MIN_ALIVE_SECS = 30      # a child dying faster than this is "flapping" → back off
BACKOFF_START = 5
BACKOFF_MAX = 300


def _spawn(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen([PY, "-u", *args], cwd=str(_DIR))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=20)
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

    try:
        while not stopping:
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
