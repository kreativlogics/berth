# berth

Deploy claims for Claude Code, enforced where the agent works.

Teams that share a staging or dev environment already lock it, in a chat channel, on a board, or with
[github/branch-deploy](https://github.com/github/branch-deploy)'s lock branches. Those locks live at the CI layer or in
people's heads. An agent at a terminal doesn't check them. berth does: when Claude runs a deploy command aimed at a guarded
environment, the hook refuses it unless you hold that environment's claim.

- **Same locks as branch-deploy.** Claims are `<env>-branch-deploy-lock` branches with a `lock.json` on your git
  remote, in branch-deploy's format. A team that uses `.lock` in pull requests and berth in the terminal sees one set
  of locks. No server, no API token: plain git with the access you already have.
- **Claims end.** Every claim has an end time, capped by `max_claim_hours` (default 2). `extend` moves it; `release`
  ends it. When Claude finishes a turn while the session still holds a claim, berth reminds you, or (strict) has
  Claude release it or say why it keeps it.
- **Nobody's lock is overwritten.** berth only creates a lock branch that does not exist, fast-forwards your own, and
  deletes with a lease on the exact commit it read. It never force-pushes. Replacing someone's stale lock is a
  separate `takeover` command that a person confirms.
- **Unattended runs deny.** In `dontAsk` or `bypassPermissions` mode, anything that would ask you is refused
  instead.
- Stdlib Python 3.9+, no dependencies, no telemetry. About 40 ms for a non-deploy tool call. A deploy check reads the
  remote once: about 125 ms against a local remote, plus your network.

## Install

```
/plugin marketplace add kreativlogics/berth
/plugin install berth@berth
```

Then, in a project:

```
/berth:config init
```

`init` looks for deploy environments in GitHub Actions, GitLab CI, Makefiles, `package.json` scripts, Capistrano stages
and `deploy-*` scripts, shows the patterns it would guard, and writes them after you agree. Or add one by hand:

```
/berth:config set environments.staging '^make (.* )?deploy-staging(\s|$)'
```

## Use

```
/berth:claim status
/berth:claim claim staging --hours 1 --reason "release 4.2 smoke test"
/berth:claim extend staging --hours 1
/berth:claim release staging
```

`status` shows each guarded environment: free, yours, held by someone (with their reason and end time), stale, or
ambiguous, plus the lock ref. Claude can run `status` on its own; claiming, extending and releasing are ordinary
commands you see it run.

When Claude tries `make deploy-staging` without the claim, it gets:

> berth: this looks like a deploy to staging (`make deploy-staging`), and nobody holds staging. Claim it first:
> /berth:claim claim staging.

When someone else holds it, the message names them, their reason and when their claim ends.

## What counts as a deploy

A command counts when it matches one of the environment's patterns (Python regexes, searched in each simple command
after berth's shell parser strips `sudo`, `env`, `time`, chains, `sh -c` and `$(...)`), or, with `generic_patterns`
on, when the program or its first words name a deploy step (`deploy`, `release`, `rollout`, `ship`, `promote`) and
the command names a guarded environment as a word. Read-only programs (`grep`, `cat`, `echo` and friends) never count.

berth also refuses raw `git push`, `git update-ref` and `gh api` writes to lock branches, edits to its own config and
state, and asks before a mirror or wildcard-pruning push that would delete lock branches along with everything else.

What berth cannot see is in [docs/limits.md](docs/limits.md): a deploy through a script the agent writes first, a
shell alias, a CI pipeline triggered by an ordinary push, or another person's machine without berth.

## Settings

| Key | Default | What it does |
|---|---|---|
| `preset` | standard | `strict` denies when berth cannot check, and blocks Claude's stop while it holds a claim |
| `unclaimed` | deny | what happens to a deploy without your claim (`ask` lets you approve it) |
| `fail_mode` | ask | what happens when the remote cannot be reached or a config file is broken |
| `stop` | remind | when Claude stops while holding a claim: `off`, `remind` or `block` |
| `max_claim_hours` | 2 | longest claim or extension, 0.25 to 24 |
| `environments` | none | guarded environments and their deploy-command patterns |
| `generic_patterns` | true | also match deploy words plus an environment name |
| `identity` | git `user.name` (the first one berth sees, kept) | your name in `created_by`; use your GitHub login if your team also uses branch-deploy |
| `store` | git remote `origin` | or `{"type": "local", "path": "..."}` for solo use |
| `stale_after_hours` | 24 | when a lock with no end time (made by branch-deploy) counts as stale |

Config is layered: defaults, the plugin's options, your `config.json` in berth's data folder, then the project's
`.berth/config.json`. A project file can add environments and make levels stricter, never looser. It cannot set
`identity`, and a `store` in it needs your one-time `approve`. Full details: [docs/reference.md](docs/reference.md).

## Scanning it

Claude Code installs only `plugins/berth/`, so that is the folder to scan or review. Tests and docs stay at the
repository root. They quote the commands berth refuses (raw pushes to lock branches, mirror pushes) as test inputs and
examples, so a scanner run on the whole repository reports those quotes. [SECURITY.md](SECURITY.md) lists the
scanner notes for both.

## Development

```
cd tests && python3 -m unittest discover
```

Tests use temporary folders and local bare repositories as remotes; they never touch a real remote.
`claude plugin validate --strict plugins/berth` checks the manifest.

## Licence

MIT. See [LICENSE](LICENSE).
