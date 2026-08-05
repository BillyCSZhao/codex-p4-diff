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


class WindowsHookLauncherTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "requires Windows PowerShell")
    def test_launcher_preserves_utf8_hook_input(self) -> None:
        launcher = ROOT / "hooks" / "run_codex_p4_session.ps1"
        with tempfile.TemporaryDirectory() as temp:
            hook_root = Path(temp)
            copied_launcher = hook_root / launcher.name
            copied_launcher.write_bytes(launcher.read_bytes())
            (hook_root / "codex_p4_session.py").write_text(
                "import sys\n"
                "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
                encoding="utf-8",
            )
            payload = json.dumps(
                {"hook_event_name": "UserPromptSubmit", "prompt": "中文需求摘要"},
                ensure_ascii=False,
            ).encode("utf-8")
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(copied_launcher),
                ],
                input=payload,
                check=True,
                capture_output=True,
            )
            echoed = json.loads(completed.stdout.decode("utf-8-sig"))
            self.assertEqual(echoed["prompt"], "中文需求摘要")


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
            branch=bridge.HISTORY_BRANCH,
            history_root=Path(history["historyRoot"]),
            state_root=Path(history["stateRoot"]),
            session_root=Path(history["stateRoot"]) / "sessions" / key,
            git_exe=str(history["gitExe"]),
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

    def finalize(
        self, ctx: audit.AuditContext, turn_id: str, assistant_message: str | None = None
    ) -> dict[str, object]:
        event = {'hook_event_name': 'Stop', 'turn_id': turn_id}
        if assistant_message is not None:
            event['last_assistant_message'] = assistant_message
        with mock.patch.object(bridge, "readonly_fstat", return_value=None):
            return audit.finalize_turn(ctx, event)


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

    def test_extract_assistant_change_summary_skips_generic_markdown(self) -> None:
        message = (
            '## 完成情况\n\n'
            '已完成。\n\n'
            'Tests passed.\n\n'
            '- **修复提交记录标题**：现在使用实际改动摘要。\n'
            '- 测试通过。'
        )
        self.assertEqual(
            audit.extract_assistant_change_summary(message),
            '修复提交记录标题：现在使用实际改动摘要。',
        )

    def test_fallback_change_summary_uses_file_actions(self) -> None:
        self.assertEqual(
            audit.fallback_change_summary([{'action': 'edit', 'path': 'Source/A.cpp'}]),
            '修改 A.cpp',
        )
        self.assertEqual(
            audit.fallback_change_summary(
                [
                    {'action': 'add', 'path': 'Config/Config.ini'},
                    {'action': 'edit', 'path': 'Source/A.cpp'},
                    {'action': 'delete', 'path': 'Source/B.h'},
                ]
            ),
            '更新 3 个文件：Config.ini、A.cpp 等',
        )

    def test_mutating_shell_classifier(self) -> None:
        self.assertTrue(audit.likely_mutating_shell("Set-Content -Path a.txt -Value x"))
        self.assertTrue(audit.likely_mutating_shell("clang-format -i Source/a.cpp"))
        self.assertTrue(audit.likely_mutating_shell("echo data > output.txt"))
        self.assertFalse(audit.likely_mutating_shell("Get-Content -LiteralPath a.txt"))
        self.assertFalse(audit.likely_mutating_shell("rg -n TODO Source"))


