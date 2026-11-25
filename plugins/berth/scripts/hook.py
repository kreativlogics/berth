#!/usr/bin/env python3
"""berth hook entry point.

  hook.py pre            PreToolUse: refuse a deploy to a guarded environment unless you hold its claim.
  hook.py stop           Stop: remind (or block once) while this session holds a claim.
  hook.py session-start  SessionStart: claims you hold here, expired ones, and claims a past session left.
  hook.py session-end    SessionEnd: record the claims this session still holds (no network, no output).

Reads the hook payload as JSON on stdin. Stdlib only, Python 3.9+.
"""

import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Keep the installed plugin folder free of .pyc files, so it still hashes and scans as published.
if os.environ.get("CLAUDE_PLUGIN_DATA"):
    sys.pycache_prefix = os.path.join(os.environ["CLAUDE_PLUGIN_DATA"], "pycache")
else:
    sys.dont_write_bytecode = True
sys.path.insert(0, HERE)

UNATTENDED = ("dontAsk", "bypassPermissions")
REMIND_EVERY = 15 * 60
CONTEXT_LIMIT = 9000
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
CLAIM_SUBS = ("claim", "extend", "takeover")
CONFIG_WRITES = ("set", "unset", "approve")
# Shell builtins that set or change environment variables (or run a file that could).
ENV_SETTERS = ("export", "declare", "typeset", "readonly", "set", "unset", "source", ".", "alias")
# Names in an inline program that point at berth's own files.
STATE_NAMES = (".berth", "held.json", "identity.json", "plugin-options.json", "approvals.json")
GIT_PATH_SUBS = ("rm", "mv", "checkout", "restore", "clean", "update-index", "stash", "checkout-index", "merge-file")
UNPACKERS = ("tar", "gtar", "bsdtar", "unzip", "7z", "7za", "bsdunzip", "rsync")


def data_dir():
    env = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    return env or os.path.join(os.path.expanduser("~"), ".claude", "berth")


def project_dir(payload):
    from berth import cfg

    return cfg.find_project(os.environ.get("CLAUDE_PROJECT_DIR") or (payload or {}).get("cwd") or os.getcwd())


def _resolve(data, project):
    """The effective config. Hooks are the only place plugin options arrive as variables, so they save a copy
    for berth's CLIs, which Claude Code runs without them."""
    from berth import cfg

    opts = cfg.env_options()
    try:
        cfg.save_options(data, opts)
    except OSError:
        pass  # The CLIs then use the last saved copy; doctor reports an unwritable data folder.
    return cfg.resolve(data, project, opts)


def _emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")


def _decide(level, reason):
    _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": level,
                                  "permissionDecisionReason": "berth: " + reason}})


def _unattended(payload):
    return (payload or {}).get("permission_mode") in UNATTENDED


def _ask_or_deny(payload, reason):
    if _unattended(payload):
        _decide("deny", reason + " (Refused because nobody is here to confirm: permission mode %s.)"
                % payload.get("permission_mode"))
    else:
        _decide("ask", reason)


def _fail(payload, message, eff=None):
    """berth itself could not decide: apply fail_mode (the project may only make it stricter)."""
    order = ("allow", "ask", "deny")
    mode = eff.get("fail_mode") if eff is not None else None
    if mode not in order:
        mode = os.environ.get("CLAUDE_PLUGIN_OPTION_FAIL_MODE", "").strip() or "ask"
        if mode not in order:
            mode = "ask"
    project_mode = _project_fail_mode(payload)
    if project_mode in order and order.index(project_mode) > order.index(mode):
        mode = project_mode
    if mode == "allow":
        return
    text = "could not check this call: %s Run /berth:config doctor. (fail_mode is %s.)" % (message, mode)
    if mode == "ask":
        _ask_or_deny(payload or {}, text)
    else:
        _decide("deny", text)


def _project_fail_mode(payload):
    try:
        with open(os.path.join(project_dir(payload), ".berth", "config.json")) as fh:
            return json.load(fh).get("fail_mode")
    except Exception:  # noqa: BLE001 - a broken file is what we are reporting.
        return None


# ---------------------------------------------------------------- PreToolUse

