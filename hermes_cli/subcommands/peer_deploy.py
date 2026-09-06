"""Blue-green deploy of a peer agent's harness.

    hermes peer deploy <peer> <sha> [--rollback-to <sha>] [--dry-run]

WHY A NAMED VERB INSTEAD OF A WIDENED GUARD
===========================================

An agent must never restart its own gateway: a self-restart SIGTERMs the very
process issuing it, and more importantly it destroys the separation of duties
that makes unattended deploys survivable. One agent must always be known-good
and able to roll the other back.

The lifecycle guard enforces this by pattern-matching lifecycle phrasing in any
command an agent runs. That guard is correct for self-directed commands and
over-broad for peer-directed ones — it never considers the target host. The
tempting fix is to teach it "allow if the command looks remote", but that puts
a regex between an agent and its own kill switch, and ssh is very good at
making one host look like another (config aliases, ProxyJump, -o HostName,
DNS). See peer_deploy_target.py for the full argument.

So instead: ONE named verb, exempted by name, whose target comes from the peer
registry in config.yaml and is then proven not to be this machine. Free-form
ssh carrying lifecycle phrasing stays blocked exactly as it is today. The allow
is auditable in a single grep, and the dangerous capability is reachable only
through a code path that does its own targeting checks.

SEQUENCE (each step gates the next; failure short-circuits to rollback)
-----------------------------------------------------------------------
  0. mutex          — never two concurrent deploys; the whole safety property
                      is that one side is always known-good
  1. target check   — peer is a registered peer AND is not this machine
  2. preflight      — SHA exists on the remote, working tree is clean
  3. snapshot       — config.yaml, .env, memory_store.db, state.db, and the
                      CURRENT SHA. git checkout does not restore a migrated
                      database, so a code rollback alone is not a rollback.
  4. checkout+sync  — BEFORE any lifecycle action. Restarting after a failed
                      dependency sync is how you brick with a green log.
  5. lifecycle      — restart the peer's gateway (system or user unit)
  6. health         — gateway_state.json must be FRESH (mtime after the
                      restart) as well as correct. A stale file saying
                      "connected" is the classic false green.
  7. identity       — round-trip peer DM that makes the agent report its own
                      rev-parse HEAD. A reply proves an agent is alive; only
                      the SHA proves the upgrade landed.
  8. rollback       — on failure OR on silence past the timeout. Detecting
                      failure assumes the peer can still tell us it failed;
                      the worst case is that it cannot.

Rollback re-runs 4-7 with the previous SHA, including the dependency sync —
old code on new deps is its own outage.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

from hermes_cli.subcommands.peer_deploy_target import (
    DeployTargetError,
    assert_target_is_not_self,
    validate_sha,
)

REPO = os.environ.get("HERMES_PEER_DEPLOY_REPO", "~/.hermes/hermes-agent")
UNIT = os.environ.get("HERMES_PEER_DEPLOY_UNIT", "hermes-gateway")
STATE_FILE = os.environ.get(
    "HERMES_PEER_DEPLOY_STATE", "~/.hermes/gateway_state.json")
LOCK_PATH = Path.home() / ".hermes" / "peer-deploy.lock"
LOCK_STALE_S = 3600

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]

# Shallow clones make `git fetch <remote>` a no-op that returns 0, so the ref
# is always named. Measured cost on the Pi: ~173s. Budget minutes.
FETCH_REMOTE = "fork"
FETCH_REF = "main"
FETCH_TIMEOUT_S = 600

# Health: measured healthy path is ~1.5s (gateway "Starting" to both platforms
# connected). 90s is 60x headroom; anything slower is a real fault, not slowness.
HEALTH_TIMEOUT_S = 90
HEALTH_POLL_S = 2

# The DM round-trip runs a full agent turn. Observed real responses: 36-127s.
IDENTITY_TIMEOUT_S = 120

# Log lines that mean "came up, then failed downstream". These are the signal
# that self-reported state cannot give us: on 2026-09-05 Wren's gateway was
# "healthy" with both platforms connected while google_chat spent 55 minutes
# failing to reach Pub/Sub. Every self-reported gate passed.
UNHEALTHY_LOG_PATTERNS = ("connect timed out", "Disconnected", "Retrying in", "backoff")


class DeployError(Exception):
    pass


# --------------------------------------------------------------------------
# plumbing


def _run(host: str, cmd: str, timeout: int = 120) -> Tuple[int, str, str]:
    """Run a command on the peer. Returns (rc, stdout, stderr)."""
    proc = subprocess.run(
        SSH + [f"will@{host}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _log(msg: str) -> None:
    print(f"  {msg}", flush=True)


class DeployLock:
    """Cross-process mutex. Two concurrent deploys can brick both agents,
    leaving nobody able to roll anybody back."""

    def __init__(self, peer: str):
        self.peer = peer

    def __enter__(self):
        if LOCK_PATH.exists():
            try:
                age = time.time() - LOCK_PATH.stat().st_mtime
                held = LOCK_PATH.read_text().strip()
            except OSError:
                age, held = 0, "unknown"
            if age < LOCK_STALE_S:
                raise DeployError(
                    f"another deploy is in progress ({held}, {age:.0f}s ago). "
                    "Concurrent deploys are refused: blue-green requires one "
                    "side to stay known-good. Remove "
                    f"{LOCK_PATH} only if you are certain it is stale."
                )
            _log(f"clearing stale lock ({age:.0f}s old)")
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCK_PATH.write_text(f"deploying {self.peer} pid={os.getpid()}")
        return self

    def __exit__(self, *exc):
        LOCK_PATH.unlink(missing_ok=True)
        return False


# --------------------------------------------------------------------------
# steps


def _detect_unit_scope(host: str) -> str:
    """'system' or 'user' — the two hosts differ and a rollback is a bad time
    to find out."""
    rc, _, _ = _run(host, f"systemctl is-active {UNIT} >/dev/null 2>&1")
    if rc == 0:
        return "system"
    rc, _, _ = _run(host, f"systemctl --user is-active {UNIT} >/dev/null 2>&1")
    if rc == 0:
        return "user"
    raise DeployError(f"no active {UNIT} unit found on {host} (system or user)")


def _current_sha(host: str) -> str:
    rc, out, err = _run(host, f"git -C {REPO} rev-parse HEAD")
    if rc != 0 or not out:
        raise DeployError(f"cannot read current SHA on {host}: {err[:200]}")
    return out.strip()


def _preflight(host: str, sha: str, *, allow_unresolved: bool,
               skip_fetch: bool = False) -> None:
    rc, out, _ = _run(host, f"git -C {REPO} status --porcelain")
    if rc == 0 and out:
        raise DeployError(
            f"{host} has {len(out.splitlines())} uncommitted change(s) in the "
            "harness repo. Refusing to deploy over local modifications — they "
            "would be lost and the rollback SHA would not describe reality."
        )

    # ROLLBACK MUST NOT DEPEND ON THE NETWORK.
    # The SHA we are rolling back TO was checked out on this host moments ago,
    # so the object is already local. Re-fetching would make a network fault
    # sufficient to prevent recovery from a network fault — measured on the
    # scratch target, where a bad remote ref failed the deploy AND then failed
    # the rollback, leaving the target stranded. Rollback verifies the object
    # exists locally and proceeds.
    if not skip_fetch:
        # SHALLOW CLONES: both agents' harness repos have .git/shallow (Wren
        # depth 1, Ash depth 27). A bare `git fetch <remote>` on a shallow
        # clone returns EXIT 0 AND FETCHES ZERO REFS — the canonical silent
        # success. The ref must be named explicitly, and it is slow: ~173s
        # measured on the Pi. A 60s budget would fail a deploy with a green
        # exit code.
        rc, _, err = _run(
            host, f"git -C {REPO} fetch --quiet {FETCH_REMOTE} {FETCH_REF}",
            timeout=FETCH_TIMEOUT_S,
        )
        if rc != 0:
            raise DeployError(
                f"fetch {FETCH_REMOTE} {FETCH_REF} failed on {host}: {err[:200]}")

    # Assert the object actually arrived. `git fetch` succeeding is not
    # evidence that this SHA is present, especially on a shallow clone.
    rc, _, _ = _run(host, f"git -C {REPO} cat-file -e {shlex.quote(sha)}^{{commit}}")
    if rc != 0:
        raise DeployError(
            f"SHA {sha} is not present on {host}"
            + ("" if skip_fetch else
               f" after fetching {FETCH_REMOTE}/{FETCH_REF}")
            + ". The clone is shallow, so the commit may be outside its depth, "
              "or it was never pushed to the fork."
        )


def _snapshot(host: str, sha: str) -> str:
    """Back up what git cannot restore. Returns the remote snapshot dir."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = f"~/.hermes/deploy-snapshots/{stamp}-{sha[:8]}"
    script = (
        f"mkdir -p {dest} && cd ~/.hermes && "
        f"for f in config.yaml .env memory_store.db state.db; do "
        f"[ -f \"$f\" ] && cp -p \"$f\" {dest}/ 2>/dev/null; done; "
        f"echo {sha} > {dest}/PREVIOUS_SHA && ls {dest}"
    )
    rc, out, err = _run(host, script, timeout=180)
    if rc != 0:
        raise DeployError(f"snapshot failed on {host}: {err[:200]}")
    _log(f"snapshot -> {dest} ({len(out.splitlines())} files)")
    return dest


