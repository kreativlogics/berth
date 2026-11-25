"""Which guarded environments a shell command deploys to, and whether it touches lock branches directly.

Each simple command (after berth's vendored parser strips wrappers, assignments, chains, `sh -c` and `$(...)`) is
turned into a match line: the program's base name, then its arguments, joined by spaces. Patterns from the config
are Python regexes searched in that line. With generic_patterns on, a command whose program or first word names a
deploy step and that also names a guarded environment as a word counts as a deploy too.
"""

import glob
import itertools
import os
import re
import shlex
import time

from . import context
from . import globs
from . import locks
from . import shell

MAX_COMMAND = 20000
# Programs that only read or print: the generic rule never treats them as deploys.
READERS = {
    "cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "rg", "ag", "ack", "ls", "echo", "printf",
    "find", "wc", "jq", "yq", "man", "which", "type", "file", "stat", "diff", "sort", "uniq", "cut", "tr", "awk",
    "sed", "bat", "tree", "open", "code", "vim", "vi", "nano", "emacs", "history", "true", ":", "test", "[",
}
# Words that name a deploy step in the generic rule, matched as whole words inside names (deploy-staging,
# deploy:staging, shipIt), never inside other words (ownership, prereleased).
DEPLOY_WORDS = {
    "deploy", "deploys", "deploying", "deployment", "deployments", "release", "releases", "releasing", "rollout",
    "rollouts", "ship", "shipit", "shipping", "promote", "promotes", "promoting", "promotion",
}
# Test runners name deploy code in their arguments (tests/test_deploy.py) without deploying.
TEST_RUNNERS = {
    "pytest", "py.test", "jest", "vitest", "mocha", "phpunit", "rspec", "tox", "nox", "unittest", "ava", "karma",
    "cypress", "playwright",
}
CLI_NAMES = ("berth.py", "berthcfg.py")
# Commands that push refs, and HTTP clients that could call a git host's API.
PUSHERS = ("push", "send-pack", "http-push")
HTTP_CLIENTS = ("curl", "wget", "http", "https", "xh", "xhs")
# Forge CLIs whose `api` subcommand takes gh's method and field options.
API_CLIS = ("gh", "glab", "hub")
# Interpreter flags that take a program as the next argument.
INLINE_FLAGS = {
    "python": ("-c",), "node": ("-e", "--eval", "-p", "--print"), "nodejs": ("-e", "--eval", "-p", "--print"),
    "ruby": ("-e",), "perl": ("-e", "-E"), "php": ("-r", "-B", "-R", "-E"), "bun": ("-e", "--eval", "-p", "--print"),
    "deno": ("eval",),
}
RUNTIME = ("$", "__SUBST__")
# xargs adds words read from stdin to the command it runs, so a push under it names its branch at run time.
_XARGS = re.compile(r"(^|[\s/|;&(])xargs(\s|$)", re.IGNORECASE)


def match_line(cmd):
    return " ".join([cmd.name] + list(cmd.args))


def _compile(patterns):
    out = []
    for pat in patterns:
        try:
            out.append(re.compile(pat))
        except re.error:
            continue  # validate() already rejected bad patterns; never crash the hook on one.
    return out


def _word(env):
    return re.compile(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(env), re.IGNORECASE)


LOCK_REF = re.compile(r"%s(?![A-Za-z0-9_-])" % re.escape(locks.LOCK_SUFFIX))
# Any part of a lock branch name, in a command with quotes and backslashes removed: enough to say a command is
# about lock branches even when the name is split up or held in a variable.
LOOSE_LOCK = re.compile(r"deploy-lock|branch-deploy", re.IGNORECASE)


def _mentions_lock(text):
    return bool(LOCK_REF.search(text))


def plain(text):
    """The command with quotes and backslashes removed, so split-up words read whole."""
    return text.replace("'", "").replace('"', "").replace("\\", "")


