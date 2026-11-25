"""Config layering: the project file can only tighten; values are validated."""

import os
import unittest

from helpers import STAGING_PATTERN, Case, cfg, fake


class Layering(Case):
    def resolve(self):
        return cfg.resolve(self.data, self.project)

    def test_project_cannot_loosen_unclaimed(self):
        self.write_config({"unclaimed": "deny"})
        self.write_config({"unclaimed": "ask"}, where="project")
        eff = self.resolve()
        self.assertEqual(eff.get("unclaimed"), "deny")
        self.assertTrue(any("looser" in n for n in eff.notes))

    def test_project_can_tighten(self):
        self.write_config({"unclaimed": "ask", "stop": "remind"})
        self.write_config({"unclaimed": "deny", "stop": "block", "fail_mode": "deny"}, where="project")
        eff = self.resolve()
        self.assertEqual((eff.get("unclaimed"), eff.get("stop"), eff.get("fail_mode")), ("deny", "block", "deny"))

    def test_project_cannot_raise_cap(self):
        self.write_config({"max_claim_hours": 2})
        self.write_config({"max_claim_hours": 8}, where="project")
        self.assertEqual(self.resolve().get("max_claim_hours"), 2.0)
        self.write_config({"max_claim_hours": 0.5}, where="project")
        self.assertEqual(self.resolve().get("max_claim_hours"), 0.5)

    def test_project_adds_env_patterns(self):
        self.write_config({"environments": {"staging": {"patterns": [STAGING_PATTERN]}}})
        self.write_config({"environments": {"staging": {"patterns": ["^cap staging"]},
                                            "production": {"patterns": []}}}, where="project")
        envs = self.resolve().get("environments")
        self.assertEqual(envs["staging"]["patterns"], [STAGING_PATTERN, "^cap staging"])
        self.assertIn("production", envs)

    def test_project_store_needs_approval(self):
        from berth import approve

        local = {"type": "local", "path": os.path.join(self.tmp, "claims")}
        self.write_config({"store": local}, where="project")
        eff = self.resolve()
        self.assertEqual(eff.get("store")["type"], "git")
        self.assertIn("store", eff.pending_approval)
        approve.grant(self.data, self.project, "store", local)
        self.assertEqual(self.resolve().get("store"), local)
        local2 = dict(local, path=os.path.join(self.tmp, "other"))
        self.write_config({"store": local2}, where="project")
        self.assertEqual(self.resolve().get("store")["type"], "git", "a changed value needs a new approval")

    def test_project_identity_ignored(self):
        self.write_config({"identity": "mallory"}, where="project")
        eff = self.resolve()
        self.assertIsNone(eff.get("identity"))
        self.assertTrue(any("identity" in n for n in eff.notes))

    def test_plugin_options_apply(self):
        os.environ["CLAUDE_PLUGIN_OPTION_MAX_CLAIM_HOURS"] = "1.5"
        os.environ["CLAUDE_PLUGIN_OPTION_PRESET"] = "strict"
        eff = cfg.resolve(self.data, self.project, cfg.env_options())
        self.assertEqual((eff.get("max_claim_hours"), eff.get("stop")), (1.5, "block"))

    def test_cli_reads_saved_options_not_variables(self):
        os.environ["CLAUDE_PLUGIN_OPTION_IDENTITY"] = "bob"
        self.assertIsNone(self.resolve().get("identity"))
        cfg.save_options(self.data, {"identity": "alice-gh"})
        self.assertEqual(self.resolve().get("identity"), "alice-gh")

    def test_project_can_only_raise_stale_after_hours(self):
        self.write_config({"stale_after_hours": 0.001}, where="project")
        eff = self.resolve()
        self.assertEqual(eff.get("stale_after_hours"), 24.0)
        self.assertTrue(any("stale_after_hours" in n for n in eff.notes))
        self.write_config({"stale_after_hours": 72}, where="project")
        self.assertEqual(self.resolve().get("stale_after_hours"), 72.0)

    def test_find_project_walks_up(self):
        sub = os.path.join(self.project, "a", "b")
        os.makedirs(sub)
        self.assertEqual(cfg.find_project(sub), self.project)
        os.makedirs(os.path.join(self.project, "a", ".berth"))
        self.assertEqual(cfg.find_project(sub), self.project, "an empty .berth folder is not a project")
        with open(os.path.join(self.project, "a", ".berth", "config.json"), "w") as fh:
            fh.write("{}")
        self.assertEqual(cfg.find_project(sub), os.path.join(self.project, "a"))


class Validation(Case):
    def check_rejected(self, data, text):
        self.write_config(data)
        with self.assertRaises(cfg.ConfigError) as ctx:
            cfg.resolve(self.data, self.project)
        self.assertIn(text, str(ctx.exception))

    def test_exponential_pattern_rejected(self):
        for pat in (r"^make (x+x+)+y", r"(a*)*b", r"(\w+\s?)+$", r"^make (x|x)+y", r"(x?x)+y", r"(.*a){20}"):
            with self.subTest(pattern=pat):
                self.check_rejected({"environments": {"qa": {"patterns": [pat]}}}, "repeats a group")
        for pat in (r"^make (.* )?deploy-staging(\s|$)", r"deploy\s+--env[= ]staging", r"(-[a-z] )*deploy"):
            with self.subTest(pattern=pat):
                self.assertFalse(cfg.nested_repeat(pat), pat)

    def test_invalid_regex_rejected(self):
        self.check_rejected({"environments": {"staging": {"patterns": ["deploy("]}}}, "regex")

    def test_bad_environment_name_rejected(self):
        self.check_rejected({"environments": {"../x": {"patterns": []}}}, "name")
        self.check_rejected({"environments": {"x-branch-deploy-lock": {"patterns": []}}}, "branch-deploy-lock")

    def test_unknown_key_hint(self):
        self.check_rejected({"max_claim_hour": 2}, "max_claim_hours")

    def test_cap_out_of_range_rejected(self):
        self.check_rejected({"max_claim_hours": 100}, "max_claim_hours")

    def test_secret_value_rejected(self):
        self.check_rejected({"identity": fake("github")}, "secret")

    def test_newer_version_refused(self):
        self.check_rejected({"version": 2}, "version")

    def test_bad_remote_name_rejected(self):
        self.check_rejected({"store": {"type": "git", "remote": "--upload-pack=x"}}, "remote")


if __name__ == "__main__":
    unittest.main()
