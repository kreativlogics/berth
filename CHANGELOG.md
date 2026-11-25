# Changelog

## 0.1.0 (unreleased)

First release.

- Discovered deploy-script patterns match after the parser removes wrapper programs; a test covers wrapped runs.
- PreToolUse deploy check: a command matching a guarded environment's patterns (or, with `generic_patterns`, a deploy
  word plus the environment's name) is allowed only while you hold a live claim and nobody else holds the global
  lock.
- Claims as github/branch-deploy lock branches on a git remote, with berth's `expires_at`. Create, extend and release
  are compare-and-swap; berth never force-pushes. A local folder store for solo use and tests.
- `berth.py` claims CLI: status, claim, extend, release, takeover (stale locks only, confirmed by a person).
- `berthcfg.py` settings CLI: show, explain, set, unset, approve, detect, init, doctor. Discovery from GitHub
  Actions, GitLab CI, Makefiles, `package.json`, Capistrano and deploy scripts.
- Stop reminder or block while a session holds a claim; SessionEnd record and SessionStart report of claims left
  behind.
- Lock-branch protection: any git write naming a lock branch (push, `checkout -b`, `branch -f`, `config`,
  `update-ref`, a fetch into one), `gh api`, HTTP-client and inline-code writes are denied, also with the name
  split by quotes, spelled with escapes or held in a variable; mirror, pruning, every-branch (also wildcard) and
  run-time-named pushes ask, and so do changes to the remote's address.
- Scripts fed to a shell or interpreter on stdin (also by a pipe), flag clusters such as `python3 -Ic`, and
  commands run by `find -exec` are checked; inline code is judged by its own program, not the whole command.
  Unknown text piped into a shell, including `printf` formats and escapes, asks. One-off push settings (`git -c`, `GIT_CONFIG_*`)
  count as part of the push; refs on stdin (`send-pack --stdin`, `update-ref --stdin`) are checked, `env -S`
  commands are read, and an unknown long git option before the subcommand asks. Program names are read in lower case, brace
  expansion is applied before matching, and `arch`, `xcrun` and `script` are unwrapped. `glab api`, `hub api` and
  writing HTTP calls with clustered short options count as lock writes; a push run through xargs in a command naming a lock is denied. berth's CLI is
  spared the deploy check only in an exact launcher form; its safety checks apply to any word naming it (not when it is only read).
- Writes into `.git/refs` or `packed-refs` that name a lock, and pushes run by programs berth cannot unwrap
  (`setsid`, `osascript`, `su -c`, `jj`, `hub`, `git-push`) in a command naming a lock, are denied; `env -iS`,
  wrapper clusters (`sudo -Hu`), `wget` option prefixes and `-e`, and curl's config-file option are read. Editors and rewriting
  programs given berth's files are denied, and unknown programs given them ask. `git config push.*` asks. The
  generic rule reads the script a shell or interpreter runs (`bash scripts/deploy.sh staging`), and patterns
  match the program as spelled as well as in lower case. Commands over 20,000 characters go to `fail_mode`; comments, heredocs and `$'...'` strings
  are read as the shell does.
- `env -S` is split with its quotes, and file name patterns are expanded against the working folder before
  matching (`./deploy-stag*.sh`); a push whose branch is a `?` or `[` pattern asks. Any write under `.git`, to a git
  config file or to a bare repository's lock ref in a command naming a lock is denied (`echo 'ref: ...' >
  .git/HEAD`). `xargs` fed a path to berth's files, `awk -i inplace` and `rsync --remove-source-files` on them are
  denied; `wget --config` and HTTP-client config variables (`WGETRC`, `CURL_HOME`) count as writes. The generic rule
  skips script-runner option values (`python3 -W ignore`, `bash -o pipefail`, `uv run --with x`), and a discovered
  script pattern matches only when the script runs, not when it is named (`git add`, `chmod`). Common file names
  (`identity.json`) outside berth's folders, copies out of them, `git config --file ... --list` and pushes in a
  command that only mentions `branch-deploy` elsewhere are no longer blocked.
- `env -S` follows env's own splitting (`\_`, quotes, `\c`); a split string berth cannot read exactly (`${VAR}`, an
  unclosed quote, an unknown escape) is asked about, even with no environments guarded, instead of read as running
  nothing. File name patterns stop at 50 files, and one with more
  than three wildcard folders, or past a 3-second budget, sends the command to `fail_mode`. `ln`, `rsync` and
  `install` given berth's files are denied again (a hard link is an edit), as is `cp` with an option after its files,
  any writer run inside berth's folder, and an editor whose own words or heredoc name berth's folder, or that has
  text piped into it from a command naming it (an `ed` script on stdin, `$HOME/...`). `git config include.*` asks
  like a remote change. Inside a git folder (`.git` or a bare repository such as a local remote), writing
  `packed-refs`, deleting `refs` or `refs/heads`, deleting `refs/...` by a file name pattern, and a `find` that
  deletes or runs programs there are denied; folders named `refs` elsewhere are left alone. `git push -o` values
  are not read as branches, `git config` option values (`--comment -l`) are not read as options, and the arguments
  of pattern readers (`grep`, `sed`, `awk`, `find`, `rg`, `jq`) are not expanded as file names. An `env -S` string
  berth cannot read is asked about even when a later part of the command has its own problem.
- `ed`, `ex` and `sqlite3` (and `vim -s`/`-e`) read their script from stdin, so any mention of berth's folder in
  the command counts for them, however the text reaches them (`nice`, a subshell, `<(...)`, `<<<`, a file written
  first). `env -P`, `-a` and `--argv0` take a value. `git config KEY VALUE -l` (or `--show-origin`) is a write.
  `find` reads `-H`/`-L`/`-P`/`--` before its paths, and one that deletes or runs programs with a name or path test
  naming refs, heads or packed-refs is denied from any folder. Where a path goes through `$(...)` or a variable, or
  through a folder linked, moved or cloned into place in the same command, refs paths are judged by name. Programs
  run by `find -exec`, `gfind -exec` and `fd -x` are expanded like any other command.
- `find` also skips BSD's leading options (`-x`, `-E`, `-s`, `-d`, `-X`, `-f <path>`) and GNU's `-Dtree`; a find
  whose `-exec` only runs readers is not a write, and refs, heads and packed-refs count only as whole path parts.
  `patch`, `osascript` and `emacs --batch` read their script from stdin like `ed`; clustered vim flags (`-Nes`)
  count. A file name pattern in a path that deletes (`remote.git/ref[s]`, `.gi?/refs`) is expanded against what
  exists, and deleting a bare repository whole counts as deleting its locks. `git config KEY --get` is a write,
  since git reads options only before the key. `fd -Hx`, `-xmake` and `--exec=` forms, and GNU env's long-option
  prefixes (`--arg`, `--ch`, `--split`), are read.
- berth's CLIs read the plugin options the hooks save (`plugin-options.json`), must be run with the hooks' `--data`,
  find the project from any subfolder, and keep the first identity they find (`identity.json`).
- Presets `standard` and `strict`; layered config where a project file only tightens, cannot set identity, and needs
  approval to change the store.
- git runs with a minimal environment, prompts off, a 20-second timeout per call and a 45-second deadline per
  hook check.
- Shell parser, path globs, write-target detection, approvals and secret checks vendored from checkrein (see
  VENDORED.md).