def _segments(word):
    out = []
    for part in re.split(r"[^A-Za-z0-9]+", word):
        out.extend(x.lower() for x in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+", part))
    return out


def is_berth_cli(cmd):
    return _cli_index(cmd) is not None


# Programs that start a Python script, and per kind the options that take the next word as their value and the
# ones that take none. Any other option means berth cannot tell which word is the script.
_LAUNCHER = re.compile(r"^(python[0-9.]*|py|pypy[0-9.]*|uv|uvx|pipx|run)$")
_PYTHON_VALUE_LETTERS = "WXQ"
_PYTHON_FLAG_LETTERS = "bBdEiIOPqsSuv"
_TOOL_VALUE = {
    "uv": {"--with", "--with-editable", "--with-requirements", "--python", "-p", "--project", "--directory",
           "--env-file", "--extra", "--group", "--package", "--index", "--default-index", "--index-url",
           "--extra-index-url", "-i", "--config-file", "--cache-dir", "--color", "--from"},
    "pipx": {"--spec", "--python", "--index-url", "--pip-args"},
}
_TOOL_FLAGS = {
    "uv": {"--no-project", "--frozen", "--locked", "--isolated", "--no-sync", "-q", "--quiet", "-v", "--verbose",
           "--offline", "--no-cache", "-n", "--script", "--no-dev", "--all-extras", "--active", "--no-config",
           "--native-tls", "-s", "--no-env-file"},
    "pipx": {"--no-cache", "-q", "--quiet", "-v", "--verbose"},
}


def _launcher_kind(word):
    name = word.lower()
    if name in ("uv", "uvx", "run"):
        return "uv"
    if name == "pipx":
        return "pipx"
    return "python" if _LAUNCHER.match(name) else None


def _option_width(kind, word):
    """How many words an option of a launcher spans (1 or 2), or None when berth does not know the option."""
    if word == "--":
        return 1
    if kind == "py":
        if re.match(r"^-[0-9][0-9.]*(-[0-9]+)?$", word):
            return 1  # py -3, py -3.12
        kind = "python"
    if kind == "python":
        if word.startswith("--"):
            option = word.split("=", 1)[0]
            if option != "--check-hash-based-pycs":
                return None
            return 1 if "=" in word else 2
        letters = word[1:]
        for k, letter in enumerate(letters):
            if letter in _PYTHON_VALUE_LETTERS:
                return 1 if letters[k + 1:] else 2
            if letter not in _PYTHON_FLAG_LETTERS:
                return None  # -c, -m and - run something other than a script file.
        return 1 if letters else None
    option = word.split("=", 1)[0]
    if option in _TOOL_VALUE.get(kind, ()):
        return 1 if "=" in word else 2
    if word in _TOOL_FLAGS.get(kind, ()):
        return 1
    return None


def _cli_index(cmd):
    """Index in [name] + args of berth's own script when it is certainly the program that runs: the command
    itself, or the script a launcher starts (python3 -X utf8, uv run python). Only this exact form is spared the
    deploy check; a berth.py that is another program's argument, or follows an option berth does not know, does
    not count."""
    words = [cmd.name] + list(cmd.args)
    if os.path.basename(cmd.name).lower() in CLI_NAMES:
        return 0
    kind = _launcher_kind(cmd.name)
    if kind is None:
        return None
    if cmd.name.lower() == "py":
        kind = "py"
    i = 1
    while i < len(words):
        w = words[i]
        if os.path.basename(w).lower() in CLI_NAMES:
            return i
        if w.startswith("-") and len(w) > 1:
            width = _option_width(kind, w)
            if width is None:
                return None
            i += width
            continue
        if w.lower() == "run" and kind in ("uv", "pipx"):
            i += 1
            continue
        nested = _launcher_kind(w)
        if nested is None:
            return None
        kind = nested
        i += 1
    return None


def _cli_word_index(cmd):
    """Index of the first word naming berth's own script anywhere in a command, for the safety checks on its
    calls (variables, --data, confirmations): there any mention counts, so no launcher form slips past."""
    if cmd.name in READERS or cmd.name == "git":
        return None  # Reading or diffing berth's source is not a call.
    words = [cmd.name] + list(cmd.args)
    for i, w in enumerate(words):
        if os.path.basename(w).lower() in CLI_NAMES:
            return i
    return None


_PATH_LIKE = re.compile(r"/|\.[A-Za-z][A-Za-z0-9]{0,4}$")


def _step_words(cmd, args):
    """The program and its first two plain arguments: the words that say what a command does. Paths
    (deploy/staging.env) and file names (staging.json) say what it acts on, so they are left out. Option values
    stay in: skipping them would also skip make -k deploy-staging."""
    words = [a for a in args if not a.startswith("-") and not _PATH_LIKE.search(a)][:2]
    return [cmd.name] + words


# Programs that run a script file named in their arguments: the script's name says what the step does.
_SCRIPT_RUNNERS = re.compile(r"^(sh|bash|zsh|dash|ksh|mksh|fish|ash|python[0-9.]*|py|pypy[0-9.]*|perl[0-9.]*|ruby|"
                             r"node|nodejs|deno|bun|php|uv|uvx|pipx|run|tsx|ts-node|npx|bunx)$")


def _script_step(cmd, args):
    """For a shell or interpreter running a script (bash scripts/deploy.sh staging), the script's name without
    its extension and the script's first two plain arguments; else None."""
    if not _SCRIPT_RUNNERS.match(cmd.name):
        return None
    rest = list(args)
    kind = cmd.name
    while rest and (rest[0].startswith("-") or _SCRIPT_RUNNERS.match(rest[0].lower())):
        if rest[0].startswith("-") and len(rest[0]) > 1:
            rest = rest[_runner_option_width(kind, rest[0]):]
        else:
            kind = rest[0].lower()
            rest = rest[1:]
    if not rest:
        return None
    stem = re.sub(r"\.[A-Za-z0-9]{1,4}$", "", os.path.basename(rest[0]))
    return [stem] + [a for a in rest[1:] if not a.startswith("-") and not _PATH_LIKE.search(a)][:2]


_SHELL_RUNNER = re.compile(r"^(sh|bash|zsh|dash|ksh|mksh|fish|ash)$")


def _runner_option_width(kind, word):
    """How many words an option of a script runner spans: 2 when it takes the next word as its value (python -W
    ignore, bash -o pipefail, node --require x, uv run --with x), else 1."""
    if word == "--" or "=" in word:
        return 1
    tool = _launcher_kind(kind)
    if tool in ("uv", "pipx"):
        return 2 if word in _TOOL_VALUE[tool] else 1
    m = _INTERPRETER.match(kind)
    family = m.group(1) if m else ("python" if tool == "python" else kind)
    if word.startswith("--"):
        if _SHELL_RUNNER.match(kind):
            return 2 if word in ("--rcfile", "--init-file") else 1
        return 2 if word in LONG_VALUE_OPTIONS.get(family, ()) else 1
    letters = "oO" if _SHELL_RUNNER.match(kind) else VALUE_OPTIONS.get(family, "")
    for k, letter in enumerate(word[1:], 1):
        if letter in letters:
            return 1 if word[k + 1:] else 2
    return 1


def _generic(cmd, env_word):
    if cmd.name in READERS or cmd.name == "git" or is_berth_cli(cmd):
        return False
    # Lock branch names contain "deploy" but only read or edit a claim; tamper() handles writes to them.
    args = [a for a in cmd.args if not _mentions_lock(a)]
    words = _script_step(cmd, args) or _step_words(cmd, args)
    if any(os.path.basename(w).lower() in TEST_RUNNERS for w in words):
        return False
    if not any(seg in DEPLOY_WORDS for w in words for seg in _segments(w)):
        return False
    return bool(env_word.search(" ".join([cmd.name] + args)))


FIND_EXEC = ("-exec", "-execdir", "-ok", "-okdir")


def commands(command, cwd=None, home=None):
    """The simple commands a command line runs, including those find runs with -exec."""
    out = []
    base = []
    for cmd in shell.parse(command, cwd=cwd, home=home).cmds:
        runs = _find_exec(cmd.args) if cmd.name in ("find", "gfind") else _fd_exec(cmd.args) if cmd.name in (
            "fd", "fdfind") else []
        for run in runs:
            base.extend(shell.parse(" ".join(shlex.quote(a) for a in run), cwd=cmd.cwd, home=home).cmds)
        base.append(cmd)
    for cmd in base:
        out.append(cmd)
        expanded = _braced(cmd)
        if expanded is not None:
            out.append(expanded)
        for each in (cmd, expanded):
            globbed = _globbed(each) if each is not None else None
            if globbed is not None:
                out.append(globbed)
    return out


MAX_GLOB = 50
# Patterns with more wildcard folders than this, or past the time budget, are not expanded: walking them could
# outlast the hook's timeout. glob_overflow() then tells the hook, which sends the command to fail_mode.
MAX_GLOB_DEPTH = 3
GLOB_BUDGET = 3.0
_GLOB_CACHE = {}
_GLOB_STATE = {"spent": 0.0, "overflow": False}


def glob_overflow():
    """True when a file name pattern in a command checked so far was too large to expand."""
    return _GLOB_STATE["overflow"]


def _glob_word(word, base):
    """Files a pattern names, as the shell would write them (relative stays relative), at most MAX_GLOB."""
    key = (word, base)
    if key in _GLOB_CACHE:
        return _GLOB_CACHE[key]
    hits = []
    parts = [p for p in word.split("/") if any(c in p for c in "*?[")]
    if len(parts) > MAX_GLOB_DEPTH or _GLOB_STATE["spent"] > GLOB_BUDGET:
        _GLOB_STATE["overflow"] = True
        _GLOB_CACHE[key] = hits
        return hits
    absolute = os.path.isabs(word)
    pattern = word if absolute else os.path.join(glob.escape(base), word)
    prefix = len(base) + 1
    start = time.monotonic()
    try:
        for h in itertools.islice(glob.iglob(pattern), MAX_GLOB):
            hits.append(h if absolute else h[prefix:])
            if _GLOB_STATE["spent"] + time.monotonic() - start > GLOB_BUDGET:
                _GLOB_STATE["overflow"] = True
                break
    except (OSError, ValueError, re.error):
        hits = []
    _GLOB_STATE["spent"] += time.monotonic() - start
    if _GLOB_STATE["spent"] > GLOB_BUDGET:
        _GLOB_STATE["overflow"] = True
    hits.sort()
    _GLOB_CACHE[key] = hits
    return hits


# Programs whose arguments are search patterns (regexes, find -path) and never run: their words are not expanded,
# so a long regex does not count as a file name pattern too large to expand.
PATTERN_READERS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "sed", "gsed", "awk", "gawk", "mawk", "nawk",
                   "find", "gfind", "fd", "jq", "yq"}


