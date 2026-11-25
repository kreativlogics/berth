"""Layered configuration: defaults < plugin options < user file < project file.

Plugin options reach hooks as CLAUDE_PLUGIN_OPTION_* variables but not commands Claude runs, so the hooks save
them to <data dir>/plugin-options.json and berth's CLIs read that copy (never the variables, which a command could
set).

Same rules as checkrein's config core (adapted, not vendored, because the schema differs):
- The user layer (plugin options, then <data dir>/config.json) may set anything.
- The project layer (<project>/.berth/config.json, committed) can only tighten: levels go up, the claim cap goes
  down, environments and patterns add up. `identity` is ignored there. `store` applies only once approved.
- An ignored loosening is reported in `notes`, never applied silently.
"""

import difflib
import json
import os
import re

from . import approve
from . import secrets

SUPPORTED_VERSION = 1
PRESETS = ("standard", "strict")
FAIL_MODES = ("allow", "ask", "deny")
UNCLAIMED = ("ask", "deny")
STOP_MODES = ("off", "remind", "block")
ENV_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
MAX_PATTERN = 400
CAP_RANGE = (0.25, 24.0)

SCHEMA = {
    "version": "meta",
    "preset": "preset",
    "fail_mode": "safety-order",
    "unclaimed": "safety-order",
    "stop": "safety-order",
    "max_claim_hours": "safety-min",
    "generic_patterns": "safety-bool",
    "environments": "safety-union",
    "identity": "user-only",
    "store": "approved",
    "stale_after_hours": "safety-max",
}

ORDERS = {"fail_mode": FAIL_MODES, "unclaimed": UNCLAIMED, "stop": STOP_MODES}

PRESET_VALUES = {
    "standard": {"fail_mode": "ask", "unclaimed": "deny", "stop": "remind", "generic_patterns": True},
    "strict": {"fail_mode": "deny", "unclaimed": "deny", "stop": "block", "generic_patterns": True},
}

DESCRIPTIONS = {
    "version": "Config format version. Leave at 1.",
    "preset": "standard or strict. strict denies when berth cannot check (fail_mode deny) and asks Claude to release "
              "or justify a claim when it stops (stop block).",
    "fail_mode": "What happens when berth cannot check a deploy (remote unreachable, broken config): ask, deny or "
                 "allow. A project can only make it stricter.",
    "unclaimed": "What happens to a deploy without your claim: deny (default) or ask. A project can only make it "
                 "stricter.",
    "stop": "When Claude stops while this session holds a claim: off, remind (a message to you) or block (Claude is "
            "asked once per claim to release it or say why it keeps it).",
    "max_claim_hours": "Longest a claim or an extension may run, in hours (0.25 to 24). A project can only lower it.",
    "generic_patterns": "Also treat any command that names a deploy program or subcommand together with a guarded "
                        "environment as a deploy. A project can turn it on, not off.",
    "environments": "Guarded environments and their deploy-command patterns: {\"staging\": {\"patterns\": "
                    "[\"^make deploy-staging\\\\b\"]}}. Patterns are Python regexes matched against each simple "
                    "command (program name, then its arguments). Lists from every layer add up.",
    "identity": "Who you are in lock files (created_by). Use your GitHub login if your team also uses "
                "branch-deploy. Your own config only; ignored in project files.",
    "store": "Where claims live: {\"type\": \"git\", \"remote\": \"origin\"} (default, lock branches on the remote) "
             "or {\"type\": \"local\", \"path\": \"/abs/dir\"} (solo use). In a project file it applies only after "
             "you approve it.",
    "stale_after_hours": "A lock with no expiry (one made by branch-deploy) counts as stale after this many hours, "
                         "for reports and takeover. A project can only raise it.",
}


class ConfigError(Exception):
    pass


