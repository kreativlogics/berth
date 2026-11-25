#!/usr/bin/env python3
"""berth claims.

  berth.py status [--json] [ENV ...]       Who holds each guarded environment, with lock refs.
  berth.py claim ENV [--hours N] [--reason TEXT]
                                           Claim an environment (at most max_claim_hours).
  berth.py extend ENV [--hours N]          Move your claim's end to N hours from now (same cap).
  berth.py release ENV                     Remove your own claim. Never removes someone else's.
  berth.py takeover ENV --ref REF [--yes] [--hours N] [--reason TEXT]
                                           Replace a stale lock (expired, or older than stale_after_hours with
                                           no end time). REF is the lock ref status shows. A person confirms.

Common: --data DIR, --project-dir DIR.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True
sys.path.insert(0, HERE)

from berth import cfg, claims, locks, store  # noqa: E402

LABELS = {
    locks.FREE: "free",
    locks.MINE: "yours",
    locks.MINE_EXPIRED: "yours, expired",
    locks.OTHER: "held",
    locks.OTHER_STALE: "held, stale",
    locks.AMBIGUOUS: "ambiguous",
}


def _service(args):
    data = args.data or os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(os.path.expanduser("~"), ".claude",
                                                                             "berth")
    project = args.project_dir or cfg.find_project(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    try:
        eff = cfg.resolve(data, project)
    except cfg.ConfigError as exc:
        sys.exit("Config error: %s" % exc)
    for note in eff.notes:
        print("Note: %s" % note, file=sys.stderr)
    return claims.Service(eff, project, data)


def cmd_status(svc, args):
    rows, glob = svc.status(args.envs or None)
    if args.json:
        print(json.dumps({"me": svc.me, "store": svc.store.describe(), "environments": rows, "global": glob},
                         indent=2, sort_keys=True))
        return
    print("You are %s (%s). Store: %s." % (svc.me, svc.me_source, svc.store.describe()))
    if not rows:
        print("No guarded environments. Add one with /berth:config (berthcfg.py set environments ...) or run "
              "berthcfg.py detect.")
    for row in rows:
        ref = "  ref %s" % row["ref"] if row["ref"] else ""
        label = LABELS[row["state"]]
        text = "" if row["text"] == label else row["text"]
        print(("  %-16s %-15s %s%s" % (row["env"], label, text, ref)).rstrip())
    if glob:
        print("  %-16s %-15s %s  ref %s" % ("(global)", LABELS[glob["state"]], glob["text"], glob["ref"]))


def cmd_claim(svc, args):
    print(svc.claim(args.env, args.hours, args.reason))


def cmd_extend(svc, args):
    print(svc.extend(args.env, args.hours))


def cmd_release(svc, args):
    print(svc.release(args.env))


def cmd_takeover(svc, args):
    if not args.yes:
        if not sys.stdin.isatty():
            sys.exit("takeover replaces someone else's lock. Run it again with --yes once a person has agreed.")
        answer = input("Replace the stale lock on %s (ref %s)? Type yes: " % (args.env, args.ref))
        if answer.strip().lower() != "yes":
            sys.exit("Nothing changed.")
    print(svc.takeover(args.env, args.ref, args.hours, args.reason))


def build_parser():
    p = argparse.ArgumentParser(prog="berth.py", allow_abbrev=False, description="Deploy claims on lock branches.")
    p.add_argument("--data")
    p.add_argument("--project-dir")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("status", allow_abbrev=False)
    s.add_argument("envs", nargs="*")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("claim", allow_abbrev=False)
    s.add_argument("env")
    s.add_argument("--hours", type=float)
    s.add_argument("--reason")
    s = sub.add_parser("extend", allow_abbrev=False)
    s.add_argument("env")
    s.add_argument("--hours", type=float)
    s = sub.add_parser("release", allow_abbrev=False)
    s.add_argument("env")
    s = sub.add_parser("takeover", allow_abbrev=False)
    s.add_argument("env")
    s.add_argument("--ref", required=True)
    s.add_argument("--yes", action="store_true")
    s.add_argument("--hours", type=float)
    s.add_argument("--reason")
    for sp in sub.choices.values():
        sp.add_argument("--data", default=argparse.SUPPRESS)
        sp.add_argument("--project-dir", default=argparse.SUPPRESS)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 2
    if getattr(args, "reason", None) is not None and len(args.reason) > 200:
        sys.exit("Keep the reason under 200 characters.")
    svc = _service(args)
    handler = globals()["cmd_" + args.cmd]
    try:
        handler(svc, args)
    except claims.ClaimError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except store.StoreError as exc:
        print("The claim store failed: %s" % exc, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
