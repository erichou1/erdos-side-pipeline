#!/usr/bin/env python3
"""
Side pipeline: adapt-a-meta-prompt → research → nudge-to-finish, in ChatGPT.

This is a SEPARATE pipeline from the EGMRA verification campaign. For each
problem it runs a three-stage ChatGPT conversation flow:

  Stage 1 (adapt)     — in a fresh chat, submit the fixed meta-prompt template
                        together with an instruction to *adapt* it to the given
                        problem, and capture the adapted prompt ChatGPT returns.
  Stage 2 (research)  — in a second fresh chat (one chat per problem, so you can
                        keep chatting after it finishes), submit the adapted
                        prompt and capture the first research attempt.
  Stage 3 (continue)  — while the attempt does not declare a complete
                        unconditional result, send a continuation nudge in the
                        SAME research chat and capture the next attempt, up to a
                        configurable cap.

It drives ONE headed Chromium (shared ChatGPT profile) with N tabs, one problem
per tab, cooperatively scheduled on a single thread (Playwright's sync API is
not thread-safe across a shared browser context). Each stage of every problem is
persisted to ``side_pipeline_runs/<id>.json`` so the status website can show the
per-iteration progress.

Reuses the proven page helpers in ``erdos_common`` (composer paste, response
extraction, conversation-URL capture, chat rename).

Examples
--------
Dry run (no browser; validates the state machine + writes sample state files):
    python3 side_pipeline.py --dry-run

Live (stop the EGMRA campaign first so the profile is free):
    python3 side_pipeline.py --problems side_pipeline_problems.json --workers 5
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import erdos_common as ec  # proven ChatGPT page helpers + PROFILE_DIR/PROJECT_URL

# ── Paths / defaults ──────────────────────────────────────────────────────────
DEFAULT_META_PROMPT = _SCRIPT_DIR / "side_pipeline_meta_prompt.txt"
DEFAULT_PROBLEMS = _SCRIPT_DIR / "side_pipeline_problems.json"
DEFAULT_STATE_DIR = _SCRIPT_DIR / "erdos_problems" / "side_pipeline_runs"

# ChatGPT Project this pipeline works inside (all adapt + research chats are created
# here, so they are grouped and named under one project). Defaults to the erdos3
# project so a second machine runs identically; override via CHATGPT_PROJECT_URL
# (env / .env) or --project-url.
DEFAULT_PROJECT_URL = os.environ.get(
    "CHATGPT_PROJECT_URL",
    "https://chatgpt.com/g/g-p-6a618ab7e89c81919024b0fa9d76a247-erdos3/project")

# ── Tuning constants ──────────────────────────────────────────────────────────
DEFAULT_WORKERS = 10
DEFAULT_MAX_ROUNDS = 10         # real solving attempts per problem (round 1 + continuations);
                               # the gated "is it solved?" self-assessment turns are NOT counted
DEFAULT_RESPONSE_TIMEOUT = 3600.0   # per response, seconds
ADAPT_MAX_TRIES = 3             # re-attempt a failed/garbled adapt before giving up
MIN_THINK_SECONDS = 600.0       # a research reply faster than this -> nudge deeper/longer thinking
DEFAULT_TICK = 3.0
URL_CAPTURE_TIMEOUT = 150.0
GEN_START_GRACE = 90.0          # wait this long for generation to *start*
STABLE_TICKS = 2                # identical response text across N ticks == settled
MIN_RESPONSE_CHARS = 40
RATE_LIMIT_COOLDOWN = 90.0      # base throttle backoff; grows exponentially per streak
RATE_LIMIT_MAX_COOLDOWN = 900.0 # cap the escalating backoff at 15 minutes
RATE_LIMIT_RESET = 300.0        # reset the backoff streak after this quiet period
RENAME_TRIES = 4                # attempts to (re)name a chat before giving up
NEW_CHAT_SPACING = 10.0         # min seconds between opening new chats (any tab); staggers worker startup + gentle on the throttle
CONTINUE_GEN_MAX = 12           # max "Continue generating" clicks per response
RELAUNCH_MAX = 60               # max Chromium relaunches to ride out crashes
RELAUNCH_PAUSE = 6.0            # seconds to wait before relaunching a dead browser
RAPID_DEATH_SECS = 45.0         # a launch dying faster than this counts as "can't start"
RAPID_DEATH_LIMIT = 3           # give up after this many consecutive rapid deaths

# ── Model guard: every NEW chat must use this model (best-effort; never blocks) ─
# A new chat's model name (as shown in ChatGPT's switcher) must contain ALL of
# these tokens, case-insensitively. Override with SIDE_PIPELINE_MODEL (space-sep).
TARGET_MODEL_TOKENS = tuple(
    t for t in os.environ.get("SIDE_PIPELINE_MODEL", "extra high").lower().split() if t)

# ── Remote control: the website writes a tiny control.json the pipeline polls ──
# state ∈ {"run","pause"}; "restart" is an integer nonce (a NEW value triggers one
# browser relaunch that resumes from saved state — it never resets any problem).
# Read the control command from the Vercel API endpoint (dynamic, always fresh)
# rather than raw.githubusercontent.com, which is CDN-cached for ~5 min and ignores
# cache-busting queries — that made stop/start/restart take minutes to reach here.
CONTROL_URL = os.environ.get(
    "SIDE_PIPELINE_CONTROL_URL",
    "https://egmra-status.vercel.app/api/control")
CONTROL_POLL_SECONDS = 8.0      # how often to check the remote control command

# Transient per-problem errors are re-queued (resumed) up to this many times before
# the problem is marked failed, so one flaky problem never stops the multi-day run.
PROBLEM_RETRY_MAX = 3

# Placeholder ec.extract_response returns when no assistant message is on the page
# (a fresh, blank, or dead tab). It is NEVER a valid reply, so the poller must not
# settle on it and resume must never reuse it as an adapted prompt.
_EXTRACT_FAILURE = "[Could not extract response]"


class _BrowserDied(Exception):
    """The Chromium context/tabs died mid-run; relaunch and resume from state."""


class _RestartRequested(Exception):
    """A remote 'restart' command: relaunch Chromium and resume from saved state."""


def _is_browser_dead(exc: BaseException) -> bool:
    blob = f"{type(exc).__name__}: {exc}"
    return any(m in blob for m in (
        "TargetClosed", "has been closed", "Browser closed", "Connection closed",
        "browser has been closed", "Playwright was closed", "Target crashed",
        "Target page, context or browser has been closed", "disconnected",
        # Playwright DRIVER-startup failures (transient, e.g. relaunch races):
        "_playwright", "Connection.init", "while reading from the driver",
        "Driver", "playwright._impl",
    ))

# ── The adaptation instruction (problem-specific parts get substituted) ───────
ADAPT_INSTRUCTION = """Please write a complete research prompt for the following problem, closely modeled on the example prompts attached below:

{statement}

Research the problem thoroughly before writing the prompt. In particular, consult the discussion and references available here:

{references}

The attached section contains several example research prompts, each engineered for a specific hard mathematical problem. They share a common structure that you must reproduce faithfully for the new problem:
- a precise, self-contained statement of the problem, with all definitions, models, and normalizations spelled out;
- an exact statement of what a complete resolution must prove, including the precise affirmative-versus-negative dichotomy where the problem is a yes/no or existence question;
- a detailed, problem-specific "partial progress / results that do not count" list;
- the "multiagent v2" orchestration heuristics (a genuinely diverse portfolio of approaches, an explicit registry of approach families, marking stalled routes as blocked, keeping incompatible routes alive, and using computational and adversarial agents throughout);
- a problem-specific adversarial checklist of the exact traps a candidate proof must survive;
- the closing discipline: return only when the problem is completely resolved and the argument has survived adversarial audit, and restrict public search to ordinary background and standard named theorems only.

Preserve the examples' format, organization, level of detail, rigor, and overall methodology as closely as possible. Replace all problem-specific mathematical content with content correct for the new setting, and rewrite the "do not count" items and the adversarial checklist so they are accurate and relevant for this problem. Do not copy any single example; produce one coherent, self-contained prompt specific to the target problem, reflecting its known literature, definitions, existing partial results, plausible approaches, technical obstacles, and useful computational or formalization directions.

Return ONLY the adapted prompt itself, with no preamble, explanation, or commentary before or after it.

Write the entire prompt directly here in the chat as ordinary Markdown text. Do NOT use the canvas, a document, or any side-panel/essay tool — put the full text inline in your reply.

