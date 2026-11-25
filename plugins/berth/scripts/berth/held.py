"""This machine's record of the claims you hold and which session made them.

Stop, SessionEnd and SessionStart read only this file, so they never wait on the network. The remote lock stays the
truth for deploys; this record can be out of date (someone unlocked with branch-deploy), which only affects reminders.
"""

import fcntl
import json
import os
import time

SESSION_TTL = 3 * 24 * 3600


def _path(data_dir):
    return os.path.join(data_dir, "held.json")


class Held(object):
    """Context manager: load under an exclusive flock, save atomically on exit if changed."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.data = None
        self._fh = None
        self.changed = False

    def __enter__(self):
        os.makedirs(self.data_dir, exist_ok=True)
        self._fh = open(os.path.join(self.data_dir, ".held.lock"), "a")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        self.data = load(self.data_dir)
        return self

    def __exit__(self, *exc):
        try:
            if self.changed and exc[0] is None:
                _prune(self.data)
                tmp = _path(self.data_dir) + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(self.data, fh, indent=2, sort_keys=True)
                os.replace(tmp, _path(self.data_dir))
        finally:
            self._fh.close()
        return False

    def record_claim(self, key, entry):
        self.data["claims"][key] = entry
        self.changed = True

    def forget_claim(self, key):
        if self.data["claims"].pop(key, None) is not None:
            self.changed = True

    def session(self, session_id):
        sessions = self.data["sessions"]
        if session_id not in sessions:
            sessions[session_id] = {"envs": [], "blocked": [], "reminded_at": 0, "at": time.time()}
            self.changed = True
        return sessions[session_id]

    def mark_session(self, session_id, env):
        entry = self.session(session_id)
        if env not in entry["envs"]:
            entry["envs"].append(env)
        entry["at"] = time.time()
        self.changed = True

    def set_left(self, items):
        self.data["left"] = items
        self.changed = True


def load(data_dir):
    empty = {"claims": {}, "sessions": {}, "left": []}
    try:
        with open(_path(data_dir)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    for key, default in empty.items():
        if not isinstance(data.get(key), type(default)):
            data[key] = default
    return data


def _prune(data):
    now = time.time()
    for sid in list(data["sessions"]):
        entry = data["sessions"][sid]
        if not isinstance(entry, dict) or now - float(entry.get("at", 0) or 0) > SESSION_TTL:
            del data["sessions"][sid]


def claim_key(store_desc, project, branch):
    return "%s|%s|%s" % (store_desc, project, branch)
