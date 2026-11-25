# What berth does not do

berth checks the commands Claude Code runs on your machine. It is a guard against an agent (or a tired person
approving it) deploying over someone else's work, not a security boundary against a hostile user.

## Known misses

Each has a test marked as an expected failure, so a future fix shows up.

- **A deploy through a script the agent writes first.** `printf 'make deploy-staging' > go.sh && bash go.sh`: the
  hook sees the command, not what the file will contain when it runs. Patterns for your own deploy scripts
  (`deploy-staging.sh`, `bin/deploy staging`) still match by name.
- **Aliases and shell functions.** A profile alias `dstg` that runs a deploy looks like an unknown command. Add a
  pattern for it if you use one.
- **Commands built at run time.** `eval "$CMD"`, variables holding the program name, or a deploy started by another
  program's config (a git hook, a task runner reading its own file) are outside what a command-line check can see.
  So are programs that run commands from their own language: `awk 'BEGIN{system("...")}'`, `make -f` on a file
  written first, or a git alias set with `git -c alias.x=...`. File name patterns are expanded against the files
  that exist when berth checks, so a file the same command creates first (`touch x && make deploy-st*`) is not
  seen; a push of a branch given as a pattern asks either way.
- **CI that deploys on an ordinary push.** If pushing to `release/staging` deploys, berth does not know that unless
  you add a pattern for the push (`^git push\b.*\brelease/staging\b`).
- **Other machines and other tools.** A teammate without berth, a person clicking a deploy button, or a CI job only
  sees the lock if it checks lock branches. branch-deploy does; other tools need their own check.
- **A copy of berth's CLI under another name.** `cp berth.py b.py && python3 b.py takeover ...` is not recognised as
  berth's own tool. Given berth's data folder on the command line it asks, as any unknown program does; a copy edited
  to name the folder inside itself does not. `takeover` still refuses anything but a stale lock and needs the
  lock's exact ref.
- **Whole-tree git commands and branch switches.** `git checkout .`, `git rm -r .`, `git clean` and switching to a
  branch with another `.berth/config.json` change the project file without naming it. Only the file itself, or a
  folder inside `.berth`, is protected by name. Your own config still applies, and a project file can only tighten
  it.
- **Inline programs that build paths.** `python3 -c` or `node -e` code that names `.berth`, `held.json` or a lock
  branch is denied, but code that builds those names from pieces is the same as a script written then run.
- **Branch names computed from nothing visible.** A push to a branch whose whole name is computed when it runs
  (`git push origin HEAD:"$(decode name)"`) asks only when it deletes or forces, or when the command mentions
  "lock". An ordinary push of `"$(git branch --show-current)"` is left alone, so a name produced entirely by
  another program is not seen.
- **The remote's address changed outside the project.** `git remote set-url`, `git config remote.*`/`url.*` and
  edits to the project's `.git/config` ask or are denied, but a `url.*.insteadOf` rule in your global git config
  (`~/.gitconfig`, `GIT_CONFIG_GLOBAL`) changes where `origin` points without touching the project.
- **Case-insensitive folders on Linux.** Paths are compared ignoring letter case on macOS and Windows only. A
  case-insensitive mount on Linux (some network or FAT volumes) holding berth's data could be reached by a path in
  another case.
- **Subagents.** Their tool calls go through the same PreToolUse hook, but the Stop reminder fires only for the main
  agent (subagents raise SubagentStop, which berth does not use).
- **Programs that run other programs.** berth unwraps the common wrappers and reads what shells, `env -S`,
  `find -exec` and `script` run. A push to a lock branch through any other program (`setsid`, `osascript`, `jj`)
  is denied only when that program's own words name the lock branch; a deploy started by such a program, or a program that
  writes berth's files without naming them, is not seen. `env -S` is split as env splits it; a split string
  with `${VAR}`, an unclosed quote or an unknown escape is asked about (denied when nobody is there), since the
  program it runs is not known.

## Design limits

- **A local remote reached only by pattern.** When the lock store's remote is a folder on this machine, a `find`
  run from a folder above it whose tests match the lock only by a loose name (`find .. -name 'staging*' -delete`),
  or a program berth does not know deleting files there, is not seen. Remotes on a server are not reachable this way.

- **Identity is a name, not proof.** `created_by` is your configured identity, else the git `user.name` (or OS
  login) berth first saw, kept in `identity.json` so a later `git config user.name` does not change it. If you
  change your name on purpose, set `identity`; SessionStart tells you when the two differ. Anyone with push access
  to the remote can write a lock with any name; berth only prevents the agent from doing it through berth or raw
  git. Use branch protection on `*-branch-deploy-lock` if you need more.
- **Clocks.** End times are compared with this machine's clock in UTC. A machine whose clock is hours off sees claims
  end early or late.
- **No automatic release at session end.** SessionEnd cannot wait for the network, so berth records claims still
  held and reports them at the next SessionStart instead. The claim's end time is the backstop.
- **branch-deploy locks never expire.** berth treats them as held until `stale_after_hours`, then offers `takeover`;
  it never removes them on its own.
- **Cost of a deploy check.** Each deploy command reads the remote once (`ls-remote`, plus a `fetch` for each lock
  that exists). On a slow or unreachable remote each git call waits up to 20 seconds and all of them together up
  to 45, then `fail_mode` decides, inside the hook's 60-second timeout.
- **Generic matching is word-based.** With `generic_patterns` on, a command whose program or first two plain
  arguments contain a deploy word, and that names a guarded environment anywhere, counts as a deploy. For a shell
  or interpreter running a script (`bash scripts/deploy.sh staging`, `uv run deploy.py staging`) the script's name
  stands in for the program. Other paths and file names do not count as the deploy word, but option values do (skipping them would also skip
  `make -k deploy-staging`), so `docker build -t app:release --build-arg ENV=staging .` or
  `gh release create v1 --notes "staging fixes"` are treated as staging deploys. Approve them when asked, or turn
  `generic_patterns` off and list your deploy commands as patterns.
- **Your git hooks run on berth's pushes.** Claims are pushed with plain `git push`, so a `pre-push` hook runs for
  them too. A hook that is slow counts toward the 20-second limit; one that refuses the lock branch makes the claim
  fail with its message.
- **Long commands.** berth reads the first 20,000 characters of a command. A longer command, while any
  environment is guarded, goes to `fail_mode` (ask by default) rather than being checked in part.
- **Regex patterns from a project file.** A project can add patterns. Patterns with the shapes that backtrack
  exponentially (a repeat, `?` or `|` inside a repeat) are refused, and commands are capped at 20,000 characters.
  If a pattern still made the hook run past Claude Code's hook timeout, the tool call would go ahead unchecked:
  a hook that times out does not decide. Keep patterns simple.
- **Not supported in v0.1:** the GitHub deployments API as a store, a server store, claims for other people, queues,
  calendar holds, Windows.