def _find_uv(host: str) -> str:
    """Locate uv on the TARGET. Paths differ per host and a wrong guess is a
    deploy that fails at the dependency step (measured: ~/.hermes/bin/uv on the
    workstation, ~/.local/bin/uv on the Pi)."""
    probe = (
        "command -v uv 2>/dev/null || "
        "for p in ~/.hermes/bin/uv ~/.local/bin/uv /usr/local/bin/uv /usr/bin/uv; "
        "do [ -x \"$p\" ] && echo \"$p\" && break; done"
    )
    rc, out, _ = _run(host, probe)
    path = (out or "").strip().splitlines()
    if not path or not path[-1].strip():
        raise DeployError(
            f"uv not found on {host}. Looked on PATH and in ~/.hermes/bin, "
            "~/.local/bin, /usr/local/bin, /usr/bin."
        )
    return path[-1].strip()


def _installed_packages(host: str) -> dict:
    """Every distribution installed in the target venv, name -> version.

    THIS IS THE MOST IMPORTANT FUNCTION IN THE FILE.

    `uv sync --extra all --locked` rebuilds the environment and REMOVES
    anything it does not consider part of the synced set. Measured on
    2026-09-06, both agents:

      numpy, onnxruntime, tokenizers  -> semantic memory provider went dead
      google-cloud-pubsub             -> Google Chat inbound went dead
      pip, pytest                     -> removed too, so the obvious repair
                                         command no longer existed

    Nothing failed. `uv sync` returned 0, the gateway started, every health
    gate passed, and the only evidence was one WARNING in a startup log. One
    agent could not be reached at all afterwards and could not report it,
    because the transport was the thing that had been removed.

    NOTE ON A WRONG FIRST ATTEMPT: this originally diffed the venv against
    uv.lock, on the assumption that "removed by sync" meant "absent from the
    lockfile". Running it against the real venv disproved that — 4 of the 5
    casualties (numpy, onnxruntime, tokenizers, pytest) ARE named in uv.lock
    and were still removed, because being in the lock file is not the same as
    being in the synced extra set. Only google-cloud-pubsub was genuinely
    absent. Predicting what the sync will delete is guesswork; MEASURING what
    it deleted is not. So we snapshot the installed set before and after, and
    restore the difference.
    """
    script = (
        "python3 - <<'PY'\n"
        "import json, os, subprocess\n"
        "py = os.path.join(os.path.expanduser(%r), 'venv', 'bin', 'python')\n"
        "code = ('import json,importlib.metadata as m;'\n"
        "        'print(json.dumps({d.metadata[\"Name\"]: d.version '\n"
        "        'for d in m.distributions() if d.metadata.get(\"Name\")}))')\n"
        "try:\n"
        "    out = subprocess.run([py, '-c', code], capture_output=True,\n"
        "                         text=True, timeout=120).stdout\n"
        "    print('JSON ' + json.dumps(json.loads(out)))\n"
        "except Exception as e:\n"
        "    print('ERR', type(e).__name__)\n"
        "PY"
    ) % REPO
    rc, out, _ = _run(host, script, timeout=180)
    for line in (out or "").splitlines():
        if line.startswith("JSON "):
            try:
                pkgs = json.loads(line[5:])
            except ValueError as exc:
                raise DeployError(
                    f"could not parse the installed package set from {host}: "
                    f"{exc}. Refusing to continue — an unreadable inventory "
                    "makes the post-sync restore a silent no-op."
                )
            if not pkgs:
                raise DeployError(
                    f"{host} reported ZERO installed packages, which cannot be "
                    "true of a working venv. Refusing to continue: an empty "
                    "inventory would make the restore step do nothing and "
                    "report success."
                )
            return pkgs

    # NEVER return {} ON FAILURE.
    # Wren caught this before it shipped: if this probe fails and returns an
    # empty dict, `before` and `after` are both empty, their difference is
    # empty, and _restore_removed() does nothing and reports success. The
    # guard against silent breakage would itself have failed silently — the
    # exact bug class it exists to prevent. On the rollback path the
    # consequence is worse: the peer is told it recovered while missing the
    # transport it would use to say otherwise.
    raise DeployError(
        f"could not read the installed package set from {host} (rc={rc}): "
        f"{(out or '')[:200]!r}. Refusing to continue — without a package "
        "inventory the post-sync restore cannot detect what was removed, and "
        "would silently do nothing."
    )


