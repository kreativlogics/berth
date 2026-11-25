"""Claim, extend, release, take over and check claims. Shared by the CLI and the hooks."""

import datetime
import getpass
import json
import os
import subprocess

from . import cfg
from . import held
from . import locks
from . import store as store_mod


class ClaimError(Exception):
    """A refusal the user should read (not a malfunction)."""


PIN_FILE = "identity.json"


def identity(eff, project_dir, data_dir=None):
    """Who you are in lock files. Returns (name, source).

    Your configured identity, else the name berth first found (git user.name, else the OS login), kept in
    <data dir>/identity.json for every project. Keeping it means a later `git config user.name` in any repository
    (which any command can run) does not change whose locks are yours; only /berth:config set identity does, and
    that asks you.
    """
    configured = eff.get("identity")
    if configured:
        return configured.strip(), eff.sources.get("identity", "config")
    pinned = _pinned(data_dir)
    if pinned:
        return pinned["name"], "%s when berth first ran" % pinned.get("source", "git user.name")
    name, source = found_identity(project_dir)
    _pin(data_dir, name, source)
    return name, source


def _pinned(data_dir):
    if not data_dir:
        return None
    try:
        with open(os.path.join(data_dir, PIN_FILE)) as fh:
            entry = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"].strip():
        return entry
    return None


def _pin(data_dir, name, source):
    if not data_dir:
        return
    try:
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, PIN_FILE)
        with open(path + ".tmp", "w") as fh:
            json.dump({"name": name, "source": source, "at": locks.iso(locks.utcnow())}, fh, indent=2)
        os.replace(path + ".tmp", path)
    except OSError:
        pass  # Unpinned still works; it is tried again next time.


def identity_drift(eff, project_dir, data_dir):
    """A note when the kept name no longer matches git user.name here, else None."""
    if eff.get("identity"):
        return None
    pinned = _pinned(data_dir)
    if not pinned:
        return None
    now, _source = found_identity(project_dir)
    if now.lower() == pinned["name"].lower():
        return None
    return ("berth still writes your locks as %r (your %s when berth first ran); your git name here is %r. If "
            "that should be you in locks, set it: /berth:config set identity %s"
            % (pinned["name"], pinned.get("source"), now, now))


def found_identity(project_dir):
    """git user.name, else the OS login."""
    try:
        res = subprocess.run(["git", "config", "user.name"], cwd=project_dir or None, env=store_mod.git_env(),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=5)
        name = res.stdout.strip()
        if res.returncode == 0 and name:
            return name, "git user.name"
    except (OSError, subprocess.SubprocessError):
        pass
    return getpass.getuser(), "OS login"


