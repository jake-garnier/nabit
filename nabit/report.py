"""Human-readable pass/fail reporting for verification results.

    print(nabit.report())                    # from the in-memory ring
    print(nabit.report(run_id="signup-123")) # scoped to one run
    print(nabit.report_from_jsonl("audit.jsonl"))  # from a sink file — the
                                                   # durable path for bulk work

Both return a string (nabit never prints on its own). The JSONL variant
aggregates the sink file, so it sees EVERY result even when the in-memory
ring buffer has long since rolled over — that's the right choice for
retro-audits and bulk verification.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Optional

from .core import get_results


def _render(name_width: int, rows: list[tuple[str, int, int, int, float]]) -> str:
    lines = []
    hdr = (f"{'verification':<{name_width}}  {'total':>7}  {'pass':>7}  "
           f"{'FAIL':>7}  {'rate':>7}  {'avg ms':>8}")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    tot_p = tot_f = 0
    sum_ms = 0.0
    for name, total, passed, failed, avg_ms in rows:
        rate = f"{100.0 * passed / total:.1f}%" if total else "-"
        lines.append(f"{name:<{name_width}}  {total:>7}  {passed:>7}  "
                     f"{failed:>7}  {rate:>7}  {avg_ms:>8.1f}")
        tot_p += passed
        tot_f += failed
        sum_ms += avg_ms * total
    tot = tot_p + tot_f
    lines.append("-" * len(hdr))
    overall = f"{100.0 * tot_p / tot:.1f}%" if tot else "-"
    lines.append(f"{'TOTAL':<{name_width}}  {tot:>7}  {tot_p:>7}  "
                 f"{tot_f:>7}  {overall:>7}  {(sum_ms / tot if tot else 0.0):>8.1f}")
    return "\n".join(lines)


def _aggregate() -> tuple[dict, list[str]]:
    """Merge results grouped by name: name -> [total, passed, failed, ms_sum]."""
    agg: dict = defaultdict(lambda: [0, 0, 0, 0.0])
    order: list[str] = []
    return agg, order


def report(run_id: Optional[str] = None) -> str:
    """Render a pass/fail table from recent in-memory results (optionally
    scoped to one run id). Note the ring buffer caps at ~1000 results — for
    bulk audits use report_from_jsonl() with a jsonl_sink file."""
    rs = get_results(run_id)
    agg: dict = defaultdict(lambda: [0, 0, 0, 0.0])
    for r in rs:
        a = agg[r.name]
        a[0] += 1
        a[1] += int(r.passed)
        a[2] += int(not r.passed)
        a[3] += r.duration_ms
    if not agg:
        return "no verification results"
    rows = [
        (name, a[0], a[1], a[2], a[3] / a[0])
        for name, a in agg.items()
    ]
    name_width = max(len(n) for n, *_ in rows + [("TOTAL",)])
    return _render(name_width, rows)


def report_from_jsonl(path: str) -> str:
    """Render a pass/fail table from a jsonl_sink file. Aggregates the whole
    file, so totals are exact regardless of result volume."""
    agg: dict = defaultdict(lambda: [0, 0, 0, 0.0])
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue  # skip corrupt lines rather than dying mid-report
            a = agg[d.get("name", "unknown")]
            passed = bool(d.get("passed"))
            a[0] += 1
            a[1] += int(passed)
            a[2] += int(not passed)
            a[3] += float(d.get("duration_ms") or 0.0)
    if not agg:
        return "no verification results"
    rows = [
        (name, a[0], a[1], a[2], a[3] / a[0])
        for name, a in agg.items()
    ]
    name_width = max(len(n) for n, *_ in rows + [("TOTAL",)])
    return _render(name_width, rows)


__all__ = ["report", "report_from_jsonl"]