def _globbed(cmd):
    """cmd after the shell's file name expansion against its working folder (./deploy-stag*.sh), or None when no
    word matches a file. Quoting is no longer known, so quoted patterns are expanded too: that only adds a command
    to check."""
    if not any(globs.has_magic(w) and any(c in w for c in "*?[") for w in cmd.argv):
        return None
    if cmd.name in PATTERN_READERS:
        return None
    base = (cmd.cwd or os.getcwd()).rstrip(os.sep) or os.sep
    argv = []
    for word in cmd.argv:
        hits = []
        if any(c in word for c in "*?[") and not word.startswith("-"):
            hits = _glob_word(word, base)
        argv.extend(hits or [word])
    if argv == cmd.argv:
        return None
    return shell.Cmd(argv, cmd.env, cmd.redirects, cmd.cwd, cmd.depth, cmd.stdin_text)


def _braced(cmd):
    """cmd after the shell's brace expansion (`{make,} deploy-{staging,x}`), or None when no word has braces.

    Quoting is no longer known, so quoted braces are expanded too: that only adds a command to check."""
    if not any("{" in w for w in cmd.argv):
        return None
    argv = []
    for word in cmd.argv:
        words = globs.expand_braces(word)
        argv.extend(w for w in (words if words is not None else [word]) if w)
    if not argv or argv == cmd.argv:
        return None
    return shell.Cmd(argv, cmd.env, cmd.redirects, cmd.cwd, cmd.depth, cmd.stdin_text)


