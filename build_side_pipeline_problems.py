#!/usr/bin/env python3
"""Build side_pipeline_problems.json from the ranked EGMRA-screened dossier.

Reads the ranked markdown list and emits a JSON array of
``{id, title, statement, references}`` records consumed by ``side_pipeline.py``,
in rank order (list order = processing order).

Proof-suitable domains only: all Mathematics and Theoretical/Mathematical CS
entries, plus mathematical/theoretical physics (IDs ``MPH-*`` / ``OPA-*``).
Purely experimental physics (neutrino-ordering measurements, tokamak transitions,
etc.) is excluded because the pipeline proves/derives rather than measures.

Problem IDs are stable across list revisions, so problems the pipeline has
already finished are skipped automatically on resume (by id) — nothing is redone.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SRC = Path("/Users/eric/Downloads/500_ranked_open_publication_worthy_egmra_screened.md")
OUT = Path(__file__).resolve().parent / "side_pipeline_problems.json"

HEADER = re.compile(r"^##\s+\d+\.\s+(\S+)\s+\u2014\s+(.+)$")
DOMAIN = re.compile(r"^\*\*Domain:\*\*\s+(.+?)\s*$")
SOURCE = re.compile(r"^\*\*(?:Source|Current catalog-status source):\*\*")
URL = re.compile(r"\((https?://[^)]+)\)")


def _physics_ok(pid: str) -> bool:
    """Keep only mathematical/theoretical physics; drop experimental targets."""
    return pid.startswith("MPH-") or pid.startswith("OPA-")


def parse(md: str) -> list[dict[str, str]]:
    lines = md.splitlines()
    headers = [i for i, ln in enumerate(lines) if HEADER.match(ln)]
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for idx, start in enumerate(headers):
        end = headers[idx + 1] if idx + 1 < len(headers) else len(lines)
        m = HEADER.match(lines[start])
        pid, title = m.group(1).strip(), m.group(2).strip()
        if pid in seen:
            continue
        body = lines[start + 1:end]

        domain = ""
        for ln in body:
            dm = DOMAIN.match(ln)
            if dm:
                domain = dm.group(1)
                break
        is_math = domain.startswith("Mathematics")
        is_tcs = domain.startswith("Theoretical")
        is_phys = domain.startswith("Physics")
        if not (is_math or is_tcs or (is_phys and _physics_ok(pid))):
            continue

        # Statement: lines between "### Problem" and the next "###".
        stmt: list[str] = []
        in_stmt = False
        for ln in body:
            st = ln.strip()
            if st == "### Problem":
                in_stmt = True
                continue
            if in_stmt and st.startswith("### "):
                break
            if in_stmt:
                stmt.append(ln)
        statement = "\n".join(stmt).strip()
        if not statement:
            continue

        # References: URLs from any Source line anywhere in the entry.
        refs: list[str] = []
        for ln in body:
            if SOURCE.match(ln):
                refs += [u for u in URL.findall(ln) if u not in refs]

        seen.add(pid)
        out.append({
            "id": pid,
            "title": title,
            "statement": statement,
            "references": " ".join(refs),
        })
    return out


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"source not found: {SRC}")
    problems = parse(SRC.read_text(encoding="utf-8"))
    OUT.write_text(json.dumps(problems, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    by_prefix: dict[str, int] = {}
    for p in problems:
        pre = re.split(r"[-.]", p["id"])[0]
        by_prefix[pre] = by_prefix.get(pre, 0) + 1
    print(f"wrote {len(problems)} problems → {OUT}")
    print("by id-prefix:", dict(sorted(by_prefix.items())), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
