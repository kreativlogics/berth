---
name: config
description: Set up or change berth - find deploy environments in CI files and deploy scripts, choose which to guard, set the claim time cap, the store and the preset, and check the install. Use when the user asks to set up, configure or tune berth, to guard a new environment, or when berth refused a deploy the user thinks is not a deploy.
argument-hint: "[show | explain <key> | detect | init | set <key> <value> | doctor]"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/berthcfg.py" --data "${CLAUDE_PLUGIN_DATA}" show*), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/berthcfg.py" --data "${CLAUDE_PLUGIN_DATA}" explain *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/berthcfg.py" --data "${CLAUDE_PLUGIN_DATA}" detect*), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/berthcfg.py" --data "${CLAUDE_PLUGIN_DATA}" doctor*)
---

# berth config

`CFG` means `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/berthcfg.py" --data "${CLAUDE_PLUGIN_DATA}"`. For what a key does,
run `CFG explain <key>`; the reference is https://github.com/kreativlogics/berth/blob/master/docs/reference.md.

Request: $ARGUMENTS

**First setup:**
1. Run `CFG detect` and `CFG show`. Tell the user which environments were found, where, and the deploy patterns
   suggested for each.
2. Ask which environments to guard. `CFG init` previews; `CFG init --yes` writes your own config, `--project` writes
   `.berth/config.json` for the whole team (it can only tighten). Remove the ones the user does not want with
   `CFG unset environments.<name>`.
3. Run `CFG doctor` to check the store (the git remote) and the hook timing.

**Change a setting:** `CFG set <key> <value>`. Examples:
- `CFG set environments.staging '^make deploy-staging(\s|$)'` adds a pattern
- `CFG set max_claim_hours 1`
- `CFG set identity <github-login>` (your own config only)
- `CFG set store '{"type": "local", "path": "/abs/folder"}'` for solo use
- `CFG set preset strict`

berth asks the user to confirm every `set`, `unset`, `approve` and `init --yes`. Never edit the config files or
berth's data folder directly; that is denied.

**A refused command that is not a deploy:** show the pattern that matched (`CFG show`), then offer a narrower pattern
or `CFG set generic_patterns false` in the user's own config. Say what the change would let through.

**A store set in the project config** applies only after the user runs `CFG approve store`; show it first.