FD_EXEC = ("-x", "--exec", "-X", "--exec-batch")


# fd's short options that take a value, so an x after them in a cluster is that value, not -x.
_FD_VALUE = "tedESjoc"


def _fd_exec(args):
    """The command fd runs with -x/-X (also clustered, -Hx, or attached, -xmake, --exec=make): the words after it,
    up to a ; of its own."""
    for i, a in enumerate(args):
        head = None
        if a in FD_EXEC:
            head = []
        elif a.startswith(("--exec=", "--exec-batch=")):
            head = [a.split("=", 1)[1]]
        elif a.startswith("-") and not a.startswith("--"):
            for k, letter in enumerate(a[1:], 1):
                if letter in _FD_VALUE:
                    break
                if letter in "xX":
                    head = [a[k + 1:]] if a[k + 1:] else []
                    break
        if head is None:
            continue
        run = head[:]
        for w in args[i + 1:]:
            if w == ";":
                break
            run.append(w)
        return [run] if run and run[0] else []
    return []


def _find_exec(args):
    runs = []
    i = 0
    while i < len(args):
        if args[i] in FIND_EXEC:
            j = i + 1
            while j < len(args) and args[j] not in (";", "+"):
                j += 1
            if j > i + 1:
                runs.append(args[i + 1:j])
            i = j
        i += 1
    return runs


def unreadable(command, cwd=None, home=None):
    """(level, reason, env_split) when the parser could not read the whole command (unknown text piped into a
    shell, an env -S string it cannot split exactly, or nesting too deep), else None. env_split is True for the
    env -S case, which can hide a lock push and so matters even with no environments guarded."""
    result = shell.parse((command or "")[:MAX_COMMAND], cwd=cwd, home=home)
    if not result.problem and not result.env_split:
        return None
    return result.problem_level or "ask", "berth cannot see everything this command runs", \
        result.env_split


def deploy_targets(command, environments, generic=True, cwd=None, home=None):
    """Return [(environment, matched line)] for every guarded environment the command deploys to."""
    if not command or not environments:
        return []
    text = command[:MAX_COMMAND]
    cmds = commands(text, cwd, home)
    compiled = {env: _compile(spec.get("patterns", [])) for env, spec in environments.items()}
    words = {env: _word(env) for env in environments}
    hits = []
    for cmd in cmds:
        if cmd.name in READERS or is_berth_cli(cmd):
            continue  # Printing or reading a deploy file, or claiming the environment, is not a deploy.
        line = match_line(cmd)
        # Patterns are written with the program as it is spelled; berth reads names in lower case, so both count.
        lines = (line, " ".join([os.path.basename(cmd.argv[0])] + list(cmd.args)))
        for env in environments:
            if any(env == e for e, _ in hits):
                continue
            if any(p.search(t) for p in compiled[env] for t in lines) or (generic and _generic(cmd, words[env])):
                hits.append((env, line[:200]))
    return hits


def names_deploy(command, cwd=None, home=None):
    """True when a command names a deploy step at all (used when the config cannot say which are guarded)."""
    for cmd in commands((command or "")[:MAX_COMMAND], cwd, home):
        if cmd.name in READERS or is_berth_cli(cmd):
            continue
        words = _script_step(cmd, list(cmd.args)) or _step_words(cmd, list(cmd.args))
        if any(os.path.basename(w).lower() in TEST_RUNNERS for w in words):
            continue
        if any(seg in DEPLOY_WORDS for w in words for seg in _segments(w)):
            return True
    return False