def _hint(key, choices):
    close = difflib.get_close_matches(key, list(choices), n=1)
    return " Did you mean %r?" % close[0] if close else ""


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate(data, where):
    """Raise ConfigError with the path, what was expected, what was found and the fix."""
    if not isinstance(data, dict):
        raise ConfigError("%s: expected a JSON object, got %s." % (where, type(data).__name__))
    version = data.get("version", SUPPORTED_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ConfigError("%s: version: expected an integer, got %r." % (where, version))
    if version > SUPPORTED_VERSION:
        raise ConfigError("%s: version %d is newer than this berth understands (%d). Update the plugin."
                          % (where, version, SUPPORTED_VERSION))
    for key, value in data.items():
        if key not in SCHEMA:
            raise ConfigError("%s: unknown key %r.%s" % (where, key, _hint(key, SCHEMA)))
        _validate_key(key, value, where)
    for text in _strings(data):
        if secrets.scan_text(text, high_only=True):
            raise ConfigError("%s: a value looks like a real secret. Config files never hold secrets." % where)
    return data


def _validate_key(key, value, where):
    if key == "preset" and value not in PRESETS:
        raise ConfigError("%s: preset: expected one of %s, got %r." % (where, ", ".join(PRESETS), value))
    if key in ORDERS and value not in ORDERS[key]:
        raise ConfigError("%s: %s: expected one of %s, got %r." % (where, key, ", ".join(ORDERS[key]), value))
    if key == "max_claim_hours":
        if not _number(value) or not CAP_RANGE[0] <= value <= CAP_RANGE[1]:
            raise ConfigError("%s: max_claim_hours: expected a number from %s to %s, got %r."
                              % (where, CAP_RANGE[0], CAP_RANGE[1], value))
    if key == "stale_after_hours" and (not _number(value) or value <= 0 or value > 24 * 365):
        raise ConfigError("%s: stale_after_hours: expected a positive number of hours, got %r." % (where, value))
    if key == "generic_patterns" and not isinstance(value, bool):
        raise ConfigError("%s: generic_patterns: expected true or false, got %r." % (where, value))
    if key == "identity" and (not isinstance(value, str) or not value.strip() or len(value) > 100
                              or any(c in value for c in "\n\r\t")):
        raise ConfigError("%s: identity: expected a short name such as your GitHub login." % where)
    if key == "environments":
        _validate_environments(value, where)
    if key == "store":
        _validate_store(value, where)


def _validate_environments(value, where):
    if not isinstance(value, dict):
        raise ConfigError("%s: environments: expected {\"name\": {\"patterns\": [...]}}." % where)
    for name, spec in value.items():
        if not ENV_NAME.match(name) or name.endswith("-branch-deploy-lock"):
            raise ConfigError("%s: environments: %r is not a valid environment name (letters, digits, . _ -)."
                              % (where, name))
        if not isinstance(spec, dict) or set(spec) - {"patterns"}:
            raise ConfigError("%s: environments.%s: expected {\"patterns\": [\"regex\", ...]}." % (where, name))
        patterns = spec.get("patterns", [])
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ConfigError("%s: environments.%s.patterns: expected a list of strings." % (where, name))
        for pat in patterns:
            if not pat or len(pat) > MAX_PATTERN:
                raise ConfigError("%s: environments.%s.patterns: each pattern must be 1 to %d characters."
                                  % (where, name, MAX_PATTERN))
            try:
                re.compile(pat)
            except re.error as exc:
                raise ConfigError("%s: environments.%s.patterns: %r is not a valid regex (%s)."
                                  % (where, name, pat, exc))
            if nested_repeat(pat):
                raise ConfigError("%s: environments.%s.patterns: %r repeats a group that can match the same text "
                                  "more than one way (a repeat, ? or | inside a repeat), which can take minutes on "
                                  "some commands. Simplify it, for example (a+)+ to a+ or (x|y)+ to [xy]+."
                                  % (where, name, pat))


def nested_repeat(pattern):
    """True when a repeat holds something that can match the same text more than one way (another repeat, an
    optional part or an alternation): the shapes that backtrack exponentially, such as (x+x+)+y or (x|x)+y."""
    try:
        parser = re._parser  # Python 3.11+.
    except AttributeError:  # pragma: no cover - Python 3.9 and 3.10.
        import sre_parse as parser
    try:
        tree = parser.parse(pattern)
    except Exception:  # noqa: BLE001 - re.compile already accepted it; never fail validation here.
        return False
    return _has_nested(tree, False)


def _has_nested(items, inside):
    for op, av in items:
        name = str(op)
        if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            low, high, body = av
            if inside:
                return True
            if _has_nested(body, high > 1):
                return True
        elif name == "SUBPATTERN":
            if _has_nested(av[-1], inside):
                return True
        elif name == "BRANCH":
            if inside or any(_has_nested(alt, inside) for alt in av[1]):
                return True
        elif name in ("ASSERT", "ASSERT_NOT"):
            if _has_nested(av[1], inside):
                return True
    return False


def _validate_store(value, where):
    if not isinstance(value, dict) or value.get("type") not in ("git", "local"):
        raise ConfigError("%s: store: expected {\"type\": \"git\", \"remote\": \"origin\"} or "
                          "{\"type\": \"local\", \"path\": \"/abs/dir\"}." % where)
    if value["type"] == "git":
        if set(value) - {"type", "remote"}:
            raise ConfigError("%s: store: a git store takes only type and remote." % where)
        remote = value.get("remote", "origin")
        if not isinstance(remote, str) or not REMOTE_NAME.match(remote):
            raise ConfigError("%s: store.remote: expected a git remote name such as origin, got %r." % (where, remote))
    else:
        if set(value) - {"type", "path"}:
            raise ConfigError("%s: store: a local store takes only type and path." % where)
        path = value.get("path")
        if not isinstance(path, str) or not os.path.isabs(os.path.expanduser(path)):
            raise ConfigError("%s: store.path: expected an absolute folder path." % where)


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            for s in _strings(v):
                yield s
    elif isinstance(obj, list):
        for v in obj:
            for s in _strings(v):
                yield s


def read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise ConfigError("%s: not valid JSON (%s)." % (path, exc))


def user_config_path(data_dir):
    return os.path.join(data_dir, "config.json") if data_dir else None


def project_config_path(project_dir):
    return os.path.join(project_dir, ".berth", "config.json") if project_dir else None


OPTIONS_FILE = "plugin-options.json"


def env_options():
    """Plugin options as hooks see them (CLAUDE_PLUGIN_OPTION_*). Only hooks call this."""
    layer = {}
    for key in ("preset", "fail_mode", "identity"):
        value = os.environ.get("CLAUDE_PLUGIN_OPTION_" + key.upper(), "").strip()
        if value:
            layer[key] = value
    cap = os.environ.get("CLAUDE_PLUGIN_OPTION_MAX_CLAIM_HOURS", "").strip()
    if cap:
        try:
            layer["max_claim_hours"] = float(cap)
        except ValueError:
            raise ConfigError("Plugin option max_claim_hours: expected a number, got %r." % cap)
    return layer


def saved_options(data_dir):
    """The plugin options the hooks last saw, for berth's CLIs. Empty until a hook has run."""
    path = os.path.join(data_dir, OPTIONS_FILE) if data_dir else None
    layer = read_json(path) if path else None
    return layer if isinstance(layer, dict) else {}


def save_options(data_dir, layer):
    """Keep the CLIs' copy of the plugin options current. Writes only on a change."""
    if not data_dir:
        return
    path = os.path.join(data_dir, OPTIONS_FILE)
    try:
        if read_json(path) == layer:
            return
    except ConfigError:
        pass
    os.makedirs(data_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(layer, fh, sort_keys=True)
    os.replace(tmp, path)


def find_project(start):
    """The project a folder belongs to: the nearest folder up with .berth/config.json, else with .git, else the
    folder itself.

    Hooks start from where Claude Code was opened and CLIs from the shell's folder; both land on the same root.
    """
    start = os.path.abspath(start)
    for marker in (os.path.join(".berth", "config.json"), ".git"):
        here = start
        while True:
            if os.path.exists(os.path.join(here, marker)):
                return here
            parent = os.path.dirname(here)
            if parent == here:
                break
            here = parent
    return start


class Effective(object):
    """The resolved config, where each value came from, and what was ignored or waits for approval."""

    def __init__(self):
        self.values = {}
        self.sources = {}
        self.notes = []
        self.pending_approval = []

    def get(self, key, default=None):
        return self.values.get(key, default)


def defaults():
    values = {
        "preset": "standard",
        "max_claim_hours": 2.0,
        "environments": {},
        "identity": None,
        "store": {"type": "git", "remote": "origin"},
        "stale_after_hours": 24.0,
    }
    values.update(PRESET_VALUES["standard"])
    return values


def resolve(data_dir=None, project_dir=None, options=None):
    """options: the plugin options layer; hooks pass env_options(), and None means the copy the hooks saved."""
    eff = Effective()
    eff.values = defaults()
    eff.sources = {key: "default" for key in eff.values}

    layer = saved_options(data_dir) if options is None else options
    _apply_user(eff, validate(dict(layer), "plugin options"), "plugin options")
    user_path = user_config_path(data_dir)
    user_file = read_json(user_path) if user_path else None
    if user_file is not None:
        validate(user_file, user_path)
        _apply_user(eff, user_file, "user config")

    proj_path = project_config_path(project_dir)
    proj_file = read_json(proj_path) if proj_path else None
    if proj_file is not None:
        validate(proj_file, proj_path)
        _apply_project(eff, proj_file, project_dir, data_dir)
    return eff


def _merge_environments(current, layer):
    merged = {name: {"patterns": list(spec.get("patterns", []))} for name, spec in current.items()}
    for name, spec in layer.items():
        entry = merged.setdefault(name, {"patterns": []})
        for pat in spec.get("patterns", []):
            if pat not in entry["patterns"]:
                entry["patterns"].append(pat)
    return merged


def _apply_user(eff, layer, label):
    if "preset" in layer:
        eff.values["preset"] = layer["preset"]
        eff.sources["preset"] = label
        for key, value in PRESET_VALUES[layer["preset"]].items():
            eff.values[key] = value
            eff.sources[key] = "%s (preset %s)" % (label, layer["preset"])
    for key, value in layer.items():
        if key in ("version", "preset"):
            continue
        if key == "environments":
            eff.values[key] = _merge_environments(eff.values[key], value)
        elif key in ("max_claim_hours", "stale_after_hours"):
            eff.values[key] = float(value)
        else:
            eff.values[key] = value
        eff.sources[key] = label


def _tighten_order(eff, key, value, label, explicit):
    order = ORDERS[key]
    current = eff.values[key]
    if order.index(value) > order.index(current):
        eff.values[key] = value
        eff.sources[key] = label
    elif order.index(value) < order.index(current) and explicit:
        eff.notes.append("Ignored: the project config asks for %s %r, looser than your %r. Only your own config "
                         "can loosen it." % (key, value, current))


def _apply_project(eff, layer, project_dir, data_dir):
    label = "project config"
    proposed = dict(PRESET_VALUES[layer["preset"]]) if "preset" in layer else {}
    proposed.update({k: v for k, v in layer.items() if k in ORDERS or k == "generic_patterns"})
    for key, value in proposed.items():
        if key in ORDERS:
            _tighten_order(eff, key, value, label, key in layer)
        elif key == "generic_patterns":
            if value and not eff.values[key]:
                eff.values[key] = True
                eff.sources[key] = label
            elif not value and eff.values[key] and key in layer:
                eff.notes.append("Ignored: the project config turns generic_patterns off. Only your own config "
                                 "can do that.")
    if "max_claim_hours" in layer:
        if layer["max_claim_hours"] < eff.values["max_claim_hours"]:
            eff.values["max_claim_hours"] = float(layer["max_claim_hours"])
            eff.sources["max_claim_hours"] = label
        elif layer["max_claim_hours"] > eff.values["max_claim_hours"]:
            eff.notes.append("Ignored: the project config raises max_claim_hours to %s, above your %s."
                             % (layer["max_claim_hours"], eff.values["max_claim_hours"]))
    if "environments" in layer:
        eff.values["environments"] = _merge_environments(eff.values["environments"], layer["environments"])
        eff.sources["environments"] = "%s + %s" % (eff.sources["environments"], label)
    if "identity" in layer:
        eff.notes.append("Ignored: identity in the project config. Only your own config can set who you are.")
    if "stale_after_hours" in layer:
        if layer["stale_after_hours"] > eff.values["stale_after_hours"]:
            eff.values["stale_after_hours"] = float(layer["stale_after_hours"])
            eff.sources["stale_after_hours"] = label
        elif layer["stale_after_hours"] < eff.values["stale_after_hours"]:
            eff.notes.append("Ignored: the project config lowers stale_after_hours to %s, below your %s. A lower "
                             "value lets fresh locks be taken over." % (layer["stale_after_hours"],
                                                                        eff.values["stale_after_hours"]))
    if "store" in layer:
        if approve.is_approved(data_dir, project_dir, "store", layer["store"]):
            eff.values["store"] = layer["store"]
            eff.sources["store"] = "%s (approved)" % label
        else:
            eff.pending_approval.append("store")
            eff.notes.append("Not applied: store in the project config waits for your approval "
                             "(/berth:config approve store).")


def write_layer(path, data):
    """Validate and write a config layer atomically. Returns the old content."""
    data.setdefault("version", SUPPORTED_VERSION)
    validate(data, path)
    old = read_json(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return old