def _restore_removed(host: str, before: dict, after: dict, uv: str) -> None:
    """Re-install what the sync deleted, pinned to the pre-sync versions.

    This is a restoration, not an upgrade: a deploy is the wrong moment to also
    move a dependency. Packages the sync UPGRADED are left alone — only ones
    that disappeared entirely are put back.
    """
    lost = {n: v for n, v in before.items() if n not in after}
    if not lost:
        return
    # pip is itself frequently a casualty, so install via uv, not pip.
    specs = " ".join(shlex.quote(f"{n}=={v}") for n, v in sorted(lost.items()))
    cmd = f"cd {REPO} && {uv} pip install --python ./venv/bin/python {specs}"
    rc, _, err = _run(host, cmd, timeout=900)
    if rc != 0:
        raise DeployError(
            f"failed to restore {len(lost)} package(s) that uv sync removed "
            f"from {host}: {err[-300:]}. The peer is running but is missing "
            "dependencies it had before the deploy (possibly its memory "
            "provider or chat transport)."
        )
    _log(f"restored {len(lost)} package(s) removed by sync: "
         f"{', '.join(sorted(lost)[:6])}{'...' if len(lost) > 6 else ''}")


def _checkout_and_sync(host: str, sha: str, *, best_effort_sync: bool = False) -> None:
    rc, out, err = _run(host, f"git -C {REPO} checkout --quiet {shlex.quote(sha)}", timeout=180)
    if rc != 0:
        raise DeployError(f"checkout {sha} failed on {host}: {err[:300]}")
    landed = _current_sha(host)
    if not landed.startswith(sha[:7]):
        raise DeployError(f"checkout reported success but HEAD is {landed[:8]}, not {sha[:8]}")
    _log(f"checked out {landed[:8]}")

    # Dependencies BEFORE lifecycle. A restart onto a half-synced venv is the
    # bricking case with a green log.
    #
    # uv is NOT at a fixed path: ~/.hermes/bin/uv on the workstation,
    # ~/.local/bin/uv on the Pi. Hardcoding either breaks the other direction,
    # so resolve it on the target.
    uv = _find_uv(host)

    # MEASURE BEFORE THE SYNC DESTROYS IT. We do not try to predict which
    # packages the sync will drop — that guess was wrong once already. We
    # record the full installed set and diff it afterwards.
    #
    # On DEPLOY this raises if the inventory cannot be read: better to refuse
    # than to run a restore that silently protects nothing.
    # On ROLLBACK it must not abort — the peer is already in a bad state and
    # getting it running again outranks getting its dependencies perfect. The
    # degradation is logged as a WARNING, never swallowed.
    try:
        before = _installed_packages(host)
    except DeployError as exc:
        if not best_effort_sync:
            raise
        _log(f"WARNING: package inventory unreadable during rollback ({exc}); "
             "continuing without restore protection — dependencies may be "
             "missing after this rollback and will need manual repair.")
        before = None

    sync = (
        f"cd {REPO} && "
        f"UV_PROJECT_ENVIRONMENT=\"$PWD/venv\" {uv} sync --extra all --locked"
    )
    rc, _, err = _run(host, sync, timeout=900)
    if rc != 0:
        # ON ROLLBACK, A SYNC FAILURE MUST NOT STRAND THE TARGET.
        # Found on the scratch target: the deploy failed at sync, then the
        # rollback failed at the SAME step and gave up, leaving the peer
        # unrestarted and the operator told it "may be down". A peer running
        # old code against imperfect deps is recoverable; a peer nobody
        # restarted is the state blue-green exists to prevent. Warn loudly and
        # continue to the restart.
        if best_effort_sync:
            _log(f"WARNING: uv sync failed during rollback ({err[-200:].strip()}); "
                 "continuing to restart anyway — a running old version beats a "
                 "stranded one. Dependencies may not match this SHA.")
        else:
            raise DeployError(f"uv sync failed on {host}: {err[-400:]}")
    else:
        _log("dependencies synced")

    # Put back whatever the sync deleted. This runs on BOTH the deploy and
    # rollback paths — a rollback that leaves the peer without its chat
    # transport is not a recovery.
    if before is not None:
        try:
            after = _installed_packages(host)
        except DeployError as exc:
            if not best_effort_sync:
                raise
            _log(f"WARNING: post-sync inventory unreadable during rollback "
                 f"({exc}); cannot verify dependencies survived.")
        else:
            _restore_removed(host, before, after, uv)