def tamper(command, cwd=None, home=None):
    """A reason string when a command would push, delete or rewrite a lock branch without berth, else None."""
    if not command:
        return None
    text = command[:MAX_COMMAND]
    cmds = commands(text, cwd, home)
    # Brace expansion can build a lock name the raw text does not hold (`deploy-{lock,x}`).
    loose = any(LOOSE_LOCK.search(plain(t)) for t in [text] + [" ".join(c.argv) for c in cmds])
    for cmd in cmds:
        named = any(_mentions_lock(a) for a in cmd.args)
        if cmd.name == "git":
            sub, rest = _git_sub(cmd.args)
            rest = _drop_messages(sub, rest)
            if sub in STDIN_REF_SUBS and (sub == "fast-import" or "--stdin" in rest):
                refs = cmd.stdin_text or text  # From a pipe: the refs could be anywhere in the command.
                if LOOSE_LOCK.search(plain(refs)):
                    return "git %s with refs on stdin that name a lock branch" % sub
            if any(_mentions_lock(a) for a in _git_configs(cmd) + rest):
                reason = _git_tamper(sub, rest)
                if reason:
                    return reason
            if loose and sub in PUSHERS and (_push_named_at_run_time(cmd, rest) or _XARGS.search(plain(text))):
                return "a push of a branch named when the command runs, in a command that also names a lock branch"
            continue
        if not (named or loose):
            continue
        if cmd.name in API_CLIS and cmd.args[:1] == ["api"]:
            method = _gh_method(cmd.args)
            if method not in _READ_METHODS:
                return "%s api %s on a lock ref" % (cmd.name, method)
        elif cmd.name in HTTP_CLIENTS and (_http_writes(cmd.name, cmd.args)
                                           or any(k in (cmd.env or {}) for k in _HTTP_CONFIG_ENV)):
            return "%s writing to a lock ref" % cmd.name
        else:
            program = program_text(cmd, text)
            if program is not None and LOOSE_LOCK.search(program):
                return "inline %s code that names a lock branch" % cmd.name
            if named and program is None and cmd.name not in READERS \
                    and _RELAYED_PUSH.search(plain(" ".join(cmd.argv))):
                return "%s running a push or ref write in a command that names a lock branch" % cmd.name
    return None


# A push or ref write that another program runs (setsid, osascript, su -c, jj git push, hub push, git-push):
# berth cannot unwrap every such program, so in a command that names a lock branch the words are enough.
_RELAYED_PUSH = re.compile(r"(^|[\s\"';(-])(push|send-pack|receive-pack|update-ref|fast-import)(\b|$)",
                           re.IGNORECASE)


MESSAGE_OPTIONS = {"-m", "--message", "-F", "--file", "-C", "-c", "--reuse-message", "--reedit-message"}
MESSAGE_SUBS = {"commit", "tag", "stash", "notes", "merge", "revert", "cherry-pick"}


def _drop_messages(sub, rest):
    """A commit's or tag's arguments without its message: lock words in a message write no branch."""
    if sub not in MESSAGE_SUBS:
        return rest
    out = []
    skip = False
    for a in rest:
        if skip:
            skip = False
        elif a in MESSAGE_OPTIONS:
            skip = True
        elif a.startswith(("--message=", "--file=")):
            continue
        elif a.startswith("-") and not a.startswith("--") and ("m" in a or "F" in a):
            # A short-flag cluster (-am, -asm): the message is the rest of the word, or the next word.
            at = min(a.index(x) for x in "mF" if x in a)
            skip = at == len(a) - 1
        else:
            out.append(a)
    return out


# git's global options that take a value, kept in one place: the vendored parser's list.
GIT_GLOBAL_VALUE = context._GIT_GLOBAL_VALUE


def _git_configs(cmd):
    """One-off git settings: the values of git's own -c and --config-env options, and settings passed in
    GIT_CONFIG_KEY_n/GIT_CONFIG_VALUE_n or GIT_CONFIG_PARAMETERS variables, as key=value."""
    args = cmd.args
    out = []
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a in ("-c", "--config-env") and i + 1 < len(args):
            out.append(args[i + 1])
            i += 2
            continue
        if a.startswith("--config-env="):
            out.append(a.split("=", 1)[1])
        i += 2 if a in GIT_GLOBAL_VALUE else 1
    env = cmd.env or {}
    for key, value in env.items():
        if key.startswith("GIT_CONFIG_KEY_"):
            out.append("%s=%s" % (value, env.get("GIT_CONFIG_VALUE_" + key[len("GIT_CONFIG_KEY_"):], "")))
        elif key == "GIT_CONFIG_PARAMETERS":
            out.extend(shlex.split(value) if value else [])
    return out


_PUSH_SETTING = re.compile(r"^(remote\.[^=]*\.(push|pushurl|url|mirror)|url\.[^=]*\.(pushinsteadof|insteadof)"
                           r"|push\.default)=?",
                           re.IGNORECASE)


# git commands that take the refs they write on stdin.
STDIN_REF_SUBS = ("send-pack", "update-ref", "fast-import")
# git's global options that take no value. Any other long option before the subcommand is unknown to berth.
GIT_GLOBAL_FLAGS = {"--bare", "--no-pager", "--paginate", "--no-replace-objects", "--literal-pathspecs",
                    "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs", "--no-optional-locks",
                    "--no-advice", "--html-path", "--man-path", "--info-path", "--version", "--help", "--exec-path",
                    "--no-lazy-fetch", "--replace-objects"}


def unknown_git_option(command, cwd=None, home=None):
    """A reason when git gets a long option before its subcommand that berth does not know: it might take a value
    and hide which subcommand runs. Else None."""
    if not command or "git" not in command:
        return None
    for cmd in commands(command[:MAX_COMMAND], cwd, home):
        if cmd.name != "git":
            continue
        args = cmd.args
        i = 0
        while i < len(args) and args[i].startswith("-"):
            a = args[i]
            name = a.split("=", 1)[0]
            if a.startswith("--") and name not in GIT_GLOBAL_VALUE and name not in GIT_GLOBAL_FLAGS:
                return "git option %s, which berth does not know, before the subcommand" % name
            i += 2 if a in GIT_GLOBAL_VALUE else 1
    return None


