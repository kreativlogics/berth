# berth reference

## Hooks

| Event | Matcher | What berth does |
|---|---|---|
| PreToolUse | `Bash\|Write\|Edit\|MultiEdit\|NotebookEdit` | Deploy check, lock-branch protection, its own config and state protection, confirmation for its own risky commands |
| Stop | all | Reminder (or, with `stop: block`, one block per claim) while this session holds a claim |
| SessionStart | `startup\|resume\|clear` | Reports your claims (live or expired) and claims a past session left behind |
| SessionEnd | all | Records the claims this session still holds, for the next SessionStart. No network: SessionEnd has a short budget and cannot block |

### PreToolUse order

1. Resolve config. The project is the nearest folder above Claude Code's project folder (or the command's folder)
   with `.berth/config.json`, else with `.git`. When the project file is broken, berth keeps every check your own
   config supports and sends only commands that name a deploy step to `fail_mode` (your `fail_mode`, raised by the
   project's if it names a stricter one). When your own file is broken, every call goes to `fail_mode`.
2. File tools (Write, Edit, MultiEdit, NotebookEdit) writing into berth's data folder, the project's `.berth/` folder,
   a local store folder or (with the git store) the project's `.git/config`: deny. On macOS and Windows the check
   ignores letter case, as those file systems do.
3. Bash, in this order:
   1. while any environment is guarded, a command longer than 20,000 characters goes to `fail_mode`: berth reads
      only that much. Text piped into a shell from a program or file berth cannot read (a download, another
      program's output, `cat` of a file, `printf` formats and escapes) asks (deny when unattended), and a command nested too deeply to read is denied;
   2. any git command that names a `*-branch-deploy-lock` branch and could create, move, push or delete it (`push`,
      `send-pack`, `checkout -b`, `branch -f`, `update-ref`, `config`, `filter-branch` and the rest; reads such as
      `fetch`, `ls-remote`, `log`, `show` and `branch --list` are fine, unless a fetch writes into a local lock
      branch; a fetch into `refs/remotes/` is a read), a write inside a git folder (`.git`, or a bare repository such as a local remote) into a `refs/heads` path naming one or into `packed-refs`, a delete of its `refs` or `refs/heads` or of `refs/...` by a file name pattern, a `find` there that deletes or runs a program other than a reader (or one from any folder whose name or path tests name refs, heads or packed-refs), and the same by name alone where a path goes through `$(...)`, a variable or a folder put in place in the same command,
      any write under `.git` or to a git config file (`~/.gitconfig`) in a command naming one, a push or ref write
      run by another program (`setsid`, `osascript`, `su -c`, `jj`, `hub`, `git-push`) whose own words name one, a writing `gh api`, `glab api` or `hub api` call, a writing HTTP client call
      (`curl`, `wget`, `http`, `xh` with a write method or a body, also inside a cluster of short options, curl's config file, wget's config file or startup command, a shortened long option of wget's, or a config file chosen by
      `WGETRC`, `CURL_HOME`, `XDG_CONFIG_HOME` or `HOME`) or inline interpreter code that names one: deny. Inline code is a program given with a
      flag (`python -c`, `node -e`, `perl -e`, `php -r`/`-B`/`-R`/`-E`, `bun -p` and the like, also inside a cluster
      such as `python3 -Ic` or `perl -we`, after options that take a value, and with versioned names such as
      `perl5.34`), or `python -m timeit`, or fed on stdin (a heredoc, a here-string, `-` or `/dev/stdin`, or a
      pipe), or an inline flag anywhere before `--`; berth reads that
      program only, or the whole command when the program comes from a pipe. The name counts when it is split by
      quotes, spelled with `$'\x..'` escapes, or held in a variable that a push in the same command uses. A script
      fed to a shell the same ways (`bash <<EOF`, `bash <<< '...'`, `echo '...' | sh`) is read as commands, and so
      is what `find -exec` and `env -S` run (split with its quotes, as env does; a split string with `${VAR}`, an
      unclosed quote or an unknown escape is asked about). Wrappers (`sudo`, `env`, `xargs`, `timeout`, `arch`, `xcrun`, `script` and
      the like) are removed first, program names are read in lower case, and a command with braces is also checked
      as the shell expands it (`{make,} deploy-{staging,x}`), and with file name patterns expanded against the
      command's folder (`./deploy-stag*.sh`, at most 50 files per word; a pattern with more than three wildcard
      folders, or one that takes more than 3 seconds, sends the command to `fail_mode`; the arguments of pattern readers
      such as `grep`, `sed`, `awk` and `find` are search patterns and are not expanded). Refs given on stdin to `send-pack --stdin`, `update-ref --stdin` and
      `fast-import` count like arguments. Comments, and heredoc bodies read by anything else, are not commands; a comment
      never hides the lines after it, and lock words in a commit or tag message never block a push. A push
      setting given with `git -c` (`remote.*.push`, `remote.*.url`, `url.*`) counts as part of the push;
   3. while any environment is guarded, ask (deny when unattended) for: a mirror push; a pruning push with a
      wildcard refspec, `--all`, `--branches` or no refspec; a push of every branch (`--all`, `--branches`, a
      wildcard refspec unless it writes only under `refs/tags/`, or `:` and `+:`); a push with a one-off push or
      address setting (`git -c`, `--config-env`, or `GIT_CONFIG_*` variables);
      a push of a branch given as a `?` or `[` file name pattern (`-o` values are not branches); a push
      that deletes or forces a branch whose name is only known when it runs (`$BRANCH`, `$(...)`), or any such push
      in a command that mentions "lock" (a push run by `xargs` there is denied); and, with the git store, a change to where the remote points
      (`git remote set-url`, `rename`, `remove`, or `git config` setting or removing a `remote.*`, `url.*`, `push.*`,
      `include.*` or `includeIf.*` key;
      reading one is fine, and `get` or `list` counts as a read only as the first word); and a long git option
      before the subcommand that berth does not know (it could take a value and hide the subcommand);
   4. a command that writes into berth's protected folders (redirects, `cp`, `mv`, `rm`, `tee`, `sed -i` and the
      other writers checkrein's parser knows), deletes or unpacks over a folder that holds one, runs `git rm`,
      `mv`, `checkout`, `restore` or `clean` on a path inside one, runs `find -delete` inside one (or above one with a
      test naming `.berth` or `*.json`), or runs inline interpreter code naming berth's files, or gives them to an editor or rewriting program (`ex`,
      `vim`, `sqlite3`, `sponge`, `osascript`, `dd`, `ln`, `rsync`, `install`, `awk -i inplace`, a writing
      `git config --file` and the like; an editor also when its own words or heredoc name berth's folder, and
      `ed`, `ex`, `sqlite3`, `patch`, `osascript`, `emacs --batch` or `vim -s`/`-e` when the command names it anywhere, since their script comes on stdin), runs one inside berth's folder, gives `cp` an option after its files,
      or runs a program other than a reader under `xargs` in a command naming one of berth's folders: deny; another program berth does not know as a reader that is given one of berth's
      files asks (deny when unattended). Copying berth's files out with `cp` is fine. A file counts by its
      path (also after `=`, as in `--file=` or `FILES=`), so a project's own `identity.json` is not berth's;
   5. berth's own CLIs (found by any word naming the script, however they are started: `python3 -X utf8`, `uv run`,
      any case): an
      environment variable set in front of them or anywhere in the command (`VAR=`, `env`, `export`, `declare`,
      `set`, `source` and the like) is denied, and so is any `--data` other than the hooks' data folder;
      `takeover` asks; `berthcfg.py set`, `unset`, `approve` and `init --yes` ask; everything else runs;
   6. deploy matching (commands that only read or print, and berth's own CLIs started directly or by a launcher
      with options berth knows, never match): for each guarded
      environment the command deploys to, read its lock and the global lock.
      Allowed only when you hold a live claim and no one else holds the global lock. Otherwise `unclaimed`
      decides (deny by default; `ask` asks, and still denies when unattended).