def pre(payload):
    from berth import cfg

    if not isinstance(payload, dict) or not payload.get("tool_name"):
        return _fail(payload or {}, "the hook input had no tool name.")
    data = data_dir()
    project = project_dir(payload)
    broken = None
    try:
        eff = _resolve(data, project)
    except cfg.ConfigError as exc:
        # The project file is broken (or the user's). Keep every check the user's own config supports, and send
        # only deploys to fail_mode, so a broken project file neither stops all work nor opens the guard.
        broken = "%s" % exc
        try:
            eff = cfg.resolve(data, None, cfg.env_options())
        except cfg.ConfigError:
            return _fail(payload, broken)
    tool = payload["tool_name"]
    tool_input = payload.get("tool_input") or {}
    protected = _protected_paths(eff, data, project)
    if tool in WRITE_TOOLS:
        return _check_file_tool(payload, tool_input, protected)
    if tool != "Bash":
        return None
    command = tool_input.get("command") or ""
    if not isinstance(command, str) or not command.strip():
        return None
    return _check_bash(payload, eff, command, data, project, protected, broken)


def _protected_paths(eff, data, project):
    paths = [data, os.path.join(project, ".berth")]
    store = eff.get("store") or {}
    if store.get("type") == "local" and store.get("path"):
        paths.append(os.path.expanduser(store["path"]))
    elif os.path.isfile(os.path.join(project, ".git", "config")):
        # The remote's address lives here; git remote set-url and git config ask instead.
        paths.append(os.path.join(project, ".git", "config"))
    return [os.path.normpath(p) for p in paths]


# macOS and Windows file systems ignore letter case by default: /Data/x and /data/x are the same file.
FOLD_CASE = sys.platform in ("darwin", "win32")


def _inside(path, root):
    from berth import context

    if context.inside(path, root):
        return True
    if not FOLD_CASE:
        return False
    a = os.path.normpath(os.path.realpath(path)).casefold()
    b = os.path.normpath(os.path.realpath(root)).casefold()
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def _inside_any(path, roots):
    return any(_inside(path, r) for r in roots)


def _holds_any(path, roots):
    """True when removing or replacing path would take a protected root with it."""
    return any(_inside(r, path) for r in roots)


def _check_file_tool(payload, tool_input, protected):
    from berth import shell

    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not raw:
        return None
    path = shell.expand_path(raw, payload.get("cwd") or os.getcwd(), os.path.expanduser("~"))
    if _inside_any(path, protected):
        _decide("deny", "%s is berth's own state or config. Change settings with /berth:config and claims with "
                        "/berth:claim, which ask you first." % raw)
    return None


def _check_bash(payload, eff, command, data, project, protected, broken=None):
    from berth import context, match

    cwd = payload.get("cwd") or project
    home = os.path.expanduser("~")
    if len(command) > match.MAX_COMMAND and eff.get("environments"):
        return _fail(payload, "the command is longer than the %d characters berth reads, so the rest could not be "
                              "checked." % match.MAX_COMMAND, eff)
    hidden = match.unreadable(command, cwd=cwd, home=home)
    if hidden and (eff.get("environments") or hidden[2]):
        level, why = hidden[0], hidden[1]
        if level == "deny":
            return _decide("deny", "%s (it nests too deeply or is too long to read). Split it into simpler "
                                   "commands." % why)
        return _ask_or_deny(payload, "%s: it feeds a shell text berth cannot read, or gives env a split string "
                                     "berth cannot read exactly. Save it to a file and review it first." % why)
    reason = match.tamper(command, cwd=cwd, home=home)
    if reason:
        return _decide("deny", "%s. Lock branches are changed only through /berth:claim, so nobody's lock is "
                               "overwritten or deleted by accident." % reason)
    risky = match.prunes_remote(command, cwd=cwd, home=home) or match.runtime_push(command, cwd=cwd, home=home) \
        or match.unknown_git_option(command, cwd=cwd, home=home)
    if not risky and (eff.get("store") or {}).get("type") != "local":
        risky = match.remote_change(command, cwd=cwd, home=home)
    if risky and eff.get("environments"):
        return _ask_or_deny(payload, "%s. Someone's deploy claim may be removed with it." % risky)
    ctx = context.Ctx(payload, eff, home=home, data_dir=data)
    ref_file = _lock_ref_file_write(ctx, command)
    if ref_file:
        return _decide("deny", "this command writes %s, a git ref file that can hold a lock branch. Lock branches "
                               "are changed only through /berth:claim." % ref_file)
    where = _protected_write(ctx, command, protected) or _editor_write(ctx, protected) \
        or _xargs_write(ctx, command, protected)
    if where:
        return _decide("deny", "this command changes %s, which holds berth's own state or config. Use "
                               "/berth:config or /berth:claim instead." % where)
    unknown = _unknown_program_on_state(ctx, protected)
    cli = match.berth_cli_calls(command, cwd=cwd, home=home)
    if cli and _check_cli(payload, cli, ctx.parsed.cmds, data):
        return None
    envs = eff.get("environments") or {}
    targets = match.deploy_targets(command, envs, generic=bool(eff.get("generic_patterns")), cwd=cwd, home=home)
    if match.glob_overflow() and envs:
        return _fail(payload, "a file name pattern in the command is too large for berth to expand in time, so what "
                              "it names could not be checked. Name the files instead.", eff)
    if broken:
        if targets or match.names_deploy(command, cwd=cwd, home=home):
            return _fail(payload, broken, eff)
        return _unknown_program_ask(payload, unknown)
    if targets and _check_deploy(payload, eff, data, project, targets):
        return None
    return _unknown_program_ask(payload, unknown)