===== EXAMPLE PROMPTS (write a new one in this style) =====
{meta}
===== END EXAMPLE PROMPTS ====="""

# ── Continuation nudges ───────────────────────────────────────────────────────
# The model's most common failure is to stall and declare the problem "open".
# choose_continuation() reads the latest reply and sends the nudge that actually
# fits the situation — it does NOT blindly rotate.
CONT_NEW_APPROACH = (
    "Declaring the problem open, hard, or unsolved is NOT an acceptable answer "
    "here. The current approach has stalled, so set it aside and start a genuinely "
    "different one: a new reduction, invariant, construction, generating function, "
    "probabilistic or algebraic method, or a link to another area you have not yet "
    "tried. Commit to the new approach and push it hard this round."
)
CONT_COUNTEREXAMPLE = (
    "Switch to refutation this round: treat the statement as possibly FALSE and "
    "actively hunt for a counterexample. Work out small, extremal, random, and "
    "structured cases explicitly (compute them), look for a configuration that "
    "violates the claim, and try to turn any near-miss into a real counterexample."
)
CONT_PLAN = (
    "Do not stop at 'this is hard/open'. Take the results you have ALREADY proved "
    "and turn them into a concrete step-by-step plan that would fully resolve the "
    "problem: list what is established, name the single key gap, choose a specific "
    "tactic to close it, and start executing that tactic now."
)
CONT_BLOCKER = (
    "Focus entirely on the one obstruction blocking a full solution. Isolate that "
    "exact lemma or inequality and spend this whole round resolving just it — prove "
    "it, or pin down precisely why it fails and design a concrete way around it. "
    "Make real progress on the blocker rather than restating it."
)
CONT_ADVANCE = (
    "Good progress — keep going on this line. Consolidate what is now rigorously "
    "established and take the next concrete step toward a complete, unconditional "
    "resolution (a full proof or a decisive counterexample), closing the single "
    "remaining gap. Keep everything unconditional and rigorous."
)

# ── Final-round nudge ─────────────────────────────────────────────────────────
# Sent on the LAST round: a decisive push to FINISH the proof end to end (NOT a
# consolidation). The independent verification step guards against a false claim.
FINAL_CONTINUATION = (
    "This is the FINAL round — make a decisive push to COMPLETE the proof now. Pull "
    "together everything you have established and close the remaining gap in full: "
    "produce a single, self-contained, rigorous, unconditional argument (or a complete "
    "explicit counterexample) that resolves the problem end to end. Do not stop at "
    "partial results, and do not merely restate the obstruction — this is the round to "
    "finish it. Keep every step rigorous and unconditional; only if the gap genuinely "
    "cannot be closed should you then state exactly what remains."
)

# Appended to a continuation when the previous reply came back faster than
# MIN_THINK_SECONDS — the problem is hard, so push for much deeper, longer work.
_THINK_LONGER = (
    "\n\nNote: your previous reply came back quickly. This is a very hard problem — "
    "take substantially more time and go much deeper this round. Develop several "
    "distinct lines of attack in parallel, carry every construction and computation "
    "all the way through, and rigorously stress-test each step before you conclude. "
    "Do not return a short or premature answer."
)

# ── Independent verification of a claimed solution ───────────────────────
# When a research reply looks solved, the proof is re-checked in a SEPARATE fresh
# chat before the problem is marked solved. The referee must answer with a single
# machine-parseable verdict line.
VERIFY_PROMPT_TEMPLATE = (
    "You are a careful, rigorous, and fair expert referee. Below is a mathematics problem "
    "followed by a claimed COMPLETE solution written by someone else. Check it "
    "rigorously and independently: every step must be justified, with no gaps, no "
    "unproven lemmas, no hidden assumptions, and it must resolve exactly what the "
    "problem asks (the full statement, not a special case).\n\n"
    "PROBLEM:\n{problem}\n\n"
    "CLAIMED COMPLETE SOLUTION:\n{proof}\n\n"
    "Reply with EXACTLY ONE line and nothing else:\n"
    "VERDICT: CORRECT — only if it is a fully correct, complete, rigorous resolution.\n"
    "VERDICT: INCORRECT — <one short sentence naming the first real gap or error> — "
    "if there is ANY gap, error, or incompleteness."
)
# Sent back into the RESEARCH chat when verification rejects the claimed solution.
VERIFY_FAILED_CONTINUATION = (
    "An independent referee checked your claimed solution and it is NOT complete:\n\n"
    "“{critique}”\n\n"
    "So the problem is not solved yet. Do not declare completion again until this "
    "exact objection is fully resolved. Either close this specific gap with a "
    "rigorous argument this round, or, if it cannot be closed, switch to a "
    "materially different approach or hunt for a counterexample. Keep going."
)

# Asked in the research chat after EVERY reply so detecting a genuine solve does
# not depend on fragile regex parsing of a free-form proof (clear solves were
# being missed). The model's explicit one-line verdict is what triggers the
# independent verification.
SELF_ASSESS_PROMPT = (
    "Pause and self-assess honestly. As of your last message, is the ORIGINAL "
    "problem now COMPLETELY and RIGOROUSLY solved — a full unconditional proof, or a "
    "complete explicit counterexample, that resolves exactly what was asked, with NO "
    "remaining gaps, unproven lemmas, conditional steps, or hand-waving?\n\n"
    "Answer with the verdict on the FIRST line, exactly one of:\n"
    "SOLVED — <one short clause on what was proved>\n"
    "NOT SOLVED — <the single most important thing still missing>"
)

# Signals used to pick the RIGHT continuation from the latest reply.
_OPEN_DECLARATION_PATTERNS = [
    r"remains open", r"still open", r"\bopen problem\b", r"long[- ]standing open",
    r"well[- ]known open", r"currently open", r"cannot be (?:solved|resolved|proved|settled)",
    r"beyond (?:current|known|existing|the reach)", r"no known (?:proof|method|approach|technique)",
    r"not (?:been )?(?:solved|resolved|settled)", r"unable to (?:solve|resolve|prove|settle)",
    r"do(?:es)? not (?:know|see) how to", r"out of reach", r"intractable",
]
_BLOCKER_PATTERNS = [
    r"reduces? to (?:showing|proving|establishing)", r"it (?:suffices|remains) to (?:show|prove)",
    r"would (?:require|suffice)", r"if we could (?:show|prove|establish)",
    r"the (?:key|main|central|remaining|crucial) (?:obstruction|difficulty|lemma|step|gap|ingredient)",
    r"remaining gap", r"theorem-strength gap", r"\bthe crux\b", r"boils down to", r"hinges on",
]
_COUNTEREXAMPLE_PATTERNS = [
    r"counterexample", r"may be false", r"might be false", r"could be false",
    r"\bfails for\b", r"violates", r"refute", r"disprove",
]
_PARTIAL_RESULT_PATTERNS = [
    r"we have (?:shown|proved|proven|established)", r"partial (?:result|progress)",
    r"we can (?:show|prove)", r"proved that", r"established that", r"we obtain(?:ed)?",
    r"\blemma \d", r"\bclaim \d", r"\bproposition \d", r"we know that",
]


def choose_continuation(text: str, round_no: int, max_rounds: int,
                        tried: set[str]) -> tuple[str, str]:
    """Pick the continuation that fits the latest reply, rather than rotating blindly.

    `tried` collects the redirections already used on this problem so a repeatedly
    stalling search doesn't get the exact same nudge twice. Returns (kind, message).
    """
    if round_no + 1 >= max_rounds:
        return "final", FINAL_CONTINUATION
    low = text.lower()
    declared_open = any(re.search(p, low) for p in _OPEN_DECLARATION_PATTERNS)
    names_blocker = any(re.search(p, low) for p in _BLOCKER_PATTERNS)
    considered_false = any(re.search(p, low) for p in _COUNTEREXAMPLE_PATTERNS)
    has_partial = any(re.search(p, low) for p in _PARTIAL_RESULT_PATTERNS)

    if declared_open:
        # It is trying to give up — redirect based on what the reply actually shows.
        if names_blocker and "blocker" not in tried:
            return "blocker", CONT_BLOCKER
        if not considered_false and "counterexample" not in tried:
            return "counterexample", CONT_COUNTEREXAMPLE
        if has_partial and "plan" not in tried:
            return "plan", CONT_PLAN
        if "new_approach" not in tried:
            return "new_approach", CONT_NEW_APPROACH
        # every redirection tried and it is STILL declaring open: keep it hunting
        # for a counterexample rather than repeating a nudge it already ignored.
        return "counterexample", CONT_COUNTEREXAMPLE
    if names_blocker:
        # circling one obstruction (without giving up) — focus it.
        return "blocker", CONT_BLOCKER
    # genuinely progressing / ambiguous but not stalling — advance the line.
    return "advance", CONT_ADVANCE

# ── Chat-title status labels (each chat is renamed to "<problem> — <status>") ─────
STATUS_LABELS = {
    "running": "in progress",
    "solved": "solved",
    "exhausted": "partial results",
    "failed": "failed",
}

# ── Solved / unsolved detection ───────────────────────────────────────────────
# Every target is a hard OPEN problem, so a "solved" verdict must be unambiguous
# and refer to the WHOLE problem. These strong completion phrases are matched only
# in the reply's CONCLUSION, must not be negated, and are vetoed by any hedge in
# that conclusion. A ∎ on an interior lemma, a counterexample to an intermediate
# claim, or a "this WOULD complete the proof" hypothetical must NOT trigger it.
_SOLVED_PATTERNS = [
    r"this completes the (?:proof|resolution) of the (?:problem|theorem|conjecture|main|question)",
    r"completes the (?:full |complete )?resolution of the (?:problem|conjecture|question)",
    r"the (?:problem|conjecture|question) (?:is|has been) (?:now )?(?:completely |fully )?(?:resolved|settled)\b",
    r"we have (?:now )?(?:completely |fully )resolved the (?:problem|conjecture|question)",
    r"(?:this|which|thereby) (?:disproves|refutes|falsifies) the (?:conjecture|main claim|problem|statement|assertion)",
    r"the (?:conjecture|main claim|statement in the problem|problem'?s? assertion) is (?:therefore |thus |hence |now )?(?:false|true)\b",
    r"the answer to the (?:problem|question) is (?:therefore |thus |now )?(?:yes|no|negative|affirmative)\b",
    r"(?:this )?establishes the (?:main )?(?:theorem|conjecture|result) (?:in full|completely|unconditionally)",
]
_UNSOLVED_PATTERNS = [
    r"remains open",
    r"still open",
    r"\bopen problem\b",
    r"unable to",
    r"could not (?:complete|close|resolve|prove|finish)",
    r"do(?:es)? not (?:yet )?(?:have|constitute|amount to) a complete",
    r"no complete proof",
    r"partial (?:progress|result|results)",
    r"gap remains",
    r"remaining gap",
    r"theorem-strength gap",
    r"this is conditional",
    r"conditional on",
    r"further work is required",
    r"not (?:a )?complete",
]

# Hedges that, if present in the CONCLUSION, veto a 'solved' verdict — the reply
# is still describing open work, a sub-result, or a conditional/partial outcome.
_HEDGE_PATTERNS = [
    r"remains? open", r"still open", r"\bopen problem\b", r"unable to",
    r"\bcannot\b", r"\bcan.?t\b", r"could not (?:complete|close|resolve|prove|finish)",
    r"no complete proof", r"partial (?:progress|result|results)",
    r"gap remains", r"remaining gap", r"\ba gap\b", r"theorem-strength gap",
    r"gap (?:that|in|at|between|of|can|remains)", r"conditional on", r"is conditional",
    r"further work (?:is|will be|would be) (?:required|needed)",
    r"not (?:yet )?(?:a |fully )?complete", r"it remains to",
    r"remains to be (?:shown|proved|proven|established|verified)",
    r"would (?:require|need|suffice|complete|have to|still)", r"can be closed",
    r"\bmodulo\b", r"assuming (?:that|the|a\b)", r"subject to",
    r"the (?:key|main|central|remaining|crucial|principal) (?:obstruction|difficulty|gap|lemma|step|ingredient|barrier)",
    r"\bcircular\b", r"the blocker", r"the obstruction", r"next (?:step|round)",
    r"do not (?:yet )?(?:have|constitute|amount to) a complete",
    r"still (?:need|lack|missing|require)", r"have not (?:yet )?", r"has not (?:yet )?been",
]

# Negation appearing right before a completion phrase turns it into a DENIAL
# ("we cannot give a complete resolution").
_NEGATION_LOOKBACK = 48
_NEGATION_RE = re.compile(
    r"\b(?:cannot|can\s?not|can.?t|could\s?not|couldn.?t|do(?:es)?\s?not|do\s?n.?t|"
    r"did\s?not|did\s?n.?t|not|no|without|unable|fail(?:s|ed|ing)?|far from|yet to|"
    r"neither|nor|impossible)\b[^.]*$",
    re.IGNORECASE,
)


def assess_response(text: str) -> str:
    """Classify a research reply as 'solved', 'unsolved', or 'unclear'.

    Heavily biased AGAINST 'solved': every target is a hard open problem, so
    'solved' fires only when the reply's CONCLUSION declares the whole problem
    resolved with a strong, non-negated verdict and shows no hedging. A ∎ on an
    interior lemma, a counterexample to an intermediate claim, or a "this would
    finish it" hypothetical must not count.
    """
    low = text.lower()
    tail = low[-3000:]                       # the reply's verdict / conclusion
    if not any(re.search(p, tail) for p in _HEDGE_PATTERNS):
        for pat in _SOLVED_PATTERNS:
            m = re.search(pat, tail)
            if m and not _NEGATION_RE.search(tail[max(0, m.start() - _NEGATION_LOOKBACK):m.start()]):
                return "solved"
    if any(re.search(p, low) for p in _UNSOLVED_PATTERNS):
        return "unsolved"
    return "unclear"


# ── Small helpers ─────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_VERDICT_CORRECT_RE = re.compile(r"verdict:\s*correct", re.IGNORECASE)
_VERDICT_INCORRECT_RE = re.compile(r"verdict:\s*incorrect\b[\s—:-]*(.*)", re.IGNORECASE)


def _is_verified(text: str) -> bool:
    """True only if the referee's verdict is CORRECT and not INCORRECT."""
    if _VERDICT_INCORRECT_RE.search(text or ""):
        return False
    return bool(_VERDICT_CORRECT_RE.search(text or ""))


