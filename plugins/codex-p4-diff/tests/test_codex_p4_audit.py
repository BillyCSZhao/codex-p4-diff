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


class AuditHelpersTest(unittest.TestCase):
    def make_context(self, root: Path) -> audit.AuditContext:
        info = {
            "serverAddress": "p4.example.test:1666",
            "userName": "tester",
            "clientRoot": str(root.parent),
        }
        project_audit_root = root.parent / "audit"
        return audit.AuditContext(
            p4_exe="p4",
            project_root=root,
            client="test-client",
            client_root=root.parent,
            info=info,
            connection={"source": "test"},
            session_id="thread-123",
            session_root=project_audit_root / "sessions" / "thread-123",
            project_audit_root=project_audit_root,
        )

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
            with self.assertRaises(bridge.CodexP4DiffError):
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
        self.assertFalse(audit.likely_mutating_shell("rg -n TODO Source 2>$null"))
        self.assertFalse(audit.likely_mutating_shell("tool 2>&1"))

    def test_replace_description_preserves_other_fields(self) -> None:
        original = (
            "Change:\t123\r\n"
            "Client:\ttest-client\r\n"
            "Description:\r\n"
            "\told line\r\n"
            "\r\n"
            "Files:\r\n"
            "\t//depot/file\r\n"
        )
        updated = audit.replace_description_in_spec(original, "new line\nsecond line")
        self.assertIn("\tnew line\r\n\tsecond line\r\nFiles:", updated)
        self.assertIn("\t//depot/file", updated)
        self.assertNotIn("old line", updated)

    def test_render_change_description_contains_traceability_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "Project"
            root.mkdir()
            ctx = self.make_context(root)
            session = audit.base_session(ctx)
            session["promptSummary"] = "Fix puzzle placement"
            session["turns"] = [
                {"turnId": "turn-abcdef", "summary": "Update validation", "fileCount": 2}
            ]
            session["files"] = [{"path": "Source/A.cpp", "action": "edit"}]
            description = audit.render_change_description(ctx, session)
            self.assertIn(audit.MANAGED_MARKER, description)
            self.assertIn("Codex-Session: thread-123", description)
            self.assertIn("turn-abcdef", description)
            self.assertIn("edit Source/A.cpp", description)

    def test_preflight_rejects_file_owned_by_another_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "Project"
            root.mkdir()
            path = root / "A.txt"
            path.write_text("content", encoding="utf-8")
            ctx = self.make_context(root)
            with mock.patch.object(
                audit,
                "opened_record_for_path",
                return_value={"depotFile": "//depot/A.txt", "change": "456"},
            ):
                with self.assertRaisesRegex(audit.OwnershipConflict, "changelist 456"):
                    audit.preflight_existing_ownership(ctx, [path], "123")

    def test_text_diff_uses_before_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            current = root / "A.txt"
            before_path = root / "before.txt"
            before_path.write_text("before\n", encoding="utf-8")
            current.write_text("after\n", encoding="utf-8")
            result = audit.build_file_diff(
                "A.txt",
                {
                    "exists": True,
                    "binary": False,
                    "snapshotPath": str(before_path),
                },
                current,
            )
            self.assertIn("-before", result)
            self.assertIn("+after", result)

    def test_unchanged_added_file_is_not_a_new_turn_change(self) -> None:
        before = {"exists": True, "sha256": "same"}
        after = {"exists": True, "sha256": "same"}
        self.assertFalse(audit.snapshot_changed(before, after))

    def test_auto_changelist_reads_current_session_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            info = {
                "serverAddress": "p4.example.test:1666",
                "userName": "tester",
                "clientRoot": str(data_root / "workspace"),
            }
            with mock.patch.object(bridge, "user_data_root", return_value=data_root):
                ledger = bridge.audit_session_root(info, "test-client", project, "thread-123") / "session.json"
                ledger.parent.mkdir(parents=True)
                ledger.write_text(
                    json.dumps(
                        {
                            "schemaVersion": bridge.AUDIT_SCHEMA_VERSION,
                            "generatedBy": bridge.TOOL_NAME,
                            "sessionId": "thread-123",
                            "client": "test-client",
                            "serverAddress": "p4.example.test:1666",
                            "userName": "tester",
                            "clientRoot": str(data_root / "workspace"),
                            "projectRoot": str(project),
                            "changelist": "12345",
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(
                    bridge.resolve_auto_changelist(info, "test-client", project, "thread-123"),
                    "12345",
                )

    def test_auto_changelist_requires_managed_description(self) -> None:
        project = Path("C:/workspace/Project")
        good_spec = (
            "Change:\t12345\nClient:\ttest-client\nStatus:\tpending\nDescription:\n"
            "\tManaged-By: codex-p4-diff\n\tCodex-Session: thread-123\n"
        ).encode("utf-8")
        completed = subprocess.CompletedProcess([], 0, good_spec, b"")
        with mock.patch.object(bridge, "run_command", return_value=completed):
            bridge.validate_auto_changelist_context(
                "p4", "test-client", project, "12345", "thread-123"
            )
        bad = subprocess.CompletedProcess([], 0, good_spec.replace(b"thread-123", b"other"), b"")
        with mock.patch.object(bridge, "run_command", return_value=bad):
            with self.assertRaisesRegex(bridge.CodexP4DiffError, "所有权标记"):
                bridge.validate_auto_changelist_context(
                    "p4", "test-client", project, "12345", "thread-123"
                )


if __name__ == "__main__":
    unittest.main()