def _unknown_program_ask(payload, unknown):
    if not unknown:
        return None
    return _ask_or_deny(payload, "%s is given %s, which holds berth's own state or config, and berth cannot "
                                 "tell whether it writes there." % unknown)


def _protected_write(ctx, command, protected):
    """The protected path a command would write, delete or replace, else None."""
    from berth import context, match

    for cmd in ctx.parsed.cmds:
        for path, kind in context.write_targets(cmd, ctx):
            if _inside_any(path, protected):
                return path
            if (kind == "delete" or cmd.name in UNPACKERS) and _holds_any(path, protected):
                return path
        hit = _git_path_write(cmd, ctx, protected) or _find_delete(cmd, ctx, protected)
        if hit:
            return hit
        program = match.program_text(cmd, command)
        if program is not None:
            named = [n for n in STATE_NAMES if n in program] + [r for r in protected if r in program]
            if named:
                return named[0]
    return None


# Programs that edit or rewrite a file named anywhere in their arguments or script, which the write-target
# parser does not model.
EDITORS = {"ex", "vi", "vim", "nvim", "view", "ed", "emacs", "nano", "pico", "micro", "kak", "hx", "sqlite3",
           "sponge", "osascript", "dd", "truncate", "patch", "shred", "split", "csplit", "ln", "rsync", "install"}
# Copiers whose destination the write-target parser reads; given berth's files only as sources they copy them out.
# An option after the files (GNU cp x dest --sparse auto) hides which word is the destination, so that counts as a
# write.
COPIERS = {"cp", "gcp", "ditto", "scp"}
# Commands that read their arguments from stdin, so a path printed earlier in the pipe is one of their arguments.
AWKS = ("awk", "gawk", "mawk", "nawk")
# Programs that only read the files they are given.
READ_ONLY = {"cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "rg", "ag", "ack", "ls", "wc", "jq",
             "yq", "diff", "cmp", "stat", "file", "bat", "tree", "md5", "md5sum", "shasum", "sha256sum", "realpath",
             "readlink", "basename", "dirname", "test", "[", "echo", "printf", "true", ":", "cd", "pwd", "type",
             "which", "man", "column", "sort", "uniq", "cut", "tr", "od", "xxd", "hexdump",
             "strings", "du", "df", "git", "find", "gfind", "open"}


FIND_ACTIONS = ("-delete", "-exec", "-execdir", "-ok", "-okdir")


def _git_dir_of(path):
    """The git folder a path is in or is: a .git folder, or a bare repository (a folder holding HEAD, objects and
    refs, as a local remote does), or None. Folders merely named refs or packed-refs elsewhere are not git's."""
    norm = path.replace("\\", "/").rstrip("/")
    while norm and norm != "/":
        if os.path.basename(norm) == ".git":
            return norm
        if os.path.isfile(norm + "/HEAD") and os.path.isdir(norm + "/objects") and os.path.isdir(norm + "/refs"):
            return norm
        norm = os.path.dirname(norm)
    return None


# Programs that can put a folder in place earlier in the same command (a link to the remote, a moved or cloned copy).
PLACERS = {"ln", "mv", "cp", "gcp", "ditto", "rsync", "git", "tar", "unzip"}


