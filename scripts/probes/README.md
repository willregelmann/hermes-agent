# Standalone probes

These are **probes, not pytest tests**. Run them directly:

```bash
python scripts/probes/peer_completion_e2e.py
```

They live outside `tests/` on purpose. Two reasons, both load-bearing.

## 1. They assert on source text, which is banned inside `tests/`

Each of these opens a `.py` file and asserts on its contents:

```python
api_src = open(api_py, encoding="utf-8").read()
```

`AGENTS.md` bans that outright under *"Never read source code in tests"* — such a
check passes when the implementation is subtly broken (the regex matches a
mis-wired call site), fails on correct refactors, and blocks structural cleanup.
As a **probe** the same read is honest: it is explicitly a statement about the
shape of the source at a moment in time, not a behaviour contract.

## 2. They have a three-state verdict that pytest cannot express

```python
if not (os.path.isfile(run_py) and os.path.isfile(api_py)):
    print("SUBJECT ABSENT. Verdict withheld.")
    sys.exit(2)
```

* exit 0 — every case passed
* exit 1 — a case failed
* **exit 2 — the subject was not found, so no verdict was reached**

That third state is the point. A pytest skip says "not run"; it does not
distinguish "the thing I measure does not exist here" from "I chose not to
measure". Withholding a verdict is a real outcome and these probes report it.

## Why they are not under `tests/` even in a subdirectory

A module-scope `sys.exit()` raises `SystemExit` **during collection**. Pytest
reports `INTERNALERROR` and `no tests ran` — and it aborts the *entire
directory*, not just the offending file. Collecting `tests/gateway/` printed
`420 tests collected` and then died with `SystemExit: 0`, a green self-check
taking the whole suite down with it.

A `tests/.../probes/` subdirectory would not fix that: it is still one
`pytest tests/` from the repo root away from being collected again, and the next
person to run the suite rediscovers the same abort. Outside the pytest rootdir
path, the exit codes are a feature rather than a hazard.

## Do not "fix" these by adding `if __name__ == "__main__":`

That silences the collection abort and leaves three source-reading pseudo-tests
collected and **green** under pytest — converting a loud failure into exactly
the false confidence the source-reading ban exists to prevent. A green
collection is not the goal.
