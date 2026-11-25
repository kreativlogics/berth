# Threat model

## What berth protects

A shared environment (staging, dev, QA) that one person or agent should deploy to at a time. The asset is the
claim: who may deploy now, until when. berth's job is to keep an agent from deploying without it, and from getting a
claim by a route a person would not see.

## Who is in the picture

- **You**, running Claude Code, who can approve or refuse anything berth asks about.
- **The agent**, which may misread a task, retry a refused command another way, or follow instructions planted in a
  file, an issue or a lock's `reason`.
- **Teammates**, who share the remote and may use berth, branch-deploy, or neither.
- **The repository**, whose files (CI config, Makefiles, `.berth/config.json`) may have been written by someone else.

## Threats and what berth does

| Threat | Response | Test |
|---|---|---|
| Agent deploys without a claim | PreToolUse deny (or ask with `unclaimed: ask`) | `test_deploy_unclaimed_denied` |
| Agent deploys while someone else holds it | Deny, naming the holder, reason and end | `test_deploy_claim_by_other_denied_names_holder` |
| Agent wraps the deploy (`sudo`, `env`, `sh -c`, `$(...)`, chains, `time`, `arch`, `xcrun`, `script`, `env -S` with quotes), spells the program in another letter case, builds it with brace expansion or a file name pattern, or puts options with values before the script | The shell parser unwraps before matching, reads program names in lower case, the brace- and glob-expanded commands are checked too, and runner option values are skipped | `test_wrapped_and_chained_deploy_detected`, `test_round_seven_bypasses_closed`, `test_round_nine_bypasses_closed`, `test_round_ten_bypasses_closed` |
| Agent writes or deletes a lock branch with raw git (a push, or a local copy made with `checkout -b`, `branch -f`, `config` or a write into `.git/refs`, `.git/HEAD` or a git config file and pushed later; or a push run by another program such as `setsid`, `osascript`, `su -c`, `jj` or `hub`), `send-pack`, `gh`/`glab`/`hub api`, an HTTP client (also with options in a cluster such as `curl -sXDELETE`) or inline code, even with the name split by quotes or held in a variable | Deny | `Tamper` tests, `test_round_eight_bypasses_closed`, `test_round_nine_bypasses_closed`, `test_http_client_writes_to_lock_ref_denied`, `test_lock_name_outside_the_push_denied`, `test_lock_branch_written_without_a_push_denied`, `test_fetch_into_a_local_lock_branch_denied` |
| Agent hides a command after a comment, in a heredoc, in a `$'...'` string, or past the length berth reads | Comments, heredocs (also closed by `EOF)` inside `$(...)`) and ANSI-C strings are read as the shell does; a command over 20,000 characters goes to `fail_mode` | `test_comment_does_not_hide_later_commands`, `test_heredoc_text_is_not_a_command`, `test_heredoc_closed_inside_substitution_does_not_hide_commands`, `test_ansi_c_quote_does_not_hide_commands`, `test_overlong_command_uses_fail_mode` |
| Agent feeds the deploy or lock write to a shell or interpreter on stdin (`bash <<EOF`, `bash <<<`, `echo ... \| sh`, `python3 - <<EOF`, `echo ... \| python3`), hides the flag in a cluster (`python3 -Ic`, `perl -we`), or runs it with `find -exec` | The fed script is read as commands, a fed or flagged program counts as inline code, `find -exec` commands are checked; unknown text piped into a shell, and `printf` formats or escapes, ask | `test_script_fed_to_a_shell_or_interpreter_checked`, `test_interpreter_program_from_a_pipe_or_flag_cluster_checked`, `test_interpreter_options_and_versioned_names_checked`, `test_round_six_bypasses_closed`, `test_find_exec_deploy_detected`, `test_unknown_text_piped_into_a_shell_asks`, `test_printf_or_echo_escapes_piped_into_a_shell_ask` |
| Agent deletes every lock with a mirror push, a pruning push, a push of every branch (also by wildcard or `:`), a push of a branch named at run time (also by `xargs`), or a push whose refs come from a one-off `git -c` or `GIT_CONFIG_*` setting | Ask; deny when unattended. Lock words in a commit message, and inline code that names a lock only elsewhere in the command, do not block (`test_lock_words_in_messages_do_not_block_a_push`, `test_inline_code_checks_only_its_own_program`) | `test_one_off_push_setting_asks`, `test_push_that_deletes_unnamed_remote_branches_asks`, `test_push_of_branch_named_at_run_time_asks`, `test_push_of_every_branch_or_built_lock_name_asks` |
| Agent points the remote at a fake copy, claims there and deploys | `git remote set-url`/`rename`/`remove` and `git config remote.*`/`url.*`/`push.*`/`include.*` ask; `.git/config` edits are denied | `test_changing_the_remote_asks` |
| Agent edits berth's config, `held.json`, approvals or the local store, or deletes a folder holding them | Deny for file tools, shell writers, editors and rewriting programs (`ex`, `ed` scripts on stdin, `sqlite3`, `sponge`, `osascript`, `ln`, `rsync`, `awk -i inplace`, `git config --file`), `xargs` fed their path, `git rm`/`checkout`/`restore`, `find -delete` and inline code naming them, also with the path in another letter case on macOS and Windows; a program berth does not know that is given one of these files asks | `test_data_and_store_edits_denied`, `test_path_in_other_letter_case_denied` |
| Agent pretends to be someone else (`CLAUDE_PLUGIN_OPTION_IDENTITY=...`, `export`, another `--data` folder) | Deny variables set around berth's CLIs and any `--data` but the hooks'; the CLIs read only the options the hooks saved | `test_cli_env_prefix_denied`, `test_cli_with_exported_variables_denied`, `test_cli_other_data_folder_denied`, `test_cli_ignores_identity_variables` |
| Agent changes `git config user.name` to match someone's lock | The first name berth found is kept; only `identity` changes it | `test_git_name_change_does_not_change_owner` |
| Agent starts berth's CLI another way to skip a confirmation (`python3 -X utf8`, `uv run`, another case), or names berth's CLI in a deploy to have it skipped | The safety checks find berth's CLI by any word naming it; only an exact launcher form (known options only) is spared the deploy check | `test_cli_started_other_ways_still_checked` |
| Agent changes identity or other settings through the CLI | `set`, `unset`, `approve`, `init --yes` ask you | `test_cfg_set_asks` |
| Agent removes someone else's lock | `release` and `extend` refuse foreign locks; `takeover` works only on stale locks, needs the lock's ref and asks you | `test_release_foreign_refused`, `test_takeover_*` |
| Two claimers race | The remote's ref creation is atomic; the loser is told who won | `test_claim_race_loser_told` |
| A lock changes between read and delete | Delete uses `--force-with-lease` on the commit read | `test_release_after_ref_moved_refused` |
| berth overwrites a lock by force | No plain force push, no `+` refspec, no mirror or prune; checked by an argv audit | `test_never_plain_force_push` |
| Unattended run (`dontAsk`, `bypassPermissions`) | Every ask becomes deny | `test_unattended_ask_becomes_deny` |
| Remote unreachable, slow, or config broken | `fail_mode` (ask by default; deny when unattended), with one 45-second deadline over a check's git calls so the hook never times out undecided. A broken project file keeps your own settings and sends only deploys to `fail_mode` | `test_remote_unreachable_uses_fail_mode`, `test_hook_budget_ends_slow_reads`, `test_broken_config_fail_mode`, `test_broken_project_config_keeps_user_settings` |
| A project file makes takeover easy (low `stale_after_hours`) or the hook slow (exponential pattern) | A project can only raise `stale_after_hours`; ambiguous locks cannot be taken over; exponential pattern shapes are refused | `test_project_can_only_raise_stale_after_hours`, `test_takeover_unreadable_lock_refused`, `test_exponential_pattern_rejected` |
| A hostile project `.berth/config.json` | It can only add environments and tighten levels; `identity` in it is ignored; `store` needs your approval, again when it changes | `test_config.py` layering tests |
| Instructions planted in CI files or lock reasons | Discovery validates names and builds patterns with `re.escape`; lock text is shown as data, with control characters removed; the skill tells Claude never to follow it | `test_instruction_text_in_repo_is_data`, `test_lock_text_sanitised` |
| A secret pasted into config | Values that look like real secrets are refused | `test_secret_value_rejected` |
| Session ends with a claim still held | Reminder at Stop; recorded at SessionEnd and reported at the next SessionStart; the end time is the backstop | `test_session.py` |

## Out of scope

Deploys berth cannot see (scripts written then run, aliases, run-time commands, CI triggered by a push) are in
[docs/limits.md](docs/limits.md). So are teammates without berth, a person with direct push access writing any lock
they like, and a compromised machine. berth relies on your git access and the remote's permissions for everything it
writes; it holds no credentials of its own.
