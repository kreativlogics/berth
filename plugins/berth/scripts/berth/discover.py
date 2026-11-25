"""Suggest guarded environments and deploy patterns from files already in the project.

Read-only and local: CI files, Makefiles, package.json scripts, Capistrano stages and deploy scripts. Every name is
checked against the environment-name rule and escaped before it goes into a pattern, so a file cannot inject regex.
Suggestions are only printed; `berthcfg.py init` writes them after a person confirms.
"""

import json
import os
import re

from . import cfg

MAX_FILE = 512 * 1024
NAME_HINT = re.compile(r"^(prod|production|stag|staging|stage|qa|test|uat|dev|develop|demo|preview|sandbox)",
                       re.IGNORECASE)


def _read(path):
    try:
        if os.path.getsize(path) > MAX_FILE:
            return ""
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _ok(name):
    return bool(name) and bool(cfg.ENV_NAME.match(name)) and not name.endswith("-branch-deploy-lock")


def _add(found, env, pattern, source):
    if not _ok(env):
        return
    entry = found.setdefault(env, {"patterns": [], "sources": []})
    if pattern not in entry["patterns"]:
        entry["patterns"].append(pattern)
    if source not in entry["sources"]:
        entry["sources"].append(source)


def _github_actions(project, found):
    folder = os.path.join(project, ".github", "workflows")
    if not os.path.isdir(folder):
        return
    for fname in sorted(os.listdir(folder)):
        if not fname.endswith((".yml", ".yaml")):
            continue
        text = _read(os.path.join(folder, fname))
        names = set(re.findall(r"^\s*environment:\s*['\"]?([A-Za-z0-9][A-Za-z0-9._-]*)['\"]?\s*$", text, re.M))
        names |= set(re.findall(r"^\s*environment:\s*\n\s+name:\s*['\"]?([A-Za-z0-9][A-Za-z0-9._-]*)", text, re.M))
        for env in sorted(names):
            src = ".github/workflows/%s" % fname
            _add(found, env, r"^gh workflow run\b.*\b%s\b" % re.escape(env), src)
            _add(found, env, r"^gh pr comment\b.*\.deploy\b.*\b%s\b" % re.escape(env), src)


def _gitlab(project, found):
    text = _read(os.path.join(project, ".gitlab-ci.yml"))
    names = set(re.findall(r"^\s*environment:\s*['\"]?([A-Za-z0-9][A-Za-z0-9._-]*)['\"]?\s*$", text, re.M))
    names |= set(re.findall(r"^\s*environment:\s*\n\s+name:\s*['\"]?([A-Za-z0-9][A-Za-z0-9._-]*)", text, re.M))
    for env in sorted(names):
        # GitLab deploys run in CI; locally they are usually a deploy step or a pipeline call naming the env.
        # "deploy" must be the program or one of its first three words, so a commit message does not count.
        _add(found, env, r"^(?!git )(\S+ ){0,3}\S*\bdeploy\b.*\b%s\b" % re.escape(env), ".gitlab-ci.yml")


def _makefile(project, found):
    for fname in ("Makefile", "makefile", "GNUmakefile"):
        text = _read(os.path.join(project, fname))
        for env in sorted(set(re.findall(r"^deploy[-_]([A-Za-z0-9][A-Za-z0-9._-]*)\s*:", text, re.M))):
            _add(found, env, r"^make (.* )?deploy[-_]%s(\s|$)" % re.escape(env), fname)


def _package_json(project, found):
    text = _read(os.path.join(project, "package.json"))
    if not text:
        return
    try:
        scripts = (json.loads(text) or {}).get("scripts") or {}
    except (ValueError, AttributeError):
        return
    if not isinstance(scripts, dict):
        return
    for key in sorted(scripts):
        m = re.match(r"^deploy[:_-]([A-Za-z0-9][A-Za-z0-9._-]*)$", str(key))
        if m:
            _add(found, m.group(1), r"^(npm|yarn|pnpm|bun) (run )?%s(\s|$)" % re.escape(key), "package.json")


def _capistrano(project, found):
    folder = os.path.join(project, "config", "deploy")
    if not os.path.isdir(folder):
        return
    for fname in sorted(os.listdir(folder)):
        if fname.endswith(".rb"):
            env = fname[:-3]
            _add(found, env, r"^(bundle exec )?cap %s\b" % re.escape(env), "config/deploy/%s" % fname)


# Wrapper programs in front of the runner are removed by the parser before matching, so they need no place here.
SCRIPT_RUNNERS = r"(sh|bash|zsh|dash|ksh|fish|python[0-9.]*|py|node|nodejs|deno|bun|ruby|" \
                 r"perl|php|tsx|ts-node|npx|bunx|uv|uvx|pipx)"


def _scripts(project, found):
    for folder in ("scripts", "bin", "."):
        path = os.path.join(project, folder)
        if not os.path.isdir(path):
            continue
        for fname in sorted(os.listdir(path)):
            if not fname.lower().startswith("deploy") or not os.path.isfile(os.path.join(path, fname)):
                continue
            m = re.match(r"^deploy[-_]([A-Za-z0-9][A-Za-z0-9._-]*?)(\.[a-z]+)?$", fname)
            src = os.path.normpath(os.path.join(folder, fname))
            if m and NAME_HINT.match(m.group(1)):
                # The script as the program, or as what a shell or interpreter runs; naming it (git add, chmod,
                # an editor) is not a deploy.
                _add(found, m.group(1), r"^(%s (.* )?)?(\S*/)?%s(\s|$)" % (SCRIPT_RUNNERS, re.escape(fname)), src)


def discover(project):
    """Return {env: {"patterns": [...], "sources": [...]}}. Never raises on unreadable files."""
    found = {}
    if not project or not os.path.isdir(project):
        return found
    for step in (_github_actions, _gitlab, _makefile, _package_json, _capistrano, _scripts):
        try:
            step(project, found)
        except OSError:
            continue
    return found
