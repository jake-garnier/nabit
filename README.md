# nabit — *nab your agent's silent failures*

Your LLM agent said it created the customer. Your database says otherwise. You
found out three days later from a support ticket.

**nabit** is a dead-simple verification layer for LLM agents. Your agent claims
it's done; `nabit` checks the *real system state*, tells you whether that was
true, and — if it wasn't — **re-runs the action to fix it.** One decorator.
Zero dependencies. Sync or async.

```python
from nabit import verify

@verify(lambda result, ctx: db.exists("customers", result["id"]),
        retries=2)                      # self-heal: re-run on failure
def create_customer(name):
    # agent / tool does the work and claims success
    return {"id": 42, "status": "created"}
```

If the agent returns `{"status": "created"}` but the row isn't in the database,
`nabit` catches the lie instead of letting a green dashboard hide it — then
retries the action up to `retries` times before giving up.

## How it's different

Most tools in this space either detect problems without fixing them, only check
the *text the model produced* (not whether the real action happened), or make you
stand up a backend to do it. nabit:

- **checks real side effects**, not output schema (vs Guardrails AI / Instructor)
- **closes the loop** — self-heal retries, not just detection (vs Drift / trace viewers)
- **verifies inline at runtime**, not in a postmortem (vs agent-coroner)
- **has zero dependencies and no backend** — it's a decorator, not a platform (vs COGEXT)

## Why this exists

Trace viewers (Langfuse, LangSmith, Helicone) answer *"what did the agent do?"*
really well. They don't answer *"was what it did actually correct?"*

The expensive failures are **semantic**, not technical: no exception thrown, the
tool call succeeded, and the output was still wrong. Those look identical to a
success at the log level. `nabit` is the layer that checks the claim against
reality.

## Install

```bash
pip install nabit
```

## Usage

### Post-conditions check reality, not the agent's word

A post-condition is `(result, context) -> bool`. `result` is what the function
returned; `context` is the bound call arguments (so you can compare inputs to
outputs). Return truthy if reality matches the claim.

```python
from nabit import verify

@verify(lambda result, ctx: ticket_store.is_closed(result["ticket_id"]))
def close_ticket(ticket_id):
    agent.act(f"close ticket {ticket_id}")
    return {"ticket_id": ticket_id, "status": "closed"}
```

### Modes — tune how loud failures are

```python
from nabit import verify, Mode

@verify(check, mode=Mode.WARN)    # log a warning, keep running  (default — safe for prod)
@verify(check, mode=Mode.RAISE)   # raise VerificationError      (great for tests / CI)
@verify(check, mode=Mode.LOG)     # info-level log only
@verify(check, mode=Mode.SILENT)  # record only; inspect later
```

### Self-heal — re-run the action when verification fails

```python
def feedback(attempt, last_result, ctx):
    # optional: nudge the agent before the next attempt
    log.warning("verification failed on attempt %d, retrying", attempt)

@verify(check, retries=3, backoff=0.5, on_retry=feedback)
def book_flight(req):
    ...
```

`retries` re-runs the whole action up to N times until the post-condition
passes; `backoff` adds linear delay between attempts; `on_retry` lets you feed
the discrepancy back to the agent. Detection *and* correction, in one decorator.

### Composable checks (no hand-written lambdas)

```python
from nabit import verify
from nabit.checks import all_of, has_keys, field_equals, file_fresh, http_ok, truthy

@verify(all_of(has_keys("id", "status"), field_equals("status", "created")))
def create(...): ...

@verify(file_fresh(lambda r, c: r["path"], max_age_s=60))   # cron wrote a fresh file?
def nightly_report(): ...

@verify(http_ok(lambda r, c: r["url"]))                     # deployed URL is live?
def deploy(): ...

@verify(truthy())                                           # not [] / "" / None
def search(...): ...
```

### Group verifications under a run id

```python
from nabit import run, summary

with run("signup-flow") as rid:
    create_customer(...)
    charge_card(...)

print(summary(run_id=rid))
# {'total': 2, 'passed': 1, 'failed': 1, 'pass_rate': 0.5, 'failures': ['charge_card']}
```

Solves the "nothing shares a run id" problem — all the checks for one logical
task carry the same id.

### Tee results anywhere (pluggable sinks, still zero-dep)

```python
import nabit
from nabit import jsonl_sink

nabit.add_sink(jsonl_sink("verifications.jsonl"))   # built-in

def otel_sink(r):                                   # or your own, 3 lines
    span.set_attribute("nabit.passed", r.passed)
nabit.add_sink(otel_sink)
```

### LangGraph / LangChain

nabit works with any framework via the decorator, but LangGraph users get a
first-class adapter (lazily imported — installing nabit never pulls in
LangChain). Two options:

**Passive callback** — add it once, verify every tool result, no restructure:

```python
from nabit.adapters.langgraph import NabitCallback
from nabit.checks import has_keys, truthy

cb = NabitCallback(checks={
    "create_customer": has_keys("id"),
    "search_hotels": truthy(),        # catches the empty-list silent failure
})
graph.invoke(state, config={"callbacks": [cb]})
print(cb.summary())
```

**Node wrapper** — wrap a node to get full self-heal (a node is just a function,
so it can be re-run):

```python
from nabit.adapters.langgraph import verify_node
from nabit.checks import predicate

builder.add_node("book", verify_node(
    book_node,
    predicate(lambda update, ctx: db.has_booking(update["booking_id"])),
    retries=2,
))
```

### Async works the same way

```python
@verify(pc, retries=2)                    # async funcs + async post-conditions
async def create_order(cart):
    ...
```

### Inspect what happened

```python
from nabit import get_results

for r in get_results():
    print(r.name, "PASS" if r.passed else "FAIL",
          f"{r.duration_ms:.0f}ms", f"attempts={r.attempts}", r.error)
```

### Also catches "it threw but the side effect still happened"

By default (`on_error=True`) the post-condition runs even when the wrapped
function raises — so a tool that errors *after* mutating state (or succeeds in
reality despite throwing) still gets verified. The original exception is
re-raised after recording.

## Scope

`nabit` is the **outcome verifier**: it answers *"did this specific action
actually happen?"* and corrects it when it didn't. It deliberately does **not**
try to be an observability platform. For *fleet-wide* behavioral monitoring —
real-time degradation detection across many runs (step-count blowups, token
spikes, slow drift over hundreds of runs), trajectory snapshot diffing, and a
dashboard — see **[The Production Agent Reliability Kit](https://jakegarnier.com/agent-reliability-kit)**,
built on published research
([SENTINEL](https://github.com/jake-garnier/sentinel), self-supervised anomaly
detection for LLM agents).

Rule of thumb: use `nabit` to verify *one action's* real effect inline; reach
for the Kit when you need to watch *patterns across runs* over time.

## License

MIT — see [LICENSE](LICENSE). Use it anywhere, including commercially.

---

Built by [Jake Garnier](https://jakegarnier.com).
