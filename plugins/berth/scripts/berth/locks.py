"""Lock records in github/branch-deploy's format (v12: lock.json on a lock branch), plus berth's expiry.

A lock berth writes has every field branch-deploy's reader requires, so branch-deploy sees it as an ordinary
sticky lock. berth adds `expires_at` and `berth`; readers that don't know them ignore them.
"""

import datetime
import json
import re

LOCK_SUFFIX = "-branch-deploy-lock"
GLOBAL_BRANCH = "global-branch-deploy-lock"
LOCK_FILE = "lock.json"
COMMIT_MESSAGE = "lock [skip ci]"
REQUIRED = ("reason", "branch", "created_at", "created_by", "sticky", "environment", "global", "unlock_command",
            "link")
CLAIM_ID = re.compile(r"^sha256:[0-9a-f]{64}$")

FREE = "free"
MINE = "mine"
MINE_EXPIRED = "mine-expired"
OTHER = "other"
OTHER_STALE = "other-stale"
AMBIGUOUS = "ambiguous"


def branch_name(environment):
    """branch-deploy replaces whitespace with '-' and appends the suffix."""
    return re.sub(r"\s", "-", environment) + LOCK_SUFFIX


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        "%03dZ" % (moment.microsecond // 1000)


def parse_time(text):
    if not isinstance(text, str):
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$", text.strip())
    if not m:
        return None
    try:
        base = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    frac = m.group(2) or ""
    micro = int((frac[1:] + "000000")[:6]) if frac else 0
    zone = m.group(3)
    if zone == "Z":
        tz = datetime.timezone.utc
    else:
        sign = 1 if zone[0] == "+" else -1
        tz = datetime.timezone(sign * datetime.timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6])))
    return base.replace(microsecond=micro, tzinfo=tz)


def is_valid(data):
    """The same checks branch-deploy's reader makes; anything else is an ambiguous lock."""
    if not isinstance(data, dict):
        return False
    if any(key not in data for key in REQUIRED):
        return False
    checks = (
        data["branch"] is None or isinstance(data["branch"], str),
        isinstance(data["created_at"], str),
        isinstance(data["created_by"], str),
        data["sticky"] is None or isinstance(data["sticky"], bool),
        data["environment"] is None or isinstance(data["environment"], str),
        isinstance(data["global"], bool),
        isinstance(data["unlock_command"], str),
        isinstance(data["link"], str),
    )
    if not all(checks):
        return False
    if "schema_version" in data and data["schema_version"] != 1:
        return False
    if "claim_id" in data and not (isinstance(data["claim_id"], str) and CLAIM_ID.match(data["claim_id"])):
        return False
    return True


def new_record(environment, identity, reason, branch, expires_at, now=None):
    now = now or utcnow()
    return {
        "schema_version": 1,
        "reason": reason or "claimed with berth",
        "branch": branch,
        "created_at": iso(now),
        "created_by": identity,
        "sticky": True,
        "environment": environment,
        "global": False,
        "unlock_command": ".unlock %s" % environment,
        "link": "",
        "expires_at": iso(expires_at),
        "berth": 1,
    }


def dumps(record):
    """Compact JSON, no trailing newline, like branch-deploy writes it."""
    return json.dumps(record, separators=(",", ":"))


class Lock(object):
    """A lock as read from a store. data is None when the branch exists without a valid lock.json."""

    def __init__(self, branch, ref, data, raw_ok=True):
        self.branch = branch
        self.ref = ref  # Commit sha (git) or content hash (local): what a compare-and-delete must match.
        self.data = data if raw_ok and is_valid(data) else None

    @property
    def holder(self):
        return self.data["created_by"] if self.data else None

    @property
    def expires_at(self):
        return parse_time(self.data.get("expires_at")) if self.data else None

    @property
    def created_at(self):
        return parse_time(self.data.get("created_at")) if self.data else None

    def owned_by(self, identity):
        return bool(self.data and identity and self.holder.strip().lower() == identity.strip().lower())

    def expired(self, now=None):
        exp = self.expires_at
        return bool(exp and exp <= (now or utcnow()))

    def stale(self, stale_after_hours, now=None):
        """Expired, or (no expiry, as branch-deploy writes them) older than stale_after_hours."""
        now = now or utcnow()
        if self.expires_at:
            return self.expired(now)
        created = self.created_at
        return bool(created and now - created >= datetime.timedelta(hours=stale_after_hours))

    def state(self, identity, stale_after_hours, now=None):
        if self.data is None:
            return AMBIGUOUS
        if self.owned_by(identity):
            return MINE_EXPIRED if self.expired(now) else MINE
        return OTHER_STALE if self.stale(stale_after_hours, now) else OTHER

    def describe(self, now=None):
        if self.data is None:
            text = "%s exists without a valid %s (ambiguous; someone must remove it by hand)"
            return text % (self.branch, LOCK_FILE)
        now = now or utcnow()
        parts = ["held by %s" % safe_text(self.holder, 60)]
        if self.created_at:
            parts.append("since %s" % short_time(self.created_at))
        exp = self.expires_at
        if exp:
            parts.append(("expired %s" if exp <= now else "until %s") % short_time(exp))
        else:
            parts.append("no expiry")
        reason = self.data.get("reason")
        if isinstance(reason, str) and reason:
            parts.append("reason: %s" % safe_text(reason, 120))
        return ", ".join(parts)


def short_time(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def safe_text(value, limit):
    """Lock contents come from other people: strip control characters and cap the length before showing them."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return text if len(text) <= limit else text[:limit - 1] + "…"