def _extract_critique(text: str) -> str:
    """The referee's stated reason for rejecting a claimed solution."""
    m = _VERDICT_INCORRECT_RE.search(text or "")
    reason = (m.group(1).strip() if m and m.group(1).strip() else (text or "").strip())
    return reason[:1500]


def _self_assessed_solved(text: str) -> bool:
    """Read the model's own SOLVED / NOT SOLVED verdict from a self-assessment reply.
    Any negative marker means not solved; otherwise require an affirmative 'solved'.
    The verdict is on the first line by instruction, but we fall back to the whole
    reply."""
    neg = (r"not\s+solved|unsolved|isn.?t\s+solved|not\s+(?:fully|completely|yet|"
           r"entirely)|still\s+(?:not|open|missing|unproven|incomplete)|incomplete")
    pos = r"\bsolved\b|\bfully\s+resolved\b|\bcompletely\s+resolved\b"
    t = (text or "").strip()
    if not t:
        return False
    first = t.splitlines()[0].lower()
    if re.search(neg, first):
        return False
    if re.search(pos, first):
        return True
    low = t.lower()
    if re.search(neg, low):
        return False
    return bool(re.search(r"\bsolved\b", low))


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "problem"


def _clean_adapted(text: str) -> str:
    """Strip an accidental fenced-code wrapper around the adapted prompt."""
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


# Text that means the adapt step produced no usable prompt (extraction failure or
# a "I can't see the attachment" style reply) — never reuse or submit it.
_UNUSABLE_ADAPT_RE = re.compile(
    r"could not extract response"
    r"|did\s?n.?t receive any usable"
    r"|please (?:resend|re-?send|upload the file)"
    r"|i (?:can.?t|cannot|couldn.?t|don.?t) (?:see|find|access|receive)"
    r" (?:the|any|your)? ?(?:attached|attachment|file|prompt|content|text)",
    re.IGNORECASE,
)
_MIN_ADAPTED_CHARS = 200        # a real adapted research prompt is long


def _is_usable_adapted(text: str) -> bool:
    """True only if `text` looks like a real adapted research prompt."""
    t = (text or "").strip()
    if len(t) < _MIN_ADAPTED_CHARS:
        return False
    return not _UNUSABLE_ADAPT_RE.search(t)


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _write_active_manifest(state_dir: Path, slots: list[Any],
                           control: Optional[dict[str, Any]] = None) -> None:
    """Publish which problems are on a worker RIGHT NOW plus a heartbeat, so the
    website shows only genuinely-active workers and never a stale 'running' file
    from a prior/crashed run or a queued-but-not-started problem. Written into the
    state dir as ``_active.json`` (skipped by the *.json problem scanners, which
    require an ``id`` field)."""
    active = []
    for s in slots:
        if not s.idle and s.record.get("id"):
            active.append({
                "worker": s.index,
                "id": s.record["id"],
                "title": s.record.get("title"),
                "phase": s.phase,
                "round": s.await_round,
            })
    try:
        _atomic_write_json(state_dir / "_active.json", {
            "heartbeat": _now_iso(),
            "pid": os.getpid(),
            "state": (control or {}).get("state", "run"),
            "paused": (control or {}).get("state") == "pause",
            "active": active,
        })
    except Exception:
        pass


def build_adapt_message(meta_prompt: str, statement: str, references: str) -> str:
    refs = references.strip() if references else (
        "(No additional references were provided; rely on your own knowledge and "
        "public search of the primary literature.)")
    return ADAPT_INSTRUCTION.format(
        statement=statement.strip(), references=refs, meta=meta_prompt.strip())