def _pattern_hits(norm):
    """The existing paths a file name pattern names (at most 200), or the path itself."""
    import glob
    import itertools

    from berth import globs

    if not globs.has_magic(norm):
        return [norm]
    return list(itertools.islice(glob.iglob(norm), 200))


def _deletes_git_refs(path):
    """Deleting this path removes a git folder's refs: it is a bare repository, or its refs, refs/heads or
    packed-refs, or something inside its refs/heads."""
    git_dir = _git_dir_of(path)
    if not git_dir:
        return False
    if path.rstrip("/") == git_dir:
        return os.path.basename(git_dir) != ".git"  # A bare repository (a local remote) deleted whole.
    return _ref_write(path, "delete", git_dir) or path.startswith(git_dir + "/refs/")


def _unknown_folder(norm, ctx):
    """True when berth cannot tell which folder a path is in: it goes through a command substitution or a variable,
    or its folder does not exist yet while the command links, moves, copies or clones something into place."""
    from berth import shell

    if shell._SUBST in norm or "$" in norm:
        return True
    if os.path.isdir(os.path.dirname(norm.rstrip("/")) or "/"):
        return False
    return any(c.name in PLACERS for c in ctx.parsed.cmds)


def _ref_write(norm, kind, git_dir):
    """A write inside a git folder that touches its lock refs: a ref named like a lock, packed-refs, the refs or
    refs/heads folder itself deleted, or a file name pattern under refs that the shell may fill in with a lock.
    git_dir None means the folder is unknown, so the path is judged by its names alone."""
    from berth import globs, match

    if git_dir is None:
        # Folder unknown: read the path from its last refs folder, or as a bare file name.
        cut = max(norm.rfind("/refs/"), norm.rfind("/refs") if norm.rstrip("/").endswith("/refs") else -1)
        inner = norm[cut:] if cut >= 0 else "/" + os.path.basename(norm)
    else:
        inner = norm[len(git_dir):]
    if inner.startswith("/refs/heads/") and match.LOOSE_LOCK.search(inner):
        return True
    if os.path.basename(norm) == "packed-refs":
        return True
    if kind == "delete" and inner.rstrip("/") in ("/refs", "/refs/heads"):
        return True
    return kind == "delete" and inner.startswith("/refs/") and globs.has_magic(inner)


FIND_TESTS = ("-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex", "-lname",
              "-ilname")
# refs, heads and packed-refs as whole path parts (not prefs or unpacked), or a lock's name.
REF_WORDS = re.compile(r"(^|[/*?\[\]])(refs|heads|packed-refs)($|[/*?\[\]])|branch-deploy", re.IGNORECASE)


def _find_parts(cmd, ctx):
    """(start paths, the values of its name and path tests) of a find that deletes or runs a program on what it
    finds, else None. Leading -H/-L/-P/-D/-O options and -- come before the paths."""
    if cmd.name not in ("find", "gfind") or not _find_writes(cmd.args):
        return None
    args = list(cmd.args)
    starts = []
    # Options before the paths: GNU -H -L -P -D<x> -O<n>, BSD -x -E -s -d -X (clustered too), BSD -f <path>.
    while args:
        a = args[0]
        if a == "--":
            args.pop(0)
            break
        if a in ("-D", "-f"):
            if a == "-f" and len(args) > 1:
                starts.append(ctx.path(args[1], cmd.cwd))
            args = args[2:]
        elif a.startswith(("-D", "-O")) or re.match(r"^-[HLPExsdX]+$", a):
            args.pop(0)
        else:
            break
    for a in args:
        if a.startswith(("-", "(", "!")):
            break
        starts.append(ctx.path(a, cmd.cwd))
    tests = [args[i + 1] for i, a in enumerate(args[:-1]) if a in FIND_TESTS]
    return starts or [cmd.cwd or ctx.project], tests


def _find_writes(args):
    """find deletes, or runs a program other than a known reader on what it finds (git, find and open can write)."""
    from berth import match

    if "-delete" in args:
        return True
    for run in match._find_exec(args):
        name = os.path.basename(run[0]).lower() if run else ""
        if name not in READ_ONLY or name in ("git", "find", "gfind", "open"):
            return True
    return False


