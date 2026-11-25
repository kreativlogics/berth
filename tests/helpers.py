"""Test helpers: a throwaway HOME, data folder, project repo and a local bare repo as its remote.

Nothing here talks to a real remote. Token-shaped values are assembled at run time so no secret-shaped string sits
in the repository.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "berth")
SCRIPTS = os.path.join(PLUGIN, "scripts")
HOOK = os.path.join(SCRIPTS, "hook.py")
BERTH = os.path.join(SCRIPTS, "berth.py")
BERTHCFG = os.path.join(SCRIPTS, "berthcfg.py")
sys.dont_write_bytecode = True
sys.path.insert(0, SCRIPTS)

from berth import cfg, claims, locks, store  # noqa: E402

STAGING_PATTERN = r"^make (.* )?deploy-staging(\s|$)"


def fake(kind):
    body = "Q7w9ErT2yU4iO6pA8sD1fG3hJ5kL0zX"
    if kind == "github":
        return "gh" + "p_" + body + "abcde"
    raise KeyError(kind)


class Case(unittest.TestCase):
    """A temp HOME, data folder, project (a git repo, user alice) and a bare remote named origin."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="berth-test-")
        self.home = os.path.join(self.tmp, "home")
        self.data = os.path.join(self.tmp, "data")
        self.project = os.path.join(self.tmp, "project")
        self.remote = os.path.join(self.tmp, "remote.git")
        for d in (self.home, self.data, self.project):
            os.makedirs(d)
        self.env = {
            "HOME": self.home,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "CLAUDE_PLUGIN_DATA": self.data,
            "CLAUDE_PROJECT_DIR": self.project,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        # In-process store calls run git too: give them the same isolated environment.
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.git("init", "-q", "--bare", self.remote, cwd=self.tmp)
        self.git("init", "-q", "-b", "trunk")
        self.git("config", "user.name", "alice")
        self.git("commit", "-q", "--allow-empty", "-m", "first")
        self.git("remote", "add", "origin", self.remote)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ processes

    def git(self, *args, cwd=None, check=True, stdin=None):
        return subprocess.run(["git"] + list(args), cwd=cwd or self.project, env=self.env, input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=check)

    def run_script(self, script, *args, extra_env=None, stdin="", cwd=None, drop_env=()):
        env = dict(self.env)
        env.update(extra_env or {})
        for key in drop_env:
            env.pop(key, None)
        return subprocess.run([sys.executable, script] + list(args), cwd=cwd or self.project, env=env, input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)

    def cli(self, *args, **kw):
        return self.run_script(BERTH, *args, **kw)

    def hook(self, mode, payload, extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        res = subprocess.run([sys.executable, HOOK, mode], input=json.dumps(payload), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "", res.stderr)
        return json.loads(res.stdout) if res.stdout.strip() else None

    def pre(self, tool, tool_input, mode="default", session="s1", extra_env=None):
        out = self.hook("pre", {"tool_name": tool, "tool_input": tool_input, "cwd": self.project,
                                "permission_mode": mode, "session_id": session}, extra_env)
        if out is None:
            return "allow", ""
        spec = out["hookSpecificOutput"]
        return spec["permissionDecision"], spec["permissionDecisionReason"]

    def bash(self, command, **kw):
        return self.pre("Bash", {"command": command}, **kw)

    def assertDecision(self, expected, result, contains=None):
        decision, reason = result
        self.assertEqual(decision, expected, reason)
        if contains:
            self.assertIn(contains, reason)

    # ------------------------------------------------------------ config and locks

    def write_config(self, data, where="user"):
        data.setdefault("version", 1)
        path = os.path.join(self.data, "config.json") if where == "user" else \
            os.path.join(self.project, ".berth", "config.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(data, fh)
        return path

    def guard_staging(self, **extra):
        conf = {"environments": {"staging": {"patterns": [STAGING_PATTERN]}}}
        conf.update(extra)
        return self.write_config(conf)

    def service(self, identity=None, now=None):
        eff = cfg.resolve(self.data, self.project)
        svc = claims.Service(eff, self.project, self.data, now=now)
        if identity:
            svc.me = identity
        return svc

    def put_lock(self, env, holder, hours=1.0, created_hours_ago=0.0, extra=None, drop=()):
        """Write a lock as someone else would (straight to the remote). Returns the commit sha."""
        now = locks.utcnow()
        record = locks.new_record(env, holder, "their deploy", "feature",
                                  now + datetime.timedelta(hours=hours),
                                  now - datetime.timedelta(hours=created_hours_ago))
        for key in drop:
            record.pop(key, None)
        record.update(extra or {})
        branch = locks.GLOBAL_BRANCH if env is None else locks.branch_name(env)
        return store.GitStore(self.project, "origin").create(branch, record)

    def remote_refs(self):
        out = self.git("ls-remote", self.remote).stdout
        return {line.split("\t")[1]: line.split("\t")[0] for line in out.splitlines() if "\t" in line}