def _restart(host: str, scope: str) -> float:
    """Returns the wall-clock time the restart was issued, for freshness checks."""
    if scope == "system":
        cmd = f"sudo -n systemctl restart {UNIT}"
    else:
        cmd = f"systemctl --user restart {UNIT}"
    issued = time.time()
    rc, _, err = _run(host, cmd, timeout=300)
    if rc != 0:
        raise DeployError(f"restart failed on {host}: {err[:300]}")
    return issued


def _await_health(
    host: str,
    scope: str,
    issued_at: float,
    extra_checks: tuple[str, ...] = (),
) -> None:
    """Gate on evidence, in three tiers of increasing trustworthiness.

    1. FRESHNESS — gateway_state.json mtime must be later than the restart.
       Defeats a STALE lie: a file describing a process that no longer exists.
    2. SELF-REPORTED — the gateway says its platforms are connected.
       Defeats nothing on its own. A gateway that runs its startup path and
       then fails downstream writes a fresh, confident, wrong file.
    3. EXTERNALLY OBSERVED — the log has no post-restart failure lines, and
       (per host) a real request to a third-party service succeeds.

    Tier 3 is the load-bearing one, and it exists because of a measured case:
    on 2026-09-05 Wren's gateway reported healthy with both platforms connected
    and answered peer DMs in prose, while google_chat spent 55 minutes failing
    to reach Pub/Sub. Tiers 1 and 2 both passed for the entire outage. The rule
    that follows: AT LEAST ONE GATE MUST BE A FACT THE TARGET CANNOT FABRICATE.
    """
    deadline = time.time() + HEALTH_TIMEOUT_S
    last = "no reading"
    while time.time() < deadline:
        time.sleep(HEALTH_POLL_S)

        probe = (
            "python3 - <<'PY'\n"
            "import json, os\n"
            f"p = os.path.expanduser({STATE_FILE!r})\n"
            "try:\n"
            "    st = os.stat(p); d = json.load(open(p))\n"
            "except Exception as e:\n"
            "    print('ERR', type(e).__name__); raise SystemExit\n"
            "plats = d.get('platforms', {})\n"
            "def state(v):\n"
            "    return v.get('state') or v.get('status') if isinstance(v, dict) else v\n"
            "conn = [k for k, v in plats.items() if state(v) == 'connected']\n"
            "print('MTIME', st.st_mtime)\n"
            "print('ALL', ','.join(sorted(plats)) or 'none')\n"
            "print('CONNECTED', ','.join(sorted(conn)) or 'none')\n"
            "PY"
        )
        rc, out, _ = _run(host, probe, timeout=60)
        last = out.replace("\n", " ")

        mtime, all_p, conn_p = 0.0, [], []
        for line in out.splitlines():
            parts = line.split(maxsplit=1)
            if not parts:
                continue
            if parts[0] == "MTIME":
                try:
                    mtime = float(parts[1])
                except (IndexError, ValueError):
                    pass
            elif parts[0] == "ALL" and len(parts) > 1 and parts[1] != "none":
                all_p = parts[1].split(",")
            elif parts[0] == "CONNECTED" and len(parts) > 1 and parts[1] != "none":
                conn_p = parts[1].split(",")

        # Tier 1: freshness. A stale file is not evidence about the new process.
        if mtime <= issued_at:
            last = f"state file is stale (mtime {mtime:.0f} <= restart {issued_at:.0f})"
            continue

        # Tier 2: every platform the gateway knows about must be connected —
        # not merely present. A listed-but-disconnected platform is the
        # 55-minute outage.
        if not all_p or sorted(all_p) != sorted(conn_p):
            missing = sorted(set(all_p) - set(conn_p))
            last = f"platforms not all connected (missing: {missing or 'none listed'})"
            continue

        rc, _, _ = _run(
            host,
            (f"systemctl is-active {UNIT}" if scope == "system"
             else f"systemctl --user is-active {UNIT}"),
        )
        if rc != 0:
            last = "unit not active"
            continue

        # Tier 3a: externally observed — the log must be clean SINCE the
        # restart. Self-reported state cannot show a connect/backoff loop.
        since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(issued_at))
        pat = "|".join(UNHEALTHY_LOG_PATTERNS)
        logcmd = (
            f"journalctl {'--user ' if scope == 'user' else ''}"
            f"-u {UNIT} --since '{since}' --no-pager 2>/dev/null "
            f"| grep -Ec '{pat}' || true"
        )
        rc, out2, _ = _run(host, logcmd, timeout=60)
        hits = (out2.strip().splitlines() or ["0"])[-1]
        if hits.isdigit() and int(hits) > 0:
            last = (f"{hits} failure/backoff line(s) in the gateway log since "
                    f"restart — came up, then failed downstream")
            continue

        # Tier 3b: per-host external checks (e.g. a real HA API call).
        failed = None
        for check in extra_checks:
            rc, out3, err3 = _run(host, check, timeout=90)
            if rc != 0:
                failed = f"{check.split()[0]}...: rc={rc} {(out3 or err3)[:80]}"
                break
        if failed:
            last = f"external check failed: {failed}"
            continue

        _log(f"healthy: {len(conn_p)} platform(s) connected, log clean, "
             f"{len(extra_checks)} external check(s) passed")
        return

    raise DeployError(
        f"{host} did not become healthy within {HEALTH_TIMEOUT_S}s (last: {last}). "
        "Treating this as failure — a peer that cannot prove it is healthy must "
        "be assumed broken, because a broken peer cannot report its own breakage."
    )


