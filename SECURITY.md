# Security policy

## Reporting a problem

Please report a way around berth through GitHub's private vulnerability reporting: the **Security** tab of this
repository, then **Report a vulnerability**. Examples: a deploy it should refuse, a claim obtained without a person
seeing it, or a lock removed that was not yours. Please do not open a public issue for these.

Include the berth version (`plugins/berth/scripts/berth/__init__.py`), the tool call or command, and what you
expected. Never include a real secret or a real remote's address.

You should hear back within a week.

## Scope

A bypass already listed in [docs/limits.md](docs/limits.md) under "Known misses" is known; a new technique for one is
still welcome. Anything else that lets the agent deploy to a guarded environment without your claim, create, change or
delete a lock branch without going through berth's checks, or change berth's config or state without a person, is in
scope.

## What berth does with your data

berth talks only to the git remote you configured (default `origin`), with plain git and your existing access. It
reads and writes `*-branch-deploy-lock` branches there and nothing else. Lock files hold your configured identity,
the current branch name, a reason you give, and times. Nothing else leaves your machine; there is no telemetry.

## Scanner notes

The installable plugin (`plugins/berth/`) was scanned with SkillSpector (static rules plus LLM review). Verdict
**REVIEW**, no HIGH or CRITICAL findings. A plugin with hooks or subprocess calls cannot reach PASS, because the
two MEDIUM rules below fire on any hook and any `subprocess` call. Each remaining MEDIUM finding:

| Rule | Where | Why it stays |
|---|---|---|
| BH1 bundled hooks | `hooks/hooks.json` | The hooks are the product: PreToolUse checks deploys and lock writes, Stop and SessionStart/SessionEnd track claims. They run only `python3 scripts/hook.py <mode>` from the plugin folder. |
| AST4 subprocess call | `scripts/berth/store.py` (git with the store), `scripts/berth/claims.py` (`git config user.name`, the current branch), `scripts/berthcfg.py` (`git remote` for `detect`, the hook run by `doctor`) | Every call runs a fixed program (`git`, or berth's own `hook.py` under the same Python) with an argument list, never a shell string. Branch names are built from validated environment names. Each call gets a minimal environment (`store.git_env`: a named list of variables, prompts off), no stdin and a timeout. |
| TT2 tainted flow | `scripts/berthcfg.py` (`detect`, `doctor`) | The "taint" is `CLAUDE_PROJECT_DIR` and `CLAUDE_PLUGIN_DATA`: Claude Code's own project and data folders, used as the working folder for `git remote`, the folder `doctor` checks is writable, and the project in `doctor`'s sample hook input. They are paths, never part of a command. |

checkrein's `vet` gives the plugin **REVIEW**: S-HOOKS (twice, `plugin.json` and `hooks/hooks.json`) for the same
reason as BH1; S-NETWORK (twice) on the `HTTP_CLIENTS` name list in `match.py` and a `curl`/`wget` branch in the
vendored parser in `context.py`, which read commands and never open a connection (berth's only network use is git with
the claim store's remote); S-BINARY (info) on `icon.png`.

Code comments describe what a command does instead of quoting it, because the scanner's script-fetch and
parameter-abuse rules read comments too.

The whole repository scans as DO_NOT_INSTALL, by design: the tests under `tests/` quote the commands berth must refuse
(pushes to lock branches, deletes of its own state, HTTP writes, chained commands), and those strings trip the
tool-abuse, script-fetch and chaining rules (TM1, TM2, SC2, P1 on a prompt-injection fixture) plus test-only MEDIUMs
(AST4 and E1 in `helpers.py` and `test_hook.py`, RA2, RP1). The tests are not part of the installed plugin; install
from `plugins/berth/` only.
