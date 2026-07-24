#!/usr/bin/env python3
"""Keep the website's side-pipeline section live from THIS machine (e.g. a Mac Mini).

The public status site is a single ``data.json`` on the ``status-live`` branch of
the erdos-open-problems repo. That file has many sections; the one this pipeline
produces is the top-level ``side_pipeline`` key. The *full* publisher
(``status_site/build_data.py``) rebuilds the whole document but needs the private
Neon database, so it can only run on the main campaign machine.

This publisher is deliberately tiny and **Neon-free**: every cycle it fetches the
current ``data.json``, replaces ONLY the ``side_pipeline`` section with a fresh
build from the local ``side_pipeline_runs/`` files, refreshes ``generated_at`` so
the dashboard's liveness stays honest, and force-updates a single root commit on
``status-live``. That lets the Mac Mini keep the side-pipeline progress updating
on the website 24/7, independent of the main machine.

Run continuously:
    python side_status_publish.py

Run once (smoke test):
    python side_status_publish.py --once

IMPORTANT — do not run this at the same time as the main machine's full
publisher (status_site/live_refresh.py). Both force-push to ``status-live``; if
both run they will overwrite each other's side-pipeline section every cycle. The
intended setup is: the Mac Mini runs the side pipeline + THIS publisher and is
the authoritative writer; the main machine's live_refresh.py is stopped.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent

# ── What / where ──────────────────────────────────────────────────────────────
STATUS_REPO = "https://github.com/erichou1/erdos-open-problems.git"
BRANCH = "status-live"
DATA_PATH_IN_REPO = "status_site/data.json"
DEFAULT_WORKTREE = Path("/tmp/erdos-side-status-live")
# The side pipeline writes its per-problem state here (matches side_pipeline.py's
# DEFAULT_STATE_DIR = <repo>/erdos_problems/side_pipeline_runs).
DEFAULT_RUNS_DIR = _SCRIPT_DIR / "erdos_problems" / "side_pipeline_runs"
DEFAULT_INTERVAL = 20          # seconds between cycles (how quickly page updates appear)
HEARTBEAT_SECONDS = 120.0      # push at least this often so the site never shows stale
BOT_NAME = "Side Pipeline Status Bot"
BOT_EMAIL = "status-bot@local.invalid"

# ── side-pipeline snapshot (copied verbatim from build_data.py; Neon-free) ────
SIDE_PIPELINE_TEXT_LIMIT = 8000
SIDE_PIPELINE_ACTIVE_STALE_S = 150.0


def _side_pipeline_active_ids(directory: Path) -> set[str]:
    """IDs the running pipeline reports as on a worker right now (fresh heartbeat)."""
    try:
        manifest = json.loads((directory / "_active.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(str(manifest.get("heartbeat")))).total_seconds()
    except (TypeError, ValueError):
        return set()
    if age > SIDE_PIPELINE_ACTIVE_STALE_S:
        return set()
    return {str(a.get("id")) for a in (manifest.get("active") or []) if a.get("id")}


def _runtime(directory: Path) -> dict[str, Any]:
    """Expose the live pipeline heartbeat/pid/state (from the _active.json manifest)
    so the dashboard can show whether the pipeline process is online right now."""
    try:
        manifest = json.loads((directory / "_active.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    state = str(manifest.get("state") or "run")
    return {
        "heartbeat": manifest.get("heartbeat"),
        "pid": manifest.get("pid"),
        "state": state,
        "paused": bool(manifest.get("paused", state == "pause")),
        "workers_active": len(manifest.get("active") or []),
    }


def _terminal_log(directory: Path, max_lines: int = 200) -> str:
    """Last N lines of the pipeline's terminal log, for the website's terminal view."""
    try:
        text = (directory / "_terminal.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-max_lines:])


def _side_pipeline(directory: Path) -> dict[str, Any]:
    """Build the side-pipeline section from the local run-state files."""
    problems: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict) or not record.get("id"):
            continue
        problem_status = str(record.get("status") or "")
        stages: list[dict[str, Any]] = []
        for stage in record.get("stages") or []:
            text = str(stage.get("text") or "")
            clipped = text[:SIDE_PIPELINE_TEXT_LIMIT]
            # A stage that claims "solved" is only truly solved if the PROBLEM was
            # verified solved (status == "solved"); otherwise show it as a candidate.
            assessment = stage.get("assessment")
            if assessment == "solved" and problem_status != "solved":
                assessment = "candidate"
            stages.append({
                "stage": stage.get("stage"),
                "role": stage.get("role"),
                "round": stage.get("round"),
                "at": stage.get("at"),
                "chars": stage.get("chars", len(text)),
                "conversation_url": stage.get("conversation_url"),
                "assessment": assessment,
                "timed_out": bool(stage.get("timed_out")),
                "text": clipped,
                "text_truncated": len(text) > len(clipped),
            })
        refs_str = str(record.get("references") or "")
        reference_links = re.findall(r"https?://[^\s)<>\"']+", refs_str)
        problems.append({
            "id": record.get("id"),
            "title": record.get("title") or record.get("id"),
            "status": record.get("status"),
            "current_stage": record.get("current_stage"),
            "phase": record.get("phase"),
            "round": record.get("round", 0),
            "max_rounds": record.get("max_rounds"),
            "worker": record.get("worker"),
            "started_at": record.get("started_at"),
            "updated_at": record.get("updated_at"),
            "adapt_conversation_url": record.get("adapt_conversation_url"),
            "research_conversation_url": record.get("research_conversation_url"),
            "verify_conversation_url": record.get("verify_conversation_url"),
            "problem_statement": str(record.get("problem_statement") or "")[:SIDE_PIPELINE_TEXT_LIMIT],
            "references": refs_str[:4000],
            "reference_links": reference_links[:8],
            "stages": stages,
        })
    problems.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    active_ids = _side_pipeline_active_ids(directory)
    for item in problems:
        item["live"] = bool(str(item.get("status")) == "running"
                            and str(item.get("id")) in active_ids)
    status_counts = collections.Counter(str(item.get("status")) for item in problems)
    live_count = sum(1 for item in problems if item.get("live"))
    running_files = status_counts.get("running", 0)
    return {
        "problems": problems,
        "summary": {
            "total": len(problems),
            "by_status": dict(sorted(status_counts.items())),
            "solved": status_counts.get("solved", 0),
            "running": live_count,
            "queued": max(0, running_files - live_count),
            "exhausted": status_counts.get("exhausted", 0),
            "failed": status_counts.get("failed", 0),
        },
        "runtime": _runtime(directory),
        "log": _terminal_log(directory),
    }


# ── git plumbing ──────────────────────────────────────────────────────────────
def _run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        args, cwd=str(cwd), check=False, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[1:])} failed ({result.returncode}):\n{result.stdout}")
    return result


def _ensure_worktree(worktree: Path) -> None:
    """A minimal local git repo tracking only the status-live branch (no history)."""
    if (worktree / ".git").exists():
        return
    worktree.mkdir(parents=True, exist_ok=True)
    _run("git", "init", "-q", cwd=worktree)
    _run("git", "remote", "add", "origin", STATUS_REPO, cwd=worktree)


def _fetch_branch(worktree: Path) -> bool:
    """Fetch the latest status-live tip (depth 1) into the working tree.

    Returns False if the branch does not exist on the remote yet.
    """
    fetched = _run("git", "fetch", "--depth", "1", "origin", BRANCH, cwd=worktree, check=False)
    if fetched.returncode != 0:
        return False
    _run("git", "checkout", "-q", "--detach", "FETCH_HEAD", cwd=worktree)
    _run("git", "reset", "-q", "--hard", "FETCH_HEAD", cwd=worktree)
    return True


def _push_snapshot(worktree: Path) -> None:
    """Commit the whole working tree as a single ROOT (parentless) commit and
    force-push it to status-live. Parentless keeps the branch a single commit so
    the repo does not accumulate 7 MB data.json blobs on every push."""
    _run("git", "add", "-A", cwd=worktree)
    tree = _run("git", "write-tree", cwd=worktree).stdout.strip()
    message = f"side-pipeline status {datetime.now(timezone.utc).isoformat()}"
    commit = _run(
        "git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
        "commit-tree", tree, "-m", message, cwd=worktree,
    ).stdout.strip()
    pushed = _run("git", "push", "--force", "origin", f"{commit}:refs/heads/{BRANCH}",
                  cwd=worktree, check=False)
    if pushed.returncode != 0:
        raise RuntimeError(f"push to {BRANCH} failed:\n{pushed.stdout}")


def _log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def cycle(worktree: Path, runs_dir: Path, last_push: float) -> float:
    """One refresh. Returns the (possibly updated) last-push timestamp."""
    _ensure_worktree(worktree)
    if not _fetch_branch(worktree):
        _log(f"branch '{BRANCH}' not found on origin yet — has the main publisher "
             "created it? skipping.")
        return last_push
    data_path = worktree / DATA_PATH_IN_REPO
    try:
        document = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log(f"could not read {DATA_PATH_IN_REPO}: {exc}; skipping.")
        return last_push

    fresh = _side_pipeline(runs_dir)
    changed = fresh != document.get("side_pipeline")
    due = (time.time() - last_push) >= HEARTBEAT_SECONDS
    if not (changed or due):
        return last_push

    document["side_pipeline"] = fresh
    document["generated_at"] = datetime.now(timezone.utc).isoformat()
    data_path.write_text(json.dumps(document, indent=2, ensure_ascii=True) + "\n",
                         encoding="utf-8")
    _push_snapshot(worktree)
    summary = fresh["summary"]
    _log(f"published side_pipeline: {summary['total']} problems "
         f"({summary['solved']} solved, {summary['running']} live, "
         f"{summary['queued']} queued){' [heartbeat]' if not changed else ''}")
    return time.time()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR,
                        help="side_pipeline_runs directory to publish "
                             "(default: <repo>/erdos_problems/side_pipeline_runs)")
    parser.add_argument("--worktree", type=Path, default=DEFAULT_WORKTREE,
                        help="scratch git checkout of the status-live branch")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help="seconds between refresh cycles (min 30)")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = parser.parse_args(argv)
    if args.interval < 10:
        parser.error("--interval must be at least 10 seconds")

    runs_dir = args.runs_dir.resolve()
    worktree = args.worktree.resolve()
    _log(f"side-pipeline status publisher — runs: {runs_dir}")
    if not runs_dir.exists():
        _log(f"note: {runs_dir} does not exist yet (the pipeline creates it on first run).")

    last_push = 0.0
    while True:
        try:
            last_push = cycle(worktree, runs_dir, last_push)
        except Exception as exc:  # ops aid: log and retry; never crash the loop
            _log(f"refresh error: {type(exc).__name__}: {exc}")
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
