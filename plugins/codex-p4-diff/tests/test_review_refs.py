from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "codex-p4-diff" / "scripts"))

import codex_p4_audit as audit
import codex_p4_diff as bridge
import codex_p4_review as review


@unittest.skipUnless(shutil.which("git"), "requires Git")
class ReviewRefsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.ctx = SimpleNamespace(
            git_exe=shutil.which("git"),
            history_root=self.repo,
            session_root=self.root / "state",
            session_id="session-123",
        )
        self.git("init", "--quiet")
        self.git("config", "user.name", "Review Test")
        self.git("config", "user.email", "review@example.invalid")
        self.git("config", "core.autocrlf", "false")
        (self.repo / "keep.txt").write_bytes(b"unrelated candidate content\n")
        (self.repo / "edit.txt").write_bytes(b"stale Git history\n")
        self.git("add", "--", "keep.txt", "edit.txt")
        self.git("commit", "--quiet", "-m", "initial")
        self.initial = self.git("rev-parse", "HEAD").decode().strip()

    def git(self, *args: str) -> bytes:
        return subprocess.run(
            [str(self.ctx.git_exe), "-C", str(self.repo), *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        ).stdout

    def create(self, candidate: str, before: dict[str, bytes | None], turn: str = "turn-1") -> dict[str, str]:
        return review.create_review_refs(
            self.ctx, turn, candidate, before, commit_tree=audit.commit_tree
        )

    def candidate(self) -> str:
        result, _ = audit.commit_tree(
            self.ctx, self.initial,
            {"edit.txt": b"actual after\n", "new.txt": b"new contents\n"},
            "candidate\n",
        )
        return result

    def test_exact_pair_preserves_other_paths_head_index_and_worktree(self) -> None:
        candidate = self.candidate()
        (self.repo / "keep.txt").write_bytes(b"staged user change\n")
        self.git("add", "--", "keep.txt")
        (self.repo / "edit.txt").write_bytes(b"unstaged user change\n")
        (self.repo / "untracked.txt").write_bytes(b"untracked user file\n")
        index_before = (self.repo / ".git" / "index").read_bytes()
        worktree_before = {p.name: p.read_bytes() for p in self.repo.iterdir() if p.is_file()}
        refs = self.create(candidate, {
            "edit.txt": b"actual before\n",
            "new.txt": None,
            "deleted.txt": b"deleted contents\n",
        })
        before, after = refs["beforeRef"], refs["afterRef"]
        self.assertEqual(self.git("show", f"{before}:edit.txt"), b"actual before\n")
        self.assertEqual(self.git("show", f"{after}:edit.txt"), b"actual after\n")
        self.assertEqual(self.git("show", f"{before}:deleted.txt"), b"deleted contents\n")
        self.assertEqual(self.git("diff", "--name-status", "--no-renames", before, after),
                         b"D\tdeleted.txt\nM\tedit.txt\nA\tnew.txt\n")
        self.assertEqual(self.git("diff", before, after, "--", "keep.txt"), b"")
        self.assertEqual(self.git("rev-parse", f"{after}^{{tree}}"), self.git("rev-parse", f"{candidate}^{{tree}}"))
        self.assertEqual(self.git("merge-base", before, after).decode().strip(), refs["beforeCommit"])
        self.assertEqual(self.git("rev-parse", f"{before}^").decode().strip(), self.initial)
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), self.initial)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index_before)
        self.assertEqual({p.name: p.read_bytes() for p in self.repo.iterdir() if p.is_file()}, worktree_before)

    def test_repeated_call_reuses_refs_and_conflicting_call_does_not_replace_them(self) -> None:
        candidate = self.candidate()
        refs = self.create(candidate, {"edit.txt": b"actual before\n"})
        self.assertEqual(self.create(candidate, {"edit.txt": b"actual before\n"}), refs)
        with self.assertRaisesRegex(bridge.CodexP4DiffError, "different contents"):
            self.create(candidate, {"edit.txt": b"different before\n"})
        self.assertEqual(self.git("rev-parse", refs["beforeRef"]).decode().strip(), refs["beforeCommit"])
        self.assertEqual(self.git("rev-parse", refs["afterRef"]).decode().strip(), refs["afterCommit"])

    def test_root_candidate_creates_root_baseline(self) -> None:
        refs = self.create(self.initial, {"edit.txt": b"before root\n"})
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", refs["beforeRef"]).decode().split(),
                         [refs["beforeCommit"]])
        self.assertEqual(self.git("diff", "--name-only", refs["beforeRef"], refs["afterRef"]), b"edit.txt\n")

    def test_safe_ids_do_not_collide_when_sanitized(self) -> None:
        self.ctx.session_id = "session..with/unsafe characters.lock"
        first = self.create(self.initial, {"edit.txt": b"before\n"}, turn="turn/a")
        second = self.create(self.initial, {"edit.txt": b"before\n"}, turn="turn?a")
        self.assertNotEqual(first["beforeRef"], second["beforeRef"])
        for refs in (first, second):
            self.git("check-ref-format", refs["beforeRef"])
            self.git("check-ref-format", refs["afterRef"])

    def test_invalid_snapshot_is_rejected_before_refs_are_created(self) -> None:
        for path in ("../outside", "/absolute", "C:/absolute", "a/../b", "a\\b", "a//b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.create(self.initial, {path: b"invalid"})
        with self.assertRaises(TypeError):
            self.create(self.initial, {"edit.txt": "unknown"})  # type: ignore[dict-item]
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", "refs/codex-p4-diff/review/"), b"")


if __name__ == "__main__":
    unittest.main()
