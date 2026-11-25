"""Claims against a local bare git remote and a local folder: create, extend, release, takeover, races."""

import datetime
import json
import os
import unittest
from unittest import mock

from helpers import Case, claims, locks, store


class StoreContract(object):
    """The same claim semantics for every store. Subclasses set up self.guard_staging(...)."""

    def test_claim_creates_branch_deploy_lock(self):
        msg = self.service().claim("staging", 1, "ship 1.2")
        self.assertIn("Claimed staging", msg)
        lock = self.service().read(["staging"])[0]["staging"]
        self.assertTrue(locks.is_valid(lock.data))
        self.assertEqual(lock.holder, "alice")
        self.assertEqual(lock.data["environment"], "staging")
        self.assertIs(lock.data["sticky"], True)
        self.assertIs(lock.data["global"], False)
        self.assertEqual(lock.data["reason"], "ship 1.2")
        self.assertEqual(lock.data["created_at"][-1], "Z")
        self.assertEqual(lock.expires_at - lock.created_at, datetime.timedelta(hours=1))

    def test_claim_when_held_by_other_refused(self):
        self.service("bob").claim("staging", 1)
        with self.assertRaisesRegex(claims.ClaimError, "held by bob"):
            self.service().claim("staging", 1)

    def test_claim_race_loser_told(self):
        # The loser's first read sees no lock; bob claims before the loser writes.
        loser = self.service()
        real_read = loser.store.read
        calls = []

        def stale_first_read(branches):
            calls.append(branches)
            if len(calls) == 1:
                self.service("bob").claim("staging", 1)
                return {}
            return real_read(branches)

        with mock.patch.object(loser.store, "read", side_effect=stale_first_read):
            with self.assertRaisesRegex(claims.ClaimError, "Someone claimed staging first.*bob"):
                loser.claim("staging", 1)
        self.assertEqual(self.service().read(["staging"])[0]["staging"].holder, "bob")

    def test_claim_over_cap_refused(self):
        with self.assertRaisesRegex(claims.ClaimError, "longest claim"):
            self.service().claim("staging", 5)

    def test_claim_unguarded_env_refused(self):
        with self.assertRaisesRegex(claims.ClaimError, "not a guarded environment"):
            self.service().claim("production", 1)

    def test_extend_fast_forward(self):
        svc = self.service()
        svc.claim("staging", 0.5)
        before = svc.read(["staging"])[0]["staging"]
        svc.extend("staging", 2)
        after = svc.read(["staging"])[0]["staging"]
        self.assertNotEqual(before.ref, after.ref)
        self.assertGreater(after.expires_at, before.expires_at)
        self.assertEqual(after.data["created_at"], before.data["created_at"])
        self.check_extended_from(before, after)

    def check_extended_from(self, before, after):
        pass

    def test_extend_over_cap_refused(self):
        svc = self.service()
        svc.claim("staging", 1)
        with self.assertRaisesRegex(claims.ClaimError, "longest claim"):
            svc.extend("staging", 3)

    def test_extend_foreign_refused(self):
        self.service("bob").claim("staging", 1)
        with self.assertRaisesRegex(claims.ClaimError, "not yours"):
            self.service().extend("staging", 1)

    def test_release_own_deletes_lock(self):
        svc = self.service()
        svc.claim("staging", 1)
        self.assertEqual(svc.release("staging"), "Released staging.")
        self.assertIsNone(svc.read(["staging"])[0]["staging"])

    def test_release_foreign_refused(self):
        self.service("bob").claim("staging", 1)
        with self.assertRaisesRegex(claims.ClaimError, "never removes someone else's lock"):
            self.service().release("staging")
        self.assertIsNotNone(self.service().read(["staging"])[0]["staging"])

    def test_release_after_ref_moved_refused(self):
        svc = self.service()
        svc.claim("staging", 1)
        old = svc.read(["staging"])[0]["staging"]
        svc.extend("staging", 1)
        with self.assertRaises(store.Conflict):
            svc.store.delete(old)
        self.assertIsNotNone(svc.read(["staging"])[0]["staging"])

    def test_identity_match_ignores_case(self):
        self.service("Alice").claim("staging", 1)
        self.service("alice").release("staging")

    def test_takeover_active_refused(self):
        self.service("bob").claim("staging", 1)
        lock = self.service().read(["staging"])[0]["staging"]
        with self.assertRaisesRegex(claims.ClaimError, "actively held"):
            self.service().takeover("staging", lock.ref[:10])

    def test_takeover_stale_replaces_with_confirm_sha(self):
        later = locks.utcnow() + datetime.timedelta(hours=3)
        self.service("bob").claim("staging", 1)
        svc = self.service(now=later)
        lock = svc.read(["staging"])[0]["staging"]
        self.assertEqual(lock.state("alice", 24, later), locks.OTHER_STALE)
        msg = svc.takeover("staging", lock.ref[:10], 1)
        self.assertIn("Removed the stale lock", msg)
        new = svc.read(["staging"])[0]["staging"]
        self.assertEqual(new.holder, "alice")
        self.assertIn("took over from bob", new.data["reason"])

    def test_takeover_wrong_sha_refused(self):
        later = locks.utcnow() + datetime.timedelta(hours=3)
        self.service("bob").claim("staging", 1)
        with self.assertRaisesRegex(claims.ClaimError, "Run status"):
            self.service(now=later).takeover("staging", "0000000")
        self.assertEqual(self.service().read(["staging"])[0]["staging"].holder, "bob")

    def test_status_lists_states(self):
        self.service("bob").claim("staging", 1)
        rows, glob = self.service().status()
        self.assertEqual([(r["env"], r["state"]) for r in rows], [("staging", locks.OTHER)])
        self.assertIsNone(glob)

    def test_lock_text_sanitised(self):
        self.service("bob\x1b[31m").claim("staging", 1, "line\nbreak")
        text = self.service().status()[0][0]["text"]
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\n", text)