def _push_named_at_run_time(cmd, rest):
    """True when a push's branches come from a variable or a substitution, or a delete names none (xargs adds
    them), rather than from its own words."""
    if any(r in a for a in rest for r in RUNTIME):
        return True
    deletes = any(a in ("-d", "--delete") for a in rest)
    return deletes and len([a for a in rest if not a.startswith("-")]) <= 1


# Push options that take the next word as their value, so a bracket in that value is not read as a pattern.
_PUSH_VALUE = ("-o", "--push-option", "--repo", "--receive-pack", "--exec", "--signed")


def _push_words(rest):
    """A push's remote and refspecs: its words that are neither options nor option values."""
    out = []
    skip = False
    for a in rest:
        if skip:
            skip = False
        elif a in _PUSH_VALUE:
            skip = True
        elif not a.startswith("-"):
            out.append(a)
    return out


def runtime_push(command, cwd=None, home=None):
    """A reason when a push could reach a lock branch without naming it: every local branch at once, or a branch
    named only when the command runs that it deletes, forces, or builds near the word "lock". Else None."""
    if not command:
        return None
    mentions_lock = "lock" in plain(command).lower()
    for cmd in commands(command[:MAX_COMMAND], cwd, home):
        if cmd.name != "git":
            continue
        sub, rest = _git_sub(cmd.args)
        if sub not in PUSHERS:
            continue
        if any(_PUSH_SETTING.match(c) for c in _git_configs(cmd)) or "GIT_CONFIG_" in plain(command):
            return "a push whose branches or address come from a one-off git setting, which could reach a lock branch"
        words = _push_words(rest)
        refspecs = [a.lstrip("+") for a in words][1:]
        wild = [r for r in refspecs if "*" in r and not r.split(":", 1)[-1].startswith("refs/tags/")]
        if any(a in ("--all", "--branches") for a in rest) or wild or ":" in refspecs:
            return "a push of every local branch, which would include a local copy of a lock branch"
        if any(c in a for a in words for c in "?["):
            return "a push of a branch named by a file name pattern, which the shell fills in when it runs"
        if not any(r in a for a in rest for r in RUNTIME):
            continue
        deletes = any(a in ("-d", "--delete") or a.startswith(":") for a in rest)
        forces = any(a in ("-f", "--force") or a.startswith("--force-with-lease") or a.startswith("+")
                     for a in rest)
        if deletes or forces or mentions_lock:
            verb = "deletes" if deletes else "force-updates" if forces else "updates"
            return "a push that %s a branch named only when the command runs, which could be a lock branch" % verb
    return None


GIT_CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list", "--show-origin",
                    "--show-scope", "--name-only", "get", "list"}
# Settings that change where a push goes or which branches it sends (push.default matching sends every branch,
# local lock copies included).
# include.* and includeIf.* pull in another file that can set any of these.
_REMOTE_KEY = re.compile(r"^((remote|url|include|includeif)\.|push\.)", re.IGNORECASE)


def remote_change(command, cwd=None, home=None):
    """A reason when a command changes where the remote points (berth reads and writes locks there), else None."""
    if not command or "git" not in command:
        return None
    for cmd in commands(command[:MAX_COMMAND], cwd, home):
        if cmd.name != "git":
            continue
        sub, rest = _git_sub(cmd.args)
        if sub == "remote" and rest[:1] and rest[0] in ("set-url", "rename", "remove", "rm"):
            return "git remote %s changes where berth reads and writes claims" % rest[0]
        if sub == "config" and _config_writes(rest):
            if any(_REMOTE_KEY.match(a) for a in rest if not a.startswith("-")):
                return ("a git config change to a remote, url or push setting changes where berth reads and writes "
                        "claims")
    return None


GIT_CONFIG_WRITES = {"--unset", "--unset-all", "--add", "--replace-all", "--rename-section", "--remove-section",
                     "-e", "--edit", "set", "unset", "rename-section", "remove-section", "edit"}
GIT_CONFIG_GETS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch"}
GIT_CONFIG_VALUE = {"-f", "--file", "--blob", "--type", "--default", "--comment", "--value"}


def _config_writes(rest):
    """True when git config sets or removes something: a write flag or verb, or a key followed by a value. A verb
    counts only as the first word (git config remote.origin.url get sets the url to "get")."""
    flags = []
    words = []
    skip = False
    for a in rest:
        if skip:
            skip = False
        elif words:
            words.append(a)  # In the legacy form git reads options only before the key: --get after it is a value.
        elif a in GIT_CONFIG_VALUE:
            skip = True
        elif a.startswith("-"):
            flags.append(a.split("=", 1)[0])
        else:
            words.append(a)
    if any(f in GIT_CONFIG_GETS for f in flags):
        return False
    if any(f in GIT_CONFIG_READS for f in flags) and len(words) < 2:
        return False  # git config -l / --show-origin with a key and a value still sets it (legacy form).
    if any(f in GIT_CONFIG_WRITES for f in flags):
        return True
    if words and words[0] in GIT_CONFIG_READS:
        return False
    if words and words[0] in GIT_CONFIG_WRITES:
        return True
    return len(words) >= 2


_MAY_PRUNE = re.compile(r"--mirror|--prune")