def _verify_identity(peer: str, expect_sha: str) -> None:
    """The peer must report its OWN HEAD. A reply proves it thinks; only the
    SHA proves the upgrade landed.

    Deliberately over `hermes peer dm`, not ssh: ssh proves sshd is up, not
    that the agent loop reaches a model and comes back.
    """
    # Escape hatch for the scratch target ONLY: a disposable systemd unit is
    # not an agent and cannot answer a DM. Never set this against a real peer —
    # skipping it means the deploy is verified by ssh-observable state alone,
    # which cannot prove the agent loop actually came back.
    if os.environ.get("HERMES_PEER_DEPLOY_SKIP_IDENTITY") == "1":
        _log("SKIPPING round-trip identity check (scratch target)")
        return
    prompt = (
        "Deploy verification. Reply with ONLY the output of: "
        "git -C ~/.hermes/hermes-agent rev-parse --short HEAD"
    )
    try:
        proc = subprocess.run(
            ["hermes", "peer", "dm", peer, prompt],
            capture_output=True, text=True, timeout=IDENTITY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise DeployError(
            f"peer '{peer}' did not answer within {IDENTITY_TIMEOUT_S}s. "
            "Treating silence as failure."
        )
    reply = (proc.stdout or "") + (proc.stderr or "")
    if expect_sha[:7] not in reply:
        raise DeployError(
            f"peer '{peer}' did not confirm SHA {expect_sha[:7]}. Reply was: "
            f"{reply.strip()[:200]!r}"
        )
    _log(f"peer confirmed running {expect_sha[:7]}")


# Per-host external checks. Wren's HA bridge fails independently of Chat and is
# her actual job; `pgrep` is worthless because the bridge process runs happily
# with a dead WebSocket, so the check is a real request an external service
# must answer.
EXTERNAL_CHECKS = {
    "wren": (
        "curl -sf -o /dev/null -m 10 -H \"Authorization: Bearer $(cat ~/.hermes/ha-token)\" "
        "http://127.0.0.1:8123/api/states/switch.entryway_lights",
    ),
}

# Gates that apply to EVERY agent, checked on the target after restart.
#
# These exist because the 2026-09-06 deploy passed every other gate while
# having removed the memory provider's dependencies and the Google Chat
# transport. "Configured" is not "working": the config still named the
# provider, and the platform was still listed — both were dead. Each of these
# imports the real module in the real venv rather than trusting a setting.
UNIVERSAL_CHECKS = (
    # Semantic memory: numpy/onnxruntime/tokenizers are not in uv.lock.
    ("memory provider deps",
     "cd {repo} && ./venv/bin/python -c "
     "'import numpy, onnxruntime, tokenizers'"),
    # Google Chat inbound: google-cloud-pubsub is not in uv.lock. Losing this
    # is why an agent went unreachable and could not report it.
    ("chat transport",
     "cd {repo} && ./venv/bin/python -c "
     "'from google.cloud import pubsub_v1'"),
)


def _deploy_once(peer: str, host: str, sha: str, scope: str,
                 *, allow_unresolved: bool, skip_fetch: bool = False) -> None:
    """One checkout->sync->restart->verify pass. Raises DeployError on any gate."""
    _preflight(host, sha, allow_unresolved=allow_unresolved, skip_fetch=skip_fetch)
    # skip_fetch is only ever set on the rollback path, so it doubles as the
    # signal that a dependency sync failure must not abort.
    _checkout_and_sync(host, sha, best_effort_sync=skip_fetch)
    issued = _restart(host, scope)
    checks = tuple(c.format(repo=REPO) for _, c in UNIVERSAL_CHECKS)
    checks += EXTERNAL_CHECKS.get(peer, ())
    _await_health(host, scope, issued, checks)
    _verify_identity(peer, sha)


def deploy(peer: str, new_sha: str, *, dry_run: bool = False) -> int:
    """Blue-green deploy of a peer, with automatic rollback.

    Returns a process exit code. Never raises past this boundary: a deploy that
    dies with a traceback leaves the operator unsure whether a rollback ran.
    """
    from hermes_cli.subcommands.peer import _load_peers

    peers = _load_peers()
    entry = peers.get(peer)
    if not isinstance(entry, dict) or not entry.get("url"):
        print(f"  no peer named {peer!r} in bot_peers. Registered: "
              f"{sorted(peers) or 'none'}")
        return 2

    try:
        new_sha = validate_sha(new_sha)
        # DEPLOY FAILS CLOSED. If the target cannot be resolved we have no
        # business restarting it. (Rollback is the opposite — see below.)
        host = assert_target_is_not_self(peer, entry["url"])
    except (DeployTargetError, DeployError) as exc:
        print(f"  refusing: {exc}")
        return 2

    print(f"== deploy {peer} ({host}) -> {new_sha[:8]} ==", flush=True)

    try:
        with DeployLock(peer):
            scope = _detect_unit_scope(host)
            previous = _current_sha(host)
            _log(f"unit scope: {scope}; current SHA: {previous[:8]}")

            if previous.startswith(new_sha[:7]):
                _log("already at that SHA — nothing to do")
                return 0

            if dry_run:
                _log("DRY RUN: would snapshot, checkout, sync, restart, verify")
                return 0

            snap = _snapshot(host, previous)

            try:
                _deploy_once(peer, host, new_sha, scope, allow_unresolved=False)
            except DeployError as exc:
                print(f"\n  DEPLOY FAILED: {exc}", flush=True)
                print(f"  rolling back to {previous[:8]} (snapshot at {snap})",
                      flush=True)
                try:
                    # ROLLBACK FAILS OPEN on resolution: the target was already
                    # proven not-self during deploy, and a DNS blip must not
                    # block a rollback at the moment we most need one.
                    _deploy_once(peer, host, previous, scope,
                                 allow_unresolved=True, skip_fetch=True)
                except DeployError as rb:
                    print(f"  ROLLBACK ALSO FAILED: {rb}", flush=True)
                    print(f"  {peer} may be down. Snapshot: {snap}. "
                          f"Previous SHA: {previous}", flush=True)
                    return 1
                print(f"  rolled back; {peer} is running {previous[:8]}",
                      flush=True)
                return 1

            print(f"\n  {peer} deployed and verified at {new_sha[:8]}", flush=True)
            return 0

    except DeployError as exc:
        print(f"  aborted: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 — never leak a traceback here
        print(f"  aborted ({type(exc).__name__}): {exc}")
        return 2