class GitStoreTests(StoreContract, Case):
    def setUp(self):
        super().setUp()
        self.guard_staging()

    def check_extended_from(self, before, after):
        parent = self.git("rev-parse", "%s^" % after.ref, cwd=self.remote).stdout.strip()
        self.assertEqual(parent, before.ref)

    def test_lock_commit_matches_upstream_format(self):
        self.service().claim("staging", 1)
        refs = self.remote_refs()
        sha = refs["refs/heads/staging-branch-deploy-lock"]
        self.assertEqual(self.git("log", "-1", "--format=%s", sha, cwd=self.remote).stdout.strip(),
                         "lock [skip ci]")
        self.assertEqual(self.git("ls-tree", "--name-only", sha, cwd=self.remote).stdout.split(), ["lock.json"])
        raw = self.git("cat-file", "blob", "%s:lock.json" % sha, cwd=self.remote).stdout
        self.assertFalse(raw.endswith("\n"))
        self.assertEqual(json.loads(raw)["schema_version"], 1)

    def test_reads_upstream_lock_without_expiry(self):
        # branch-deploy builds its lock commit on the default branch's tree: other files sit beside lock.json.
        with open(os.path.join(self.project, "README"), "w") as fh:
            fh.write("x")
        self.git("add", "README")
        self.git("commit", "-q", "-m", "readme")
        record = locks.new_record("staging", "bob", "upstream", "main", locks.utcnow(),
                                  locks.utcnow() - datetime.timedelta(hours=30))
        for key in ("expires_at", "berth", "schema_version"):
            record.pop(key)
        blob = self.git("hash-object", "-w", "--stdin", stdin=locks.dumps(record)).stdout.strip()
        self.git("read-tree", "HEAD")
        self.git("update-index", "--add", "--cacheinfo", "100644,%s,lock.json" % blob)
        tree = self.git("write-tree").stdout.strip()
        commit = self.git("commit-tree", tree, "-p", "HEAD", "-m", "lock [skip ci]").stdout.strip()
        self.git("push", "-q", "origin", "%s:refs/heads/staging-branch-deploy-lock" % commit)
        self.git("read-tree", "HEAD")
        lock = self.service().read(["staging"])[0]["staging"]
        self.assertEqual(lock.holder, "bob")
        self.assertIsNone(lock.expires_at)
        self.assertEqual(lock.state("alice", 24), locks.OTHER_STALE)
        self.assertEqual(lock.state("alice", 48), locks.OTHER)
        self.assertIn("no expiry", lock.describe())

    def test_never_plain_force_push(self):
        pushes = []
        real = store.GitStore._git

        def spy(self_, args, **kw):
            if args and args[0] == "push":
                pushes.append(list(args))
            return real(self_, args, **kw)

        later = locks.utcnow() + datetime.timedelta(hours=3)
        with mock.patch.object(store.GitStore, "_git", spy):
            svc = self.service()
            svc.claim("staging", 1)
            svc.extend("staging", 1)
            svc.release("staging")
            self.service("bob").claim("staging", 1)
            stale = self.service(now=later)
            stale.takeover("staging", stale.read(["staging"])[0]["staging"].ref[:9], 1)
        self.assertGreaterEqual(len(pushes), 6)
        for args in pushes:
            for a in args:
                self.assertNotIn(a, ("-f", "--force", "--mirror", "--prune", "--all", "--tags"), args)
                if a.startswith("--force"):
                    self.assertTrue(a.startswith("--force-with-lease=refs/heads/"), args)
                self.assertFalse(a.startswith("+"), args)
            refspec = args[-1]
            self.assertTrue(refspec.split(":")[-1].endswith("-branch-deploy-lock"), args)
            if refspec.startswith(":"):
                self.assertTrue(any(a.startswith("--force-with-lease=") for a in args), "delete without lease")

    def test_git_gets_a_minimal_environment(self):
        seen = {}
        real_run = store.subprocess.run

        def spy(argv, **kw):
            seen.update(kw.get("env") or {})
            return real_run(argv, **kw)

        extra = {"UNRELATED_SETTING": "1", "SSH_AUTH_SOCK": "/tmp/agent.sock"}
        with mock.patch.dict(os.environ, extra), mock.patch.object(store.subprocess, "run", spy):
            store.GitStore(self.project).check()
        self.assertNotIn("UNRELATED_SETTING", seen)
        self.assertEqual(seen["SSH_AUTH_SOCK"], "/tmp/agent.sock")
        self.assertEqual(seen["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(seen["HOME"], self.home)

    def test_takeover_unreadable_lock_refused(self):
        self.put_lock("staging", "bob", extra={"schema_version": 2})
        svc = self.service()
        lock = svc.read(["staging"])[0]["staging"]
        self.assertEqual(lock.state("alice", 24), locks.AMBIGUOUS)
        with self.assertRaisesRegex(claims.ClaimError, "cannot tell whether it is stale"):
            svc.takeover("staging", lock.ref[:9], 1)
        self.assertIn("refs/heads/staging-branch-deploy-lock", self.remote_refs())

    def test_fetch_flag_only_on_git_that_has_it(self):
        # --no-write-fetch-head arrived in git 2.29; older git would reject every lock read.
        self.put_lock("staging", "bob")
        for version, expect in (((2, 25, 1), False), ((2, 29, 0), True)):
            fetches = []
            real = store.GitStore._git

            def spy(self_, args, **kw):
                if args and args[0] == "fetch":
                    fetches.append(list(args))
                return real(self_, args, **kw)

            with self.subTest(version=version), mock.patch.object(store, "git_version", lambda: version), \
                    mock.patch.object(store.GitStore, "_git", spy):
                self.assertEqual(self.service().read(["staging"])[0]["staging"].holder, "bob")
                self.assertEqual(any("--no-write-fetch-head" in f for f in fetches), expect, fetches)

    def test_unknown_remote_reported(self):
        self.guard_staging(store={"type": "git", "remote": "upstream"})
        with self.assertRaisesRegex(store.StoreError, "not configured"):
            self.service().claim("staging", 1)


    def test_hook_budget_ends_slow_reads(self):
        # Every git call in a hook shares one deadline, so the total stays under the hook's timeout.
        store.set_budget(0.1)
        try:
            with self.assertRaises(store.StoreError):
                store.GitStore(self.project, "origin").read(["staging-branch-deploy-lock"])
        finally:
            store.set_budget(None)

class LocalStoreTests(StoreContract, Case):
    def setUp(self):
        super().setUp()
        self.guard_staging(store={"type": "local", "path": os.path.join(self.tmp, "claims")})

    def test_files_named_like_lock_branches(self):
        self.service().claim("staging", 1)
        self.assertEqual(sorted(os.listdir(os.path.join(self.tmp, "claims"))),
                         [".berth-store.lock", "staging-branch-deploy-lock.json"])


if __name__ == "__main__":
    unittest.main()
