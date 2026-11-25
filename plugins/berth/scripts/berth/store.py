"""Where claims live.

GitStore keeps them as lock branches on a git remote, exactly where github/branch-deploy keeps its locks, using
plain git (no API, no token beyond your git access). LocalStore keeps them in a folder, for solo use and tests.

Every write is a compare-and-swap:
- create pushes an orphan commit, which can never fast-forward an existing ref, so it only succeeds if the lock
  branch does not exist;
- update pushes a child of the lock commit that was read (fast-forward only);
- delete uses --force-with-lease with the sha that was read, so it removes the ref only if nobody changed it.
No other kind of push is made.
"""

import errno
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time

from . import locks

GIT_TIMEOUT = 20
# The only variables git sees: what it needs to find its config, reach the remote (SSH agent, credential helpers,
# proxies, CA bundles) and name the commit author. Everything else in the session's environment stays out.
GIT_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "USERPROFILE", "APPDATA",
    "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "GIT_EXEC_PATH",
    "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH",
    "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_CONFIG_DIR", "GH_ENTERPRISE_TOKEN",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL",
)


def git_env(extra=None):
    """The minimal environment for a git subprocess, with prompts turned off."""
    env = {k: os.environ[k] for k in GIT_ENV_KEYS if k in os.environ}
    # Your own ssh settings (core.sshCommand, GIT_SSH) stay as they are; git runs in its own session with no
    # terminal and no stdin, so ssh cannot stop to ask for a passphrase or a host key.
    env.update({"LC_ALL": "C", "LANG": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "",
                "SSH_ASKPASS": "", "GCM_INTERACTIVE": "never"})
    env.update(extra or {})
    return env


_GIT_VERSION = []
_DEADLINE = []


def set_budget(seconds):
    """Give every git call from now on one shared deadline (hooks use it so the total stays under their timeout)."""
    del _DEADLINE[:]
    if seconds:
        _DEADLINE.append(time.monotonic() + seconds)


def _timeout():
    if not _DEADLINE:
        return GIT_TIMEOUT
    left = _DEADLINE[0] - time.monotonic()
    if left <= 0.5:
        raise StoreError("the remote did not answer in time.")
    return min(GIT_TIMEOUT, left)


def git_version():
    """The installed git's version as a tuple, (0,) when it cannot be read. Read once per process."""
    if not _GIT_VERSION:
        version = (0,)
        try:
            res = subprocess.run(["git", "--version"], env=git_env(), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
                                 timeout=GIT_TIMEOUT)
            found = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", res.stdout or "")
            if found:
                version = tuple(int(x or 0) for x in found.groups())
        except (OSError, subprocess.SubprocessError):
            pass
        _GIT_VERSION.append(version)
    return _GIT_VERSION[0]


class StoreError(Exception):
    """The store could not be read or written (network, permissions, a broken repo)."""


class Conflict(Exception):
    """A compare-and-swap lost: the lock changed between read and write."""


def make(spec, project_dir, data_dir):
    if spec.get("type") == "local":
        return LocalStore(os.path.expanduser(spec["path"]))
    return GitStore(project_dir, spec.get("remote") or "origin")


