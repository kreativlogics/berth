"""PreToolUse: deploy detection, claim checks, tamper protection, berth's own CLIs and timing."""

import os
import sys
import time
import unittest

from helpers import BERTH, BERTHCFG, HOOK, STAGING_PATTERN, Case


class DeployChecks(Case):
    def setUp(self):
        super().setUp()
        self.guard_staging()

    def test_deploy_unclaimed_denied(self):
        self.assertDecision("deny", self.bash("make deploy-staging"), "nobody holds staging")

    def test_deploy_with_own_claim_allowed(self):
        res = self.cli("claim", "staging", "--hours", "1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertDecision("allow", self.bash("make deploy-staging"))

    def test_deploy_claim_by_other_denied_names_holder(self):
        self.put_lock("staging", "bob")
        self.assertDecision("deny", self.bash("make deploy-staging"), "held by bob")

    def test_deploy_expired_own_claim_denied(self):
        self.put_lock("staging", "alice", hours=-0.5, created_hours_ago=1)
        self.assertDecision("deny", self.bash("make deploy-staging"), "expired")

    def test_foreign_global_lock_denies_all(self):
        self.assertEqual(self.cli("claim", "staging").returncode, 0)
        self.put_lock(None, "bob", extra={"environment": None, "global": True})
        self.assertDecision("deny", self.bash("make deploy-staging"), "global lock")

    def test_ambiguous_lock_branch_denied(self):
        empty_tree = self.git("hash-object", "-t", "tree", "-w", "--stdin", stdin="").stdout.strip()
        commit = self.git("commit-tree", empty_tree, "-m", "x").stdout.strip()
        self.git("push", "-q", "origin", "%s:refs/heads/staging-branch-deploy-lock" % commit)
        self.assertDecision("deny", self.bash("make deploy-staging"), "ambiguous")

    def test_wrapped_and_chained_deploy_detected(self):
        for command in ("sudo make deploy-staging", "env X=1 make deploy-staging", "true && make deploy-staging",
                        "sh -c 'make deploy-staging'", "echo $(make deploy-staging)", "cd /tmp; make deploy-staging",
                        "time make -j2 deploy-staging"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command))

    def test_non_deploy_commands_allowed(self):
        for command in ("git status", "make test", "npm run build", "ls -la", "git log --oneline -5"):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))

    def test_generic_pattern_matches_deploy_word_and_env(self):
        for command in ("./scripts/deploy.sh staging", "kubectl rollout restart deployment/api -n staging",
                        "fly deploy --app web-staging", "npm run deploy:staging", "bin/shipIt staging"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command))

    def test_generic_word_inside_other_words_not_deploy(self):
        for command in ("python3 scripts/fix_ownership.py staging", "rails runner scripts/membership_sync.rb staging",
                        "pytest tests/test_deploy.py -k staging", "python3 -m pytest tests/test_release.py staging",
                        "./scripts/prereleased.sh staging"):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))

    def test_project_config_found_from_subfolder(self):
        # Claude Code started in a subfolder: the project's .berth/config.json at the repo root still applies.
        self.write_config({})
        self.write_config({"environments": {"staging": {"patterns": [r"^make deploy-staging"]}}}, where="project")
        sub = os.path.join(self.project, "app")
        os.makedirs(sub)
        self.assertDecision("deny", self.bash("make deploy-staging", extra_env={"CLAUDE_PROJECT_DIR": sub}),
                            "nobody holds staging")

    def test_generic_patterns_off_uses_config_patterns_only(self):
        self.guard_staging(generic_patterns=False)
        self.assertDecision("allow", self.bash("./scripts/deploy.sh staging"))
        self.assertDecision("deny", self.bash("make deploy-staging"))

    def test_reader_commands_not_matched(self):
        for command in ("grep deploy-staging Makefile", "cat deploy-staging.log", "echo deploy staging",
                        "git log --grep 'deploy staging'"):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))

    def test_unguarded_env_ignored(self):
        self.assertDecision("allow", self.bash("make deploy-production"))

    def test_unclaimed_ask_level_asks(self):
        self.guard_staging(unclaimed="ask")
        self.assertDecision("ask", self.bash("make deploy-staging"), "Claim it first")

    def test_unattended_ask_becomes_deny(self):
        self.guard_staging(unclaimed="ask")
        for mode in ("dontAsk", "bypassPermissions"):
            with self.subTest(mode=mode):
                self.assertDecision("deny", self.bash("make deploy-staging", mode=mode), "nobody is here")

    def test_remote_unreachable_uses_fail_mode(self):
        self.git("remote", "set-url", "origin", os.path.join(self.tmp, "missing.git"))
        self.assertDecision("ask", self.bash("make deploy-staging"), "could not check")
        self.assertDecision("deny", self.bash("make deploy-staging", mode="dontAsk"))
        self.guard_staging(fail_mode="deny")
        self.assertDecision("deny", self.bash("make deploy-staging"), "fail_mode is deny")
        self.guard_staging(fail_mode="allow")
        self.assertDecision("allow", self.bash("make deploy-staging"))

    def test_broken_project_config_keeps_user_settings(self):
        self.guard_staging(preset="strict", fail_mode="deny")
        self.write_config({"version": 99}, where="project")
        self.assertDecision("deny", self.bash("make deploy-staging"), "could not check")
        self.assertDecision("deny", self.bash("./bin/deploy qa"), "could not check")
        self.assertDecision("allow", self.bash("git status"))
        self.assertDecision("deny", self.bash("git push origin :staging-branch-deploy-lock"), "lock")
        self.assertDecision("deny", self.pre("Write", {"file_path": os.path.join(self.data, "held.json"),
                                                       "content": "{}"}), "berth's own")

    def test_broken_config_fail_mode(self):
        with open(os.path.join(self.data, "config.json"), "w") as fh:
            fh.write("{not json")
        self.assertDecision("ask", self.bash("make deploy-staging"), "could not check")
        self.write_config({"fail_mode": "deny"}, where="project")
        self.assertDecision("deny", self.bash("make deploy-staging"))


