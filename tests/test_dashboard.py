import json

import pytest

from nabit.dashboard import load_records, aggregate, build_html, write_dashboard


def _write_jsonl(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


BASE = {"passed": True, "duration_ms": 1.0, "result": None, "error": None,
        "attempts": 1, "run_id": None, "timestamp": 1727900000.0}


@pytest.fixture
def two_files(tmp_path):
    a = tmp_path / "captions.jsonl"
    b = tmp_path / "hackbot.jsonl"
    _write_jsonl(a, [
        dict(BASE, name="check_x", passed=True),
        dict(BASE, name="check_x", passed=False, result={"id": 1},
             error="nope", run_id="r1"),
        dict(BASE, name="check_y", passed=True),
    ])
    _write_jsonl(b, [
        dict(BASE, name="check_x", passed=False, result={"task": 7},
             error="bad", run_id="r2"),
        "not json at all",              # corrupt line — skipped
        dict(name="orphan"),            # missing keys — filled with None
    ])
    return [str(a), str(b)]


def test_load_records_merges_and_labels_sources(two_files):
    recs = load_records(two_files)
    assert len(recs) == 5  # corrupt line skipped
    sources = {r["_source"] for r in recs}
    assert sources == {"captions", "hackbot"}
    orphan = [r for r in recs if r["name"] == "orphan"][0]
    assert orphan["passed"] is None and orphan["result"] is None


def test_load_records_max_cap(tmp_path):
    p = tmp_path / "big.jsonl"
    _write_jsonl(p, [dict(BASE, name=f"n{i}") for i in range(50)])
    assert len(load_records([str(p)], max_records=10)) == 10


def test_aggregate_counts_and_sorting(two_files):
    recs = load_records(two_files)
    rows = aggregate(recs)
    by_key = {(r["source"], r["name"]): r for r in rows}
    x_cap = by_key[("captions", "check_x")]
    assert x_cap["total"] == 2 and x_cap["passed"] == 1 and x_cap["failed"] == 1
    assert x_cap["rate"] == 50.0
    # rows with failures sort first
    assert rows[0]["failed"] >= rows[-1]["failed"]


def test_build_html_embeds_data_and_escapes(two_files):
    recs = load_records(two_files)
    html = build_html(recs, title="prod audit </script> attack")
    assert html.startswith("<!DOCTYPE html>")
    assert "__DATA__" not in html           # placeholder replaced
    assert "<\\/script>" in html or "</script> attack" not in html  # neutralized
    assert "check_x" in html
    # the embedded JSON parses back
    import re
    payload = re.search(r'<script id="data" type="application/json">(.*?)</script>',
                        html, re.S).group(1)
    data = json.loads(payload.replace("<\\/", "</"))
    assert data["title"] == "prod audit </script> attack"
    assert len(data["records"]) == 5
    assert len(data["aggregate"]) == 4


def test_write_dashboard_creates_file(two_files, tmp_path):
    out = tmp_path / "dash.html"
    path = write_dashboard(two_files, str(out))
    content = out.read_text()
    assert out.exists()
    assert "// verification dashboard" in content
    assert path == str(out)


def test_write_dashboard_empty_raises(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    with pytest.raises(SystemExit):
        write_dashboard([str(empty)], str(tmp_path / "x.html"))
