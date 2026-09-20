"""``hermes dream`` — dream once, and read the dreams back (docs/capabilities/dreaming.md)."""

from __future__ import annotations

import json


def _dream_run(args) -> int:
    from hermes_cli.dreams import dream_once

    say = (lambda *a, **k: None) if getattr(args, "json", False) else print
    dream = dream_once(seed=getattr(args, "seed", None), print_fn=say)
    if getattr(args, "json", False):
        print(json.dumps(dream.index_row(), ensure_ascii=False))
    else:
        print(f"\n{dream.seed}\n")
        print(f"— {dream.dreamer or 'it'} said: {dream.final_response or '(nothing)'}\n")
    return 1 if dream.error else 0


def _dream_list(args) -> int:
    from hermes_cli.dreams import list_dreams

    rows = list_dreams(limit=int(getattr(args, "limit", 20) or 20))
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print("No dreams yet.")
        return 0
    for row in rows:
        print(f"{row.get('dream_id', '?'):<28} {row.get('at', ''):<26} "
              f"{row.get('tool_calls', 0):>3} calls  {row.get('seed', '')[:60]}")
    return 0


def _dream_show(args) -> int:
    from hermes_cli.dreams import read_dream

    text = read_dream(str(getattr(args, "dream_id", "latest") or "latest"))
    if not text:
        print("No such dream.")
        return 1
    print(text)
    return 0


_DREAM_ACTIONS = {"run": _dream_run, "list": _dream_list, "ls": _dream_list,
                  "show": _dream_show, "read": _dream_show, None: _dream_list}


def cmd_dream(args) -> int:
    return _DREAM_ACTIONS[getattr(args, "dream_action", None)](args)


def build_dream_parser(subparsers) -> None:
    """Attach the ``dream`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "dream", help="Dream once: an ephemeral turn where every tool result is invented",
        description="A dream is one turn in a session that is thrown away. The agent's model, "
                    "system prompt and tools are its own; every tool RESULT is invented by a small "
                    "model, and nothing it does reaches anything real. The log under "
                    "<hermes home>/dreams/ is all that survives — read it back to see how the "
                    "agent behaved in a world that answered without being real.",
        epilog=("Examples:\n"
                "  hermes dream run\n"
                '  hermes dream run --seed "You are in the server room and the fans have stopped"\n'
                "  hermes dream list\n"
                "  hermes dream show latest\n"),
        formatter_class=__import__("argparse").RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest="dream_action")
    run = actions.add_parser("run", help="Dream once and write the log")
    run.add_argument("--seed", default=None, help="Open on this scene instead of a generated one")
    run.add_argument("--json", action="store_true", help="Emit the index row instead of the dream")
    listing = actions.add_parser("list", aliases=["ls"], help="List dreams, newest first")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--json", action="store_true")
    show = actions.add_parser("show", aliases=["read"], help="Print one dream's log")
    show.add_argument("dream_id", nargs="?", default="latest", help="Dream id, or 'latest'")
    parser.set_defaults(func=cmd_dream)
