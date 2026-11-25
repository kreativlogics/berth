#!/usr/bin/env python3
"""berth settings.

  berthcfg.py show [--json]                 Effective settings and where each came from.
  berthcfg.py explain KEY                   What a key does and its current value.
  berthcfg.py set KEY VALUE [--project]     Change your config (or the project's with --project).
                                            environments.NAME PATTERN adds a deploy pattern for NAME.
  berthcfg.py unset KEY [--project]         Remove a key (environments.NAME removes one environment).
  berthcfg.py approve store                 Approve the project config's store.
  berthcfg.py detect [--json]               Suggest environments and patterns from CI files and deploy scripts.
  berthcfg.py init [--project] [--preset P] [--yes]
                                            Write the detected environments. Without --yes it only prints.
  berthcfg.py doctor                        Check the install, config, store and hook timing.

Common: --data DIR, --project-dir DIR. set, unset, approve and init --yes are confirmed by a person (the hook asks).
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True
sys.path.insert(0, HERE)

from berth import approve, cfg, claims, discover, store  # noqa: E402

PLUGIN_ROOT = os.path.dirname(HERE)


def _paths(args):
    data = args.data or os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(os.path.expanduser("~"), ".claude",
                                                                             "berth")
    project = args.project_dir or cfg.find_project(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    return data, project


def _resolve(data, project):
    try:
        return cfg.resolve(data, project)
    except cfg.ConfigError as exc:
        sys.exit("Config error: %s" % exc)


def cmd_show(args):
    data, project = _paths(args)
    eff = _resolve(data, project)
    if args.json:
        print(json.dumps({"values": eff.values, "sources": eff.sources, "notes": eff.notes,
                          "pending_approval": eff.pending_approval}, indent=2, sort_keys=True))
        return
    print("Files: user %s | project %s" % (cfg.user_config_path(data), cfg.project_config_path(project)))
    for key in ("preset", "fail_mode", "unclaimed", "stop", "max_claim_hours", "stale_after_hours",
                "generic_patterns", "identity", "store"):
        print("%-18s %-40s (%s)" % (key, json.dumps(eff.get(key)), eff.sources.get(key)))
    envs = eff.get("environments") or {}
    print("environments       (%s)" % eff.sources.get("environments"))
    if not envs:
        print("  none yet: run detect, or set environments.<name> <pattern>")
    for name in sorted(envs):
        print("  %s" % name)
        for pat in envs[name]["patterns"] or ["(generic rule only)"]:
            print("    %s" % pat)
    for note in eff.notes:
        print("Note: %s" % note)


def cmd_explain(args):
    data, project = _paths(args)
    eff = _resolve(data, project)
    key = args.key.split(".", 1)[0]
    if key not in cfg.DESCRIPTIONS:
        sys.exit("Unknown key %r.%s" % (args.key, cfg._hint(key, list(cfg.SCHEMA))))
    print("%s: %s" % (key, cfg.DESCRIPTIONS[key]))
    print("Now: %s (from %s)" % (json.dumps(eff.get(key)), eff.sources.get(key)))


def _parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _layer_path(args, data, project):
    return cfg.project_config_path(project) if args.project else cfg.user_config_path(data)


def _save(path, layer, data, project, what):
    try:
        cfg.write_layer(path, layer)
    except cfg.ConfigError as exc:
        sys.exit("Not saved: %s" % exc)
    print("Saved %s in %s." % (what, path))
    for note in _resolve(data, project).notes:
        print("Note: %s" % note)


def cmd_set(args):
    data, project = _paths(args)
    path = _layer_path(args, data, project)
    layer = cfg.read_json(path) or {}
    if args.key.startswith("environments."):
        name = args.key.split(".", 1)[1]
        envs = dict(layer.get("environments") or {})
        entry = {"patterns": list((envs.get(name) or {}).get("patterns") or [])}
        if args.value and args.value not in entry["patterns"]:
            entry["patterns"].append(args.value)
        envs[name] = entry
        layer["environments"] = envs
    else:
        layer[args.key] = _parse_value(args.value)
    _save(path, layer, data, project, args.key)


def cmd_unset(args):
    data, project = _paths(args)
    path = _layer_path(args, data, project)
    layer = cfg.read_json(path) or {}
    if args.key.startswith("environments."):
        name = args.key.split(".", 1)[1]
        envs = dict(layer.get("environments") or {})
        if args.pattern:
            pats = [p for p in (envs.get(name) or {}).get("patterns", []) if p != args.pattern]
            envs[name] = {"patterns": pats}
        else:
            envs.pop(name, None)
        layer["environments"] = envs
    else:
        layer.pop(args.key, None)
    _save(path, layer, data, project, "the change to %s" % args.key)


def cmd_approve(args):
    data, project = _paths(args)
    path = cfg.project_config_path(project)
    layer = cfg.read_json(path)
    if args.key != "store":
        sys.exit("Only store needs approval.")
    if not layer or "store" not in layer:
        sys.exit("The project config (%s) does not set store." % path)
    try:
        cfg.validate(layer, path)
    except cfg.ConfigError as exc:
        sys.exit("Not approved: %s" % exc)
    approve.grant(data, project, "store", layer["store"])
    print("Approved store = %s for %s. A changed value needs a new approval."
          % (json.dumps(layer["store"]), os.path.realpath(project)))


def _git(args, cwd):
    try:
        res = subprocess.run(["git"] + args, cwd=cwd, env=store.git_env(), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True, timeout=5)
        return res.stdout.strip() if res.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def detect(data, project):
    found = discover.discover(project)
    remotes = _git(["remote"], project).split()
    return {"environments": found, "remotes": remotes,
            "store_hint": "git remote origin" if "origin" in remotes else (
                "no 'origin' remote: set store to another remote, or a local folder for solo use")}


def cmd_detect(args):
    data, project = _paths(args)
    result = detect(data, project)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if not result["environments"]:
        print("No deploy environments found in CI files, Makefiles, package.json, Capistrano stages or deploy "
              "scripts. Add one with: berthcfg.py set environments.<name> '<pattern>'")
    for env, info in sorted(result["environments"].items()):
        print("%s  (from %s)" % (env, ", ".join(info["sources"])))
        for pat in info["patterns"]:
            print("    %s" % pat)
    print("Store: %s." % result["store_hint"])


def cmd_init(args):
    data, project = _paths(args)
    path = _layer_path(args, data, project)
    layer = cfg.read_json(path) or {}
    found = detect(data, project)["environments"]
    envs = dict(layer.get("environments") or {})
    for env, info in found.items():
        entry = {"patterns": list((envs.get(env) or {}).get("patterns") or [])}
        for pat in info["patterns"]:
            if pat not in entry["patterns"]:
                entry["patterns"].append(pat)
        envs[env] = entry
    layer["environments"] = envs
    if args.preset:
        layer["preset"] = args.preset
    layer.setdefault("version", cfg.SUPPORTED_VERSION)
    if not args.yes:
        print(json.dumps(layer, indent=2, sort_keys=True))
        print("This is what init would write to %s. Check the environments, then run again with --yes." % path)
        return
    _save(path, layer, data, project, "%d environment(s)" % len(envs))


def cmd_doctor(args):
    data, project = _paths(args)
    problems = [0]

    def line(ok, text):
        if not ok:
            problems[0] += 1
        print("%s %s" % ("ok  " if ok else "FAIL", text))

    line(sys.version_info >= (3, 9), "Python %s (3.9 or newer needed)" % sys.version.split()[0])
    line(os.path.exists(os.path.join(PLUGIN_ROOT, "hooks", "hooks.json")), "hooks/hooks.json present")
    try:
        os.makedirs(data, exist_ok=True)
        probe = os.path.join(data, ".probe")
        with open(probe, "w") as fh:
            fh.write("x")
        os.unlink(probe)
        line(True, "data folder writable: %s" % data)
    except OSError as exc:
        line(False, "data folder not writable: %s (%s)" % (data, exc))
    try:
        eff = cfg.resolve(data, project)
        line(True, "config valid (preset %s, %d guarded environment(s))" % (
            eff.get("preset"), len(eff.get("environments") or {})))
        for note in eff.notes:
            print("     note: %s" % note)
    except cfg.ConfigError as exc:
        eff = None
        line(False, "config: %s" % exc)
    if eff is not None:
        svc = claims.Service(eff, project, data)
        print("     identity: %s (%s)" % (svc.me, svc.me_source))
        start = time.time()
        try:
            if isinstance(svc.store, store.GitStore):
                svc.store.check()
            svc.read(svc.guarded()[:1])
            line(True, "store readable: %s (%.0f ms)" % (svc.store.describe(), (time.time() - start) * 1000))
        except store.StoreError as exc:
            line(False, "store: %s" % exc)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": project,
                          "permission_mode": "default", "session_id": "doctor"})
    start = time.time()
    # The hook gets the folder and the options the hooks saved, so its own save writes back the same copy.
    env = {k: os.environ[k] for k in store.GIT_ENV_KEYS if k in os.environ}
    env["CLAUDE_PLUGIN_DATA"] = data
    for key, value in cfg.saved_options(data).items():
        env["CLAUDE_PLUGIN_OPTION_" + key.upper()] = str(value)
    res = subprocess.run([sys.executable, os.path.join(HERE, "hook.py"), "pre"], input=payload, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
    ms = (time.time() - start) * 1000
    line(res.returncode == 0 and not res.stderr, "hook runs (%.0f ms for a sample non-deploy call)" % ms)
    print("%d problem(s)." % problems[0])
    return 1 if problems[0] else 0


def build_parser():
    p = argparse.ArgumentParser(prog="berthcfg.py", allow_abbrev=False, description="berth settings")
    p.add_argument("--data")
    p.add_argument("--project-dir")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("show", allow_abbrev=False)
    s.add_argument("--json", action="store_true")
    e = sub.add_parser("explain", allow_abbrev=False)
    e.add_argument("key")
    st = sub.add_parser("set", allow_abbrev=False)
    st.add_argument("key")
    st.add_argument("value")
    st.add_argument("--project", action="store_true")
    un = sub.add_parser("unset", allow_abbrev=False)
    un.add_argument("key")
    un.add_argument("--pattern")
    un.add_argument("--project", action="store_true")
    ap = sub.add_parser("approve", allow_abbrev=False)
    ap.add_argument("key")
    d = sub.add_parser("detect", allow_abbrev=False)
    d.add_argument("--json", action="store_true")
    i = sub.add_parser("init", allow_abbrev=False)
    i.add_argument("--preset", choices=cfg.PRESETS)
    i.add_argument("--project", action="store_true")
    i.add_argument("--yes", action="store_true")
    doc = sub.add_parser("doctor", allow_abbrev=False)
    for sp in (s, e, st, un, ap, d, i, doc):
        sp.add_argument("--data", default=argparse.SUPPRESS)
        sp.add_argument("--project-dir", default=argparse.SUPPRESS)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {"show": cmd_show, "explain": cmd_explain, "set": cmd_set, "unset": cmd_unset,
                "approve": cmd_approve, "detect": cmd_detect, "init": cmd_init, "doctor": cmd_doctor}
    if args.cmd not in handlers:
        parser.print_help()
        return 2
    return handlers[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