class GitStore(object):
    kind = "git"

    def __init__(self, repo_dir, remote="origin"):
        self.repo_dir = repo_dir
        self.remote = remote

    def describe(self):
        return "git remote %s" % self.remote

    def _git(self, args, stdin=None, env_extra=None, check=True):
        env = git_env(env_extra)
        limit = _timeout()
        try:
            extra = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
            res = subprocess.run(["git"] + args, cwd=self.repo_dir, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, universal_newlines=True, timeout=limit,
                                 start_new_session=True, **extra)
        except subprocess.TimeoutExpired:
            raise StoreError("git %s timed out after %.0f s." % (args[0], limit))
        except OSError as exc:
            raise StoreError("could not run git (%s)." % exc)
        if check and res.returncode != 0:
            raise StoreError("git %s failed: %s" % (args[0], _last_line(res.stderr)))
        return res

    def check(self):
        """Raise StoreError unless the folder is a git repo with the remote configured."""
        res = self._git(["rev-parse", "--git-dir"], check=False)
        if res.returncode != 0:
            raise StoreError("%s is not inside a git repository." % self.repo_dir)
        res = self._git(["remote"], check=False)
        if self.remote not in res.stdout.split():
            raise StoreError("the git remote %r is not configured in %s." % (self.remote, self.repo_dir))

    def read(self, branches):
        """Return {branch: Lock} for the branches that exist on the remote."""
        self.check()
        refs = ["refs/heads/%s" % b for b in branches]
        res = self._git(["ls-remote", "--", self.remote] + refs)
        found = {}
        for line in res.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1] in refs:
                found[parts[1][len("refs/heads/"):]] = parts[0].strip()
        out = {}
        for branch, sha in found.items():
            out[branch] = self._read_one(branch, sha)
        return out

    def _read_one(self, branch, sha):
        # --no-write-fetch-head (git 2.29+) keeps a lock read from touching FETCH_HEAD.
        quiet = ["--no-write-fetch-head"] if git_version() >= (2, 29) else []
        self._git(["fetch", "--quiet", "--no-tags"] + quiet + ["--", self.remote,
                   "+refs/heads/%s:refs/berth/%s" % (branch, branch)])
        got = self._git(["rev-parse", "--verify", "refs/berth/%s^{commit}" % branch]).stdout.strip()
        res = self._git(["cat-file", "blob", "%s:%s" % (got, locks.LOCK_FILE)], check=False)
        if res.returncode != 0:
            return locks.Lock(branch, got, None, raw_ok=False)
        try:
            data = json.loads(res.stdout)
        except ValueError:
            return locks.Lock(branch, got, None, raw_ok=False)
        return locks.Lock(branch, got, data)

    def _commit(self, record, parent=None):
        blob = self._git(["hash-object", "-w", "--stdin"], stdin=locks.dumps(record)).stdout.strip()
        tree = self._git(["mktree"], stdin="100644 blob %s\t%s\n" % (blob, locks.LOCK_FILE)).stdout.strip()
        args = ["commit-tree", tree, "-m", locks.COMMIT_MESSAGE]
        if parent:
            args += ["-p", parent]
        return self._git(args, env_extra=self._identity_env()).stdout.strip()

    def _identity_env(self):
        """commit-tree needs an author; use the repo's, or a neutral one when none is configured."""
        name = self._git(["config", "user.name"], check=False).stdout.strip()
        email = self._git(["config", "user.email"], check=False).stdout.strip()
        env = {}
        if not name:
            env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "berth"
        if not email:
            env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "berth"
        return env

    def _push(self, args):
        res = self._git(["push", "--porcelain"] + args, check=False)
        if res.returncode == 0:
            return
        text = (res.stdout + "\n" + res.stderr).lower()
        if any(word in text for word in ("rejected", "stale info", "non-fast-forward", "already exists",
                                         "fetch first")):
            raise Conflict(_last_line(res.stdout + "\n" + res.stderr))
        raise StoreError("the push to the remote failed: %s" % _last_line(res.stderr or res.stdout))

    def create(self, branch, record):
        self.check()
        commit = self._commit(record)
        self._push(["--", self.remote, "%s:refs/heads/%s" % (commit, branch)])
        return commit

    def update(self, lock, record):
        commit = self._commit(record, parent=lock.ref)
        self._push(["--", self.remote, "%s:refs/heads/%s" % (commit, lock.branch)])
        return commit

    def delete(self, lock):
        self._push(["--force-with-lease=refs/heads/%s:%s" % (lock.branch, lock.ref), "--", self.remote,
                    ":refs/heads/%s" % lock.branch])


class LocalStore(object):
    """Locks as <dir>/<branch>.json, written under an flock so the compare-and-swap is atomic."""

    kind = "local"

    def __init__(self, path):
        self.path = path

    def describe(self):
        return "local folder %s" % self.path

    def _file(self, branch):
        return os.path.join(self.path, branch + ".json")

    def _locked(self):
        try:
            os.makedirs(self.path, exist_ok=True)
            fh = open(os.path.join(self.path, ".berth-store.lock"), "a")
        except OSError as exc:
            raise StoreError("cannot use the local store %s (%s)." % (self.path, exc))
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    def _read_file(self, branch):
        try:
            with open(self._file(branch), "rb") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreError("cannot read %s (%s)." % (self._file(branch), exc))
        ref = hashlib.sha256(raw).hexdigest()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return locks.Lock(branch, ref, None, raw_ok=False)
        return locks.Lock(branch, ref, data)

    def read(self, branches):
        if not os.path.isdir(self.path):
            return {}
        out = {}
        for branch in branches:
            lock = self._read_file(branch)
            if lock is not None:
                out[branch] = lock
        return out

    def _write(self, branch, record, exclusive):
        body = locks.dumps(record).encode("utf-8")
        target = self._file(branch)
        if exclusive:
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    raise Conflict("the lock already exists")
                raise StoreError("cannot write %s (%s)." % (target, exc))
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
        else:
            tmp = target + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(body)
            os.replace(tmp, target)
        return hashlib.sha256(body).hexdigest()

    def create(self, branch, record):
        with self._locked():
            return self._write(branch, record, exclusive=True)

    def update(self, lock, record):
        with self._locked():
            current = self._read_file(lock.branch)
            if current is None or current.ref != lock.ref:
                raise Conflict("the lock changed since it was read")
            return self._write(lock.branch, record, exclusive=False)

    def delete(self, lock):
        with self._locked():
            current = self._read_file(lock.branch)
            if current is None or current.ref != lock.ref:
                raise Conflict("the lock changed since it was read")
            os.unlink(self._file(lock.branch))


def _last_line(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else "no output"