def prunes_remote(command, cwd=None, home=None):
    """A reason when a push would delete remote branches it does not name (so any lock branch), else None."""
    if not command or not _MAY_PRUNE.search(plain(command)):
        return None
    for cmd in commands(command[:MAX_COMMAND], cwd, home):
        if cmd.name == "git":
            reason = _git_prunes_remote(cmd.args)
            if reason:
                return reason
    return None


def _git_prunes_remote(args):
    sub, rest = _git_sub(args)
    if sub not in PUSHERS:
        return None
    if "--mirror" in rest:
        return "a mirror push deletes every remote branch you do not have locally, lock branches included"
    if "--prune" in rest:
        # A prune deletes only inside the refspecs pushed: exact ones (trunk:trunk) are safe, while a wildcard,
        # --all, --branches or no refspec at all (the remote's configured push) can reach lock branches.
        refspecs = [a for a in rest if not a.startswith("-")][1:]
        if any(a in ("--all", "--branches") for a in rest) or not refspecs or any("*" in r for r in refspecs):
            return "a pruning push deletes remote branches you do not have locally, lock branches included"
    return None


# git subcommands that only read: naming a lock branch in them is fine (berth's skill shows locks this way).
GIT_READS = {
    "fetch", "ls-remote", "log", "show", "diff", "rev-parse", "rev-list", "cat-file", "for-each-ref", "show-ref",
    "status", "grep", "blame", "describe", "merge-base", "shortlog", "name-rev", "ls-tree", "whatchanged",
    "reflog",
}
GIT_LIST_FLAGS = {"-l", "--list", "-r", "--remotes", "-a", "--all", "--contains", "--no-contains", "--merged",
                  "--no-merged", "--points-at", "--show-current", "-v", "-vv", "--verbose"}


def _git_tamper(sub, rest):
    """Any git command that names a lock branch and could create, move, delete or push it: a local copy of a lock
    branch is one plain push away from replacing the real one."""
    if sub == "fetch":
        # A fetch writes local branches only through a refspec destination.
        dests = [a.split(":", 1)[1] for a in rest if ":" in a and not a.startswith("-")]
        dests = [d for d in dests if not d.lstrip("+").startswith("refs/remotes/")]
        return "a fetch into a local lock branch" if any(_mentions_lock(d) for d in dests) else None
    if sub in GIT_READS or sub in ("commit", "add") or (sub == "stash" and rest[:1] != ["branch"]):
        return None
    if sub == "branch" and rest and all(a in GIT_LIST_FLAGS or not a.startswith("-") for a in rest) \
            and any(a in GIT_LIST_FLAGS for a in rest):
        return None
    if sub in PUSHERS:
        return "a push to a lock branch"
    return "git %s naming a lock branch" % (sub or "")


# Short options that take a value in curl's parser, so a cluster with a value at its end is read as it would be there.
_CURL_VALUE = "AbcCdDeEFHmoPQrtTuUwxXyYz"
_READ_METHODS = ("GET", "HEAD")


def _http_writes(name, args):
    if name == "curl":
        return _curl_writes(args)
    if name == "wget":
        return _wget_writes(args)
    return any(a.upper() in ("DELETE", "PATCH", "POST", "PUT") or re.match(r"^[\w.-]+:?=(?!=)", a) for a in args)


def _curl_writes(args):
    i = 0
    while i < len(args):
        a = args[i]
        following = args[i + 1] if i + 1 < len(args) else ""
        if a == "--":
            return False
        if a.startswith("--"):
            option, _sep, value = a.partition("=")
            if option == "--request":
                if (value if _sep else following).upper() not in _READ_METHODS:
                    return True
            elif option in ("--json", "--upload-file", "--config") or option.startswith(("--data", "--form")):
                return True  # --config reads more options (a method, a body) from a file or stdin.
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            step = 1
            for k, letter in enumerate(a[1:], 1):
                if letter in "dFTK":
                    return True
                if letter in _CURL_VALUE:
                    value = a[k + 1:] or following
                    if letter == "X" and value.upper() not in _READ_METHODS:
                        return True
                    step = 1 if a[k + 1:] else 2
                    break
            i += step
            continue
        i += 1
    return False


# Options in wget's parser that send a method or a body; it also accepts any unambiguous start of a long option.
_WGET_WRITES = ("--method", "--post-data", "--post-file", "--body-data", "--body-file", "--execute", "--config")
# Variables that point an HTTP client at a config file of its own, which can set a method or a body.
_HTTP_CONFIG_ENV = ("WGETRC", "CURL_HOME", "XDG_CONFIG_HOME", "HOME")


def _wget_writes(args):
    for i, a in enumerate(args):
        if a == "--":
            return False
        if a.startswith("--"):
            option, sep, value = a.partition("=")
            if len(option) < 4 or not any(w.startswith(option) for w in _WGET_WRITES):
                continue
            if "--method".startswith(option):
                value = value if sep else (args[i + 1] if i + 1 < len(args) else "")
                if value.upper() in _READ_METHODS:
                    continue
            return True
        if a.startswith("-") and not a.startswith("--") and "e" in a[1:]:
            return True  # -e runs a wgetrc command (method=, post_data=).
    return False


