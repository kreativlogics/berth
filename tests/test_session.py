"""Stop, SessionStart and SessionEnd: reminders and records of held claims."""

import datetime
import json
import os
import unittest

from helpers import BERTH, Case, locks


class Sessions(Case):
    def setUp(self):
        super().setUp()
        self.guard_staging()

    def claim_in_session(self, sid="s1", hours="1"):
        self.bash('python3 "%s" --data "%s" claim staging --hours %s' % (BERTH, self.data, hours), session=sid)
        res = self.cli("claim", "staging", "--hours", hours)
        self.assertEqual(res.returncode, 0, res.stderr)

    def stop(self, sid="s1", active=False):
        return self.hook("stop", {"session_id": sid, "cwd": self.project, "stop_hook_active": active,
                                  "hook_event_name": "Stop"})

    def test_stop_remind_message_when_held(self):
        self.claim_in_session()
        out = self.stop()
        self.assertIn("you still hold staging", out["systemMessage"])
        self.assertNotIn("decision", out)
        self.assertIsNone(self.stop(), "reminders are rate limited")

    def test_claim_from_subfolder_tracked(self):
        # Bash commands get no CLAUDE_PROJECT_DIR: the CLI finds the project root itself.
        sub = os.path.join(self.project, "app")
        os.makedirs(sub)
        self.bash('python3 "%s" --data "%s" claim staging --hours 1' % (BERTH, self.data), session="s1")
        res = self.cli("claim", "staging", "--hours", "1", cwd=sub, drop_env=("CLAUDE_PROJECT_DIR",))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("you still hold staging", self.stop()["systemMessage"])

    def test_cli_from_subfolder_reads_project_config(self):
        self.write_config({})
        self.write_config({"environments": {"qa": {"patterns": []}}}, where="project")
        sub = os.path.join(self.project, "app")
        os.makedirs(sub)
        res = self.cli("claim", "qa", "--hours", "1", cwd=sub, drop_env=("CLAUDE_PROJECT_DIR",))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_stop_silent_for_other_sessions(self):
        self.claim_in_session("s1")
        self.assertIsNone(self.stop("s2"))

    def test_stop_silent_after_release(self):
        self.claim_in_session()
        self.cli("release", "staging")
        self.assertIsNone(self.stop())

    def test_stop_block_once_per_claim(self):
        self.guard_staging(stop="block")
        self.claim_in_session()
        out = self.stop()
        self.assertEqual(out["decision"], "block")
        self.assertIn("still holds the deploy claim on staging", out["reason"])
        again = self.stop()
        self.assertNotIn("decision", again or {})

    def test_stop_hook_active_no_block(self):
        self.guard_staging(stop="block")
        self.claim_in_session()
        self.assertIsNone(self.stop(active=True))

    def test_stop_off_silent(self):
        self.write_config({"stop": "off", "environments": {"staging": {"patterns": []}}})
        self.claim_in_session()
        self.assertIsNone(self.stop())

    def test_session_end_records_held(self):
        self.claim_in_session()
        self.assertIsNone(self.hook("session-end", {"session_id": "s1", "cwd": self.project, "reason": "logout"}))
        with open(os.path.join(self.data, "held.json")) as fh:
            left = json.load(fh)["left"]
        self.assertEqual([x["env"] for x in left], ["staging"])

    def test_session_start_reports_held_and_expired(self):
        self.claim_in_session()
        self.hook("session-end", {"session_id": "s1", "cwd": self.project, "reason": "other"})
        path = os.path.join(self.data, "held.json")
        with open(path) as fh:
            state = json.load(fh)
        past = locks.iso(locks.utcnow() - datetime.timedelta(minutes=5))
        state["claims"]["x|%s|qa-branch-deploy-lock" % os.path.realpath(self.project)] = {
            "env": "qa", "expires_at": past, "project": os.path.realpath(self.project), "store": "git remote origin"}
        with open(path, "w") as fh:
            json.dump(state, fh)
        out = self.hook("session-start", {"session_id": "s2", "cwd": self.project, "source": "startup"})
        text = out["systemMessage"]
        self.assertIn("you hold staging (until", text)
        self.assertIn("you hold qa (expired", text)
        self.assertIn("a session that ended still held staging", text)
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], text)
        again = self.hook("session-start", {"session_id": "s3", "cwd": self.project, "source": "startup"})
        self.assertNotIn("session that ended", again["systemMessage"])

    def test_session_start_reports_git_name_change(self):
        self.assertEqual(self.cli("status").returncode, 0)
        self.git("config", "user.name", "alice2")
        out = self.hook("session-start", {"session_id": "s9", "cwd": self.project, "source": "startup"})
        self.assertIn("still writes your locks as 'alice'", out["systemMessage"])

    def test_session_start_quiet_without_claims(self):
        self.assertIsNone(self.hook("session-start", {"session_id": "s1", "cwd": self.project}))


if __name__ == "__main__":
    unittest.main()