4. If the store cannot be read, or the remote does not answer within 45 seconds over all of a check's git calls:
   `fail_mode`.

"Unattended" means `permission_mode` is `dontAsk` or `bypassPermissions`. In those modes every ask becomes a deny,
with the reason saying so.

## Claims CLI

`/berth:claim` runs `scripts/berth.py`. Every command accepts `--data DIR` and `--project-dir DIR`; the hook allows
Claude to run it only with the hooks' own `--data`, which the skills pass. Without `--project-dir` the project is
found from the current folder as in "PreToolUse order". Options must be spelled in full (`--data`, not `--da`).

| Command | What it does |
|---|---|
| `status [--json] [ENV ...]` | Who you are, the store, and each guarded environment's state, holder, reason, end time and lock ref |
| `claim ENV [--hours N] [--reason TEXT]` | Creates the lock branch. Refused if anyone holds it or a foreign global lock exists. `--hours` defaults to and is capped by `max_claim_hours` |
| `extend ENV [--hours N]` | Moves your claim's end to N hours from now (same cap). Only your own claim |
| `release ENV` | Deletes your lock branch, only if it is still the commit berth read. Never someone else's |
| `takeover ENV --ref REF [--yes] [--hours N] [--reason TEXT]` | Replaces a stale lock. `REF` is at least 7 characters of the lock ref from `status`, so you confirm the exact lock. Refused for active locks. Without `--yes` it prompts on a terminal and refuses with no terminal. The hook asks you before Claude can run it |

Reasons are capped at 200 characters. Exit codes: 0 done, 1 refused (the message says why), 2 usage, 3 the store
failed.

States: `free`, `yours`, `yours, expired`, `held`, `held, stale` (expired, or no end time and older than
`stale_after_hours`), `ambiguous` (a lock branch without a readable `lock.json`; it blocks, as in branch-deploy).

## Settings CLI

