"""RED: the peer-completion watcher must requeue foreign events.

STATUS: this suite is expected to FAIL until
gateway/run.py::_peer_completion_watcher exists. It is committed red on
purpose — a started artifact a cold tick can advance, rather than an
instruction a cold tick cannot originate from.

THE PROPERTY UNDER TEST
process_registry.completion_queue is MULTI-CONSUMER WITH NO ROUTING.
_async_delegation_watcher (gateway/run.py:27895) drains it and requeues
anything whose type is not "async_delegation" — see its comment: "We must
NOT consume watch/completion events here (other drains own them), so
requeue anything that isn't ours."

A peer-completion watcher that does not do the same will SILENTLY EAT
delegation events. Delegation then "just stops working sometimes" with no
error anywhere, because a consumed event is indistinguishable from an
event that was never queued.

WHY THIS CASE FIRST, and not the delivery path: violating it produces no
exception, no log line, and no failing assertion anywhere else in the
tree. It is the one property that cannot be retrofitted by observation
after the fact.

SUBJECT DECLARATION — asserted before any verdict counts.
"""
import os
import queue
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Repo root is two levels up from tests/gateway/, not one. My first version
# used dirname(HERE) — which resolved to tests/ — and S1 correctly reported
# the subject absent rather than skipping. The guard did its job on its
# own author.
TREE = os.environ.get("WATCHER_TREE",
                      os.path.dirname(os.path.dirname(HERE)))

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if detail and not ok:
        print(f"        {detail}")


print("=" * 70)
print("RED SUITE: peer-completion watcher requeues foreign events")
print(f"  host        : {os.uname().nodename}")
print(f"  interpreter : {sys.executable}")
print(f"  tree        : {TREE}")
print("=" * 70)

# ---- S: SUBJECT PREREQUISITES ---------------------------------------
# The watcher lives in its own GatewayRunner mixin (gateway/run_peer_completion.py); the model
# watcher it copies lives in gateway/run_notifications.py.
run_py = os.path.join(TREE, "gateway", "run_peer_completion.py")
model_py = os.path.join(TREE, "gateway", "run_notifications.py")
check("S1 gateway/run_peer_completion.py exists in the declared tree", os.path.isfile(run_py),
      f"absent: {run_py} — a missing subject is a FAILURE, not a skip")
if not os.path.isfile(run_py):
    print("\nSUBJECT ABSENT. Verdict withheld.")
    sys.exit(2)

src = open(run_py, encoding="utf-8").read()
model_src = open(model_py, encoding="utf-8").read() if os.path.isfile(model_py) else ""

check("S2 the model watcher is present (we are modelling on real code)",
      "_async_delegation_watcher" in model_src,
      "_async_delegation_watcher not found — wrong tree?")

# ---- A: THE SUBJECT MUST EXIST AT ALL --------------------------------
# This is the case that is RED today.
check("A1 _peer_completion_watcher is defined",
      "_peer_completion_watcher" in src,
      "NOT IMPLEMENTED — this is the expected red. Define "
      "_peer_completion_watcher in gateway/run_peer_completion.py.")

# Spawning is table-driven (run_startup.py::_POST_RECONNECT_WATCHERS): assert membership of the
# table the startup path actually iterates, not a source substring.
_spawned = False
try:
    sys.path.insert(0, TREE)
    from gateway.run import GatewayRunner as _GR  # type: ignore
    _spawned = "_peer_completion_watcher" in getattr(_GR, "_POST_RECONNECT_WATCHERS", ())
except Exception:  # pragma: no cover
    _spawned = False
check("A2 it is spawned as a supervised task",
      _spawned,
      "NOT WIRED — a watcher that is never spawned is a gate after an "
      "early return (lesson 52). Defining it is not enough.")

# ---- B: THE REQUEUE PROPERTY, driven on a real queue -----------------
# Import is deferred so A1/A2 report honestly even when the module will
# not import. A raised import is an ERROR, never folded into a False.
drain = None
import_error = None
if "_peer_completion_watcher" in src:
    try:
        sys.path.insert(0, TREE)
        from gateway.run_peer_completion import _drain_peer_completions as drain  # type: ignore
    except Exception as exc:  # pragma: no cover
        import_error = f"{type(exc).__name__}: {exc}"

if drain is None:
    check("B1 foreign events survive a peer drain",
          False,
          import_error or "no _drain_peer_completions to exercise (expected red)")
    check("B2 peer events ARE consumed by a peer drain",
          False,
          import_error or "no _drain_peer_completions to exercise (expected red)")
    check("B3 NON-VACUITY: the drain saw a non-empty queue",
          False,
          import_error or "drain never ran")
else:
    q = queue.Queue()
    foreign = [
        {"type": "async_delegation", "id": "d1"},
        {"type": "watch", "id": "w1"},
        {"type": "process_complete", "id": "p1"},
    ]
    mine = [{"type": "peer_completion", "id": "pc1", "handoff_id": "h1"}]
    for e in foreign + mine:
        q.put(e)
    seeded = q.qsize()

    taken = drain(q)

    check("B3 NON-VACUITY: the drain saw a non-empty queue", seeded == 4,
          f"seeded {seeded}, expected 4")

    left = []
    while not q.empty():
        left.append(q.get_nowait())
    left_types = sorted(e["type"] for e in left)

    check("B1 foreign events survive a peer drain",
          left_types == ["async_delegation", "process_complete", "watch"],
          f"left behind: {left_types} — foreign events were EATEN")

    check("B2 peer events ARE consumed by a peer drain",
          [e["id"] for e in taken] == ["pc1"],
          f"drain returned {taken!r}")

    # B4 CONTROL — an arm that can FAIL INDEPENDENTLY of B1/B2 (lesson 49a).
    # If the drain is a no-op, B1 passes trivially; this catches that by
    # requiring the SAME call to both preserve foreign events and return
    # nothing when there is nothing of ours.
    q2 = queue.Queue()
    for e in [{"type": "watch", "id": "w2"},
              {"type": "async_delegation", "id": "d2"}]:
        q2.put(e)
    taken2 = drain(q2)
    left2 = []
    while not q2.empty():
        left2.append(q2.get_nowait())
    check("B4 CONTROL: foreign-only queue yields nothing, loses nothing",
          taken2 == [] and sorted(e["type"] for e in left2)
          == ["async_delegation", "watch"],
          f"taken={taken2!r} left={left2!r}")

print()
failed = [r for r in results if not r[1]]
print("=" * 70)
print(f"cases: {len(results)}   failed: {len(failed)}")
if failed:
    print("EXPECTED RED until _peer_completion_watcher lands:")
    for name, _ok, _d in failed:
        print(f"    {name}")
print("=" * 70)


def test_requeue_foreign_events():
    """pytest entry — asserts on collected failures, so 'no tests ran'
    cannot masquerade as green."""
    assert results, "no cases executed — a count is part of the verdict"
    assert not failed, f"{len(failed)} case(s) failed: {[f[0] for f in failed]}"


if __name__ == "__main__":
    sys.exit(1 if failed else 0)