def _find_ref_delete(cmd, ctx, names_lock):
    """A find that deletes or runs programs where it may reach lock refs: its tests name refs, heads, packed-refs
    or a lock (from any start, since a parent folder may hold the remote), or it starts inside a git folder's refs,
    or in a git folder with no name test or in a command naming a lock."""
    parts = _find_parts(cmd, ctx)
    if not parts:
        return None
    starts, tests = parts
    if any(REF_WORDS.search(t) for t in tests):
        return starts[0]
    for start in starts:
        git_dir = _git_dir_of(start) or (_git_dir_of(start + "/.git") if os.path.isdir(start + "/.git") else None)
        if not git_dir:
            continue
        if start.startswith(git_dir + "/refs") or names_lock or (start.rstrip("/") == git_dir and not tests):
            return start
    return None


def _lock_ref_file_write(ctx, command):
    """A write inside a git folder (a .git folder, or a bare repository such as a local remote) that touches a lock
    ref or packed-refs, or deletes the refs folders; or any file under .git or git config file written by a command
    whose text names a lock (echo 'ref: ...-lock' > .git/HEAD)."""
    from berth import context, match

    text = match.plain(command)
    names_lock = bool(match.LOOSE_LOCK.search(text))
    for cmd in ctx.parsed.cmds:
        hit = _find_ref_delete(cmd, ctx, names_lock)
        if hit:
            return hit
        for path, _kind in context.write_targets(cmd, ctx):
            norm = path.replace("\\", "/")
            config = os.path.basename(norm) == ".gitconfig" or norm.endswith("/git/config")
            git_dir = _git_dir_of(norm)
            if git_dir and _ref_write(norm, _kind, git_dir):
                return path
            if _kind == "delete" and any(_deletes_git_refs(hit) for hit in _pattern_hits(norm)):
                return path
            if not git_dir and _unknown_folder(norm, ctx) and _ref_write(norm, _kind, None):
                return path
            if not git_dir and not config:
                continue
            if match.LOOSE_LOCK.search(norm) or names_lock:
                return path
    return None


def _named_paths(cmd, ctx, protected):
    """The protected paths or berth file names that a command's words name, as paths or inside text."""
    from berth import match

    from berth import globs

    hits = []
    for word in cmd.args:
        text = match.plain(word)
        # A path may follow an option or a name (--file=x, FILES=x); common file names alone mean nothing.
        parts = [text] + [text.split("=", 1)[1]] if "=" in text else [text]
        if word.startswith("-") and "=" not in text:
            parts = []
        paths = [w for part in parts for w in (globs.expand_braces(part) or [part]) if w and not globs.has_magic(w)]
        inside = [ctx.path(w, cmd.cwd) for w in paths if _inside_any(ctx.path(w, cmd.cwd), protected)]
        if inside:
            hits.append(inside[0])
        elif any(r in text for r in protected):
            hits.append(word[:120])
    return hits


def _editor_write(ctx, protected):
    """An editor, database shell or other rewriting program given a protected path; also git config --file."""
    for cmd in ctx.parsed.cmds:
        in_place = cmd.name in AWKS and any(a == "-i" or a.startswith(("-i", "--include")) for a in cmd.args)
        late_option = cmd.name in COPIERS and _option_after_files(cmd.args)
        if cmd.name in EDITORS or in_place or late_option:
            hits = _named_paths(cmd, ctx, protected) or _text_names(cmd, ctx, protected)
            if hits:
                return hits[0]
            if cmd.cwd and _inside_any(cmd.cwd, protected):
                return cmd.cwd  # ln -s x, with no destination, links into the folder it runs in.
        if cmd.name == "git" and cmd.args[:1] == ["config"] and not _git_config_read(cmd.args[1:]):
            for i, a in enumerate(cmd.args):
                value = a.split("=", 1)[1] if a.startswith("--file=") else (
                    cmd.args[i + 1] if a in ("-f", "--file") and i + 1 < len(cmd.args) else None)
                if value and _inside_any(ctx.path(value, cmd.cwd), protected):
                    return ctx.path(value, cmd.cwd)
    return None


def _option_after_files(args):
    seen_file = False
    for a in args:
        if a == "--":
            return False
        if a.startswith("-"):
            if seen_file:
                return True
        else:
            seen_file = True
    return False


def _git_config_read(args):
    """git config reading, not writing, read as git reads it: the words after a value option (--comment -l) are
    values, not options."""
    from berth import match

    return not match._config_writes(args)


