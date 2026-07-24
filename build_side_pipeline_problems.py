#!/usr/bin/env python3
"""Convert the 100-problem markdown dossier into side_pipeline_problems.json.

Reads the source-verified problem list and emits a JSON array of
``{id, title, statement, references}`` records consumed by ``side_pipeline.py``.
Each ``references`` field carries the primary source URL(s) so the adaptation
step (and the public dashboard) can link straight to each problem.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SRC = Path("/Users/eric/Downloads/100_verified_open_math_problems.md")
OUT = Path(__file__).resolve().parent / "side_pipeline_problems.json"

HEADER = re.compile(r"^##\s+\d+\.\s+(.+)$")
SOURCE_LINE = re.compile(r"^\*\*(?:Primary|Current)\b")
URL = re.compile(r"\((https?://[^)]+)\)")
STOP = ("**Primary", "**Current", "---", "<a id")


def parse(md: str) -> list[dict[str, str]]:
    lines = md.splitlines()
    headers = [i for i, ln in enumerate(lines) if HEADER.match(ln)]
    problems: list[dict[str, str]] = []
    for idx, start in enumerate(headers):
        end = headers[idx + 1] if idx + 1 < len(headers) else len(lines)
        head = HEADER.match(lines[start]).group(1).strip()
        # "EP-324 — Polynomial image forming a Sidon set" → id, title
        parts = re.split(r"\s+\u2014\s+", head, maxsplit=1)
        pid = parts[0].strip()
        title = parts[1].strip() if len(parts) > 1 else pid

        body = lines[start + 1:end]
        try:
            stmt_start = next(i for i, ln in enumerate(body)
                              if ln.strip() == "### Exact problem statement") + 1
        except StopIteration:
            raise SystemExit(f"{pid}: no '### Exact problem statement' section")

        stmt_lines: list[str] = []
        refs: list[str] = []
        for ln in body[stmt_start:]:
            if any(ln.startswith(tok) for tok in STOP):
                if SOURCE_LINE.match(ln):
                    refs += [u for u in URL.findall(ln) if u not in refs]
                if ln.startswith("---") or ln.startswith("<a id"):
                    break
                continue
            stmt_lines.append(ln)

        statement = "\n".join(stmt_lines).strip()
        if not statement:
            raise SystemExit(f"{pid}: empty statement")
        problems.append({
            "id": pid,
            "title": title,
            "statement": statement,
            "references": "\n".join(refs),
        })
    return problems


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"source not found: {SRC}")
    problems = parse(SRC.read_text(encoding="utf-8"))
    OUT.write_text(json.dumps(problems, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    missing_refs = [p["id"] for p in problems if not p["references"]]
    print(f"wrote {len(problems)} problems → {OUT}")
    if missing_refs:
        print(f"WARNING: {len(missing_refs)} without references: {missing_refs}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
