from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-p4-diff" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import codex_p4_audit as audit
import codex_p4_diff as bridge


class AuditTestCase(unittest.TestCase):
    def make_context(
        self, data_root: Path, project: Path, session_id: str = "thread-123"
    ) -> audit.AuditContext:
        p4_context = {
            "p4Exe": "p4",
            "serverAddress": "p4.example.test:1666",
            "userName": "tester",
            "client": "test-client",
            "clientRoot": str(project.parent),
            "projectRoot": str(project),
        }
        with mock.patch.object(bridge, "user_data_root", return_value=data_root):
            history = bridge.ensure_history_repo(p4_context, project)
        key = bridge.sanitize_component(session_id, 80)
        return audit.AuditContext(
            p4=p4_context,
            project_root=project,
            session_id=session_id,
            branch=f"codex/session/{key}",
            history_root=Path(history["historyRoot"]),
            state_root=Path(history["stateRoot"]),
            session_root=Path(history["stateRoot"]) / "sessions" / key,
            git_exe=str(history["gitExe"]),
            base_commit=str(history["baseCommit"]),
        )

    def start_turn(self, ctx: audit.AuditContext, turn_id: str, prompt: str = "Update file") -> None:
        audit.record_prompt(
            ctx,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": ctx.session_id,
                "turn_id": turn_id,
                "prompt": prompt,
            },
        )

    def finalize(self, ctx: audit.AuditContext, turn_id: str) -> dict[str, object]:
        with mock.patch.object(bridge, "readonly_fstat", return_value=None):
            return audit.finalize_turn(ctx, {"hook_event_name": "Stop", "turn_id": turn_id})


class AuditHelpersTest(AuditTestCase):
    def test_parse_apply_patch_paths_includes_move_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            command = """*** Begin Patch
*** Update File: Source/A.cpp
*** Move to: Source/B.cpp
*** Add File: Source/C.cpp
*** Delete File: Source/D.cpp
*** End Patch
"""
            paths = audit.parse_apply_patch_paths(command, root, root)
            self.assertEqual(
                [path.relative_to(root).as_posix() for path in paths],
                ["Source/A.cpp", "Source/B.cpp", "Source/C.cpp", "Source/D.cpp"],
            )

    def test_parse_apply_patch_paths_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            with self.assertRaisesRegex(bridge.CodexP4DiffError, "越出项目目录"):
                audit.parse_apply_patch_paths(
                    "*** Begin Patch\n*** Update File: ../outside.txt\n*** End Patch\n",
                    root,
                    root,
                )

    def test_redact_summary_removes_common_secrets(self) -> None:
        summary = audit.redact_summary("Fix login token=abc123 password:hello Bearer xyz")
        self.assertEqual(
            summary,
            "Fix login token=<redacted> password=<redacted> Bearer <redacted>",
        )

    def test_mutating_shell_classifier(self) -> None:
        self.assertTrue(audit.likely_mutating_shell("Set-Content -Path a.txt -Value x"))
        self.assertTrue(audit.likely_mutating_shell("clang-format -i Source/a.cpp"))
        self.assertTrue(audit.likely_mutating_shell("echo data > output.txt"))
        self.assertFalse(audit.likely_mutating_shell("Get-Content -LiteralPath a.txt"))
        self.assertFalse(audit.likely_mutating_shell("rg -n TODO Source"))


class TurnCommitIntegrationTest(AuditTestCase):
    def test_two_changed_turns_create_two_commits_on_one_session_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "Source" / "A.txt"
            target.parent.mkdir()
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)

            self.start_turn(ctx, "turn-1", "Change greeting")
            audit.record_before_path(ctx, "turn-1", target)
            target.write_text("after one\n", encoding="utf-8")
            first = self.finalize(ctx, "turn-1")

            self.start_turn(ctx, "turn-2", "Refine greeting")
            audit.record_before_path(ctx, "turn-2", target)
            target.write_text("after two\n", encoding="utf-8")
            second = self.finalize(ctx, "turn-2")

            self.assertEqual(first["status"], "committed")
            self.assertEqual(second["status"], "committed")
            count = subprocess.run(
                [
                    ctx.git_exe,
                    "-C",
                    str(ctx.history_root),
                    "rev-list",
                    "--count",
                    f"{bridge.BASE_BRANCH}..{ctx.branch}",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(count, "2")
            mirror = subprocess.run(
                [ctx.git_exe, "-C", str(ctx.history_root), "show", f"{ctx.branch}:files/Source/A.txt"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(mirror, "after two\n")
            log = subprocess.run(
                [ctx.git_exe, "-C", str(ctx.history_root), "log", "--format=%s", ctx.branch],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("[Codex] Change greeting", log)
            self.assertIn("[Codex] Refine greeting", log)

    def test_exact_first_touch_patch_is_committed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("user baseline\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-patch")
            audit.record_before_path(ctx, "turn-patch", target)
            target.write_text("codex result\n", encoding="utf-8")
            result = self.finalize(ctx, "turn-patch")
            session = audit.load_session(ctx)
            audit_path = session["turns"]["turn-patch"]["auditPath"]
            patch_text = subprocess.run(
                [ctx.git_exe, "-C", str(ctx.history_root), "show", f"{result['commit']}:{audit_path.rsplit('/', 1)[0]}/changes.patch"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("-user baseline", patch_text)
            self.assertIn("+codex result", patch_text)

    def test_no_change_turn_creates_no_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("same\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-empty")
            audit.record_before_path(ctx, "turn-empty", target)
            result = self.finalize(ctx, "turn-empty")
            self.assertEqual(result["status"], "no-changes")
            verify = subprocess.run(
                [ctx.git_exe, "-C", str(ctx.history_root), "rev-parse", "--verify", ctx.branch],
                check=False,
                capture_output=True,
            )
            self.assertNotEqual(verify.returncode, 0)

    def test_large_file_records_receipt_without_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "Asset.uasset"
            target.write_bytes(b"before-binary")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-large")
            with mock.patch.object(audit, "MAX_CAPTURE_BYTES", 4):
                audit.record_before_path(ctx, "turn-large", target)
                target.write_bytes(b"after-binary-content")
                result = self.finalize(ctx, "turn-large")
            session = audit.load_session(ctx)
            audit_path = session["turns"]["turn-large"]["auditPath"]
            event_json = subprocess.run(
                [ctx.git_exe, "-C", str(ctx.history_root), "show", f"{result['commit']}:{audit_path}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            record = json.loads(event_json)
            self.assertEqual(record["files"][0]["mirrorState"], "receipt-only")
            self.assertFalse(record["files"][0]["after"]["captured"])


class LocalOwnershipTest(AuditTestCase):
    def test_another_active_session_cannot_claim_same_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("content", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            self.start_turn(first, "turn-1")
            self.start_turn(second, "turn-2")
            event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }
            self.assertEqual(audit.prepare_tool(first, event), 1)
            with self.assertRaisesRegex(audit.OwnershipConflict, "另一个 Codex 会话"):
                audit.prepare_tool(second, event)


if __name__ == "__main__":
    unittest.main()