# Editors that read their editing script from stdin, which may come from anywhere in the command (a pipe through
# nice or a subshell, a process substitution, a here-string, a file written earlier).
STDIN_SCRIPTED = {"ed", "ex", "sqlite3", "patch", "osascript"}


def _script_on_stdin(cmd):
    if cmd.name in STDIN_SCRIPTED:
        return True
    if cmd.name == "emacs":
        return any(a in ("--batch", "-batch", "--script", "-script") for a in cmd.args)
    return cmd.name in ("vi", "vim", "nvim", "view") and any(
        a.startswith("+") or (a.startswith("-") and not a.startswith("--") and set(a[1:]) & set("esE"))
        for a in cmd.args)


def _text_names(cmd, ctx, protected):
    """A protected folder named in an editor's own words or heredoc, or anywhere in the command for an editor that
    reads its script from stdin, with $HOME and ~ filled in."""
    from berth import match

    home = ctx.home
    text = " ".join(cmd.argv) + "\n" + (cmd.stdin_text or "")
    if _script_on_stdin(cmd):
        text += "\n" + match.plain(ctx.command or "")
    text = text.replace("${HOME}", home).replace("$HOME", home).replace("~/", home + "/")
    hits = [r for r in protected if r in text]
    if ".berth/" in text or text.rstrip().endswith(".berth"):
        hits.append(".berth")
    return hits


def _xargs_write(ctx, command, protected):
    """With xargs in the command, a path to berth's files printed earlier in the pipe becomes an argument of the
    command xargs runs, as when a path is printed and piped to a program that removes it. Any program there but a
    reader counts as a write."""
    from berth import match

    if not match._XARGS.search(match.plain(command)):
        return None
    named = [r for r in protected if r in command] + ([".berth"] if ".berth" in command else [])
    if not named:
        return None
    for cmd in ctx.parsed.cmds:
        if cmd.name not in READ_ONLY and cmd.name not in match.READERS and not match.is_berth_cli(cmd):
            return named[0]
    return None


def _unknown_program_on_state(ctx, protected):
    """'<program>' when a program berth does not know is given one of berth's files; else None."""
    from berth import match

    for cmd in ctx.parsed.cmds:
        if cmd.name in READ_ONLY or cmd.name in EDITORS or cmd.name in COPIERS or match.is_berth_cli(cmd) \
                or match.inline_program(cmd):
            continue
        if cmd.args[:2] == ["-m", "json.tool"] and len([a for a in cmd.args[2:] if not a.startswith("-")]) <= 1:
            continue  # Pretty-prints a file to stdout; a second file would be its output.
        if cmd.name in match.READERS or cmd.name in UNPACKERS:
            continue
        hits = _named_paths(cmd, ctx, protected)
        if hits:
            return "`%s`" % cmd.name, hits[0]
    return None


def _git_path_write(cmd, ctx, protected):
    """git rm, mv, checkout, restore and friends on a path under a protected root. A whole-tree form
    (git checkout .) is left alone: it is everyday work, and docs/limits.md lists it."""
    from berth import context

    if cmd.name != "git":
        return None
    sub, rest = context.git_subcommand(cmd.args)
    if sub not in GIT_PATH_SUBS or (sub == "merge-file" and any(a in ("-p", "--stdout") for a in rest)):
        return None
    _configs, cdir = context.git_globals(cmd.args)
    base = ctx.path(cdir, cmd.cwd) if cdir else (cmd.cwd or ctx.cwd)
    for word in rest:
        if word.startswith("-") or word == "--":
            continue
        path = ctx.path(word, base)
        if _inside_any(path, protected):
            return path
    return None


def _find_delete(cmd, ctx, protected):
    """find ... -delete (or -exec rm) inside a protected root, or above one with a test that names berth's files
    (.berth, held.json, *.json). find . -name '*.pyc' -delete stays allowed."""
    if cmd.name not in ("find", "gfind"):
        return None
    args = list(cmd.args)
    deletes = "-delete" in args or any(
        a in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < len(args)
        and os.path.basename(args[i + 1]).lower() in ("rm", "unlink", "rmdir", "shred")
        for i, a in enumerate(args))
    if not deletes:
        return None
    starts = []
    for a in args:
        if a.startswith(("-", "(", "!")):
            break
        starts.append(a)
    names_state = any("json" in a or "berth" in a for a in args)  # Every STATE_NAMES entry has one.
    for word in starts or ["."]:
        path = ctx.path(word, cmd.cwd)
        if _inside_any(path, protected) or (names_state and _holds_any(path, protected)):
            return path
    return None


