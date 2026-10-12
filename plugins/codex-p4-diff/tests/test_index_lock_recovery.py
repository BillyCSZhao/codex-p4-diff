from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "codex-p4-diff" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import codex_p4_audit as audit


@unittest.skipUnless(shutil.which("git"), "requires Git")
class IndexLockRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-index-lock-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        project = root / "project"
        project.mkdir()
        self.ctx = audit.AuditContext({}, project, "test", "codex/local-history", project,
                                      root / "state", root / "state" / "session", shutil.which("git"))
        self.ctx.session_root.mkdir(parents=True)
        subprocess.run([self.ctx.git_exe, "init", "-q", str(project)], check=True, capture_output=True)
        self.lock = project / ".git" / "index.lock"
        self.owner_path = self.ctx.state_root / "index-lock-owner.json"
        self.blob = subprocess.run(
            [self.ctx.git_exe, "-C", str(project), "hash-object", "-w", "--stdin"],
            input=b"codex\n", check=True, capture_output=True,
        ).stdout.decode("ascii").strip()
        self.wait_patch = mock.patch.object(audit, "PROJECT_LOCK_WAIT_SECONDS", 0.08)
        self.wait_patch.start()
        self.addCleanup(self.wait_patch.stop)

    def owner(self, pid: int) -> dict:
        return {
            "schemaVersion": 1, "token": "a" * 32, "pid": pid,
            "historyRoot": os.path.normcase(str(self.ctx.history_root.resolve())),
            "lockPath": os.path.normcase(str(self.lock.resolve())),
            "identity": audit.index_lock_identity(self.lock),
        }

    def sync(self) -> list[str]:
        return audit.sync_main_index_safely(self.ctx, {"A.txt": self.blob}, {"A.txt": None})

    def test_unknown_git_lock_is_never_reclaimed(self) -> None:
        self.lock.write_bytes(b"foreign-lock")
        with self.assertRaisesRegex(audit.AuditError, "index"):
            self.sync()
        self.assertEqual(self.lock.read_bytes(), b"foreign-lock")
        self.assertFalse(self.owner_path.exists())

    def test_live_or_reused_pid_is_never_reclaimed(self) -> None:
        self.lock.touch()
        audit.atomic_write_json(self.owner_path, self.owner(os.getpid()))
        with self.assertRaisesRegex(audit.AuditError, "index"):
            self.sync()
        self.assertTrue(self.lock.exists())
        self.assertTrue(self.owner_path.exists())
        self.assertFalse(audit.index_lock_owner_exited(os.getpid()))
        self.assertFalse(audit.index_lock_owner_exited(1 << 65))

    def test_dead_plugin_owner_recovers_after_hard_exit(self) -> None:
        code = r'''
import os, pathlib, sys
from unittest import mock
sys.path.insert(0, sys.argv[1])
import codex_p4_audit as audit
project, state = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
ctx = audit.AuditContext({}, project, "child", "codex/local-history", project,
                         state, state / "child", sys.argv[4])
with mock.patch.object(audit, "index_entries", side_effect=lambda *a, **k: os._exit(17)):
    audit.sync_main_index_safely(ctx, {"A.txt": "0" * 40}, {"A.txt": None})
'''
        child = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPTS),
                                str(self.ctx.history_root), str(self.ctx.state_root), self.ctx.git_exe],
                               capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 17, child.stderr.decode(errors="replace"))
        self.assertEqual(self.lock.read_bytes(), b"")
        record = json.loads(self.owner_path.read_text(encoding="utf-8"))
        self.assertTrue(audit.index_lock_owner_exited(record["pid"]))
        self.assertEqual(self.sync(), [])
        self.assertEqual(audit.index_entries(self.ctx, ["A.txt"])["A.txt"], self.blob)
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.owner_path.exists())

    def test_owner_and_file_identity_must_match(self) -> None:
        self.lock.touch()
        owner = self.owner(123456)
        mismatches = {
            "historyRoot": "another-repository", "lockPath": "another-index.lock",
            "identity": {"device": 0, "inode": 0, "ctimeNs": 0},
            "token": "not-a-token", "pid": True,
        }
        for field, value in mismatches.items():
            with self.subTest(field=field):
                audit.atomic_write_json(self.owner_path, {**owner, field: value})
                with mock.patch.object(audit, "index_lock_owner_exited", return_value=True):
                    with audit.project_lock(self.ctx.state_root, lock_name=".index-owner.lock"):
                        self.assertFalse(audit.recover_owned_index_lock(self.ctx, self.lock, self.owner_path))
                self.assertTrue(self.lock.exists())

    def test_live_plugin_lease_excludes_another_writer(self) -> None:
        ready = Path(self.temp.name) / "ready"
        code = r'''
import os, pathlib, sys
from unittest import mock
sys.path.insert(0, sys.argv[1])
import codex_p4_audit as audit
project, state = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
ctx = audit.AuditContext({}, project, "child", "codex/local-history", project,
                         state, state / "child", sys.argv[4])
def hold(*args, **kwargs):
    pathlib.Path(sys.argv[5]).touch()
    sys.stdin.read(1)
    os._exit(17)
with mock.patch.object(audit, "index_entries", side_effect=hold):
    audit.sync_main_index_safely(ctx, {"A.txt": "0" * 40}, {"A.txt": None})
'''
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(SCRIPTS),
                                  str(self.ctx.history_root), str(self.ctx.state_root), self.ctx.git_exe,
                                  str(ready)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            with self.assertRaises(audit.ProjectLockTimeout):
                self.sync()
            self.assertEqual(self.lock.read_bytes(), b"")
            self.assertFalse(audit.index_lock_owner_exited(child.pid))
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.assertEqual(self.sync(), [])

    def test_lock_replaced_during_owner_query_is_preserved(self) -> None:
        self.lock.touch()
        audit.atomic_write_json(self.owner_path, self.owner(123456))

        def replace_lock(pid: int) -> bool:
            self.lock.rename(self.lock.with_name("retired.lock"))
            self.lock.write_bytes(b"new-writer")
            return True

        with mock.patch.object(audit, "index_lock_owner_exited", side_effect=replace_lock):
            with audit.project_lock(self.ctx.state_root, lock_name=".index-owner.lock"):
                self.assertFalse(audit.recover_owned_index_lock(self.ctx, self.lock, self.owner_path))
        self.assertEqual(self.lock.read_bytes(), b"new-writer")

    def test_exception_cleans_only_this_acquisition(self) -> None:
        with mock.patch.object(audit, "index_entries", side_effect=OSError("injected failure")):
            with self.assertRaisesRegex(OSError, "injected failure"):
                self.sync()
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.owner_path.exists())
        self.assertEqual(self.sync(), [])

    def test_git_writer_is_excluded_while_index_is_owned(self) -> None:
        original = audit.index_entries
        raced = []

        def while_locked(ctx, paths, env=None):
            self.assertEqual(self.lock.read_bytes(), b"")
            self.assertTrue(self.owner_path.exists())
            attempted = subprocess.run(
                [ctx.git_exe, "-C", str(ctx.history_root), "update-index", "--add", "--cacheinfo",
                 "100644", self.blob, "Other.txt"], capture_output=True,
            )
            raced.append(attempted.returncode)
            return original(ctx, paths, env)

        with mock.patch.object(audit, "index_entries", side_effect=while_locked):
            self.assertEqual(self.sync(), [])
        self.assertEqual(len(raced), 1)
        self.assertNotEqual(raced[0], 0)
        self.assertIsNone(audit.index_entries(self.ctx, ["Other.txt"])["Other.txt"])


if __name__ == "__main__":
    unittest.main()