def current_branch(project_dir):
    try:
        res = subprocess.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=project_dir or None,
                             env=store_mod.git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=5)
        return res.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class Service(object):
    def __init__(self, eff, project_dir, data_dir, now=None):
        self.eff = eff
        self.project_dir = project_dir
        self.data_dir = data_dir
        self.store = store_mod.make(eff.get("store"), project_dir, data_dir)
        self.me, self.me_source = identity(eff, project_dir, data_dir)
        self._now = now

    def now(self):
        return self._now or locks.utcnow()

    @property
    def stale_hours(self):
        return float(self.eff.get("stale_after_hours"))

    @property
    def cap(self):
        return float(self.eff.get("max_claim_hours"))

    def guarded(self):
        return sorted(self.eff.get("environments") or {})

    def _require_guarded(self, env):
        if env not in (self.eff.get("environments") or {}):
            raise ClaimError("%s is not a guarded environment here. Guarded: %s. Add it with /berth:config."
                             % (env, ", ".join(self.guarded()) or "none"))

    def read(self, envs):
        """Return ({env: Lock or None}, global Lock or None)."""
        branches = [locks.branch_name(e) for e in envs] + [locks.GLOBAL_BRANCH]
        found = self.store.read(branches)
        return {e: found.get(locks.branch_name(e)) for e in envs}, found.get(locks.GLOBAL_BRANCH)

    def _hours(self, hours):
        hours = self.cap if hours is None else float(hours)
        if hours <= 0:
            raise ClaimError("Hours must be more than 0.")
        if hours > self.cap:
            raise ClaimError("The longest claim allowed here is %s hour(s) (max_claim_hours, from %s). Claim for "
                             "less and extend later." % (_fmt(self.cap), self.eff.sources.get("max_claim_hours")))
        return hours

    def _key(self, branch):
        return held.claim_key(self.store.describe(), os.path.realpath(self.project_dir or "."), branch)

    def _remember(self, env, branch, ref, expires):
        with held.Held(self.data_dir) as h:
            h.record_claim(self._key(branch), {"env": env, "branch": branch, "ref": ref,
                                               "expires_at": locks.iso(expires), "store": self.store.describe(),
                                               "project": os.path.realpath(self.project_dir or ".")})

    def _forget(self, branch):
        with held.Held(self.data_dir) as h:
            h.forget_claim(self._key(branch))

    def _check_global(self, glob):
        if glob is None:
            return
        if glob.data is None:
            raise ClaimError("A global lock branch exists without a valid lock.json, so every environment is "
                             "blocked. Someone must remove %s by hand." % locks.GLOBAL_BRANCH)
        if not glob.owned_by(self.me):
            raise ClaimError("A global lock is set (%s). No environment can be claimed until it is removed."
                             % glob.describe(self.now()))

    def claim(self, env, hours=None, reason=None):
        self._require_guarded(env)
        hours = self._hours(hours)
        found, glob = self.read([env])
        self._check_global(glob)
        lock = found[env]
        if lock is not None:
            state = lock.state(self.me, self.stale_hours, self.now())
            if state in (locks.MINE, locks.MINE_EXPIRED):
                raise ClaimError("You already hold %s (%s). Use extend to change its end time." % (
                    env, lock.describe(self.now())))
            hint = " It looks stale; a person can replace it with takeover." if state == locks.OTHER_STALE else ""
            raise ClaimError("%s is claimed: %s.%s" % (env, lock.describe(self.now()), hint))
        now = self.now()
        expires = now + datetime.timedelta(hours=hours)
        record = locks.new_record(env, self.me, reason, current_branch(self.project_dir), expires, now)
        branch = locks.branch_name(env)
        try:
            ref = self.store.create(branch, record)
        except store_mod.Conflict:
            found, _ = self.read([env])
            other = found[env]
            raise ClaimError("Someone claimed %s first: %s." % (env, other.describe(self.now()) if other
                                                                 else "the lock changed while claiming"))
        self._remember(env, branch, ref, expires)
        return "Claimed %s until %s (%s, %s)." % (env, locks.short_time(expires), self.me, self.store.describe())

    def extend(self, env, hours=None):
        self._require_guarded(env)
        hours = self._hours(hours)
        found, _ = self.read([env])
        lock = found[env]
        if lock is None:
            raise ClaimError("Nobody holds %s; claim it instead." % env)
        if not lock.owned_by(self.me):
            raise ClaimError("%s is not yours: %s." % (env, lock.describe(self.now())))
        expires = self.now() + datetime.timedelta(hours=hours)
        record = dict(lock.data)
        record["expires_at"] = locks.iso(expires)
        try:
            ref = self.store.update(lock, record)
        except store_mod.Conflict:
            raise ClaimError("The %s lock changed while extending; check it with status." % env)
        self._remember(env, lock.branch, ref, expires)
        return "Extended %s until %s." % (env, locks.short_time(expires))

    def release(self, env):
        found, _ = self.read([env])
        lock = found.get(env)
        branch = locks.branch_name(env)
        if lock is None:
            self._forget(branch)
            return "Nobody holds %s; nothing to release." % env
        if not lock.owned_by(self.me):
            raise ClaimError("%s is not yours (%s). berth never removes someone else's lock; ask them, or use "
                             "takeover if it is stale." % (env, lock.describe(self.now())))
        try:
            self.store.delete(lock)
        except store_mod.Conflict:
            raise ClaimError("The %s lock changed while releasing; nothing was removed. Check it with status."
                             % env)
        self._forget(branch)
        return "Released %s." % env

    def takeover(self, env, expected_ref, hours=None, reason=None):
        """Replace a stale lock of someone else. expected_ref must be a prefix of the lock's ref (from status)."""
        self._require_guarded(env)
        hours = self._hours(hours)
        found, glob = self.read([env])
        self._check_global(glob)
        lock = found[env]
        if lock is None:
            raise ClaimError("Nobody holds %s; claim it instead." % env)
        if not expected_ref or len(expected_ref) < 7 or not lock.ref.startswith(expected_ref):
            raise ClaimError("The lock on %s is %s, not %r. Run status and pass the ref it shows." % (
                env, lock.ref[:12], expected_ref))
        state = lock.state(self.me, self.stale_hours, self.now())
        if state in (locks.MINE, locks.MINE_EXPIRED):
            raise ClaimError("You already hold %s; use extend." % env)
        if state == locks.OTHER:
            raise ClaimError("%s is actively held (%s). Takeover is only for stale locks; ask the holder." % (
                env, lock.describe(self.now())))
        if state != locks.OTHER_STALE:
            # An unreadable lock may be a fresh one from a newer tool; nobody can tell how old it is.
            raise ClaimError("The lock on %s is not one berth can read (%s), so berth cannot tell whether it is "
                             "stale. Ask whoever made it, or remove the branch by hand." % (
                                 env, lock.describe(self.now())))
        previous = lock.describe(self.now())
        try:
            self.store.delete(lock)
        except store_mod.Conflict:
            raise ClaimError("The %s lock changed during takeover; nothing was removed." % env)
        note = "took over from %s" % locks.safe_text(lock.holder, 60)
        msg = self.claim(env, hours, "%s (%s)" % (reason, note) if reason else note)
        return "Removed the stale lock (%s). %s" % (previous, msg)

    def status(self, envs=None):
        envs = envs or self.guarded()
        found, glob = self.read(envs)
        rows = []
        for env in envs:
            lock = found[env]
            if lock is None:
                rows.append({"env": env, "state": locks.FREE, "text": "free", "ref": None})
            else:
                rows.append({"env": env, "state": lock.state(self.me, self.stale_hours, self.now()),
                             "text": lock.describe(self.now()), "ref": lock.ref[:12]})
        glob_row = None
        if glob is not None:
            glob_row = {"state": locks.AMBIGUOUS if glob.data is None else (
                locks.MINE if glob.owned_by(self.me) else locks.OTHER), "text": glob.describe(self.now()),
                "ref": glob.ref[:12]}
        return rows, glob_row

    def check_deploy(self, envs):
        """Return [(env, ok, message)] for a deploy to these environments."""
        found, glob = self.read(envs)
        out = []
        for env in envs:
            if glob is not None and (glob.data is None or not glob.owned_by(self.me)):
                out.append((env, False, "a global lock blocks every environment (%s)" % glob.describe(self.now())))
                continue
            lock = found[env]
            if lock is None:
                out.append((env, False, "nobody holds %s" % env))
                continue
            state = lock.state(self.me, self.stale_hours, self.now())
            if state == locks.MINE:
                out.append((env, True, "you hold %s" % env))
            elif state == locks.MINE_EXPIRED:
                out.append((env, False, "your claim on %s has expired (%s); extend it" % (env, lock.describe(
                    self.now()))))
            elif state == locks.AMBIGUOUS:
                out.append((env, False, lock.describe(self.now())))
            else:
                stale = " (stale; a person can take it over)" if state == locks.OTHER_STALE else ""
                out.append((env, False, "%s is %s%s" % (env, lock.describe(self.now()), stale)))
        return out


def _fmt(hours):
    return ("%.2f" % hours).rstrip("0").rstrip(".")


def resolve(data_dir, project_dir):
    return cfg.resolve(data_dir, project_dir)