def _check_cli(payload, calls, cmds, data):
    """Returns True once a decision has been written."""
    from berth import held

    setters = [c.name for c in cmds if c.name in ENV_SETTERS]
    ask = []
    for script, sub, cmd in calls:
        if cmd.env or setters:
            _decide("deny", "berth's tools are run on their own, without environment variables set before them "
                            "(they could change who you appear to be or where settings come from). Run the berth "
                            "command by itself.")
            return True
        given = _data_args(cmd.argv)
        if not given or any(not _same_path(os.path.expanduser(d), data) for d in given):
            _decide("deny", "berth's tools are run with --data %s, the folder its hooks use, so they read the same "
                            "settings and claims." % data)
            return True
        if script == "berth.py" and sub in CLAIM_SUBS:
            env = _first_word_after(cmd.argv, sub)
            sid = payload.get("session_id")
            if env and sid:
                with held.Held(data) as h:
                    h.mark_session(sid, env)
            if sub == "takeover":
                ask.append("taking over a stale lock replaces someone else's claim on %s" % (env or "an environment"))
        if script == "berthcfg.py" and (sub in CONFIG_WRITES or (sub == "init" and "--yes" in cmd.argv)):
            ask.append("this changes berth's settings (%s %s)" % (script, sub))
    if ask:
        _ask_or_deny(payload, "; ".join(ask) + ". Confirm only if you meant it.")
        return True
    return False


def _same_path(a, b):
    a, b = os.path.normpath(os.path.realpath(a)), os.path.normpath(os.path.realpath(b))
    return a.casefold() == b.casefold() if FOLD_CASE else a == b


def _data_args(argv):
    out = []
    for i, a in enumerate(argv):
        if a == "--data":
            out.append(argv[i + 1] if i + 1 < len(argv) else "")
        elif a.startswith("--data="):
            out.append(a.split("=", 1)[1])
    return out


def _first_word_after(argv, sub):
    try:
        i = list(argv).index(sub)
    except ValueError:
        return None
    for a in argv[i + 1:]:
        if not a.startswith("-"):
            return a
    return None


def _check_deploy(payload, eff, data, project, targets):
    """Writes a decision when a deploy is refused or cannot be checked, and returns True then; False to allow."""
    from berth import claims, store

    names = []
    for env, _line in targets:
        if env not in names:
            names.append(env)
    try:
        svc = claims.Service(eff, project, data)
        results = svc.check_deploy(names)
    except (store.StoreError, OSError) as exc:
        _fail(payload, "the claim store could not be read (%s)" % exc, eff)
        return True
    refused = [(env, msg) for env, ok, msg in results if not ok]
    if not refused:
        return False
    line = dict(targets).get(refused[0][0], "")
    text = "this looks like a deploy to %s (`%s`), and %s. Claim it first: /berth:claim claim %s." % (
        ", ".join(e for e, _ in refused), line[:120], "; ".join(m for _, m in refused), refused[0][0])
    if eff.get("unclaimed") == "deny":
        _decide("deny", text)
    else:
        _ask_or_deny(payload, text)
    return True


# ---------------------------------------------------------------- Stop / SessionStart / SessionEnd

def _session_claims(h, session_id, project):
    """Claims in held.json for this project whose environment this session claimed."""
    entry = h.data["sessions"].get(session_id) if session_id else None
    if not entry:
        return []
    envs = set(entry.get("envs") or [])
    real = os.path.realpath(project)
    return [c for c in h.data["claims"].values()
            if isinstance(c, dict) and c.get("env") in envs and c.get("project") == real]


def _when(claim):
    from berth import locks

    exp = locks.parse_time(claim.get("expires_at"))
    if not exp:
        return "no end time"
    word = "expired" if exp <= locks.utcnow() else "until"
    return "%s %s" % (word, locks.short_time(exp))