UNKNOWN = object()  # An interpreter reading its program from a pipe: the program is not on the command line.
# Options that take a value, per interpreter, so the value is not read as the script or a flag cluster.
VALUE_OPTIONS = {
    "python": "WXQ", "perl": "IMm", "ruby": "IrCE", "node": "rC", "nodejs": "rC", "php": "cdfz", "bun": "r",
}
# Long options that take the next word as their value (unless given as --option=value).
LONG_VALUE_OPTIONS = {
    "python": {"--check-hash-based-pycs"},
    "node": {"--require", "--import", "--loader", "--experimental-loader", "--conditions", "--input-type", "--title",
             "--eval-file", "--env-file", "--inspect-port", "--redirect-warnings", "--stack-size"},
    "bun": {"--require", "--preload", "--import", "--conditions", "--env-file", "--cwd", "--config", "--tsconfig-override"},
}
LONG_VALUE_OPTIONS["nodejs"] = LONG_VALUE_OPTIONS["node"]
# Modules that run their arguments as code.
CODE_MODULES = {"timeit"}
_INTERPRETER = re.compile(r"^(python|perl|ruby|nodejs|node|php|bun|deno)[0-9.]*$")
# Options after which the interpreter runs a named module or file, never a program from stdin.
SCRIPT_OPTIONS = {"python": "m", "php": "f", "node": "", "nodejs": "", "ruby": "", "perl": "", "bun": ""}


def inline_program(cmd):
    """The program an interpreter runs from its own command line or stdin: the text (the program and its
    arguments, plus any heredoc or here-string), UNKNOWN when it reads one from a pipe, or None when it runs a
    script file or is not an interpreter."""
    found = _INTERPRETER.match(cmd.name)
    base = found.group(1) if found else ""
    flags = INLINE_FLAGS.get(base)
    if not flags:
        return None
    args = list(cmd.args)
    letters = "".join(f[1:] for f in flags if len(f) == 2 and f.startswith("-"))
    values = VALUE_OPTIONS.get(base, "")
    i = 0
    while i < len(args):
        a = args[i]
        if a in flags:
            return " ".join(args[i + 1:]) + "\n" + cmd.stdin_text
        if a.startswith("--") and a.split("=", 1)[0] in flags:
            return " ".join([a.split("=", 1)[1]] + args[i + 1:]) + "\n" + cmd.stdin_text
        if a == "--":
            i += 1
            break
        if a.startswith("--"):
            i += 2 if a in LONG_VALUE_OPTIONS.get(base, ()) else 1
            continue
        if a.startswith("-") and len(a) > 1:
            for j, letter in enumerate(a[1:], 1):
                if letter in letters:
                    return " ".join([a[j + 1:]] + args[i + 1:]) + "\n" + cmd.stdin_text
                if letter in SCRIPT_OPTIONS.get(base, ""):
                    module = a[j + 1:] or (args[i + 1] if i + 1 < len(args) else "")
                    return " ".join(args[i + 1:]) if module in CODE_MODULES else None
                if letter in values:
                    i += 0 if a[j + 1:] else 1  # The value is the rest of the word, or the next word.
                    break
            i += 1
            continue
        break
    script = args[i] if i < len(args) else "-"
    if script not in shell.STDIN_PATHS:
        # An option berth does not know may have taken a value and moved the script; an inline flag anywhere
        # before -- still means inline code.
        for k, a in enumerate(args):
            if a == "--":
                break
            if a in flags:
                return " ".join(args[k + 1:]) + "\n" + cmd.stdin_text
        return None
    return cmd.stdin_text or UNKNOWN


def program_text(cmd, command):
    """An interpreter's program with quotes and backslashes removed: its own, or the whole command when it comes
    from a pipe. None when cmd runs no program of its own."""
    program = inline_program(cmd)
    if program is None:
        return None
    return plain(command if program is UNKNOWN else program)


def _git_sub(args):
    i = 0
    takes = GIT_GLOBAL_VALUE
    while i < len(args):
        a = args[i]
        if a in takes:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, args[i + 1:]
    return "", []


def _gh_method(args):
    for i, a in enumerate(args):
        if a in ("-X", "--method") and i + 1 < len(args):
            return args[i + 1].upper()
        if a.startswith("--method="):
            return a.split("=", 1)[1].upper()
        if a.startswith("-X") and len(a) > 2:
            return a[2:].upper()
    if any(a in ("-f", "-F", "--field", "--raw-field", "--input") or a.startswith(("--field=", "--raw-field="))
           for a in args):
        return "POST"
    return "GET"


def berth_cli_calls(command, cwd=None, home=None):
    """Return [(script, subcommand, Cmd)] for each call to berth's own CLIs (berth.py, berthcfg.py) in a command."""
    out = []
    if not command or "berth" not in plain(command).lower():
        return out
    for cmd in commands(command[:MAX_COMMAND], cwd, home):
        i = _cli_word_index(cmd)
        if i is None:
            continue
        words = [cmd.name] + list(cmd.args)
        out.append((os.path.basename(words[i]).lower(), _cli_sub(words[i + 1:]), cmd))
    return out


def _cli_sub(args):
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--data", "--project-dir"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return ""