def load_problems(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "problems" in data:
        data = data["problems"]
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected a JSON list of problems (or {{'problems': [...]}}).")
    problems: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            raise SystemExit(f"{path}: problem #{i + 1} is not an object.")
        statement = str(raw.get("statement") or "").strip()
        if not statement:
            raise SystemExit(f"{path}: problem #{i + 1} is missing a non-empty 'statement'.")
        title = str(raw.get("title") or "").strip()
        pid = str(raw.get("id") or "").strip() or _slugify(title or f"problem-{i + 1}")
        base, n = pid, 2
        while pid in seen:
            pid = f"{base}-{n}"
            n += 1
        seen.add(pid)
        problems.append({
            "id": pid,
            "title": title or pid,
            "statement": statement,
            "references": str(raw.get("references") or "").strip(),
        })
    return problems

# ── Crash-safe resume ─────────────────────────────────────────────────────
def _adapt_response_text(record: dict[str, Any]) -> str:
    """Return the captured adapted prompt from a prior run, if it is usable."""
    for stage in reversed(record.get("stages") or []):
        if stage.get("stage") == "adapt" and stage.get("role") == "response":
            cleaned = _clean_adapted(stage.get("text") or "")
            return cleaned if _is_usable_adapted(cleaned) else ""
    return ""


def _last_research_round(record: dict[str, Any]) -> int:
    rounds = [int(s.get("round") or 0) for s in (record.get("stages") or [])
              if s.get("stage") in ("research", "continue")]
    return max(rounds) if rounds else 0


def classify_resume(record: dict[str, Any]) -> str:
    """Decide how to resume a prior state file: done | research | adapted | fresh."""
    if not record:
        return "fresh"
    if record.get("status") in ("solved", "exhausted"):
        return "done"
    # A usable adapted prompt is the prerequisite for resuming either phase; without
    # it the research chat (if any) was built on garbage, so start over fresh.
    if not _adapt_response_text(record):
        return "fresh"
    has_research = any(s.get("stage") in ("research", "continue")
                       for s in (record.get("stages") or []))
    if record.get("research_conversation_url") and has_research:
        return "research"
    return "adapted"


def load_prior_state(state_dir: Path) -> dict[str, dict[str, Any]]:
    """Load per-problem state files written by a previous (possibly crashed) run."""
    prior: dict[str, dict[str, Any]] = {}
    if not state_dir.exists():
        return prior
    for path in state_dir.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("id"):
            prior[str(record["id"])] = record
    return prior


def _remaining_count(problems: list[dict[str, str]], state_dir: Path) -> int:
    """How many problems are not yet finished (solved/exhausted)."""
    prior = load_prior_state(state_dir)
    return sum(1 for p in problems
              if classify_resume(prior.get(p["id"], {})) != "done")


def _trip_rate_limit(sched: dict[str, float], log) -> None:
    """Account-wide, escalating backoff shared by every worker (EGMRA-style).

    ChatGPT throttles the whole account, not one tab, so when any worker sees a
    rate-limit modal we pause ALL workers. Repeated hits back off exponentially
    (RATE_LIMIT_COOLDOWN, 2x, 4x, ... capped at RATE_LIMIT_MAX_COOLDOWN); the
    streak resets after RATE_LIMIT_RESET seconds without a throttle.
    """
    now = time.time()
    if now - sched.get("rate_limit_last", 0.0) > RATE_LIMIT_RESET:
        sched["rate_limit_streak"] = 0.0
    streak = sched.get("rate_limit_streak", 0.0) + 1.0
    sched["rate_limit_streak"] = streak
    sched["rate_limit_last"] = now
    backoff = min(RATE_LIMIT_MAX_COOLDOWN, RATE_LIMIT_COOLDOWN * (2.0 ** (streak - 1.0)))
    until = now + backoff
    if until > sched.get("rate_limit_until", 0.0):
        sched["rate_limit_until"] = until
    log(f"[rate-limit] throttled — pausing all workers ~{backoff:.0f}s (streak {int(streak)})")


# ── Remote control polling ────────────────────────────────────────────────────
def _poll_control(url: str, timeout: float = 4.0) -> Optional[dict[str, Any]]:
    """Fetch the remote control document. Returns the parsed dict, or None on any
    error (the caller then keeps the last known command). A cache-busting query is
    appended so GitHub's raw CDN never serves a stale command."""
    try:
        sep = "&" if "?" in url else "?"
        req = urllib.request.Request(
            f"{url}{sep}cb={int(time.time())}",
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ── Model guard: best-effort enforcement of the chat model (never blocks) ─────
def _current_model_name(page: Any) -> str:
    """Best-effort read of the model shown in ChatGPT's model switcher."""
    for sel in ('[data-testid="model-switcher-dropdown-button"]',
                'button[data-testid="model-switcher-dropdown-button"]',
                'button[aria-label*="odel"]',
                'div[data-testid="model-switcher"] button'):
        try:
            el = page.query_selector(sel)
            if el:
                txt = " ".join((el.inner_text() or "").split())
                if txt:
                    return txt
        except Exception:
            pass
    return ""


def _model_ok(name: str, tokens: tuple[str, ...]) -> bool:
    low = (name or "").lower()
    return bool(name) and all(tok in low for tok in tokens)


# After this many consecutive failed switch attempts, stop trying to change the
# model and just record what's selected — so a target that isn't a selectable
# switcher entry (e.g. a reasoning-effort setting) never thrashes the menu on
# every new chat, and a since-removed model (e.g. Pro when you run out) is never
# forced back on. Reset to 0 on any success.
_MODEL_SWITCH_GIVEUP = 3
_model_switch_fails = 0


def _ensure_chat_model(page: Any, tokens: tuple[str, ...], log=None) -> tuple[bool, str]:
    """Make the current (new) chat use the target model. Returns (ok, model_name).

    NEVER blocks the run: if the switcher can't be driven (e.g. the UI changed),
    it logs a warning and returns (False, <name>) so the pipeline keeps going on
    whatever model is selected rather than freezing for days.
    """
    global _model_switch_fails
    tokens = tuple(t.lower() for t in tokens if t)
    if not tokens or _model_switch_fails >= _MODEL_SWITCH_GIVEUP:
        return True, _current_model_name(page)   # enforcement off / gave up → record only
    name = _current_model_name(page)
    if _model_ok(name, tokens):
        _model_switch_fails = 0
        return True, name
    try:
        btn = None
        for sel in ('[data-testid="model-switcher-dropdown-button"]',
                    'button[data-testid="model-switcher-dropdown-button"]',
                    'button[aria-label*="odel"]'):
            btn = page.query_selector(sel)
            if btn:
                break
        if btn is None:
            if log:
                log(f"[model] WARN could not find the model switcher (chat shows {name!r})")
            return False, name
        page.evaluate("el => el.click()", btn)
        time.sleep(0.5)

        def _click_match() -> bool:
            for it in page.query_selector_all('[role="menuitem"], [role="option"]'):
                try:
                    label = " ".join((it.inner_text() or "").split()).lower()
                except Exception:
                    continue
                if label and all(tok in label for tok in tokens):
                    try:
                        page.evaluate("el => el.click()", it)
                        return True
                    except Exception:
                        pass
            return False

        clicked = _click_match()
        if not clicked:
            # The target may live in a submenu ("More models" / "Legacy models").
            for it in page.query_selector_all('[role="menuitem"]'):
                try:
                    label = (it.inner_text() or "").lower()
                except Exception:
                    continue
                if any(k in label for k in ("more model", "legacy", "other model", "all model")):
                    try:
                        page.evaluate("el => el.click()", it)
                        time.sleep(0.4)
                        if _click_match():
                            clicked = True
                            break
                    except Exception:
                        pass
        time.sleep(0.6)
        name = _current_model_name(page)
        ok = _model_ok(name, tokens)
        if ok:
            _model_switch_fails = 0
        else:
            _model_switch_fails += 1
            try:
                page.keyboard.press("Escape")   # close a stray menu over the composer
            except Exception:
                pass
            if log:
                log(f"[model] WARN wanted {'+'.join(tokens)} but chat shows {name!r} — "
                    f"proceeding" + (f"; giving up auto-switch after {_model_switch_fails}x"
                                     if _model_switch_fails >= _MODEL_SWITCH_GIVEUP else ""))
        return ok, name
    except Exception as exc:
        if log:
            log(f"[model] WARN model-switch error: {exc!r} — proceeding")
        return False, name


# ── Drivers: a thin interface over one ChatGPT tab ────────────────────────────
class BrowserDriver:
    """Backed by a real Playwright page (one ChatGPT tab)."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self._continue_clicks = 0

    def open_new_chat(self) -> None:
        ec.start_new_chat(self.page)

    def ensure_model(self, tokens: tuple[str, ...], log=None) -> tuple[bool, str]:
        return _ensure_chat_model(self.page, tokens, log)

    def alive(self) -> bool:
        try:
            return not self.page.is_closed()
        except Exception:
            return False

    def goto_conversation(self, url: str) -> bool:
        try:
            self.page.goto(url, wait_until="domcontentloaded")
            time.sleep(2.0)
            return True
        except Exception:
            return False

    def submit(self, prompt: str) -> None:
        self._continue_clicks = 0
        ec.send_prompt(self.page, prompt)

    def capture_conversation_url(self, known_cids: set[str]) -> Optional[str]:
        url = self.page.url
        cid = ec._conversation_id(url)
        if cid and cid not in known_cids:
            return url
        return None

    def current_conversation_url(self) -> Optional[str]:
        """The URL of whatever conversation this tab is currently showing, if any.
        Used to backfill the chat link at settle/rename time: right after submit
        the project page can keep its /project URL through a long generation, so
        the early capture misses it — but by the time the reply has settled the
        tab reliably shows /g/g-p-.../c/<cid> (the same URL rename relies on)."""
        url = self.page.url
        return url if ec._conversation_id(url) else None

    def rename(self, title: str) -> bool:
        """Rename the currently-open conversation via ChatGPT's per-chat options
        menu. VERIFIED recipe (their inline title input is finicky): open the
        cid-specific options trigger, click "Rename", then commit the "Chat title"
        input with a JS value-set + dispatched Enter key events. Real typing
        detaches the input and blur cancels it — only the dispatched Enter commits."""
        page = self.page
        try:
            url = page.url or ""
            cid = url.split("/c/")[-1].split("?")[0] if "/c/" in url else ""
            if not cid:
                return False
            trigger = None
            for _ in range(20):
                trigger = page.query_selector(
                    f'button[data-conversation-options-trigger="{cid}"]')
                if trigger:
                    break
                time.sleep(0.25)
            if trigger is None:
                return False
            page.evaluate("el => el.click()", trigger)
            rename_item = None
            for _ in range(20):
                time.sleep(0.2)
                for it in page.query_selector_all('[role="menuitem"]'):
                    try:
                        if "rename" in (it.inner_text() or "").lower():
                            rename_item = it
                            break
                    except Exception:
                        pass
                if rename_item:
                    break
            if rename_item is None:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                return False
            page.evaluate("el => el.click()", rename_item)
            has_input = False
            for _ in range(20):
                time.sleep(0.2)
                if page.query_selector('input[aria-label="Chat title"]'):
                    has_input = True
                    break
            if not has_input:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                return False
            ok = page.evaluate(
                """(title) => {
                    const el = document.querySelector('input[aria-label="Chat title"]');
                    if (!el) return false;
                    el.focus();
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, title);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    ['keydown','keypress','keyup'].forEach(t =>
                        el.dispatchEvent(new KeyboardEvent(t, {
                            key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true})));
                    return true;
                }""",
                title,
            )
            time.sleep(0.6)
            return bool(ok)
        except Exception:
            return False

    def is_busy(self) -> bool:
        return ec.is_generating(self.page)

    def maybe_continue_generation(self) -> bool:
        if self._continue_clicks >= CONTINUE_GEN_MAX:
            return False
        try:
            btn = self.page.query_selector(
                'button:has-text("Continue generating"), '
                'button[data-testid="continue-generating-button"]')
            if btn:
                self.page.evaluate("el => el.click()", btn)
                self._continue_clicks += 1
                return True
        except Exception:
            pass
        return False

    def detect_rate_limit(self) -> bool:
        try:
            return ec.detect_rate_limit(self.page)
        except Exception:
            return False

    def dismiss_rate_limit(self) -> None:
        # EGMRA-style: click the alert's acknowledge button ("Got it" on the
        # too-many-requests modal), else fall back to the generic modal close.
        try:
            for btn in self.page.query_selector_all('button, [role="button"]'):
                try:
                    label = (btn.inner_text() or "").strip().lower()
                except Exception:
                    continue
                if label in ("got it", "ok", "okay", "dismiss", "close", "try again"):
                    try:
                        self.page.evaluate("el => el.click()", btn)
                        time.sleep(0.5)
                        return
                    except Exception:
                        pass
            ec.dismiss_modal(self.page)
        except Exception:
            pass

    def response_text(self) -> str:
        return ec.extract_response(self.page)


class SimDriver:
    """Scripted stand-in for --dry-run (no browser). Exercises the state machine."""

    def __init__(self, solve_at_round: Optional[int] = 2, busy_secs: float = 1.0) -> None:
        self.solve_at_round = solve_at_round
        self.busy_secs = busy_secs
        self.chat_index = 0
        self.await_kind: Optional[str] = None
        self.research_round = 0
        self._busy_until = 0.0
        self._cid = 0
        self._committed = ""   # what is currently on screen
        self._pending = ""     # what will show once generation finishes

    def open_new_chat(self) -> None:
        self.chat_index += 1
        self._cid += 1
        self.await_kind = None
        self._committed = ""
        self._pending = ""

    def ensure_model(self, tokens: tuple[str, ...], log=None) -> tuple[bool, str]:
        return True, "sim-model"

    def goto_conversation(self, url: str) -> bool:
        self.await_kind = "research"
        return True

    def submit(self, prompt: str) -> None:
        p = (prompt or "").lower()
        if self.chat_index <= 1 and self.await_kind is None:
            self.await_kind = "adapt"
        elif "self-assess honestly" in p:
            self.await_kind = "selfassess"
        elif "verdict" in p:
            self.await_kind = "verify"
        else:
            self.await_kind = "research"
            self.research_round += 1
        self._pending = self._make_text()
        self._busy_until = time.time() + self.busy_secs

    def _make_text(self) -> str:
        if self.await_kind == "adapt":
            return ("ADAPTED PROMPT (simulated)\n\nResolve completely the dependence of the "
                    "quantity on the parameter. " + "Simulated adaptation body. " * 6)
        solved = bool(self.solve_at_round and self.research_round >= self.solve_at_round)
        if self.await_kind == "verify":
            return ("VERDICT: CORRECT — the proof is complete and rigorous." if solved
                    else "VERDICT: INCORRECT — a key lemma is left unproven.")
        if self.await_kind == "selfassess":
            return ("SOLVED — a full unconditional proof was given." if solved
                    else "NOT SOLVED — a key lemma is still missing.")
        # research / continuation reply (distinct per round so it never looks stale)
        if solved:
            return (f"(round {self.research_round}) We assemble the components: matching upper "
                    "and lower bounds and an explicit unconditional counterexample. This "
                    "completes the proof of the problem, which is now fully solved. \u220e")
        return (f"(round {self.research_round}) Substantial partial progress was made, but a "
                "theorem-strength gap remains and the problem remains open at this stage. "
                "Further work is required to close the gap.")

    def capture_conversation_url(self, known_cids: set[str]) -> Optional[str]:
        return f"https://chatgpt.com/c/sim-{id(self) % 100000}-{self._cid}"

    def current_conversation_url(self) -> Optional[str]:
        return f"https://chatgpt.com/c/sim-{id(self) % 100000}-{self._cid}"

    def rename(self, title: str) -> bool:
        return True

    def alive(self) -> bool:
        return True

    def is_busy(self) -> bool:
        return time.time() < self._busy_until

    def maybe_continue_generation(self) -> bool:
        return False

    def detect_rate_limit(self) -> bool:
        return False

    def dismiss_rate_limit(self) -> None:
        return None

    def response_text(self) -> str:
        if self.is_busy():
            return self._committed      # stale prior message while generating
        self._committed = self._pending
        return self._committed


# ── Per-problem worker slot (non-blocking state machine) ──────────────────────
class Slot:
    def __init__(self, index: int, driver: Any, *, meta_prompt: str, state_dir: Path,
                 max_rounds: int, response_timeout: float, sched: dict[str, float],
                 new_chat_spacing: float, log) -> None:
        self.index = index
        self.driver = driver
        self.meta_prompt = meta_prompt
        self.state_dir = state_dir
        self.max_rounds = max_rounds
        self.response_timeout = response_timeout
        self.sched = sched
        self.new_chat_spacing = new_chat_spacing
        self.log = log

        self.problem: Optional[dict[str, str]] = None
        self.phase = "idle"
        self.record: dict[str, Any] = {}
        self.adapt_msg = ""
        self.adapted_text = ""
        self.await_kind: Optional[str] = None       # "adapt" | "research"
        self.await_round = 0
        # response-wait bookkeeping
        self.submitted_at = 0.0
        self.phase_deadline = 0.0
        self.gen_start_deadline = 0.0
        self.url_deadline = 0.0
        self.last_text = ""
        self.stable = 0
        self.busy_seen = False
        self.prev_response = ""      # message on screen before this submit
        self.cooldown_until = 0.0
        self.base_name = ""
        self.resume_url: Optional[str] = None
        self._renamed_running = False
        self._tried_conts: set[str] = set()
        self._adapt_tries = 0
        self._solved_candidate = ""
        self._verify_critique = ""
        self._research_text = ""
        self._research_elapsed = 0.0

    # -- lifecycle --------------------------------------------------------------
    @property
    def idle(self) -> bool:
        return self.problem is None

    def assign(self, problem: dict[str, str]) -> None:
        self.problem = problem
        name = (problem.get("title") or problem["id"]).strip()
        self.base_name = name
        self.adapt_title = f"{name} — adapt"[:100]
        self.adapt_msg = build_adapt_message(
            self.meta_prompt, problem["statement"], problem.get("references", ""))
        self.record = {
            "id": problem["id"],
            "title": problem["title"],
            "status": "running",
            "current_stage": "starting",
            "phase": "start_adapt",
            "round": 0,
            "max_rounds": self.max_rounds,
            "worker": self.index,
            "started_at": _now_iso(),
            "updated_at": _now_iso(),
            "project_url": ec.PROJECT_URL,
            "adapt_conversation_url": None,
            "research_conversation_url": None,
            "verify_conversation_url": None,
            "problem_statement": problem["statement"],
            "references": problem.get("references", ""),
            "stages": [],
        }
        self.phase = "start_adapt"
        self.await_kind = None
        self.await_round = 0
        self._adapt_tries = 0
        self._solved_candidate = ""
        self._verify_critique = ""
        self.log(f"[slot {self.index}] START {problem['id']} — {name}")
        self._save()

    def resume(self, problem: dict[str, str], prior: dict[str, Any]) -> None:
        """Re-attach to a prior (possibly crashed) run instead of starting over."""
        kind = classify_resume(prior)
        if kind in ("fresh", "done"):
            self.assign(problem)
            return
        self.problem = problem
        name = (problem.get("title") or problem["id"]).strip()
        self.base_name = name
        self.adapt_title = f"{name} — adapt"[:100]
        self.adapt_msg = build_adapt_message(
            self.meta_prompt, problem["statement"], problem.get("references", ""))
        self.record = dict(prior)
        self.record.setdefault("stages", [])
        self.record["id"] = problem["id"]
        self.record["title"] = problem.get("title") or problem["id"]
        self.record["worker"] = self.index
        self.record["max_rounds"] = self.max_rounds
        self.record["status"] = "running"
        self.record["project_url"] = ec.PROJECT_URL
        self.record.setdefault("problem_statement", problem["statement"])
        self.record.setdefault("references", problem.get("references", ""))
        self.record.setdefault("verify_conversation_url", None)
        self.record.pop("error", None)
        self.adapted_text = _adapt_response_text(prior)
        self.await_kind = None
        if kind == "research":
            self.await_round = max(1, _last_research_round(prior))
            self.resume_url = prior.get("research_conversation_url")
            self.phase = "resume_research"
            self.log(f"[slot {self.index}] RESUME research r{self.await_round} "
                     f"{problem['id']} — {name}")
        else:  # adapted: reuse the saved adapted prompt, open a fresh research chat
            self.await_round = 0
            self.phase = "start_research"
            self.log(f"[slot {self.index}] RESUME adapted {problem['id']} — {name}")
        self._save()

    def _status_title(self, status: str) -> str:
        suffix = f" — {STATUS_LABELS.get(status, status)}"
        return self.base_name[:100 - len(suffix)] + suffix

    def fail(self, message: str) -> None:
        if self.record:
            self.record["status"] = "failed"
            self.record["current_stage"] = "failed"
            self.record["error"] = message[:500]
            url = self.record.get("research_conversation_url")
            if url:
                self._rename_chat(url, self._status_title("failed"))
            self._save()
        self.log(f"[slot {self.index}] FAILED {self.record.get('id')}: {message}")
        self._release()

    def _release(self) -> None:
        self.problem = None
        self.phase = "idle"
        self._renamed_running = False
        self._tried_conts = set()
        self._adapt_tries = 0
        self._solved_candidate = ""
        self._verify_critique = ""
        self._research_text = ""
        self._research_elapsed = 0.0

    # -- persistence ------------------------------------------------------------
    def _labels(self) -> tuple[str, str]:
        status = self.record.get("status", "running")
        if status in ("solved", "exhausted", "failed"):
            return status, status
        if self.phase in ("start_adapt", "await_adapt_url", "await_adapt_response"):
            return "adapting", "start_adapt"
        if self.phase in ("start_research", "await_research_url"):
            return "researching", "start_research"
        if self.phase in ("start_verify", "await_verify_url",
                          "await_verify_response", "recontinue_after_verify"):
            return "verifying", "await_verify_response"
        if self.await_round <= 1:
            return "researching", "await_response"
        return f"continuing (round {self.await_round})", "await_response"

    def _save(self) -> None:
        stage_label, phase = self._labels()
        self.record["updated_at"] = _now_iso()
        self.record["current_stage"] = stage_label
        self.record["phase"] = phase
        self.record["round"] = self.await_round
        _atomic_write_json(self.state_dir / f"{self.record['id']}.json", self.record)

    def _add_stage(self, stage: str, role: str, text: str, url: Optional[str],
                   *, round_no: Optional[int] = None, assessment: Optional[str] = None,
                   timed_out: bool = False) -> None:
        self.record["stages"].append({
            "stage": stage,
            "role": role,
            "round": round_no,
            "at": _now_iso(),
            "chars": len(text or ""),
            "conversation_url": url,
            "assessment": assessment,
            "timed_out": timed_out,
            "text": text or "",
        })
        self._save()

    # -- response-wait arming ---------------------------------------------------
    def _arm_response_wait(self, kind: str, round_no: int) -> None:
        now = time.time()
        self.await_kind = kind
        self.await_round = round_no
        self.submitted_at = now
        self.phase_deadline = now + self.response_timeout
        self.gen_start_deadline = now + GEN_START_GRACE
        self.last_text = ""
        self.stable = 0
        self.busy_seen = False
        try:
            self.prev_response = (self.driver.response_text() or "").strip()
        except Exception:
            self.prev_response = ""

    def _may_open_chat(self) -> bool:
        now = time.time()
        if now < self.sched.get("rate_limit_until", 0.0):
            return False
        if now < self.sched.get("next_new_chat_at", 0.0):
            return False
        self.sched["next_new_chat_at"] = now + self.new_chat_spacing
        return True

    # -- chat renaming ----------------------------------------------------------
    def _rename_chat(self, url: Optional[str], title: str) -> None:
        """Rename a conversation reliably. ChatGPT can only rename the ACTIVE,
        settled sidebar item, so renaming a still-streaming or not-yet-listed chat
        fails ("could not locate active chat"). We rename from the loaded chat page
        and, if the first attempt misses, navigate to the chat URL (making it the
        active, settled item) and retry."""
        for _ in range(RENAME_TRIES):
            try:
                if self.driver.rename(title):
                    self.log(f"[{self.record.get('id')}] chat renamed -> {title!r}")
                    return
            except Exception:
                pass
            if url:
                try:
                    self.driver.goto_conversation(url)
                except Exception:
                    pass
            time.sleep(0.5)
        self.log(f"[{self.record.get('id')}] WARN chat rename failed -> {title!r}")

    def _backfill_conversation_url(self, field: str, known_cids: set[str]) -> None:
        """Ensure the chat link is recorded once a reply has settled. The early
        post-submit capture can miss the URL (the project page keeps /project
        through a long generation); by settle time the tab reliably shows the
        conversation, so grab it here if the field is still empty."""
        if self.record.get(field):
            return
        url = self.driver.current_conversation_url()
        if not url:
            return
        self.record[field] = url
        cid = ec._conversation_id(url)
        if cid:
            known_cids.add(cid)
        self._save()
        self.log(f"[{self.record['id']}] {field} backfilled")

    # -- main step (called every tick; returns quickly) -------------------------
    def step(self, known_cids: set[str]) -> None:
        now = time.time()
        if now < self.cooldown_until or now < self.sched.get("rate_limit_until", 0.0):
            return
        handler = getattr(self, f"_step_{self.phase}", None)
        if handler is None:
            return
        handler(known_cids)

    def _step_start_adapt(self, known_cids: set[str]) -> None:
        if not self._may_open_chat():
            return
        self.driver.open_new_chat()
        if self.driver.detect_rate_limit():
            self.driver.dismiss_rate_limit()
            _trip_rate_limit(self.sched, self.log)
            return
        _, self.record["model"] = self.driver.ensure_model(TARGET_MODEL_TOKENS, self.log)
        self.driver.submit(self.adapt_msg)
        self._add_stage("adapt", "prompt", self.adapt_msg, None)
        self.url_deadline = time.time() + URL_CAPTURE_TIMEOUT
        self._arm_response_wait("adapt", 0)
        self.phase = "await_adapt_url"
        self.log(f"[{self.record['id']}] adapt prompt submitted")

    def _step_await_adapt_url(self, known_cids: set[str]) -> None:
        url = self.driver.capture_conversation_url(known_cids)
        if url:
            cid = ec._conversation_id(url)
            if cid:
                known_cids.add(cid)
            self.record["adapt_conversation_url"] = url
            self.phase = "await_adapt_response"
            self._save()
        elif time.time() >= self.url_deadline:
            self.phase = "await_adapt_response"
            self._save()

    def _step_await_adapt_response(self, known_cids: set[str]) -> None:
        text, done = self._poll_response()
        if not done:
            return
        adapted = _clean_adapted(text)
        if not _is_usable_adapted(adapted):
            # Transient failure (tab/browser crash, extraction glitch, or a timeout
            # with no real reply). Retry the adapt in a fresh chat rather than
            # failing the problem outright — this is what caused the mass failures.
            self._adapt_tries += 1
            if self._adapt_tries < ADAPT_MAX_TRIES:
                self.log(f"[{self.record['id']}] adapt response unusable "
                         f"({len(adapted)} chars); retry {self._adapt_tries}/{ADAPT_MAX_TRIES}")
                self.phase = "start_adapt"
                return
            self.fail("adapt step produced no usable prompt after retries")
            return
        self.adapted_text = adapted
        self._backfill_conversation_url("adapt_conversation_url", known_cids)
        self._add_stage("adapt", "response", adapted,
                        self.record.get("adapt_conversation_url"))
        self.log(f"[{self.record['id']}] adapted prompt captured ({len(adapted)} chars)")
        # The adapt chat has now settled, so it is safe to rename it.
        self._rename_chat(self.record.get("adapt_conversation_url"), self.adapt_title)
        self.phase = "start_research"

    def _step_start_research(self, known_cids: set[str]) -> None:
        if not _is_usable_adapted(self.adapted_text):
            self.fail("adapt step produced no usable prompt")
            return
        if not self._may_open_chat():
            return
        self.driver.open_new_chat()
        if self.driver.detect_rate_limit():
            self.driver.dismiss_rate_limit()
            _trip_rate_limit(self.sched, self.log)
            return
        _, self.record["model"] = self.driver.ensure_model(TARGET_MODEL_TOKENS, self.log)
        self.driver.submit(self.adapted_text)
        self._add_stage("research", "prompt", self.adapted_text, None, round_no=1)
        self.url_deadline = time.time() + URL_CAPTURE_TIMEOUT
        self._arm_response_wait("research", 1)
        self.phase = "await_research_url"
        self.log(f"[{self.record['id']}] research prompt submitted")

    def _step_await_research_url(self, known_cids: set[str]) -> None:
        url = self.driver.capture_conversation_url(known_cids)
        if url:
            cid = ec._conversation_id(url)
            if cid:
                known_cids.add(cid)
            self.record["research_conversation_url"] = url
            self.phase = "await_response"
            self._save()
        elif time.time() >= self.url_deadline:
            self.phase = "await_response"
            self._save()

    def _step_resume_research(self, known_cids: set[str]) -> None:
        if not self._may_open_chat():
            return
        url = self.resume_url
        if not url or not self.driver.goto_conversation(url):
            self.log(f"[{self.record['id']}] resume re-open failed; restarting research")
            self.phase = "start_research"
            return
        if self.driver.detect_rate_limit():
            self.driver.dismiss_rate_limit()
            _trip_rate_limit(self.sched, self.log)
            return
        self.record["research_conversation_url"] = url
        cid = ec._conversation_id(url)
        if cid:
            known_cids.add(cid)
        self._rename_chat(url, self._status_title("running"))
        self._renamed_running = True
        # Treat whatever completed reply is already on screen as this round's response.
        self._arm_response_wait("research", max(1, self.await_round))
        self.prev_response = ""
        self.phase = "await_response"
        self.log(f"[{self.record['id']}] resumed research at round {self.await_round}")
        self._save()

    def _step_await_response(self, known_cids: set[str]) -> None:
        text, done = self._poll_response()
        if not done:
            return
        self._backfill_conversation_url("research_conversation_url", known_cids)
        stage = "research" if self.await_round <= 1 else "continue"
        signal = assess_response(text)
        self._add_stage(stage, "response", text,
                        self.record.get("research_conversation_url"),
                        round_no=self.await_round, assessment=signal)
        self._research_text = text
        self._research_elapsed = time.time() - self.submitted_at
        # The research chat has settled after the first response; label it now.
        if not self._renamed_running and self.record.get("research_conversation_url"):
            self._rename_chat(self.record["research_conversation_url"],
                              self._status_title("running"))
            self._renamed_running = True
        # Only spend a (slow, usage-limited) self-assessment turn when the reply
        # SHOWS SIGNS of a completed solution, or on the final round. Most rounds
        # are plainly still in progress, so asking "are you done?" every round just
        # wastes a turn — skip straight to the next continuation instead.
        looks_solved = signal == "solved"
        final_round = self.await_round >= self.max_rounds
        mins = self._research_elapsed / 60
        if looks_solved or final_round:
            why = "looks solved" if looks_solved else "final round"
            self.log(f"[{self.record['id']}] round {self.await_round} reply "
                     f"({len(text)} chars, {mins:.1f}min) — {why}; asking to confirm")
            # Ask the model DIRECTLY whether it is done. A free-form proof is hard
            # to classify by regex, so its explicit one-line verdict is what gates
            # the independent verification.
            self.driver.submit(SELF_ASSESS_PROMPT)
            self._arm_response_wait("selfassess", self.await_round)
            self.phase = "await_selfassess"
        else:
            self.log(f"[{self.record['id']}] round {self.await_round} reply "
                     f"({len(text)} chars, {mins:.1f}min) — continuing")
            self._continue_or_exhaust()

    def _continue_or_exhaust(self) -> None:
        """Send the next continuation nudge, or mark exhausted at the round cap."""
        if self.await_round >= self.max_rounds:
            self.record["status"] = "exhausted"
            self._finish("exhausted")
            return
        nxt = self.await_round + 1
        # Send the nudge that fits the research reply; on the last round push to FINISH.
        kind, msg = choose_continuation(self._research_text, self.await_round,
                                        self.max_rounds, self._tried_conts)
        self._tried_conts.add(kind)
        # If the research reply came back too fast, tell it to think much longer.
        too_fast = self._research_elapsed < MIN_THINK_SECONDS
        if too_fast:
            msg = msg + _THINK_LONGER
        self.driver.submit(msg)
        self._add_stage("continue", "prompt", msg,
                        self.record.get("research_conversation_url"), round_no=nxt)
        self._arm_response_wait("research", nxt)
        self.phase = "await_response"
        self.log(f"[{self.record['id']}] continuation {nxt} ({kind}"
                 f"{', +think-longer' if too_fast else ''}) submitted "
                 f"[prev reply {self._research_elapsed / 60:.1f}min]")

    def _step_await_selfassess(self, known_cids: set[str]) -> None:
        text, done = self._poll_response()
        if not done:
            return
        claims_solved = _self_assessed_solved(text)
        self._add_stage("selfassess", "response", text,
                        self.record.get("research_conversation_url"),
                        round_no=self.await_round,
                        assessment="claims_solved" if claims_solved else "not_solved")
        self.log(f"[{self.record['id']}] round {self.await_round} self-assessment → "
                 f"{'SOLVED' if claims_solved else 'not solved'}")
        if claims_solved:
            # The model says it is done — independently verify the proof in a fresh chat.
            self._solved_candidate = self._research_text
            self.phase = "start_verify"
            return
        # It confirmed NOT solved — keep pushing (or exhaust at the round cap).
        self._continue_or_exhaust()

    # -- independent verification of a claimed solution -------------------------
    def _step_start_verify(self, known_cids: set[str]) -> None:
        if not self._may_open_chat():
            return
        self.driver.open_new_chat()
        if self.driver.detect_rate_limit():
            self.driver.dismiss_rate_limit()
            _trip_rate_limit(self.sched, self.log)
            return
        _, self.record["model"] = self.driver.ensure_model(TARGET_MODEL_TOKENS, self.log)
        msg = VERIFY_PROMPT_TEMPLATE.format(
            problem=self.record.get("problem_statement", ""),
            proof=self._solved_candidate)
        self.driver.submit(msg)
        self._add_stage("verify", "prompt", msg, None, round_no=self.await_round)
        self.url_deadline = time.time() + URL_CAPTURE_TIMEOUT
        self._arm_response_wait("verify", self.await_round)
        self.phase = "await_verify_url"
        self.log(f"[{self.record['id']}] verification prompt submitted")

    def _step_await_verify_url(self, known_cids: set[str]) -> None:
        url = self.driver.capture_conversation_url(known_cids)
        if url:
            cid = ec._conversation_id(url)
            if cid:
                known_cids.add(cid)
            self.record["verify_conversation_url"] = url
            self.phase = "await_verify_response"
            self._save()
        elif time.time() >= self.url_deadline:
            self.phase = "await_verify_response"
            self._save()

    def _step_await_verify_response(self, known_cids: set[str]) -> None:
        text, done = self._poll_response()
        if not done:
            return
        self._backfill_conversation_url("verify_conversation_url", known_cids)
        verified = _is_verified(text)
        self._add_stage("verify", "response", text,
                        self.record.get("verify_conversation_url"),
                        round_no=self.await_round,
                        assessment="verified" if verified else "rejected")
        if verified:
            self._rename_chat(self.record.get("verify_conversation_url"),
                              f"{self.base_name} — verification"[:100])
            self.record["status"] = "solved"
            self.log(f"[{self.record['id']}] verification CONFIRMED — marking solved")
            self._finish("solved")
            return
        self.log(f"[{self.record['id']}] verification REJECTED the claimed solution")
        self._verify_critique = _extract_critique(text)
        if self.await_round >= self.max_rounds:
            self.record["status"] = "exhausted"
            self._finish("exhausted")
            return
        self.phase = "recontinue_after_verify"

    def _step_recontinue_after_verify(self, known_cids: set[str]) -> None:
        research_url = self.record.get("research_conversation_url")
        if not research_url or not self.driver.goto_conversation(research_url):
            self.record["status"] = "exhausted"
            self._finish("exhausted")
            return
        if self.driver.detect_rate_limit():
            self.driver.dismiss_rate_limit()
            _trip_rate_limit(self.sched, self.log)
            return
        nxt = self.await_round + 1
        msg = VERIFY_FAILED_CONTINUATION.format(critique=self._verify_critique)
        self.driver.submit(msg)
        self._add_stage("continue", "prompt", msg, research_url, round_no=nxt)
        self._arm_response_wait("research", nxt)
        self.phase = "await_response"
        self.log(f"[{self.record['id']}] continuation {nxt} (post-verification) submitted")

    def _finish(self, status: str) -> None:
        url = self.record.get("research_conversation_url")
        if url:
            self._rename_chat(url, self._status_title(status))
        self._save()
        self.log(f"[slot {self.index}] DONE {self.record['id']} — {status}")
        self._release()

    # -- shared non-blocking response poll --------------------------------------
    def _poll_response(self) -> tuple[str, bool]:
        """Return (text, done). Never blocks longer than a few UI queries."""
        now = time.time()
        if self.driver.detect_rate_limit():
            self.driver.dismiss_rate_limit()
            _trip_rate_limit(self.sched, self.log)
            return "", False
        if now >= self.phase_deadline:
            return self.driver.response_text(), True   # cut a runaway generation
        if self.driver.is_busy():
            self.busy_seen = True
            self.stable = 0
            return "", False
        if self.driver.maybe_continue_generation():
            self.busy_seen = True
            self.stable = 0
            return "", False
        text = self.driver.response_text()
        stripped = text.strip()
        # Ignore the stale prior message that is still on screen until the new
        # reply appears (critical for continuation rounds in the same chat), and
        # never settle on the extraction-failure placeholder (blank/dead tab).
        if not stripped or stripped == self.prev_response or stripped == _EXTRACT_FAILURE:
            return "", False
        if len(stripped) < MIN_RESPONSE_CHARS and now < self.gen_start_deadline:
            return "", False
        if text == self.last_text:
            self.stable += 1
        else:
            self.last_text = text
            self.stable = 0
            return "", False
        if self.stable >= STABLE_TICKS:
            return text, True
        return "", False


# ── Scheduler ─────────────────────────────────────────────────────────────────
def _load_control_nonce(state_dir: Path) -> int:
    try:
        data = json.loads((state_dir / "_control_state.json").read_text(encoding="utf-8"))
        return int(data.get("restart", 0))
    except Exception:
        return 0


def _save_control_nonce(state_dir: Path, nonce: int) -> None:
    try:
        _atomic_write_json(state_dir / "_control_state.json", {"restart": int(nonce)})
    except Exception:
        pass


def _run_loop(drivers: list[Any], problems: list[dict[str, str]], *,
              meta_prompt: str, state_dir: Path, max_rounds: int,
              response_timeout: float, tick: float, new_chat_spacing: float, log) -> None:
    known_cids: set[str] = set()
    sched = {"next_new_chat_at": 0.0}
    slots = [Slot(i, d, meta_prompt=meta_prompt, state_dir=state_dir,
                  max_rounds=max_rounds, response_timeout=response_timeout,
                  sched=sched, new_chat_spacing=new_chat_spacing, log=log)
             for i, d in enumerate(drivers)]
    prior_state = load_prior_state(state_dir)
    for rec in prior_state.values():
        for key in ("research_conversation_url", "adapt_conversation_url"):
            val = rec.get(key)
            if val:
                cid = ec._conversation_id(val)
                if cid:
                    known_cids.add(cid)
    pending, resumed, skipped = [], 0, 0
    for problem in problems:
        kind = classify_resume(prior_state.get(problem["id"], {}))
        if kind == "done":
            skipped += 1
            continue
        if kind in ("research", "adapted"):
            resumed += 1
        pending.append(problem)
    log(f"[resume] {skipped} already done (skipped), {resumed} to resume, "
        f"{len(pending) - resumed} fresh")
    queue = collections.deque(pending)
    retry_counts: dict[str, int] = {}
    control: dict[str, Any] = {"state": "run", "restart": _load_control_nonce(state_dir)}
    handled_restart = control["restart"]
    heartbeat = 0.0
    control_at = 0.0
    while queue or any(not s.idle for s in slots):
        now = time.time()
        # -- remote control: stop/start via 'state', one-shot relaunch via 'restart' --
        if now - control_at >= CONTROL_POLL_SECONDS:
            control_at = now
            fetched = _poll_control(CONTROL_URL)
            if fetched is not None:
                state = str(fetched.get("state") or "run").lower()
                if state not in ("run", "pause"):
                    state = "run"
                if state != control.get("state"):
                    log(f"[control] state → {state}")
                control["state"] = state
                try:
                    nonce = int(fetched.get("restart") or 0)
                except (TypeError, ValueError):
                    nonce = 0
                control["restart"] = nonce
                if nonce and nonce != handled_restart:
                    _save_control_nonce(state_dir, nonce)   # persist BEFORE relaunching
                    log("[control] restart requested — relaunching (state preserved)")
                    raise _RestartRequested()
        paused = control.get("state") == "pause"

        if slots and not any(s.driver.alive() for s in slots):
            raise _BrowserDied("all tabs closed")
        if not paused:
            for slot in slots:
                if slot.idle and queue:
                    problem = queue.popleft()
                    prior = prior_state.get(problem["id"])
                    if prior and classify_resume(prior) in ("research", "adapted"):
                        slot.resume(problem, prior)
                    else:
                        slot.assign(problem)
                if not slot.idle:
                    try:
                        slot.step(known_cids)
                    except _BrowserDied:
                        raise
                    except _RestartRequested:
                        raise
                    except Exception as exc:
                        if _is_browser_dead(exc):
                            raise _BrowserDied(repr(exc))
                        # A per-problem error keeps other tabs alive. Re-queue the
                        # problem (it resumes from its saved state) a few times
                        # before giving up, so one flaky problem can't stop the run.
                        pid = slot.record.get("id")
                        problem_obj = slot.problem
                        tries = (retry_counts.get(pid, 0) + 1) if pid else PROBLEM_RETRY_MAX + 1
                        if pid:
                            retry_counts[pid] = tries
                        if pid and problem_obj and tries <= PROBLEM_RETRY_MAX:
                            slot._save()
                            prior_state[pid] = dict(slot.record)
                            slot._release()
                            queue.append(problem_obj)
                            log(f"[slot {slot.index}] error on {pid} "
                                f"({tries}/{PROBLEM_RETRY_MAX}): {exc!r} — re-queued (will resume)")
                        else:
                            slot.fail(repr(exc))
        now = time.time()
        if now - heartbeat >= 30.0:
            heartbeat = now
            busy = sum(not s.idle for s in slots)
            log(f"[scheduler] {busy} active, {len(queue)} queued"
                f"{' [PAUSED]' if paused else ''}")
            _write_active_manifest(state_dir, slots, control)
        time.sleep(tick)
    log("[scheduler] all problems processed")
    _write_active_manifest(state_dir, slots, control)


def run_pipeline(problems: list[dict[str, str]], *, workers: int, profile_dir: Path,
                 meta_prompt: str, state_dir: Path, max_rounds: int,
                 response_timeout: float, tick: float, headless: bool,
                 dry_run: bool, sim_solve_at: Optional[int], log) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    n = max(1, min(workers, len(problems)))
    if dry_run:
        drivers = [SimDriver(solve_at_round=sim_solve_at, busy_secs=0.6) for _ in range(n)]
        _run_loop(drivers, problems, meta_prompt=meta_prompt, state_dir=state_dir,
                  max_rounds=max_rounds, response_timeout=response_timeout,
                  tick=min(tick, 0.25), new_chat_spacing=0.05, log=log)
        return

    from playwright.sync_api import sync_playwright
    rapid_deaths = 0
    for attempt in range(1, RELAUNCH_MAX + 2):
        remaining = _remaining_count(problems, state_dir)
        if remaining == 0:
            log("[pipeline] all problems complete")
            return
        if attempt > RELAUNCH_MAX:
            log(f"[pipeline] reached the relaunch cap ({RELAUNCH_MAX}); "
                f"{remaining} problem(s) still unfinished — rerun to resume.")
            return
        launched_at = time.time()
        log(f"[pipeline] launching Chromium (attempt {attempt}, profile {profile_dir}); "
            f"{remaining} problem(s) remaining")
        try:
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled"],
                    no_viewport=False,
                    viewport={"width": 1280, "height": 900},
                )
                try:
                    page0 = context.pages[0] if context.pages else context.new_page()
                    _ensure_logged_in(page0, profile_dir)
                    pages = [page0]
                    for _ in range(n - 1):
                        pages.append(context.new_page())
                    drivers = [BrowserDriver(p) for p in pages]
                    _run_loop(drivers, problems, meta_prompt=meta_prompt, state_dir=state_dir,
                              max_rounds=max_rounds, response_timeout=response_timeout,
                              tick=tick, new_chat_spacing=NEW_CHAT_SPACING, log=log)
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
            log("[pipeline] run loop finished")
            return
        except SystemExit:
            raise
        except KeyboardInterrupt:
            raise
        except _RestartRequested:
            # A remote 'restart' — relaunch the browser and resume. Not a crash, so
            # it does not count toward the rapid-death circuit breaker.
            rapid_deaths = 0
            log(f"[pipeline] restart requested — relaunching in {RELAUNCH_PAUSE:.0f}s "
                "(resuming from saved state)")
            time.sleep(RELAUNCH_PAUSE)
        except Exception as exc:
            # Self-heal from ANY failure (browser death or an unexpected error) so
            # the run survives days unattended. A tight crash-loop is still capped by
            # the rapid-death circuit breaker below.
            alive_secs = time.time() - launched_at
            if alive_secs < RAPID_DEATH_SECS:
                rapid_deaths += 1
                if rapid_deaths >= RAPID_DEATH_LIMIT:
                    log(f"[pipeline] the run keeps dying within {RAPID_DEATH_SECS:.0f}s of "
                        f"launch ({rapid_deaths}x in a row). Stopping — make sure the "
                        f"{profile_dir} profile is free (no other Chromium on it) and logged "
                        f"in, then rerun to resume from saved state. Last error: {exc!r}")
                    return
            else:
                rapid_deaths = 0
            kind = ("Chromium died" if (isinstance(exc, _BrowserDied) or _is_browser_dead(exc))
                    else "unexpected error")
            log(f"[pipeline] {kind} after {alive_secs:.0f}s ({exc!r}); "
                f"relaunching in {RELAUNCH_PAUSE:.0f}s to resume from saved state")
            time.sleep(RELAUNCH_PAUSE)


def _ensure_logged_in(page: Any, profile_dir: Path) -> None:
    page.goto(ec.CHATGPT_URL, wait_until="domcontentloaded")
    time.sleep(3)
    if "login" in page.url or "auth" in page.url:
        raise SystemExit(
            f"Not logged into ChatGPT in profile {profile_dir}.\n"
            "Open that profile headed and log in once, then rerun. (Stop the EGMRA "
            "campaign first so the profile is free.)")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--problems", type=Path, default=DEFAULT_PROBLEMS)
    parser.add_argument("--meta-prompt", type=Path, default=DEFAULT_META_PROMPT)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                        help="max continuation rounds after the first attempt")
    parser.add_argument("--response-timeout", type=float, default=DEFAULT_RESPONSE_TIMEOUT)
    parser.add_argument("--tick", type=float, default=DEFAULT_TICK)
    parser.add_argument("--profile-dir", type=Path, default=None,
                        help="ChatGPT Chromium profile (default: $CHATGPT_PROFILE_DIR "
                             "or the top-level .chatgpt_profile)")
    parser.add_argument("--project-url", default=DEFAULT_PROJECT_URL,
                        help="ChatGPT Project URL to create all chats inside "
                             "(default: the erdos3 project)")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="no browser; scripted responses to validate the pipeline")
    parser.add_argument("--sim-solve-at", type=int, default=2,
                        help="dry-run: research round at which the sim declares success")
    args = parser.parse_args(argv)

    if not args.meta_prompt.exists():
        raise SystemExit(f"meta-prompt template not found: {args.meta_prompt}")
    meta_prompt = args.meta_prompt.read_text(encoding="utf-8").strip()
    if not args.problems.exists():
        raise SystemExit(
            f"problems file not found: {args.problems}\n"
            "Create it from side_pipeline_problems.example.json (a JSON list of "
            "{id, title, statement, references}).")
    problems = load_problems(args.problems)
    if not problems:
        raise SystemExit("no problems to run")

    # All adapt/research chats are created inside this ChatGPT Project so they are
    # grouped and named together. start_new_chat() navigates to ec.PROJECT_URL.
    ec.PROJECT_URL = args.project_url

    profile_dir = args.profile_dir or Path(
        os.environ.get("CHATGPT_PROFILE_DIR") or ec.PROFILE_DIR)

    def log(message: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)

    log(f"[pipeline] {len(problems)} problem(s), {args.workers} worker(s), "
        f"max {args.max_rounds} continuation rounds"
        + (" [DRY RUN]" if args.dry_run else ""))
    if not args.dry_run:
        log(f"[pipeline] project: {args.project_url}")
    run_pipeline(
        problems,
        workers=args.workers,
        profile_dir=profile_dir,
        meta_prompt=meta_prompt,
        state_dir=args.state_dir,
        max_rounds=args.max_rounds,
        response_timeout=args.response_timeout,
        tick=args.tick,
        headless=args.headless,
        dry_run=args.dry_run,
        sim_solve_at=args.sim_solve_at,
        log=log,
    )
    log(f"[pipeline] done — state in {args.state_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
