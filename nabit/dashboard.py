"""Self-contained HTML dashboard for nabit verification results.

Turns JSONL sink files into ONE static HTML file — inline CSS/JS, no server,
no account, no dependencies, works offline:

    python -m nabit dashboard audit.jsonl                 # writes + opens
    python -m nabit dashboard a.jsonl b.jsonl -o out.html # merge sources
    python -m nabit report audit.jsonl                    # terminal table

The file embeds every record as JSON and does filtering/aggregation
client-side, so a multi-thousand-row audit becomes a browsable report you can
email, archive, or serve from anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

# Safety cap — pathological sink files shouldn't OOM the browser.
DEFAULT_MAX_RECORDS = 200_000

# Record keys carried into the dashboard (VerificationResult.to_dict schema).
_RECORD_KEYS = ("name", "passed", "duration_ms", "result", "error",
                "attempts", "run_id", "timestamp")


def load_records(paths: Iterable[str], max_records: int = DEFAULT_MAX_RECORDS,
                 sources: Optional[dict] = None) -> list[dict]:
    """Load JSONL sink files into dashboard records. Bad lines are skipped;
    each record gains `_source` (from `sources` when given, else the file
    stem) so merged files are distinguishable in the UI."""
    records: list[dict] = []
    for path in paths:
        source = (sources or {}).get(path) or Path(path).stem
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue  # corrupt line — skip, don't die mid-report
                if not isinstance(d, dict):
                    continue
                rec = {k: d.get(k) for k in _RECORD_KEYS}
                rec["_source"] = source
                records.append(rec)
                if len(records) >= max_records:
                    return records
    return records


def aggregate(records: list[dict]) -> list[dict]:
    """Per (source, name) totals — used for the initial summary render."""
    agg: dict = {}
    for r in records:
        key = (r["_source"], r.get("name") or "unknown")
        a = agg.setdefault(key, {"source": key[0], "name": key[1],
                                 "total": 0, "passed": 0, "failed": 0,
                                 "ms": 0.0})
        a["total"] += 1
        if r.get("passed"):
            a["passed"] += 1
        else:
            a["failed"] += 1
        a["ms"] += float(r.get("duration_ms") or 0.0)
    rows = []
    for a in agg.values():
        a["avg_ms"] = a["ms"] / a["total"] if a["total"] else 0.0
        a["rate"] = (100.0 * a["passed"] / a["total"]) if a["total"] else 100.0
        rows.append(a)
    # worst first — failures are what you came to look at
    rows.sort(key=lambda a: (-a["failed"], -a["total"], a["source"], a["name"]))
    return rows


def load_history(history_dir: str | Path) -> list[dict]:
    """Read the compact history lines (one per scheduled run) that
    nabit.runner.run_audit appends to <dir>/history.jsonl. Each line gains
    `_source` from the dir name so multiple audit dirs merge cleanly."""
    path = Path(history_dir) / "history.jsonl"
    lines: list[dict] = []
    if not path.exists():
        return lines
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                d["_source"] = Path(history_dir).stem
                lines.append(d)
    return lines


def _latest_run_file(history_dir: str | Path) -> Optional[str]:
    """The most recent full-results file in a history dir (not history.jsonl)."""
    files = sorted(
        p for p in Path(history_dir).glob("*.jsonl") if p.name != "history.jsonl"
    )
    return str(files[-1]) if files else None


def build_html(records: list[dict], title: str = "nabit — verification dashboard",
               generated: Optional[str] = None,
               history: Optional[list[dict]] = None) -> str:
    """Render the self-contained dashboard. All dynamic behavior (search,
    filters, drill-down) is vanilla JS over the embedded records. When
    `history` (compact per-run aggregates from run_audit) is present the
    dashboard adds a trend section; drill-down then covers the latest run."""
    data = {
        "title": title,
        "generated": generated or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "records": records,
        "aggregate": aggregate(records),
        "history": history or [],
    }
    payload = json.dumps(data, default=str).replace("</", "<\\/")  # </script> safety
    return _TEMPLATE.replace("__DATA__", payload).replace("__TITLE__", _escape(title))


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def write_dashboard(paths: list[str], out: str, title: str = "nabit — verification dashboard",
                    max_records: int = DEFAULT_MAX_RECORDS) -> str:
    """Build the dashboard from JSONL files and/or history directories (as
    produced by `python -m nabit run`). A directory adds the trend view and
    contributes its latest run's records for drill-down."""
    files: list[str] = []
    history: list[dict] = []
    sources: dict = {}
    for p in paths:
        if Path(p).is_dir():
            history.extend(load_history(p))
            latest = _latest_run_file(p)
            if latest:
                files.append(latest)
                sources[latest] = Path(p).stem  # label by audit, not timestamp
        else:
            files.append(p)
    records = load_records(files, max_records, sources=sources)
    if not records and not history:
        raise SystemExit("no verification records found in: %s" % ", ".join(paths))
    html = build_html(records, title=title, history=history)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<style>
  :root{
    --bg:#0a0e14; --panel:#0f141c; --panel2:#111823; --border:#1e2733; --border-hi:#2b3a4d;
    --text:#c9d4e0; --muted:#7c8a9c; --dim:#6b7688;
    --accent:#4ade80; --accent2:#38bdf8; --danger:#f87171; --warn:#fbbf24;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:14px;
    line-height:1.5;padding:24px;
    background-image:radial-gradient(circle at 15% -10%, #12203033, transparent 40%)}
  .wrap{max-width:1100px;margin:0 auto}
  h1{font-size:1.35rem;font-weight:700;letter-spacing:-.5px}
  h1 .g{color:var(--accent)}
  .sub{color:var(--dim);font-size:.78rem;margin:4px 0 20px}
  .cards{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:18px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px}
  .card .n{font-size:1.5rem;font-weight:700}
  .card .l{color:var(--dim);font-size:.72rem;margin-top:2px}
  .card.bad .n{color:var(--danger)} .card.ok .n{color:var(--accent)}
  .card.info .n{color:var(--accent2)}
  .bar{height:16px;background:var(--panel2);border:1px solid var(--border);border-radius:4px;overflow:hidden;display:flex}
  .bar .p{background:var(--accent);opacity:.75} .bar .f{background:var(--danger)}
  .filters{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
  .filters input,.filters select{background:var(--panel);color:var(--text);border:1px solid var(--border-hi);
    border-radius:7px;padding:7px 10px;font-family:var(--mono);font-size:.8rem}
  .filters input{flex:1;min-width:180px}
  .filters input:focus,.filters select:focus{outline:none;border-color:var(--accent)}
  table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);
    border-radius:10px;overflow:hidden;margin-bottom:22px}
  th,td{padding:9px 12px;text-align:right;font-size:.82rem;border-bottom:1px solid var(--border)}
  th{color:var(--muted);font-weight:500;text-align:right;background:var(--panel2)}
  th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
  tr:last-child td{border-bottom:none}
  td .nm{color:var(--text)} td .src{color:var(--dim);font-size:.72rem}
  .pass{color:var(--accent)} .fail{color:var(--danger)}
  details{background:var(--panel);border:1px solid var(--border);border-radius:9px;
    margin-bottom:8px;overflow:hidden}
  summary{cursor:pointer;padding:10px 14px;font-size:.82rem;list-style:none;display:flex;
    gap:10px;align-items:center;flex-wrap:wrap}
  summary:hover{background:var(--panel2)}
  summary::-webkit-details-marker{display:none}
  .tag{font-size:.68rem;padding:2px 8px;border-radius:5px;border:1px solid var(--border-hi);color:var(--muted)}
  .tag.run{color:var(--accent2)}
  .tag.err{color:var(--danger);border-color:#f8717144}
  .detail{padding:4px 14px 14px;border-top:1px solid var(--border)}
  .detail pre{background:var(--bg);border:1px solid var(--border);border-radius:7px;padding:12px;
    font-size:.75rem;overflow-x:auto;margin-top:10px;white-space:pre-wrap;word-break:break-word}
  .detail .lbl{color:var(--dim);font-size:.7rem;margin-top:10px}
  .fgroup{border-left:3px solid var(--danger)}
  .fgroup>summary{font-size:.85rem}
  .fgroup .chev{display:inline-block;color:var(--dim);transition:transform .15s}
  .fgroup[open]>.summary-row .chev, .fgroup[open] summary .chev{transform:rotate(90deg)}
  .fgroup .gcount{font-weight:700}
  .groupbody{padding:6px 14px 12px 26px;border-top:1px solid var(--border);background:var(--bg)}
  .groupbody details{background:var(--panel)}
  .more-btn{background:var(--panel2);color:var(--accent2);border:1px solid var(--border-hi);
    border-radius:7px;padding:8px 14px;font-family:var(--mono);font-size:.78rem;cursor:pointer;margin-top:10px}
  .more-btn:hover{border-color:var(--accent2)}
  .mini{background:transparent;color:var(--muted);border:1px solid var(--border-hi);border-radius:6px;
    padding:3px 9px;font-family:var(--mono);font-size:.7rem;cursor:pointer}
  .mini:hover{color:var(--text);border-color:var(--accent)}
  .section{color:var(--muted);font-size:.85rem;margin:6px 0 12px}
  .section .g{color:var(--accent)}
  footer{color:var(--dim);font-size:.72rem;text-align:center;margin-top:26px}
  @media(max-width:800px){.cards{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="wrap">
  <h1><span class="g">nabit</span> // verification dashboard</h1>
  <div class="sub" id="meta"></div>
  <div class="cards" id="cards"></div>

  <div id="historywrap" style="display:none">
    <div class="section"><span class="g">$</span> history — scheduled runs over time <span id="histmeta"></span></div>
    <table><thead><tr>
      <th>check</th><th>source</th><th>runs</th><th>latest</th><th>prev</th><th>trend</th><th>failures/run</th>
    </tr></thead><tbody id="histbody"></tbody></table>
  </div>

  <div class="section"><span class="g">$</span> verifications by check <span id="aggcount"></span></div>
  <div class="filters" style="margin-bottom:10px">
    <input id="q" type="search" placeholder="filter checks by name…">
    <select id="fsource"><option value="">all sources</option></select>
    <select id="frun"><option value="">all runs</option></select>
    <select id="fstatus">
      <option value="">all statuses</option>
      <option value="fail">failures only</option>
      <option value="pass">passes only</option>
    </select>
  </div>
  <table id="aggtable"><thead><tr>
    <th>check</th><th>source</th><th>total</th><th>pass</th><th>FAIL</th><th>rate</th><th>pass/fail</th><th>avg ms</th>
  </tr></thead><tbody></tbody></table>

  <div class="section"><span class="g">$</span> failures — claims that lied <span id="failcount"></span>
    <button class="mini" id="expandall" style="margin-left:8px">expand all</button>
    <button class="mini" id="collapseall">collapse all</button>
  </div>
  <div id="fails"></div>

  <footer>generated by nabit · the data in this file never left your machine</footer>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const R = DATA.records;
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

$('#meta').textContent = DATA.title + ' · generated ' + DATA.generated + ' · ' + R.length + ' verifications';

const state = {q:'', source:'', run:'', status:''};

// populate filters
const sources = [...new Set(R.map(r => r._source))].sort();
const runs = [...new Set(R.map(r => r.run_id).filter(Boolean))].sort();
for (const s of sources) $('#fsource').insertAdjacentHTML('beforeend', `<option>${esc(s)}</option>`);
for (const r of runs) $('#frun').insertAdjacentHTML('beforeend', `<option>${esc(r)}</option>`);

for (const [id, key] of [['#q','q'],['#fsource','source'],['#frun','run'],['#fstatus','status']]) {
  $(id).addEventListener('input', e => { state[key] = e.target.value; render(); });
}

function filtered() {
  return R.filter(r =>
    (!state.q || (r.name||'').toLowerCase().includes(state.q.toLowerCase())) &&
    (!state.source || r._source === state.source) &&
    (!state.run || r.run_id === state.run) &&
    (!state.status || (state.status === 'fail' ? !r.passed : r.passed))
  );
}

function fmtTs(t) { const d = new Date((t||0)*1000); return isNaN(d) ? '' : d.toLocaleString(); }

function render() {
  const rows = filtered();

  // cards
  const total = rows.length;
  const passed = rows.filter(r => r.passed).length;
  const failed = total - passed;
  const rate = total ? (100*passed/total).toFixed(1) + '%' : '-';
  const nruns = new Set(rows.map(r => r.run_id).filter(Boolean)).size;
  $('#cards').innerHTML = `
    <div class="card"><div class="n">${total}</div><div class="l">verifications</div></div>
    <div class="card ok"><div class="n">${passed}</div><div class="l">passed</div></div>
    <div class="card bad"><div class="n">${failed}</div><div class="l">silent failures</div></div>
    <div class="card"><div class="n">${rate}</div><div class="l">pass rate</div></div>
    <div class="card info"><div class="n">${nruns || '—'}</div><div class="l">runs</div></div>`;

  // aggregate table
  const agg = {};
  for (const r of rows) {
    const k = r._source + ' ' + (r.name||'unknown');
    const a = agg[k] || (agg[k] = {name:r.name||'unknown', source:r._source, total:0, passed:0, ms:0});
    a.total++; if (r.passed) a.passed++; a.ms += (+r.duration_ms || 0);
  }
  const list = Object.values(agg).sort((a,b) => (b.total-b.passed)-(a.total-a.passed) || b.total-a.total);
  $('#aggcount').textContent = `(${list.length} checks)`;
  $('#aggtable tbody').innerHTML = list.map(a => {
    const failed = a.total - a.passed;
    const rate = (100*a.passed/a.total).toFixed(1);
    const pw = (100*a.passed/a.total).toFixed(1);
    return `<tr>
      <td><span class="nm">${esc(a.name)}</span></td>
      <td><span class="src">${esc(a.source)}</span></td>
      <td>${a.total}</td><td class="pass">${a.passed}</td>
      <td class="${failed?'fail':''}">${failed}</td>
      <td class="${failed?'fail':'pass'}">${rate}%</td>
      <td><div class="bar" style="min-width:90px"><div class="p" style="width:${pw}%"></div><div class="f" style="width:${100-pw}%"></div></div></td>
      <td>${(a.ms/a.total).toFixed(1)}</td>
    </tr>`;
  }).join('') || `<tr><td colspan="8" style="text-align:center;color:var(--dim)">no matches</td></tr>`;

  // failures drill-down — grouped by error signature so thousands of the
  // same failure collapse into one expandable, paginated group
  const fails = rows.filter(r => !r.passed);
  G = {};
  const groups = [];
  for (const r of fails) {
    // normalize digits so "stored count 288 != actual 81" groups together
    const sig = (r.name || 'unknown') + ' :: ' +
      String(r.error || '(no error message)').replace(/\d+/g, 'N');
    let g = G[sig];
    if (!g) { g = G[sig] = { sig, name: r.name || 'unknown',
      label: String(r.error || '(no error message)').replace(/\d+/g, 'N'),
      items: [] }; groups.push(g); }
    g.items.push(r);
  }
  groups.sort((a, b) => b.items.length - a.items.length);
  $('#failcount').textContent = fails.length
    ? `(${groups.length} group${groups.length===1?'':'s'} covering ${fails.length} failures — expand a group to drill in)`
    : '(none — all claims held up)';
  $('#fails').innerHTML = groups.map(g => {
    if (!(g.sig in rendered)) rendered[g.sig] = Math.min(CHUNK, g.items.length);
    return `<details class="fgroup">
      <summary>
        <span class="chev">▶</span>
        <span class="fail gcount">${g.items.length} ×</span>
        <span class="nm">${esc(g.label)}</span>
        <span class="tag">${esc(g.name)}</span>
      </summary>
      <div class="groupbody">${buildBody(g)}</div>
    </details>`;
  }).join('') || '<div class="section" style="color:var(--dim)">nothing failed. suspicious — widen the filters?</div>';
}

function failCard(r) {
  let payload;
  try { payload = typeof r.result === 'string' ? r.result : JSON.stringify(r.result, null, 2); }
  catch(e) { payload = String(r.result); }
  return `<details>
    <summary>
      <span class="fail">✗</span>
      <span class="nm">${esc(r.name)}</span>
      <span class="tag">${esc(r._source)}</span>
      ${r.run_id ? `<span class="tag run">run:${esc(r.run_id)}</span>` : ''}
      <span class="tag">${fmtTs(r.timestamp)}</span>
    </summary>
    <div class="detail">
      <div class="lbl">the claim (what the system said)</div>
      <pre>${esc(payload)}</pre>
      ${r.error ? `<div class="lbl">error</div><pre>${esc(r.error)}</pre>` : ''}
      <div class="lbl">attempts: ${r.attempts ?? 1} · duration: ${(+r.duration_ms||0).toFixed(1)}ms</div>
    </div>
  </details>`;
}

function buildBody(g) {
  const n = rendered[g.sig];
  let html = g.items.slice(0, n).map(failCard).join('');
  if (g.items.length > n)
    html += `<button class="more-btn" data-sig="${esc(g.sig)}">show more — loaded ${n} of ${g.items.length} · load ${Math.min(CHUNK, g.items.length - n)} more</button>`;
  return html;
}

// group state + delegated handlers (survive render() re-runs)
const CHUNK = 100;
let G = {};
const rendered = {};

$('#fails').addEventListener('click', e => {
  const btn = e.target.closest('.more-btn');
  if (!btn) return;
  const g = G[btn.dataset.sig];
  if (!g) return;
  rendered[g.sig] = Math.min((rendered[g.sig] || CHUNK) + CHUNK, g.items.length);
  btn.closest('.groupbody').innerHTML = buildBody(g);
});
$('#expandall').addEventListener('click', () =>
  document.querySelectorAll('#fails details.fgroup').forEach(d => d.open = true));
$('#collapseall').addEventListener('click', () =>
  document.querySelectorAll('#fails details.fgroup').forEach(d => d.open = false));

// --- history / trend section (populated only when scheduled runs exist) ------
function spark(vals, w = 130, h = 22) {
  const max = Math.max(...vals, 1);
  const bw = w / Math.max(vals.length, 1);
  const bars = vals.map((v, i) => {
    const bh = v > 0 ? Math.max(v / max * h, 2) : 1.5;
    return `<rect x="${(i * bw).toFixed(1)}" y="${(h - bh).toFixed(1)}" width="${Math.max(bw - 1.5, 1).toFixed(1)}" height="${bh.toFixed(1)}" fill="${v > 0 ? 'var(--danger)' : 'var(--accent)'}" opacity="0.85" rx="1"/>`;
  }).join('');
  return `<svg width="${w}" height="${h}" style="vertical-align:middle">${bars}</svg>`;
}

function renderHistory() {
  const H = DATA.history || [];
  if (!H.length) return;
  $('#historywrap').style.display = '';
  // per (source, check) series across runs, ordered by time
  const per = {};
  for (const h of H) {
    for (const [n, c] of Object.entries(h.checks || {})) {
      const k = h._source + ' ' + n;
      (per[k] || (per[k] = { source: h._source, name: n, runs: [] })).runs.push(
        { ts: +h.ts || 0, ts_str: h.ts_str, failed: c.failed, total: c.total });
    }
  }
  const rows = Object.values(per);
  for (const r of rows) r.runs.sort((a, b) => a.ts - b.ts);
  rows.sort((a, b) => b.runs.length - a.runs.length || a.name.localeCompare(b.name));
  $('#histmeta').textContent = `(${H.length} run${H.length===1?'':'s'} — drill-down below shows the latest)`;
  $('#histbody').innerHTML = rows.map(r => {
    const last = r.runs[r.runs.length - 1];
    const prev = r.runs.length > 1 ? r.runs[r.runs.length - 2] : null;
    let delta;
    if (!prev) delta = '<span style="color:var(--dim)">first run</span>';
    else if (last.failed > prev.failed) delta = `<span class="fail">▲ +${last.failed - prev.failed}</span>`;
    else if (last.failed < prev.failed) delta = `<span class="pass">▼ ${last.failed - prev.failed}</span>`;
    else delta = '<span style="color:var(--dim)">— same</span>';
    const vals = r.runs.map(x => x.failed);
    return `<tr>
      <td><span class="nm">${esc(r.name)}</span></td>
      <td><span class="src">${esc(r.source)}</span></td>
      <td>${r.runs.length}</td>
      <td class="${last.failed ? 'fail' : 'pass'}">${last.failed}/${last.total}</td>
      <td>${prev ? prev.failed : '—'}</td>
      <td>${delta}</td>
      <td>${spark(vals)}</td>
    </tr>`;
  }).join('');
}

render();
renderHistory();
</script>
</body>
</html>
"""


def main(argv: Optional[list[str]] = None) -> None:
    from .report import report_from_jsonl

    parser = argparse.ArgumentParser(
        prog="python -m nabit",
        description="nabit — did it actually work? Dashboards and reports from JSONL sink files.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dash = sub.add_parser("dashboard", help="generate a self-contained HTML dashboard from JSONL sink files")
    p_dash.add_argument("files", nargs="+", help="JSONL sink file(s); multiple files are merged with a source label")
    p_dash.add_argument("-o", "--out", default="nabit-dashboard.html", help="output HTML path (default: nabit-dashboard.html)")
    p_dash.add_argument("--title", default="nabit — verification dashboard")
    p_dash.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    p_dash.add_argument("--no-open", action="store_true", help="don't open the browser")

    p_rep = sub.add_parser("report", help="print a terminal pass/fail table from a JSONL sink file")
    p_rep.add_argument("file", help="JSONL sink file")

    p_run = sub.add_parser("run", help="run a registered audit and record history (cron-friendly)")
    p_run.add_argument("name", nargs="?", help="audit name (as defined by @nabit.audit)")
    p_run.add_argument("--all", action="store_true", help="run every audit in the audits file")
    p_run.add_argument("--audits", default=None,
                       help="path to the audits module (default: $NABIT_AUDITS or ./nabit_audits.py)")
    p_run.add_argument("--out", default="nabit-history", help="history directory (default: nabit-history/)")
    p_run.add_argument("--strict", action="store_true", help="exit 1 if any check failed")

    p_ls = sub.add_parser("audits", help="list audits defined in an audits module")
    p_ls.add_argument("--audits", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "report":
        print(report_from_jsonl(args.file))
        return

    if args.cmd in ("run", "audits"):
        import os as _os

        audits_path = (args.audits if getattr(args, "audits", None)
                       else _os.environ.get("NABIT_AUDITS") or "nabit_audits.py")
        from .runner import load_audits, run_audit

        audits = load_audits(audits_path)
        if args.cmd == "audits":
            for n in sorted(audits):
                print(n, "-", (audits[n].__doc__ or "").strip().splitlines()[0]
                      if audits[n].__doc__ else "")
            if not audits:
                print("no @nabit.audit functions found in", audits_path)
            return
        names = sorted(audits) if args.all else [args.name]
        if not args.all and not args.name:
            parser.error("run needs an audit name (or --all)")
        any_failed = 0
        for n in names:
            if n not in audits:
                parser.error(f"unknown audit {n!r}; available: {', '.join(sorted(audits)) or 'none'}")
            res = run_audit(audits[n], history_dir=args.out)
            print(f"== {n}: {res['passed']}/{res['total']} passed, "
                  f"{res['failed']} failed ({res['duration_s']}s) -> {res['run_file']}")
            from .report import report_from_jsonl as _rfj

            print(_rfj(res["run_file"]))
            any_failed += res["failed"]
        if args.strict and any_failed:
            sys.exit(1)
        return

    out = write_dashboard(args.files, args.out, title=args.title,
                          max_records=args.max_records)
    file_paths = [p if not Path(p).is_dir() else (_latest_run_file(p) or "")
                  for p in args.files]
    n = len(load_records([f for f in file_paths if f]))
    n_hist = sum(len(load_history(p)) for p in args.files if Path(p).is_dir())
    print(f"wrote {out} ({n} verifications" + (f", {n_hist} history runs" if n_hist else "") + ")")
    if not args.no_open:
        import webbrowser

        webbrowser.open("file://" + str(Path(out).resolve()))


if __name__ == "__main__":
    main()
