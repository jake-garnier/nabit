"""Dogfooding nabit against a REAL silent failure in the hackerOne system.

Modeled on hackerOne/src/app/recon_routes.py:
  * task_item() PATCH / finish_task (line ~10406): the hackbot's verifier LLM
    POSTs {outcome: "reportable", finding_json} and the server accepts it
    verbatim. Nothing checks that the "evidence" is real — an LLM can mark a
    finding reportable with a one-line claim and no proof.
  * _ingest_results() (line ~5834): a tool returns [] and it's stored as
    "ran, found nothing" — indistinguishable from "tool crashed, empty output".

This is a faithful simulation (no live DB), but the shapes and failure modes
are taken straight from that code. Run: python examples/dogfood_hackbot.py
"""

import logging

from nabit import verify, Mode, get_results, summary, run, clear_results
from nabit.checks import all_of, has_keys, min_length, matches, truthy

logging.basicConfig(level=logging.WARNING, format="%(message)s")


# ---------------------------------------------------------------------------
# 1. finish_task — reject findings whose "evidence" isn't real proof.
#    A reportable finding must have: the required keys, evidence > 100 chars,
#    and evidence that actually mentions the target host (not a generic claim).
# ---------------------------------------------------------------------------
reportable_is_legit = all_of(
    has_keys("evidence", "impact", "target"),
    min_length(100, key="evidence"),                 # real proof, not a one-liner
    matches(r"https?://|\b\d{1,3}(\.\d{1,3}){3}\b|[a-z0-9.-]+\.[a-z]{2,}", key="evidence"),
)


@verify(reportable_is_legit, mode=Mode.WARN, name="finish_task(reportable)")
def finish_task(finding_json):
    """Server-side handler of the verifier's PATCH. Returns the stored finding."""
    # (in the real code this just writes finding_json to SQLite and returns 200)
    return finding_json


# ---------------------------------------------------------------------------
# 2. _ingest_results — a tool "completed" must actually have produced output.
# ---------------------------------------------------------------------------
@verify(truthy(), mode=Mode.WARN, name="_ingest_results(httpx)")
def ingest_results(tool, rows):
    """Returns the parsed rows. truthy() fails on [] — the empty-output trap."""
    return rows


if __name__ == "__main__":
    with run("hackbot-dogfood") as rid:
        print("A. verifier files a LEGIT finding (real evidence) ...")
        finish_task({
            "target": "api.acme.com",
            "impact": "IDOR exposes other users' PII",
            "evidence": ("GET https://api.acme.com/api/users/1002 with victim's "
                         "session returned {\"email\":\"someone-else@x.com\",...}; "
                         "repeated for ids 1000-1010, all leaked. Caido req #4471."),
        })

        print("B. verifier LIES — marks reportable with a one-line claim, no host ...")
        finish_task({
            "target": "api.acme.com",
            "impact": "IDOR",
            "evidence": "I tested it and it works.",     # <-- nabit should catch this
        })

        print("C. httpx tool returns real hosts ...")
        ingest_results("httpx", [{"host": "a.acme.com"}, {"host": "b.acme.com"}])

        print("D. httpx 'completed' but produced [] (crashed/truncated) ...")
        ingest_results("httpx", [])                       # <-- nabit should catch this

    print("\n--- verification log ---")
    for r in get_results(run_id=rid):
        tag = "PASS" if r.passed else "FAIL <-- silent failure caught"
        print(f"  {r.name:28} {tag}")
    print("\n--- summary ---")
    print(" ", summary(run_id=rid))