`/berth:config` runs `scripts/berthcfg.py`.

| Command | What it does |
|---|---|
| `show [--json]` | Effective settings, where each came from, and notes (ignored or pending project keys) |
| `explain KEY` | What a key does and its current value |
| `set KEY VALUE [--project]` | Writes your config (or the project's). `environments.NAME PATTERN` adds a pattern |
| `unset KEY [--project] [--pattern P]` | Removes a key, an environment, or one pattern |
| `approve store` | Approves the project config's `store` for this project. A changed value needs a new approval |
| `detect [--json]` | Suggested environments and patterns from CI files and deploy scripts, plus the remotes |
| `init [--project] [--preset P] [--yes]` | Writes what `detect` found. Without `--yes` it prints what it would write |
| `doctor` | Python version, hooks file, data folder, config, identity, a timed store read and a timed sample hook run |

## Config

Files, lowest to highest precedence:

1. built-in defaults;
2. the plugin's options (`preset`, `fail_mode`, `identity`, `max_claim_hours`), set when you enable the plugin.
   Claude Code gives them to hooks only, so each hook saves a copy to `plugin-options.json` in the data folder and
   the CLIs read that copy, never environment variables;
3. your file: `config.json` in berth's data folder (`CLAUDE_PLUGIN_DATA`, else `~/.claude/berth`);
4. the project's `.berth/config.json`, which can only tighten.

| Key | Class | Default | Notes |
|---|---|---|---|
| `version` | meta | 1 | A newer version than berth knows is refused |
| `preset` | preset | standard | `standard` or `strict`; a project can raise it |
| `fail_mode` | safety order allow < ask < deny | ask (strict: deny) | |
| `unclaimed` | safety order ask < deny | deny | |
| `stop` | safety order off < remind < block | remind (strict: block) | |
| `max_claim_hours` | safety minimum | 2 | 0.25 to 24; a project can only lower it |
| `generic_patterns` | safety bool | true | a project can turn it on, not off |
| `environments` | safety union | `{}` | `{"staging": {"patterns": ["<regex>"]}}`; a project adds environments and patterns |
| `identity` | user only | the name berth first found: git `user.name`, else the OS login | ignored in a project file; the first name found is kept in `identity.json`, so a later `git config user.name` does not change whose locks are yours |
| `store` | needs approval in a project | `{"type": "git", "remote": "origin"}` | or `{"type": "local", "path": "<folder>"}` |
| `stale_after_hours` | safety maximum | 24 | a project can only raise it |

Environment names: letters, digits, `.`, `_`, `-`, up to 63 characters. Patterns: valid Python regexes up to 400
characters, with no repeated group that itself repeats (such as `(a+)+`), which can take minutes to match. Values
that look like real secrets are refused.

## Lock format

Branch `<env>-branch-deploy-lock` (global: `global-branch-deploy-lock`) on the store's remote, holding `lock.json`
(compact JSON), commit message `lock [skip ci]`. berth writes every field branch-deploy requires:

```json
{"reason":"release 4.2 smoke test","branch":"feature/x","created_at":"2026-11-25T10:00:00.000Z",
 "created_by":"alice","sticky":true,"environment":"staging","global":false,
 "unlock_command":".unlock staging","link":"","schema_version":1,
 "expires_at":"2026-11-25T11:00:00.000Z","berth":1}
```

`expires_at` and `berth` are berth's additions; branch-deploy ignores unknown fields. Locks from branch-deploy have
no end time: berth treats them as held until `stale_after_hours`, then as stale.

Git operations: `ls-remote` to see which lock branches exist, `fetch` into `refs/berth/*` and `cat-file` to read
them. Writes are compare-and-swap:

- create: an orphan commit pushed without force. It cannot fast-forward an existing branch, so it only succeeds when
  the branch does not exist. Of two people claiming at once, one wins and the other is told who;
- extend: a child of the commit read, pushed without force (fast-forward only);
- release and takeover: a delete with `--force-with-lease` on the commit read.

git runs with a minimal environment (the variables it needs for config, SSH agents, credential helpers, proxies, CA
bundles and commit identity), prompts off, no stdin, its own process session (so ssh cannot stop to ask for a
passphrase or host key) and a 20-second timeout. Your `core.sshCommand` and `GIT_SSH` settings are used as they
are.

The local store keeps the same records as files named after the branches, with a lock file around each change.

## Data folder

| File | What it holds |
|---|---|
| `config.json` | your settings |
| `held.json` | claims made from this machine, the sessions that made them, reminder times, and claims sessions left behind |
| `approvals.json` | approved project `store` values, by project path and value hash |
| `identity.json` | the name berth first found for you (git `user.name`, else the OS login), used while `identity` is unset |
| `plugin-options.json` | the plugin options the hooks last saw, for the CLIs |
| `pycache/` | the hook's bytecode cache, kept out of the plugin folder |

The agent cannot write to the data folder or the project's `.berth/` folder; the CLIs that change them ask you first.