def stop(payload):
    from berth import cfg, held

    if payload.get("stop_hook_active"):
        return
    sid = payload.get("session_id")
    if not sid:
        return
    data = data_dir()
    if not os.path.exists(os.path.join(data, "held.json")):
        return
    project = project_dir(payload)
    try:
        eff = _resolve(data, project)
    except cfg.ConfigError:
        return  # A Stop hook must never trap the session; doctor reports the config problem.
    mode = eff.get("stop")
    if mode == "off":
        return
    with held.Held(data) as h:
        mine = _session_claims(h, sid, project)
        if not mine:
            return
        entry = h.session(sid)
        names = ", ".join("%s (%s)" % (c["env"], _when(c)) for c in mine)
        if mode == "block":
            fresh = [c for c in mine if c.get("ref") not in entry["blocked"]]
            if fresh:
                entry["blocked"].extend(c.get("ref") for c in fresh)
                h.changed = True
                _emit({"decision": "block", "reason": (
                    "berth: this session still holds the deploy claim on %s. If the deploy work is done, release "
                    "it with /berth:claim release <env>. If it is still needed, say so to the user and stop again."
                    % names)})
                return
        if time.time() - float(entry.get("reminded_at") or 0) >= REMIND_EVERY:
            entry["reminded_at"] = time.time()
            h.changed = True
            _emit({"systemMessage": "berth: you still hold %s. Release it when you're done: /berth:claim release "
                                    "<env>." % names})


def session_start(payload):
    from berth import cfg, claims, held

    data = data_dir()
    project = project_dir(payload)
    lines = []
    try:
        eff = _resolve(data, project)
    except cfg.ConfigError as exc:
        _emit({"systemMessage": "berth: config problem: %s Run /berth:config doctor." % exc})
        return
    lines.extend("berth: %s" % n for n in eff.notes)
    if eff.get("environments"):
        drift = claims.identity_drift(eff, project, data)
        if drift:
            lines.append("berth: %s" % drift)
    real = os.path.realpath(project)
    state = held.load(data)
    here = [c for c in state["claims"].values() if isinstance(c, dict) and c.get("project") == real]
    for c in sorted(here, key=lambda c: c.get("env") or ""):
        lines.append("berth: you hold %s (%s, %s)." % (c.get("env"), _when(c), c.get("store")))
    left = [x for x in state["left"] if isinstance(x, dict) and x.get("project") == real]
    if left:
        lines.append("berth: a session that ended still held %s. Release what you no longer need: /berth:claim "
                     "status." % ", ".join(sorted({str(x.get("env")) for x in left})))
        with held.Held(data) as h:
            h.set_left([x for x in h.data["left"] if not (isinstance(x, dict) and x.get("project") == real)])
    if not lines:
        return
    text = "\n".join(lines)[:CONTEXT_LIMIT]
    _emit({"systemMessage": text,
           "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})


def session_end(payload):
    """SessionEnd cannot block and its output is discarded; record what was left for the next SessionStart."""
    from berth import held

    sid = payload.get("session_id")
    data = data_dir()
    if not sid or not os.path.exists(os.path.join(data, "held.json")):
        return
    project = project_dir(payload)
    with held.Held(data) as h:
        mine = _session_claims(h, sid, project)
        if not mine:
            return
        keep = [x for x in h.data["left"] if isinstance(x, dict) and x.get("session") != sid]
        for c in mine:
            keep.append({"env": c.get("env"), "project": c.get("project"), "expires_at": c.get("expires_at"),
                         "session": sid, "reason": str(payload.get("reason") or "")[:40]})
        h.set_left(keep[-50:])


# Seconds all of a hook's git calls may take together, below each hook's timeout in hooks.json, so a slow remote
# ends in fail_mode rather than in a timeout that lets the call go ahead.
GIT_BUDGET = {"pre": 45, "stop": 7, "session-start": 7, "session-end": 3}


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    from berth import store

    store.set_budget(GIT_BUDGET.get(mode))
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = None
    try:
        if mode == "pre":
            if not isinstance(payload, dict):
                _fail({}, "the hook input was not valid JSON.")
            else:
                pre(payload)
        elif mode == "stop":
            stop(payload if isinstance(payload, dict) else {})
        elif mode == "session-start":
            session_start(payload if isinstance(payload, dict) else {})
        elif mode == "session-end":
            session_end(payload if isinstance(payload, dict) else {})
    except Exception as exc:  # noqa: BLE001 - every crash goes through fail_mode or stays quiet.
        if mode == "pre":
            _fail(payload if isinstance(payload, dict) else {}, "internal error (%s: %s)." % (
                type(exc).__name__, exc))
        elif mode == "session-start":
            _emit({"systemMessage": "berth: session check failed (%s). Run /berth:config doctor."
                                    % type(exc).__name__})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