class Tamper(Case):
    def setUp(self):
        super().setUp()
        self.guard_staging()

    def test_raw_git_delete_of_lock_branch_denied(self):
        for command in ("git push origin :staging-branch-deploy-lock",
                        "git push --delete origin staging-branch-deploy-lock",
                        "git -C . push -f origin x:refs/heads/staging-branch-deploy-lock",
                        "git update-ref -d refs/heads/global-branch-deploy-lock"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "lock")

    def test_push_that_deletes_unnamed_remote_branches_asks(self):
        for command in ("git push --mirror origin", "git push --prune origin 'refs/heads/*:refs/heads/*'",
                        "git push --prune origin --all", "git push --prune origin",
                        "git push --prune --branches origin"):
            with self.subTest(command=command):
                self.assertDecision("ask", self.bash(command), "lock branches")
                self.assertDecision("deny", self.bash(command, mode="dontAsk"), "nobody is here")
        for command in ("git push origin trunk", "git push --prune origin trunk:trunk", "git push -u origin HEAD"):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))

    def test_mirror_push_allowed_with_nothing_guarded(self):
        self.write_config({})
        self.assertDecision("allow", self.bash("git push --mirror origin"))

    def test_raw_push_to_lock_branch_denied(self):
        self.assertDecision("deny", self.bash("git push origin HEAD:staging-branch-deploy-lock"))

    def test_gh_api_delete_lock_ref_denied(self):
        for command in ("gh api -X DELETE repos/o/r/git/refs/heads/staging-branch-deploy-lock",
                        "gh api --method PATCH repos/o/r/git/refs/heads/staging-branch-deploy-lock -f sha=abc",
                        "gh api repos/o/r/git/refs -f ref=refs/heads/staging-branch-deploy-lock -f sha=abc"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command))

    def test_http_client_writes_to_lock_ref_denied(self):
        url = "https://api.github.com/repos/o/r/git/refs/heads/staging-branch-deploy-lock"
        for command in ("curl -X DELETE %s" % url, "curl -s --request PATCH %s -d '{\"sha\":\"x\"}'" % url,
                        "curl -XDELETE %s" % url,
                        "curl -d '{\"ref\":\"refs/heads/staging-branch-deploy-lock\"}' "
                        "https://api.github.com/repos/o/r/git/refs",
                        "wget --method=DELETE -O- %s" % url, "http DELETE %s" % url,
                        "R=staging-branch-deploy-lock; "
                        "curl -X DELETE https://api.github.com/repos/o/r/git/refs/heads/$R",
                        "R=refs/heads/staging-branch-deploy-lock; gh api -X DELETE repos/o/r/git/$R"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "lock")

    def test_lock_name_outside_the_push_denied(self):
        for command in ("B=staging-branch-deploy-lock; git push origin :$B",
                        "git push origin --delete $(git branch -r --list 'origin/*-branch-deploy-lock' | sed 's#o/##')",
                        "git branch -r | grep deploy-lock | xargs git push origin --delete"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "lock")

    def test_inline_code_naming_lock_denied(self):
        command = "python3 -c \"import subprocess; subprocess.run(['git', 'push', 'origin', " \
                  "':staging-branch-deploy-lock'])\""
        self.assertDecision("deny", self.bash(command), "lock")

    def test_reading_lock_branches_allowed(self):
        for command in ("git ls-remote origin 'refs/heads/*-branch-deploy-lock'",
                        "git fetch origin staging-branch-deploy-lock",
                        "gh api repos/o/r/git/refs/heads/staging-branch-deploy-lock",
                        "curl -s https://api.github.com/repos/o/r/git/refs/heads/staging-branch-deploy-lock"):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))

    def test_data_and_store_edits_denied(self):
        store_dir = os.path.join(self.tmp, "claims")
        self.guard_staging(store={"type": "local", "path": store_dir})
        cases = [
            ("Write", {"file_path": os.path.join(self.data, "config.json"), "content": "{}"}),
            ("Edit", {"file_path": os.path.join(self.data, "held.json"), "old_string": "a", "new_string": "b"}),
            ("Write", {"file_path": os.path.join(self.project, ".berth", "config.json"), "content": "{}"}),
            ("Write", {"file_path": os.path.join(store_dir, "staging-branch-deploy-lock.json"), "content": "{}"}),
            ("Bash", {"command": "echo {} > %s" % os.path.join(self.data, "held.json")}),
            ("Bash", {"command": "rm -rf %s" % store_dir}),
            ("Bash", {"command": "cp /dev/null .berth/config.json"}),
            ("Bash", {"command": "git rm -r .berth"}),
            ("Bash", {"command": "git checkout HEAD~3 -- .berth/config.json"}),
            ("Bash", {"command": "git restore --source=HEAD~1 .berth"}),
            ("Bash", {"command": "rm -rf %s" % os.path.dirname(self.data)}),
            ("Bash", {"command": "mv %s /tmp/elsewhere" % os.path.dirname(self.data)}),
            ("Bash", {"command": "find %s -name held.json -delete" % self.data}),
            ("Bash", {"command": "find %s -name '*.json' -delete" % os.path.dirname(self.data)}),
            ("Bash", {"command": "python3 -c \"import os; os.remove('.berth/config.json')\""}),
            ("Bash", {"command": "perl -e 'unlink \"%s\"'" % os.path.join(self.data, "held.json")}),
        ]
        for tool, tool_input in cases:
            with self.subTest(tool=tool, input=tool_input):
                self.assertDecision("deny", self.pre(tool, tool_input), "berth's own")

    def test_ordinary_file_edits_allowed(self):
        self.assertDecision("allow", self.pre("Write", {"file_path": os.path.join(self.project, "a.txt"),
                                                        "content": "x"}))
        for command in ("rm -rf build", "find . -name '*.pyc' -delete", "git checkout .", "git rm -r --cached dist",
                        "python3 -c 'print(1)'", "git restore src/app.py"):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))

    def test_push_of_branch_named_at_run_time_asks(self):
        for command in ('git push origin --delete "$BRANCH"', "git push origin :$(cat name.txt)",
                        "git push -f origin HEAD:$TARGET"):
            with self.subTest(command=command):
                self.assertDecision("ask", self.bash(command), "named only when the command runs")
        self.assertDecision("allow", self.bash('git push origin "$BRANCH"'))


    def test_comment_does_not_hide_later_commands(self):
        for command in ("# Deploy to staging\nmake deploy-staging", "echo '#'; make deploy-staging",
                        "echo done # ok\nmake deploy-staging",
                        "gh issue comment '#12' --body hi && make deploy-staging"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "staging")
        self.assertDecision("deny", self.bash("# tidy up\ngit push origin :staging-branch-deploy-lock"), "lock")

    def test_heredoc_text_is_not_a_command(self):
        for command in ("git commit -m \"$(cat <<'EOF'\nDeploy staging fix\n\nRetry the upload\nEOF\n)\"",
                        "cat > notes.md <<'EOF'\nmake deploy-staging\nEOF"):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))

    def test_paths_naming_deploy_and_env_are_not_deploys(self):
        for command in ("cp deploy/staging.json out.json", "mkdir -p deploy/staging", "chmod +x deploy/staging.sh",
                        "shellcheck scripts/deploy-staging.sh", "python3 -m json.tool deploy/staging.json"):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))
        self.assertDecision("deny", self.bash("./scripts/deploy-staging.sh"), "staging")

    def test_overlong_command_uses_fail_mode(self):
        self.assertDecision("ask", self.bash("echo %s; make deploy-staging" % ("x" * 20010)), "longer than")

    def test_lock_branch_written_without_a_push_denied(self):
        for command in ("git checkout -b staging-branch-deploy-lock origin/staging-branch-deploy-lock",
                        "git branch -f staging-branch-deploy-lock HEAD",
                        "git config remote.origin.push :refs/heads/staging-branch-deploy-lock"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "lock branch")
        self.assertDecision("allow", self.bash("git branch -r --list '*-branch-deploy-lock'"))

    def test_push_of_every_branch_or_built_lock_name_asks(self):
        for command in ("git push origin --all", "B=staging-branch-dep; git push origin HEAD:${B}loy-lock",
                        "git push origin 'refs/heads/*'", "git push origin 'refs/heads/*:refs/heads/*'",
                        "git push origin ':'", "git push origin +:", "git push -f origin 'refs/tags/*X:refs/heads/*'",
                        "git --attr-source HEAD push origin --all"):
            with self.subTest(command=command):
                self.assertDecision("ask", self.bash(command), "lock branch")
        self.assertDecision("allow", self.bash('git push -u origin "$(git branch --show-current)"'))
        self.assertDecision("allow", self.bash("git push origin 'refs/tags/*'"))
        self.assertDecision("deny", self.bash("git push origin HEAD:refs/heads/staging-branch-$'\\x64'eploy-lock"),
                            "lock branch")

    def test_fetch_into_a_local_lock_branch_denied(self):
        self.assertDecision("deny", self.bash("git fetch . HEAD:refs/heads/staging-branch-deploy-lock"), "lock")
        self.assertDecision("allow", self.bash("git fetch origin staging-branch-deploy-lock"))
        self.assertDecision("allow", self.bash("git fetch origin staging-branch-deploy-lock:refs/remotes/origin/x"))
        self.assertDecision("allow", self.bash("git fetch origin staging-branch-deploy-lock:"
                                               "refs/remotes/origin/staging-branch-deploy-lock"))

    def test_script_fed_to_a_shell_or_interpreter_checked(self):
        for command in ("bash <<'EOF'\nmake deploy-staging\nEOF", "sh -s <<EOF\nmake deploy-staging\nEOF",
                        "bash <<< 'make deploy-staging'", "echo 'make deploy-staging' | bash"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "staging")
        for command in ("bash <<'EOF'\ngit push origin :staging-branch-deploy-lock\nEOF",
                        "bash <<< 'git push origin :staging-branch-deploy-lock'"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "lock")
        for command in ("python3 - <<'EOF'\nimport shutil; shutil.rmtree('.berth')\nEOF",
                        "python3 <<'EOF'\nopen('.berth/config.json', 'w').write('{}')\nEOF"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "berth's own")
        self.assertDecision("allow", self.bash("python3 tool.py <<'EOF'\nopen('.berth/config.json')\nEOF"))

    def test_interpreter_program_from_a_pipe_or_flag_cluster_checked(self):
        lock = "staging-branch-deploy-lock"
        for command in ("echo 'import os; os.system(\"git push origin :%s\")' | python3" % lock,
                        "cat <<'X' | python3\nprint('%s')\nX" % lock,
                        "python3 -W ignore <<'X'\nprint('%s')\nX" % lock,
                        "python3 /dev/stdin <<< 'print(\"%s\")'" % lock,
                        "python3 -Ic 'print(\"%s\")'" % lock, "perl -we 'print \"%s\"'" % lock,
                        "node -pe '\"%s\"'" % lock, "ruby -le 'puts \"%s\"'" % lock):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "lock")
        for command in ("echo 'import shutil; shutil.rmtree(\".berth\")' | python3",
                        "perl -we 'unlink \".berth/config.json\"'"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "berth's own")

    def test_interpreter_options_and_versioned_names_checked(self):
        for command in ("node --require ./x.js -e \"require('fs').writeFileSync('.berth/config.json','{}')\"",
                        "node -C dev -e \"require('fs').rmSync('.berth',{recursive:true})\"",
                        "python3 --check-hash-based-pycs never -c \"import os; os.remove('.berth/config.json')\"",
                        "php -B \"unlink('.berth/config.json');\" -E ''",
                        "bun -p \"require('fs').rmSync('.berth',{recursive:true})\"",
                        "perl5.34 -e \"unlink '.berth/config.json'\"",
                        "python3 -m timeit -n1 \"import os; os.remove('.berth/config.json')\""):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "berth's own")

    def test_round_six_bypasses_closed(self):
        lock = "HEAD:refs/heads/staging-branch-deploy-lock"
        held = os.path.join(self.data, "held.json")
        deny_lock = ("git send-pack --stdin --force ../remote.git <<< '%s'" % lock,
                     "echo %s | git send-pack --stdin ../remote.git" % lock,
                     "git update-ref --stdin <<< 'delete refs/heads/staging-branch-deploy-lock'",
                     "git --shallow-file log push origin %s" % lock,
                     "env -S 'git push origin %s'" % lock, "env --split-string='git push origin %s'" % lock,
                     "watch --interval 5 git push origin %s" % lock, "sudo --user bob git push origin %s" % lock,
                     "echo | xargs -J % git push origin " + lock,
                     "ruby -W -e 'system(\"git push origin %s\")'" % lock)
        for command in deny_lock:
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "lock")
        for command in ("env -S 'make deploy-staging'", "make -- deploy-staging berth.py --data %s" % self.data,
                        "./deploy.sh staging berth.py --data %s" % self.data):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "staging")
        for command in ("git merge-file %s /tmp/base /tmp/new" % os.path.join(self.data, "config.json"),
                        "git checkout-index -f -- .berth/config.json", "env -S 'rm -f %s'" % held,
                        "ruby -K -e 'File.write(\"%s\",\"{}\")'" % held,
                        "node --disable-warning X -e 'require(\"fs\").writeFileSync(\"%s\",\"{}\")'" % held):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "berth's own")
        for command in ("git pu''sh --all origin", "git send-pack --all ../remote.git",
                        "export GIT_CONF''IG_PARAMETERS=\"'remote.origin.mirror=true'\"; git push origin",
                        "git -c push.default=matching push -f origin", "git --frobnicate x push origin"):
            with self.subTest(command=command):
                self.assertDecision("ask", self.bash(command))
        for command in ("git reflog show origin/staging-branch-deploy-lock",
                        "git merge-file -p %s /tmp/base /tmp/new" % os.path.join(self.data, "config.json"),
                        "git --no-pager log -1", "node --test --test-reporter spec"):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))

    def test_round_seven_bypasses_closed(self):
        lock = "staging-branch-deploy-lock"
        for command in ("GIT push origin HEAD:refs/heads/%s" % lock, "BASH -c 'git push origin %s'" % lock,
                        "git push origin --delete staging-branch-deploy-{lock,x}",
                        "git push origin --delete staging-branch-{deploy-lock,x}",
                        "echo %s | xargs git push origin --delete" % lock,
                        "curl -sXDELETE https://forge.example.invalid/refs/heads/%s" % lock,
                        "curl -sd@body.json https://forge.example.invalid/refs/heads/%s" % lock,
                        "glab api -X DELETE projects/1/repository/branches/%s" % lock,
                        "hub api --method DELETE repos/o/r/git/refs/heads/%s" % lock,
                        "script -q /dev/null git push origin %s" % lock,
                        "script -qc 'git push origin %s' /dev/null" % lock,
                        "arch -x86_64 git push origin %s" % lock, "xcrun --sdk macosx git push origin %s" % lock):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "lock")
        for command in ("MAKE deploy-staging", "{make,} deploy-staging", "make deploy-{staging,x}",
                        "python3 -I fab deploy staging berth.py --data %s" % self.data,
                        "script -q /dev/null make deploy-staging", "arch -arm64 make deploy-staging"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "staging")
        for command in ('python3 -I x "%s" --data "%s" takeover staging --ref abcdef1' % (BERTH, self.data),
                        'Python3 "%s" --data "%s" takeover staging --ref abcdef1' % (BERTH, self.data),
                        'pipx run --spec x "%s" --data "%s" takeover staging --ref abcdef1' % (BERTH, self.data)):
            with self.subTest(command=command):
                self.assertDecision("ask", self.bash(command))
        for command in ("curl -s https://forge.example.invalid/refs/heads/%s" % lock,
                        "curl -sXGET https://forge.example.invalid/refs/heads/%s" % lock,
                        "glab api projects/1/repository/branches/%s" % lock,
                        "xcrun -f make", 'uvx --from x python "%s" --data "%s" status' % (BERTH, self.data)):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))

    def test_round_eight_bypasses_closed(self):
        lock = "staging-branch-deploy-lock"
        config = os.path.join(self.data, "config.json")
        for command in ("git commit-tree -p FETCH_HEAD -m mine 'FETCH_HEAD^{tree}' > .git/refs/heads/%s" % lock,
                        "cp .git/refs/heads/trunk .git/refs/heads/%s" % lock,
                        "setsid git push origin :%s" % lock, "hub push origin :%s" % lock,
                        "jj git push --branch %s" % lock, "git-push origin :%s" % lock,
                        "osascript -e 'do shell script \"git push origin :%s\"'" % lock,
                        "env -iS 'git push origin :%s'" % lock, "sudo -Hu me git push origin :%s" % lock,
                        "script -q -F log.txt git push origin :%s" % lock,
                        "wget --meth=DELETE https://forge.example.invalid/refs/heads/%s" % lock,
                        "wget -e method=DELETE https://forge.example.invalid/refs/heads/%s" % lock,
                        "curl -K- https://forge.example.invalid/refs/heads/%s <<< '-X DELETE'" % lock):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "lock")
        for command in ("ex -sc '%%s/staging/zz/|x' %s" % config, "sqlite3 /tmp/x '.output %s'" % config,
                        "jq . %s | sponge %s" % (config, config),
                        "osascript -e 'do shell script \"rm %s\"'" % config,
                        "git config --file %s a.b c" % config,
                        "env -iS 'python3 %s --data %s set unclaimed ask'" % (BERTHCFG, self.data)):
            with self.subTest(command=command):
                self.assertNotEqual("allow", self.bash(command)[0])
        for command in ("bash scripts/deploy.sh staging", "python3 deploy.py staging", "uv run deploy.py staging",
                        "bash deploy-staging.sh", "watch -tn 5 make deploy-staging", "sudo -iu ops make deploy-staging"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "staging")
        self.assertDecision("ask", self.bash("git config push.default matching"))
        self.assertDecision("ask", self.bash("mystery-tool %s" % config))
        for command in ("cat %s" % BERTH, "git diff -- %s" % BERTHCFG, "grep -n claim %s" % BERTHCFG,
                        "bash scripts/test.sh staging", "wget -q https://forge.example.invalid/refs/heads/%s" % lock,
                        "cat %s" % config):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))

    def test_round_nine_bypasses_closed(self):
        lock = "staging-branch-deploy-lock"
        config = os.path.join(self.data, "config.json")
        for name in ("HEAD:" + lock, lock, "deploy-staging.sh"):
            open(os.path.join(self.project, name), "w").close()
        for command in ("env -S \"sh -c 'git push -f origin HEAD:%s'\"" % lock,
                        "git push -f origin HEAD:staging-bra?ch-deploy-lo?k",
                        "git push origin --delete staging-bra?ch-deploy-lo?k",
                        "echo 'ref: refs/heads/%s' > .git/HEAD" % lock,
                        "printf '[branch \"trunk\"]\\n merge = refs/heads/%s\\n' >> ~/.gitconfig" % lock,
                        "wget --config=rc.txt -O- https://forge.example.invalid/refs/heads/%s" % lock,
                        "WGETRC=rc.txt wget https://forge.example.invalid/refs/heads/%s" % lock,
                        "CURL_HOME=. curl https://forge.example.invalid/refs/heads/%s" % lock,
                        "cp .git/refs/heads/trunk %s/refs/heads/%s" % (self.remote, lock)):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "lock")
        for command in ("env -S \"sh -c 'cp x %s'\"" % config, "echo %s | xargs rm" % config,
                        "echo %s | xargs truncate -s0" % config, "awk -i inplace '{print}' %s" % config):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "berth's own")
        for command in ("env -S \"bash -c 'make deploy-staging'\"", "bash ./deploy-stag*.sh",
                        "./deploy-stag?ng.sh", "uv run --with requests deploy.py staging",
                        "python3 -W ignore deploy.py staging", "python3 -X utf8 deploy.py staging",
                        "bash -o pipefail scripts/deploy.sh staging",
                        "node --require dotenv/config scripts/deploy.js staging"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "staging")
        # Ordinary work that the round-nine rules must leave alone.
        for command in ("vim app/identity.json", "vim config/approvals.json", "prettier --write src/held.json",
                        "make lint FILES=src/identity.json", "git config -f .git/config --list",
                        "git config --file .git/config --get remote.origin.url", "cp %s /tmp/backup" % config,
                        "cd ~/src/branch-deploy && docker push img", "echo src/app.py | xargs rm",
                        "docker buildx build --push -t app . && git log origin/%s" % lock):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))

    def test_round_ten_bypasses_closed(self):
        config = os.path.join(self.data, "config.json")
        for command in ("ln %s /tmp/c && printf x > /tmp/c" % config, "rsync /tmp/x %s --exclude foo" % config,
                        "rsync -a --remove-sent-files %s/ /tmp/out/" % self.data,
                        "cd %s && ln -sf /tmp/x2/config.json" % self.data, "cp /tmp/x %s --sparse auto" % config,
                        "printf 'a\\n{}\\n.\\nw %s\\nq\\n' | ed -s" % config,
                        "ed -s <<'EOF'\nw %s\nq\nEOF" % config):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "berth's own")
        for command in ("cp /tmp/x %s/packed-refs" % self.remote, "rm -r %s/refs/heads" % self.remote):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "lock")
        for command in ("git config include.path ../evil.cfg", "git config -f .git/config include.path list"):
            with self.subTest(command=command):
                self.assertDecision("ask", self.bash(command), "remote")
        self.assertDecision("ask", self.bash("make deploy-staging; : /*/*/*/*/*/*"), "too large")
        for command in ("git push -o merge_request.title=\"Fix [x]\" origin feature",
                        "git push -o merge_request.description=\"Why?\" origin feature",
                        "git config --get remote.origin.url", "git config -f .git/config --list",
                        "git config get remote.origin.url", "cp %s /tmp/backup" % config):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))
        self.guard_staging(generic_patterns=False)
        self.assertDecision("deny", self.bash("env -S 'make\\_deploy-staging'"), "staging")

    def test_round_eleven_regressions_closed(self):
        lock = "staging-branch-deploy-lock"
        heads = os.path.join(self.remote, "refs", "heads")
        for command in ("find %s -delete" % heads, "find %s/%s -delete" % (heads, lock), "rm -rf %s/*" % heads,
                        "find %s -name packed-refs -delete" % self.remote):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "lock")
        held = os.path.join(self.data, "held.json")
        self.assertDecision("deny", self.bash("git config --file %s --comment -l core.x y" % held), "berth's own")
        config = os.path.join(self.data, "config.json")
        for command in ("grep -E '[0-9]{4}/[0-9]{2}/[0-9]{2}/[a-z]*' log.txt", "find . -path '*/a/*/b/*/c/*' -print",
                        "sed -E 's/a*/b*/;s/c*/d*/' f", "rm -rf docs/refs", "mv refs refs.bak",
                        "rm -rf node_modules/x/refs", "rm -rf build/refs/heads", "touch out/packed-refs",
                        "rm -r test/fixtures/repo.git/refs", "cat %s; vim x" % config,
                        "vim notes.txt && cat %s" % config, "find . -name '*.orig' -delete"):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command, mode="dontAsk"))

    def test_round_twelve_regressions_closed(self):
        config = os.path.join(self.data, "config.json")
        script = "printf 'e %s\\n1c\\n{}\\n.\\nw\\nq\\n'" % config
        for command in ("%s | nice ed -s" % script, "%s | timeout 5 ed -s" % script, "%s | (ed -s)" % script,
                        "%s | { ed -s; }" % script, "%s | ED=1 ed -s" % script, "ed -s < <(%s)" % script,
                        "ed -s <<< \"$(%s)\"" % script, "%s > f && ed -s < f" % script,
                        "git config --file %s a.b 1 --name-only" % config):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command, mode="dontAsk"), "berth's own")
        lock = "staging-branch-deploy-lock"
        parent = os.path.dirname(self.remote)
        for command in ("B=staging-bran C=ch-deploy-lo D=ck env -P /usr/bin -S 'git push origin :${B}${C}${D}'",
                        "git config remote.origin.url /x/evil.git --show-origin",
                        "git config --file .git/config remote.origin.url /x/evil.git -l"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command, mode="dontAsk"))
        for command in ("cd %s && find -H remote.git -path '*heads/*' -delete" % parent,
                        "cd %s && find -L remote.git -path '*heads/*' -type f -exec rm {} +" % parent,
                        "cd %s && find -- remote.git -path '*heads/*' -delete" % parent,
                        "find .. -path '*heads/staging*' -delete",
                        "cd \"$(git remote get-url origin)\" && rm refs/heads/%s" % lock,
                        "cd \"$(git remote get-url origin)\" && rm -rf refs",
                        "cd \"$(git remote get-url origin)\" && rm -f packed-refs",
                        "ln -s %s /tmp/berth-x && rm -rf /tmp/berth-x/refs/heads" % self.remote):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command, mode="dontAsk"), "lock")
        with open(os.path.join(self.project, "deploy-staging.sh"), "w") as fh:
            fh.write("#!/bin/sh\n")
        self.write_config({"generic_patterns": False, "environments": {"staging": {"patterns": [
            STAGING_PATTERN, r"^(sh )?(\S*/)?deploy-staging\.sh(\s|$)"]}}})
        self.assertDecision("deny", self.bash("./deploy-staging.sh", mode="dontAsk"), "staging")
        for command in ("find . -maxdepth 0 -exec ./deploy-stag*.sh \\;",
                        "find . -maxdepth 0 -exec sh deploy-stag*.sh \\;",
                        "gfind . -maxdepth 0 -exec ./deploy-staging.sh \\;", "fd -x ./deploy-staging.sh",
                        "env -P /usr/bin make deploy-staging"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command, mode="dontAsk"), "staging")
        for command in ("cat %s | vim -" % config, "jq . %s | sponge /tmp/out.json" % config,
                        "find .git -name '*.lock' -delete", "find .git -type f -name '*.pack' -exec ls -l {} +",
                        "find . -name '*.py' -exec grep -l refs {} +", "git config --get-all remote.origin.url x",
                        "git config --list"):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command, mode="dontAsk"))

    def test_wrapped_discovered_script_denied(self):
        # Discovery's script pattern has no wrapper prefix: the parser removes wrappers before matching.
        from berth import discover
        os.makedirs(os.path.join(self.project, "scripts"), exist_ok=True)
        with open(os.path.join(self.project, "scripts", "deploy-staging.sh"), "w") as fh:
            fh.write("#!/bin/sh\n")
        found = discover.discover(self.project)
        self.write_config({"generic_patterns": False,
                           "environments": {"staging": {"patterns": found["staging"]["patterns"]}}})
        for command in ("sudo sh scripts/deploy-staging.sh", "nohup bash scripts/deploy-staging.sh",
                        "time ./scripts/deploy-staging.sh", "exec sh scripts/deploy-staging.sh",
                        "sudo -u deployer bash scripts/deploy-staging.sh"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command, mode="dontAsk"), "staging")

    def test_round_thirteen_regressions_closed(self):
        config = os.path.join(self.data, "config.json")
        refs = os.path.join(self.remote, "refs")
        for command in ("find -x %s/heads -name '*' -type f -delete" % refs, "find -E %s -type f -exec rm {} +" % refs,
                        "find -s %s -delete" % refs, "find -Dtree %s -delete" % refs, "find -L -D tree %s -delete" % refs,
                        "find -f %s -delete" % refs, "rm -rf %s/ref[s]" % self.remote, "rm -rf %s/re*" % self.remote,
                        "rm -rf %s" % self.remote, "cd %s && rm -rf .gi?/refs/heads" % self.project):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command, mode="dontAsk"), "lock")
        for command in ("printf -- '--- %s\\n+++ %s\\n' | patch -p0" % (config, config),
                        "printf 'do shell script \"rm %s\"' | osascript" % config,
                        "printf 'call delete(\"%s\")\\nqa!\\n' | vim -Nes" % config):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command, mode="dontAsk"), "berth's own")
        for command in ("git config include.path --get", "git config remote.origin.pushurl --get-urlmatch",
                        "git config include.path -l"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command, mode="dontAsk"), "remote")
        self.guard_staging(generic_patterns=False)
        for command in ("fd -Hx make deploy-staging", "fd -1x make deploy-staging", "fd -xmake deploy-staging",
                        "fd --exec=make deploy-staging", "fd --exec-batch=make deploy-staging",
                        "env --arg x make deploy-staging", "env --ch /tmp make deploy-staging"):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command, mode="dontAsk"), "staging")
        for command in ("find . -name '*.yml' -exec grep -l branch-deploy {} +",
                        "find . -type f -name '*.md' -exec grep -n deploy-lock {} \\;",
                        "find . -name '*.packed.js' -delete", "find dist -name '*-unpacked' -exec rm -rf {} +",
                        "find . -name '*prefs*' -exec cat {} +", "find .git -type f -exec ls -la {} +",
                        "find -x . -name '*.pyc' -delete", "fd -e x -x black {}", "git config --get include.path",
                        "git config --list --show-origin", "vim -R notes.txt"):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command, mode="dontAsk"))

    def test_env_split_string_it_cannot_read_asks(self):
        # A variable, an unclosed quote or an unknown escape in env -S hides the program it runs: ask, never allow.
        hidden = ("env -S 'git push origin ${B}'", 'env -S "git push origin x\\q"', "env -S 'git push \"origin'",
                  "env --split-string='make deploy-${E}'",
                  # A later problem in the same command must not hide the env -S one.
                  "B=staging-branch-deploy-lock env -S 'git push origin HEAD:${B}'; cat u.txt | sh")
        self.write_config({})
        for command in hidden:
            with self.subTest(command=command, environments=False):
                self.assertDecision("ask", self.bash(command), "env a split string")
        self.guard_staging()
        for command in hidden:
            with self.subTest(command=command, environments=True):
                self.assertDecision("ask", self.bash(command), "env a split string")
                self.assertDecision("deny", self.bash(command, mode="dontAsk"))
        self.assertDecision("allow", self.bash("env -S 'echo \\${X} a'"))

    def test_inline_code_checks_only_its_own_program(self):
        for command in ("git ls-remote origin 'refs/heads/*-branch-deploy-lock' | "
                        "python3 -c 'import sys; print(sys.stdin.read())'",
                        "python3 -c 'print(1)' && cat %s" % os.path.join(self.data, "held.json"),
                        "python3 -m json.tool .berth/config.json"):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))

    def test_printf_or_echo_escapes_piped_into_a_shell_ask(self):
        for command in ("printf 'make deploy-%s\\n' staging | sh", "printf 'make deploy-\\163taging\\n' | bash",
                        "echo 'make deploy-\\0163taging' | sh"):
            with self.subTest(command=command):
                self.assertDecision("ask", self.bash(command), "cannot see")
        self.assertDecision("deny", self.bash("printf 'make deploy-staging\\n' | sh"), "staging")

    def test_one_off_push_setting_asks(self):
        self.assertDecision("ask", self.bash("git -c remote.origin.push=:refs/heads/$B push origin"), "one-off")
        self.assertDecision("ask", self.bash("GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.mirror "
                                             "GIT_CONFIG_VALUE_0=true git push origin"), "one-off")
        self.assertDecision("deny", self.bash("git -c remote.origin.push=:refs/heads/staging-branch-deploy-lock "
                                              "push origin"), "lock")

    def test_unknown_text_piped_into_a_shell_asks(self):
        self.assertDecision("ask", self.bash("./print-steps | bash"), "cannot see")
        self.write_config({})
        self.assertDecision("allow", self.bash("./print-steps | bash"))

    def test_heredoc_closed_inside_substitution_does_not_hide_commands(self):
        self.assertDecision("deny", self.bash("x=\"$(cat <<'EOF'\nmsg\nEOF)\" && make deploy-staging"), "staging")
        self.assertDecision("deny", self.bash("x=$(cat <<'EOF'\nmsg\nEOF) ; git push origin :staging-branch-deploy-lock"),
                            "lock")

    def test_ansi_c_quote_does_not_hide_commands(self):
        self.assertDecision("deny", self.bash("echo $'a\\'b'; make deploy-staging"), "staging")
        self.assertDecision("deny", self.bash("echo $'x\\'y'; git push origin :staging-branch-deploy-lock"), "lock")
        self.assertDecision("deny", self.bash("echo $'x\\'y'; rm -rf .berth"), "berth's own")

    def test_find_exec_deploy_detected(self):
        self.assertDecision("deny", self.bash("find . -maxdepth 0 -exec make deploy-staging \\;"), "staging")

    def test_lock_words_in_messages_do_not_block_a_push(self):
        commands = ("git commit -m \"$(cat <<'EOF'\nFix branch-deploy-lock parsing\nEOF\n)\" && git push -u origin HEAD",
                    "gh pr create --body 'fixes deploy-lock race' && git push -u origin HEAD",
                    "git commit -m 'Handle branch-deploy locks' && git push",
                    "git commit -m 'docs: staging-branch-deploy-lock' && git push origin HEAD",
                    "git tag -m 'about staging-branch-deploy-lock' v1",
                    "git tag -am 'Fix staging-branch-deploy-lock parsing' v1.2")
        for command in commands:
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))
        self.write_config({})
        self.assertDecision("allow", self.bash(commands[2]))
        self.guard_staging()
        self.assertDecision("deny", self.bash("git stash branch staging-branch-deploy-lock"), "lock")

    def test_changing_the_remote_asks(self):
        for command in ("git remote set-url origin /tmp/fake.git", "git config url./tmp/fake.git.insteadOf origin"):
            with self.subTest(command=command):
                self.assertDecision("ask", self.bash(command), "where berth reads and writes claims")
        self.assertDecision("ask", self.bash("git config remote.origin.url /tmp/fake.git"), "where berth")
        self.assertDecision("ask", self.bash("git config remote.origin.url get"), "where berth")
        self.assertDecision("ask", self.bash("git --attr-source HEAD remote set-url origin /tmp/fake.git"),
                            "where berth")
        for command in ("git config --get remote.origin.url", "git remote -v", "git config user.name x",
                        "git config remote.origin.url", "git config remote.origin.fetch",
                        "git config get remote.origin.url"):
            with self.subTest(command=command):
                self.assertDecision("allow", self.bash(command))
        config = os.path.join(self.project, ".git", "config")
        self.assertDecision("deny", self.pre("Write", {"file_path": config, "content": "x"}), "berth's own")

    @unittest.skipUnless(sys.platform in ("darwin", "win32"), "case-insensitive file systems only")
    def test_path_in_other_letter_case_denied(self):
        upper = os.path.join(os.path.dirname(self.data), os.path.basename(self.data).upper())
        self.assertDecision("deny", self.pre("Write", {"file_path": os.path.join(upper, "config.json"),
                                                       "content": "{}"}), "berth's own")
        self.assertDecision("deny", self.bash("rm -rf %s" % upper), "berth's own")

    def test_configured_patterns_skip_readers_and_berth(self):
        self.guard_staging(environments={"staging": {"patterns": [r"\bdeploy\b.*\bstaging\b"]}})
        self.assertDecision("allow", self.bash("cat docs/deploy/staging.md"))
        self.assertDecision("allow", self.bash('python3 "%s" --data "%s" claim staging --reason "deploy staging fix"'
                                               % (BERTH, self.data)))
        self.assertDecision("deny", self.bash("./run deploy staging"), "staging")