class TurnCommitIntegrationTest(AuditTestCase):
    def test_two_changed_turns_create_two_commits_on_fixed_branch(self) -> None:
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
            first_message = '将问候语更新为第一版。'
            first = self.finalize(ctx, "turn-1", first_message)

            self.start_turn(ctx, "turn-2", "Refine greeting")
            audit.record_before_path(ctx, "turn-2", target)
            target.write_text("after two\n", encoding="utf-8")
            second_message = '将问候语调整为最终版本。'
            second = self.finalize(ctx, "turn-2", second_message)

            self.assertEqual(first["status"], "committed")
            self.assertEqual(second["status"], "committed")
            count = subprocess.run(
                [
                    ctx.git_exe,
                    "-C",
                    str(ctx.history_root),
                    "rev-list",
                    "--count",
                    ctx.branch,
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(count, "3")
            mirror = subprocess.run(
                [ctx.git_exe, "-C", str(ctx.history_root), "show", f"{ctx.branch}:Source/A.txt"],
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
            self.assertIn("[Codex] 将问候语更新为第一版。", log)
            self.assertIn("[Codex] 将问候语调整为最终版本。", log)
            message = subprocess.run(
                [ctx.git_exe, "-C", str(ctx.history_root), "show", "-s", "--format=%B", str(second["commit"])],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("Change: 将问候语调整为最终版本。", message)
            self.assertIn("Change-Summary-Source: assistant", message)
            self.assertIn("Request: Refine greeting", message)
            self.assertIn("Session: thread-123", message)
            self.assertIn("Turn: turn-2", message)
            self.assertIn("Recorded-At:", message)
            self.assertIn("p4=-@-/-", message)
            note = subprocess.run(
                [
                    ctx.git_exe,
                    "-C",
                    str(ctx.history_root),
                    "notes",
                    "--ref=codex-p4-diff",
                    "show",
                    str(second["commit"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            receipt = json.loads(note)
            self.assertEqual(receipt["changeSummary"], "将问候语调整为最终版本。")
            self.assertEqual(receipt["changeSummarySource"], "assistant")
            self.assertEqual(
                receipt["assistantMessageSha256"],
                audit.sha256_bytes(second_message.encode("utf-8")),
            )
            self.assertEqual(receipt["promptSummary"], "Refine greeting")

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
            patch_text = subprocess.run(
                [
                    ctx.git_exe,
                    "-C",
                    str(ctx.history_root),
                    "notes",
                    "--ref=codex-p4-diff",
                    "show",
                    str(result["commit"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            patch_text = json.loads(patch_text)["textPatch"]
            self.assertIn("-user baseline", patch_text)
            self.assertIn("+codex result", patch_text)

    def test_missing_assistant_message_uses_file_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / 'workspace' / 'Project'
            project.mkdir(parents=True)
            target = project / 'Source' / 'A.cpp'
            target.parent.mkdir()
            target.write_text('before\n', encoding='utf-8')
            ctx = self.make_context(data_root / 'local', project)

            self.start_turn(ctx, 'turn-fallback', '按你推荐的做吧')
            audit.record_before_path(ctx, 'turn-fallback', target)
            target.write_text('after\n', encoding='utf-8')
            result = self.finalize(ctx, 'turn-fallback')

            message = subprocess.run(
                [
                    ctx.git_exe,
                    '-C',
                    str(ctx.history_root),
                    'show',
                    '-s',
                    '--format=%B',
                    str(result['commit']),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertTrue(message.startswith('[Codex] 修改 A.cpp\n'))
            self.assertIn('Change-Summary-Source: files', message)
            self.assertIn('Request: 按你推荐的做吧', message)
            self.assertEqual(result['summary'], '修改 A.cpp')
            self.assertEqual(result['summarySource'], 'files')
            note = subprocess.run(
                [
                    ctx.git_exe,
                    '-C',
                    str(ctx.history_root),
                    'notes',
                    '--ref=codex-p4-diff',
                    'show',
                    str(result['commit']),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            receipt = json.loads(note)
            self.assertEqual(receipt['changeSummary'], '修改 A.cpp')
            self.assertEqual(receipt['changeSummarySource'], 'files')
            self.assertIsNone(receipt['assistantMessageSha256'])
            self.assertEqual(receipt['promptSummary'], '按你推荐的做吧')

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
            count = subprocess.run(
                [ctx.git_exe, "-C", str(ctx.history_root), "rev-list", "--count", ctx.branch],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(count, "1")

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
            event_json = subprocess.run(
                [
                    ctx.git_exe,
                    "-C",
                    str(ctx.history_root),
                    "notes",
                    "--ref=codex-p4-diff",
                    "show",
                    str(result["commit"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            record = json.loads(event_json)
            self.assertEqual(record["files"][0]["historyState"], "receipt-only")
            self.assertFalse(record["files"][0]["after"]["captured"])

    def test_separate_sessions_share_one_global_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            first_target = project / "A.txt"
            second_target = project / "B.txt"
            first_target.write_text("a0\n", encoding="utf-8")
            second_target.write_text("b0\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")

            self.start_turn(first, "turn-a", "Change A")
            audit.record_before_path(first, "turn-a", first_target)
            first_target.write_text("a1\n", encoding="utf-8")
            first_result = self.finalize(first, "turn-a")

            self.start_turn(second, "turn-b", "Change B")
            audit.record_before_path(second, "turn-b", second_target)
            second_target.write_text("b1\n", encoding="utf-8")
            second_result = self.finalize(second, "turn-b")

            parent = subprocess.run(
                [second.git_exe, "-C", str(project), "rev-parse", f"{second_result['commit']}^"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(parent, first_result["commit"])
            self.assertEqual(first.branch, second.branch)

    def test_turn_refuses_to_overwrite_same_path_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("user staged\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            subprocess.run(
                [ctx.git_exe, "-C", str(project), "add", "-f", "--", "A.txt"], check=True
            )
            self.start_turn(ctx, "turn-staged")
            audit.record_before_path(ctx, "turn-staged", target)
            target.write_text("codex result\n", encoding="utf-8")
            with self.assertRaisesRegex(audit.AuditError, "已暂存"):
                self.finalize(ctx, "turn-staged")

    def test_turn_preserves_unrelated_staged_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            unrelated = project / "User.txt"
            target.write_text("a0\n", encoding="utf-8")
            unrelated.write_text("user staged\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            subprocess.run(
                [ctx.git_exe, "-C", str(project), "add", "-f", "--", "User.txt"], check=True
            )
            staged_blob = subprocess.run(
                [ctx.git_exe, "-C", str(project), "rev-parse", ":User.txt"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            self.start_turn(ctx, "turn-a")
            audit.record_before_path(ctx, "turn-a", target)
            target.write_text("a1\n", encoding="utf-8")
            result = self.finalize(ctx, "turn-a")

            self.assertEqual(result["status"], "committed")
            staged_after = subprocess.run(
                [ctx.git_exe, "-C", str(project), "rev-parse", ":User.txt"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(staged_after, staged_blob)
            tree_check = subprocess.run(
                [ctx.git_exe, "-C", str(project), "cat-file", "-e", f"{result['commit']}:User.txt"],
                check=False,
                capture_output=True,
            )
            self.assertNotEqual(tree_check.returncode, 0)


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