class OwnTools(Case):
    def setUp(self):
        super().setUp()
        self.guard_staging()

    def call(self, script, sub, data=None):
        return 'python3 "%s" --data "%s" %s' % (script, data or self.data, sub)

    def test_cli_takeover_asks(self):
        self.assertDecision("ask", self.bash(self.call(BERTH, "takeover staging --ref abcdef1 --yes")), "stale lock")

    def test_cli_takeover_unattended_denied(self):
        self.assertDecision("deny", self.bash(self.call(BERTH, "takeover staging --ref abcdef1 --yes"),
                                              mode="bypassPermissions"))

    def test_cli_other_data_folder_denied(self):
        # Another data folder means another user config, where any identity could be written.
        other = os.path.join(self.tmp, "other")
        for command in (self.call(BERTH, "release staging", data=other), self.call(BERTHCFG, "show", data=other),
                        'python3 "%s" release staging' % BERTH, 'python3 "%s" show' % BERTHCFG):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "--data")

    def test_cli_with_exported_variables_denied(self):
        for prefix in ("export CLAUDE_PLUGIN_OPTION_IDENTITY=bob; ", "set -a; ", "declare -x USER=bob && ",
                       "typeset -x GIT_CONFIG_GLOBAL=/tmp/g; "):
            with self.subTest(prefix=prefix):
                self.assertDecision("deny", self.bash(prefix + self.call(BERTH, "release staging")),
                                    "environment variables")

    def test_cli_ignores_identity_variables(self):
        # Only the hooks' own environment carries real plugin options; the CLI reads what the hooks saved.
        self.put_lock("staging", "bob")
        res = self.cli("release", "staging", extra_env={"CLAUDE_PLUGIN_OPTION_IDENTITY": "bob"})
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("refs/heads/staging-branch-deploy-lock", self.remote_refs())

    def test_cli_uses_plugin_options_saved_by_hooks(self):
        option = {"CLAUDE_PLUGIN_OPTION_IDENTITY": "alice-gh"}
        self.assertDecision("allow", self.bash("git status", extra_env=option))
        res = self.cli("claim", "staging", "--hours", "1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("alice-gh", res.stdout)
        self.assertDecision("allow", self.bash("make deploy-staging", extra_env=option))

    def test_doctor_keeps_saved_plugin_options(self):
        option = {"CLAUDE_PLUGIN_OPTION_IDENTITY": "alice-gh"}
        self.assertDecision("allow", self.bash("git status", extra_env=option))
        path = os.path.join(self.data, "plugin-options.json")
        with open(path) as fh:
            before = fh.read()
        res = self.run_script(BERTHCFG, "doctor")
        self.assertIn("hook runs", res.stdout)
        with open(path) as fh:
            self.assertEqual(fh.read(), before)

    def test_git_name_change_does_not_change_owner(self):
        # The name berth first found is kept; a later git config user.name does not make bob's lock yours.
        self.assertEqual(self.cli("status").returncode, 0)
        self.put_lock("staging", "bob")
        self.git("config", "user.name", "bob")
        self.assertDecision("deny", self.bash("make deploy-staging"), "held by bob")
        res = self.cli("release", "staging")
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("refs/heads/staging-branch-deploy-lock", self.remote_refs())

    def test_cli_env_prefix_denied(self):
        for command in ("CLAUDE_PLUGIN_OPTION_IDENTITY=bob " + self.call(BERTH, "claim staging"),
                        "env CLAUDE_PLUGIN_OPTION_IDENTITY=bob " + self.call(BERTH, "release staging")):
            with self.subTest(command=command):
                self.assertDecision("deny", self.bash(command), "environment variables")

    def test_cli_started_other_ways_still_checked(self):
        for command in ('python3 -X utf8 "%s" --data "%s" takeover staging --ref abcdef1' % (BERTH, self.data),
                        'python3 -W ignore -- "%s" --data "%s" takeover staging --ref abcdef1' % (BERTH, self.data),
                        'uv run "%s" --data "%s" takeover staging --ref abcdef1' % (BERTH, self.data),
                        'python3 "%s" --data "%s" set identity bob' % (BERTHCFG.replace("berthcfg.py", "Berthcfg.py"),
                                                                        self.data)):
            with self.subTest(command=command):
                self.assertDecision("ask", self.bash(command))

    def test_cli_claim_status_release_allowed(self):
        for sub in ("status", "claim staging --hours 1", "extend staging", "release staging"):
            with self.subTest(sub=sub):
                self.assertDecision("allow", self.bash('python3 "%s" --data "%s" %s' % (BERTH, self.data, sub)))

    def test_cfg_set_asks(self):
        for sub in ("set max_claim_hours 1", "unset preset", "approve store", "init --yes"):
            with self.subTest(sub=sub):
                self.assertDecision("ask", self.bash(self.call(BERTHCFG, sub)), "settings")

    def test_cfg_show_allowed(self):
        for sub in ("show", "explain stop", "detect", "doctor", "init"):
            with self.subTest(sub=sub):
                self.assertDecision("allow", self.bash(self.call(BERTHCFG, sub)))

    def test_claim_call_marks_session(self):
        self.bash(self.call(BERTH, "claim staging --hours 1"), session="abc")
        from berth import held

        self.assertIn("staging", held.load(self.data)["sessions"]["abc"]["envs"])


class Timing(Case):
    def test_hook_under_100ms(self):
        self.guard_staging()
        payload = '{"tool_name":"Bash","tool_input":{"command":"git status"},"cwd":"%s","session_id":"t"}' % \
            self.project
        import subprocess

        best = None
        for _ in range(5):
            start = time.time()
            subprocess.run([sys.executable, HOOK, "pre"], input=payload, env=self.env, stdout=subprocess.PIPE,
                           universal_newlines=True, check=True)
            took = (time.time() - start) * 1000
            best = took if best is None else min(best, took)
        self.assertLess(best, 100, "best of 5 runs took %.0f ms" % best)


class KnownMisses(Case):
    """Documented in docs/limits.md. Each would need the shell's state or file contents the hook cannot see."""

    def setUp(self):
        super().setUp()
        self.guard_staging()

    @unittest.expectedFailure
    def test_deploy_via_script_written_then_run(self):
        self.assertDecision("deny", self.bash("printf 'make deploy-staging' > go.sh && bash go.sh"))

    def test_copy_of_berth_cli_takeover(self):
        # A copy of berth's scripts under another name is not berth's CLI, but it is given berth's data folder,
        # which an unknown program asks about.
        self.assertDecision("ask", self.bash('cp -r "%s" /tmp/x && python3 /tmp/x/b.py --data "%s" takeover staging '
                                             '--ref abcdef1 --yes' % (os.path.dirname(BERTH), self.data)))

    @unittest.expectedFailure
    def test_alias_deploy(self):
        # An alias or function from the user's shell profile that runs a deploy under another name.
        self.assertDecision("deny", self.bash("dstg"))


if __name__ == "__main__":
    unittest.main()
