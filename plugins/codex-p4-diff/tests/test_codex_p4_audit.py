from __future__ import annotations

from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
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

    def advance_history(
        self,
        ctx: audit.AuditContext,
        updates: dict[str, bytes | None],
        message: str = "concurrent history advance",
    ) -> str:
        parent = audit.current_history_head(ctx)
        commit, _ = audit.commit_tree(ctx, parent, updates, message)
        bridge.run_command(
            [
                ctx.git_exe,
                "-C",
                str(ctx.history_root),
                "update-ref",
                f"refs/heads/{ctx.branch}",
                commit,
                parent,
            ]
        )
        return commit


class AuditHelpersTest(AuditTestCase):
    def test_hook_main_fails_open_for_non_object_json(self) -> None:
        stdin = io.TextIOWrapper(io.BytesIO(b"[]"), encoding="utf-8")
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            self.assertEqual(audit.hook_main(), 0)
        response = json.loads(stdout.getvalue())
        self.assertIn("正常工具操作未被阻断", response["hookSpecificOutput"]["additionalContext"])

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

    def test_quoted_regex_redirect_character_is_read_only(self) -> None:
        command = (
            "Get-Content -LiteralPath 'Book.xlsx'; "
            "Select-String -Pattern '<c r=\"A4\"[^>]*>.*?</c>'"
        )
        self.assertFalse(audit.likely_mutating_shell(command))
        self.assertFalse(audit.likely_mutating_shell("Write-Output Set-Content"))
        self.assertFalse(audit.likely_mutating_shell("if [[ alpha > beta ]]; then echo ok; fi"))

    def test_shell_paths_only_include_explicit_write_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            target = project / "Source" / "A.txt"
            target.parent.mkdir(parents=True)
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            command = (
                "$ErrorActionPreference = 'Stop'\n"
                f"$projectRoot = '{project}'\n"
                "$target = Join-Path $projectRoot 'Source/A.txt'\n"
                "Set-Content -LiteralPath $target -Value 'Stop'"
            )
            paths = audit.extract_shell_paths(ctx, command, project)
            self.assertEqual(paths, [target.resolve()])

    def test_shell_junction_has_no_file_ownership_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            ctx = self.make_context(data_root / "local", project)
            command = (
                "$junction = Join-Path 'Saved/Codex' 'node_modules'\n"
                "New-Item -ItemType Junction -Path $junction -Target 'C:/dependencies/node_modules'"
            )
            self.assertTrue(audit.likely_mutating_shell(command))
            self.assertEqual(audit.extract_shell_paths(ctx, command, project), [])

    def test_shell_redirection_records_only_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            ctx = self.make_context(data_root / "local", project)
            paths = audit.extract_shell_paths(ctx, "'hello > text' > 'Output/result.txt'", project)
            self.assertEqual(paths, [(project / "Output" / "result.txt").resolve()])

            with mock.patch.object(
                audit,
                "path_is_link_like",
                side_effect=lambda path: path.name == "linked.log",
            ):
                result = audit.analyze_shell_command(
                    ctx,
                    "Set-Content A.txt content; echo warning > linked.log",
                    project,
                    "powershell",
                )
            self.assertEqual(result.paths, [(project / "A.txt").resolve()])
            self.assertIn("shell-link-skipped", result.warnings)

    def test_shell_writer_argument_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp).resolve()
            (project / "Dest").mkdir()
            for relative in ("A.txt", "B.txt"):
                (project / relative).write_text("content\n", encoding="utf-8")
            ctx = audit.AuditContext(
                {}, project, "shell", bridge.HISTORY_BRANCH, project, project, project, "git"
            )
            cases = [
                ("rm -LiteralPath A.txt -ErrorAction Stop", "powershell", ["A.txt"]),
                ("cp A.txt Dest -EA Stop", "powershell", ["Dest/A.txt"]),
                (
                    "mv A.txt B.txt -WarningAction SilentlyContinue",
                    "powershell",
                    ["A.txt", "B.txt"],
                ),
                ("Get-Content A.txt && Set-Content B.txt x", "powershell", ["B.txt"]),
                (
                    "& 'Set-Content' -LiteralPath B.txt -Value x",
                    "powershell",
                    ["B.txt"],
                ),
                (
                    "& 'C:/Tools/clang-format.exe' -i A.txt B.txt",
                    "powershell",
                    ["A.txt", "B.txt"],
                ),
                ("Remove-Item -vb A.txt", "powershell", ["A.txt"]),
                ("Out-File -NoClobber output.txt", "powershell", ["output.txt"]),
                ("Copy-Item -Container A.txt Dest", "powershell", ["Dest/A.txt"]),
                ("rm.exe -rf A.txt", "powershell", ["A.txt"]),
                ("echo failure 2> error.log", "posix", ["error.log"]),
            ]
            for command, dialect, expected in cases:
                with self.subTest(command=command):
                    result = audit.analyze_shell_command(ctx, command, project, dialect)
                    actual = [path.relative_to(project).as_posix() for path in result.paths]
                    self.assertEqual(actual, expected)
                    self.assertEqual(result.warnings, [])

    def test_shell_expands_directories_wildcards_and_multi_file_writers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp).resolve()
            for relative in (
                "Dir/one.txt",
                "Dir/Nested/two.txt",
                "Dir/__pycache__/ignored.pyc",
                "Logs/one.txt",
                "Logs/two.log",
                "A.txt",
                "B.txt",
                "One.md",
                "Two.md",
            ):
                target = project / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("content\n", encoding="utf-8")
            (project / "Dest").mkdir()
            ctx = audit.AuditContext(
                {}, project, "shell", bridge.HISTORY_BRANCH, project, project, project, "git"
            )

            cases = [
                (
                    "Remove-Item Dir -Recurse",
                    "powershell",
                    ["Dir/one.txt", "Dir/Nested/two.txt"],
                ),
                ("Remove-Item -Path Logs/*.txt", "powershell", ["Logs/one.txt"]),
                ("Copy-Item A.txt Dest", "powershell", ["Dest/A.txt"]),
                (
                    "sed -i.bak expr A.txt B.txt",
                    "posix",
                    ["A.txt", "B.txt", "A.txt.bak", "B.txt.bak"],
                ),
                ("clang-format -i -style=file A.txt B.txt", "posix", ["A.txt", "B.txt"]),
                (
                    "prettier --write --config cfg A.txt B.txt",
                    "posix",
                    ["A.txt", "B.txt"],
                ),
                ("sed --in-place=.bak expr A.txt B.txt", "posix", ["A.txt", "B.txt", "A.txt.bak", "B.txt.bak"]),
                ("rm '*.md' *.md", "posix", ["*.md", "One.md", "Two.md"]),
            ]
            for command, dialect, expected in cases:
                with self.subTest(command=command):
                    result = audit.analyze_shell_command(ctx, command, project, dialect)
                    actual = [path.relative_to(project).as_posix() for path in result.paths]
                    self.assertEqual(actual, expected)
                    self.assertEqual(result.warnings, [])

    def test_shell_avoids_false_commands_and_reports_partial_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp).resolve()
            literal = project / "a[1].txt"
            literal.write_text("content\n", encoding="utf-8")
            ctx = audit.AuditContext(
                {}, project, "shell", bridge.HISTORY_BRANCH, project, project, project, "git"
            )
            read_only = [
                ("Write-Output ok # > fake.txt", "powershell"),
                (r"printf x \> fake.txt", "posix"),
                ("(( x > 1 ))", "posix"),
                ("[[ alpha > beta ]]", "posix"),
                ("tool key=touch A.txt", "posix"),
                ("$x = touch", "powershell"),
                ("sed -n '1p' README.md", "posix"),
                ("clang-format README.md", "posix"),
                ("prettier --check README.md", "posix"),
            ]
            for command, dialect in read_only:
                with self.subTest(command=command):
                    result = audit.analyze_shell_command(ctx, command, project, dialect)
                    self.assertFalse(result.mutating)
                    self.assertEqual(result.paths, [])

            literal_result = audit.analyze_shell_command(
                ctx, "Remove-Item -LiteralPath 'a[1].txt'", project, "powershell"
            )
            self.assertEqual(literal_result.paths, [literal])
            response_file = audit.analyze_shell_command(
                ctx, "clang-format -i @files.rsp", project, "posix"
            )
            self.assertTrue(response_file.mutating)
            self.assertEqual(response_file.paths, [])
            self.assertIn("shell-response-file-unresolved", response_file.warnings)

            background = audit.analyze_shell_command(
                ctx, "sleep 1 & touch background.txt", project, "posix"
            )
            self.assertEqual(background.paths, [(project / "background.txt").resolve()])
            self.assertTrue(background.mutating)
            powershell_script = audit.analyze_shell_command(
                ctx, "& Set-Content.ps1 -LiteralPath 'a[1].txt'", project, "powershell"
            )
            self.assertFalse(powershell_script.mutating)
            self.assertEqual(powershell_script.paths, [])

    def test_shell_powershell_arrays_reparse_points_and_global_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp).resolve()
            (project / "Dest").mkdir()
            for relative in ("A.txt", "B.txt", "C.txt"):
                (project / relative).write_text("content\n", encoding="utf-8")
            ctx = audit.AuditContext(
                {}, project, "shell", bridge.HISTORY_BRANCH, project, project, project, "git"
            )
            cases = [
                (
                    "Remove-Item -Path A.txt,B.txt",
                    ["A.txt", "B.txt"],
                ),
                (
                    "Copy-Item -Path A.txt,B.txt -Destination Dest",
                    ["Dest/A.txt", "Dest/B.txt"],
                ),
                (
                    "Move-Item A.txt,B.txt Dest",
                    ["A.txt", "B.txt", "Dest/A.txt", "Dest/B.txt"],
                ),
            ]
            for command, expected in cases:
                with self.subTest(command=command):
                    result = audit.analyze_shell_command(ctx, command, project, "powershell")
                    actual = [path.relative_to(project).as_posix() for path in result.paths]
                    self.assertEqual(actual, expected)
                    self.assertEqual(result.warnings, [])

            fake_stat = mock.Mock(st_file_attributes=0x400)
            with mock.patch.object(audit.os, "lstat", return_value=fake_stat):
                self.assertTrue(audit.path_is_link_like(project / "junction"))

            with mock.patch.object(audit, "MAX_EXPANDED_SHELL_PATHS", 2):
                limited = audit.analyze_shell_command(
                    ctx, "touch A.txt B.txt C.txt", project, "posix"
                )
            self.assertEqual(len(limited.paths), 2)
            self.assertIn("shell-path-limit", limited.warnings)


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

    def test_turn_records_same_path_staging_as_receipt_only(self) -> None:
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
            staged_before = subprocess.run(
                [ctx.git_exe, "-C", str(project), "rev-parse", ":A.txt"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.start_turn(ctx, "turn-staged")
            audit.record_before_path(ctx, "turn-staged", target)
            target.write_text("codex result\n", encoding="utf-8")
            result = self.finalize(ctx, "turn-staged")

            self.assertEqual(result["status"], "committed")
            staged_after = subprocess.run(
                [ctx.git_exe, "-C", str(project), "rev-parse", ":A.txt"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(staged_after, staged_before)
            note = subprocess.run(
                [
                    ctx.git_exe,
                    "-C",
                    str(project),
                    "notes",
                    "--ref=codex-p4-diff",
                    "show",
                    str(result["commit"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            receipt = json.loads(note)
            self.assertEqual(receipt["files"][0]["historyState"], "staged-receipt-only")
            self.assertEqual(receipt["files"][0]["after"]["sha256"], audit.sha256_file(target))

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


    def test_posttool_only_existing_edit_is_partial_after_only_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-post-only-edit")
            target.write_text("after\n", encoding="utf-8")
            event = {
                "hook_event_name": "PostToolUse",
                "turn_id": "turn-post-only-edit",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }

            self.assertEqual(audit.finish_tool(ctx, event), 1)
            result = self.finalize(ctx, "turn-post-only-edit")
            note = subprocess.run(
                [
                    ctx.git_exe,
                    "-C",
                    str(project),
                    "notes",
                    "--ref=codex-p4-diff",
                    "show",
                    str(result["commit"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            receipt = json.loads(note)
            change = receipt["files"][0]
            self.assertEqual(result["auditCoverage"], "partial")
            self.assertIn(
                "posttool-before-snapshot-unavailable", result["coverageWarnings"]
            )
            self.assertIsNone(change["before"]["exists"])
            self.assertEqual(change["action"], "unknown")
            self.assertFalse(change["textPatch"])
            self.assertEqual(change["historyState"], "after-only-receipt")
            self.assertIsNone(receipt["textPatch"])
            tree_path = subprocess.run(
                [ctx.git_exe, "-C", str(project), "cat-file", "-e", f"{result['commit']}:A.txt"],
                capture_output=True,
            )
            self.assertNotEqual(tree_path.returncode, 0)

    def test_posttool_only_delete_keeps_after_only_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-post-only-delete")
            target.unlink()
            event = {
                "hook_event_name": "PostToolUse",
                "turn_id": "turn-post-only-delete",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }

            audit.finish_tool(ctx, event)
            result = self.finalize(ctx, "turn-post-only-delete")
            note = subprocess.run(
                [
                    ctx.git_exe,
                    "-C",
                    str(project),
                    "notes",
                    "--ref=codex-p4-diff",
                    "show",
                    str(result["commit"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            change = json.loads(note)["files"][0]
            self.assertEqual(result["auditCoverage"], "partial")
            self.assertIsNone(change["before"]["exists"])
            self.assertFalse(change["after"]["exists"])
            self.assertEqual(change["action"], "unknown")
            self.assertEqual(change["historyState"], "after-only-receipt")
            self.assertFalse(change["textPatch"])

    def test_normal_pre_and_post_remain_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-pre-post")
            event = {
                "turn_id": "turn-pre-post",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }

            event["hook_event_name"] = "PreToolUse"
            audit.prepare_tool(ctx, event)
            target.write_text("after\n", encoding="utf-8")
            event["hook_event_name"] = "PostToolUse"
            audit.finish_tool(ctx, event)
            result = self.finalize(ctx, "turn-pre-post")
            note = subprocess.run(
                [
                    ctx.git_exe,
                    "-C",
                    str(project),
                    "notes",
                    "--ref=codex-p4-diff",
                    "show",
                    str(result["commit"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            receipt = json.loads(note)
            self.assertEqual(result["auditCoverage"], "exact")
            self.assertNotIn(
                "posttool-before-snapshot-unavailable", result["coverageWarnings"]
            )
            self.assertEqual(receipt["files"][0]["ownershipAttribution"], "exact")
            self.assertTrue(receipt["files"][0]["textPatch"])


class LocalOwnershipTest(AuditTestCase):
    def test_repeated_exact_pre_post_does_not_accumulate_uncertainty_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-repeat")
            event = {
                "turn_id": "turn-repeat",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }

            for _ in range(100):
                event["hook_event_name"] = "PreToolUse"
                audit.prepare_tool(ctx, event)
                event["hook_event_name"] = "PostToolUse"
                audit.finish_tool(ctx, event)
            uncertainty = ctx.state_root / "uncertainty"
            self.assertFalse(uncertainty.exists())

    def test_alternating_uncertainty_writers_preserve_foreign_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            before_epoch = audit.time.time() - 1

            audit.write_uncertainty_markers(second, "turn-b", [target])
            audit.write_uncertainty_markers(first, "turn-a", [target])

            self.assertTrue(
                audit.has_foreign_uncertainty_since(
                    first, "turn-a", "A.txt", before_epoch
                )
            )
            summary = json.loads(
                audit.uncertainty_summary_path(
                    audit.uncertainty_marker_directory(first, "A.txt")
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(len(summary["markers"]), 2)
            self.assertEqual(
                {
                    (item["sessionId"], item["turnId"])
                    for item in summary["markers"]
                },
                {("session-one", "turn-a"), ("session-two", "turn-b")},
            )

    def test_scope_lock_timeouts_use_one_bounded_fallback_and_compact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            original_lock = audit.project_lock

            @contextmanager
            def fail_scope_lock(
                state_root: Path,
                *,
                lock_name: str = ".project.lock",
                wait_seconds: float | None = None,
            ):
                if lock_name == ".scope.lock":
                    raise audit.ProjectLockTimeout("injected scope timeout")
                with original_lock(
                    state_root, lock_name=lock_name, wait_seconds=wait_seconds
                ):
                    yield

            with mock.patch.object(audit, "project_lock", fail_scope_lock):
                for sequence in range(100):
                    audit.write_uncertainty_markers(
                        ctx, f"turn-{sequence}", [target]
                    )

            directory = audit.uncertainty_marker_directory(ctx, "A.txt")
            self.assertEqual(
                [path.name for path in directory.glob("*.json")], ["pending.json"]
            )
            self.assertTrue(
                audit.has_foreign_uncertainty_since(ctx, "query-turn", "A.txt", 0)
            )
            self.assertFalse((directory / "pending.json").exists())
            summary = json.loads(
                audit.uncertainty_summary_path(directory).read_text(encoding="utf-8")
            )
            self.assertLessEqual(len(summary["markers"]), 2)
            self.assertEqual(
                [path.name for path in directory.glob("*.json")], ["summary.json"]
            )

    def test_pending_fallback_timestamp_cannot_move_high_water_backwards(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            ctx = self.make_context(data_root / "local", project)
            directory = audit.uncertainty_marker_directory(ctx, "A.txt")
            pending = directory / "pending.json"
            newer = {
                "sessionId": "",
                "turnId": "",
                "recordedAt": "newer",
                "recordedEpoch": 200.0,
            }
            older = {**newer, "recordedAt": "older", "recordedEpoch": 100.0}

            audit.atomic_write_json(pending, newer)
            audit.atomic_write_json(pending, older)
            with mock.patch.object(audit.time, "time", return_value=300.0):
                self.assertTrue(
                    audit.has_foreign_uncertainty_since(
                        ctx, "query-turn", "A.txt", 150.0
                    )
                )

            summary = json.loads(
                audit.uncertainty_summary_path(directory).read_text(encoding="utf-8")
            )
            self.assertEqual(summary["markers"][0]["recordedEpoch"], 300.0)

    def test_compaction_does_not_delete_pending_written_after_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            ctx = self.make_context(data_root / "local", project)
            directory = audit.uncertainty_marker_directory(ctx, "A.txt")
            pending = directory / "pending.json"
            base_epoch = audit.time.time()
            audit.atomic_write_json(
                pending,
                {
                    "sessionId": "",
                    "turnId": "",
                    "recordedAt": "old",
                    "recordedEpoch": base_epoch - 10,
                },
            )
            original_persist = audit.persist_uncertainty_markers_locked
            injected = False

            def persist_with_new_pending(
                scope: Path,
                markers: object,
                cleanup: object,
            ) -> list[dict[str, object]]:
                nonlocal injected
                if not injected:
                    injected = True
                    audit.atomic_write_json(
                        pending,
                        {
                            "sessionId": "",
                            "turnId": "",
                            "recordedAt": "new",
                            "recordedEpoch": base_epoch + 1,
                        },
                    )
                return original_persist(scope, markers, cleanup)

            with mock.patch.object(
                audit,
                "persist_uncertainty_markers_locked",
                side_effect=persist_with_new_pending,
            ):
                audit.has_foreign_uncertainty_since(
                    ctx, "query-turn", "A.txt", base_epoch - 5
                )

            self.assertTrue(pending.is_file())
            self.assertTrue(
                audit.has_foreign_uncertainty_since(
                    ctx, "next-query", "A.txt", base_epoch + 0.5
                )
            )
            self.assertFalse(pending.exists())

    def test_foreign_unresolved_pretool_makes_active_owner_receipt_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            self.start_turn(first, "turn-a")
            self.start_turn(second, "turn-b")
            audit.prepare_tool(
                first,
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(target)},
                },
            )
            unresolved = {
                "hook_event_name": "PreToolUse",
                "turn_id": "turn-b",
                "tool_name": "Write",
                "tool_input": {},
            }
            self.assertEqual(audit.prepare_tool(second, unresolved), 0)
            target.write_text("foreign uncertain write\n", encoding="utf-8")

            result = self.finalize(first, "turn-a")
            self.assertEqual(result["auditCoverage"], "partial")
            self.assertIn(
                "concurrent-write-uncertainty-before-finalize",
                result["coverageWarnings"],
            )
            note = subprocess.run(
                [
                    first.git_exe,
                    "-C",
                    str(project),
                    "notes",
                    "--ref=codex-p4-diff",
                    "show",
                    str(result["commit"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            change = json.loads(note)["files"][0]
            self.assertEqual(change["ownershipAttribution"], "uncertain")
            self.assertEqual(
                change["historyState"], "concurrent-uncertainty-receipt-only"
            )
            self.assertFalse(change["textPatch"])

    def test_case_alias_pending_add_has_one_canonical_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            self.start_turn(first, "turn-1")
            self.start_turn(second, "turn-2")
            first_event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(project / "NewFile.txt")},
            }
            second_event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(project / "newfile.txt")},
            }

            with mock.patch.object(audit, "CASE_INSENSITIVE_OWNERSHIP_PATHS", True):
                self.assertEqual(audit.prepare_tool(first, first_event), 1)
                with self.assertRaises(audit.OwnershipConflict):
                    audit.prepare_tool(second, second_event)
                registry = json.loads(
                    audit.ownership_path(first).read_text(encoding="utf-8")
                )
                self.assertEqual(list(registry["files"]), ["newfile.txt"])
                self.assertEqual(
                    registry["files"]["newfile.txt"]["path"], "NewFile.txt"
                )

    def test_takeover_generation_makes_old_turn_receipt_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            self.start_turn(first, "turn-1")
            self.start_turn(second, "turn-2")
            event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }
            audit.prepare_tool(first, event)
            registry_path = audit.ownership_path(first)
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            key = audit.ownership_key("A.txt")
            registry["files"][key]["leaseUntilEpoch"] = audit.time.time() - 1
            audit.atomic_write_json(registry_path, registry)
            audit.prepare_tool(second, event)
            target.write_text("second session\n", encoding="utf-8")

            first_result = self.finalize(first, "turn-1")
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["files"][key]["sessionId"], second.session_id)
            note = subprocess.run(
                [
                    first.git_exe,
                    "-C",
                    str(project),
                    "notes",
                    "--ref=codex-p4-diff",
                    "show",
                    str(first_result["commit"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            receipt = json.loads(note)
            self.assertEqual(first_result["auditCoverage"], "partial")
            self.assertIn(
                "ownership-superseded-before-finalize",
                first_result["coverageWarnings"],
            )
            self.assertEqual(receipt["files"][0]["action"], "unknown")
            self.assertEqual(
                receipt["files"][0]["historyState"],
                "ownership-superseded-receipt-only",
            )
            self.assertFalse(receipt["files"][0]["textPatch"])
            tree_path = subprocess.run(
                [
                    first.git_exe,
                    "-C",
                    str(project),
                    "cat-file",
                    "-e",
                    f"{first_result['commit']}:A.txt",
                ],
                capture_output=True,
            )
            self.assertNotEqual(tree_path.returncode, 0)
            second_result = self.finalize(second, "turn-2")
            self.assertEqual(second_result["status"], "committed")

    def test_registry_rebuild_cannot_reuse_old_claim_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            self.start_turn(first, "turn-1")
            self.start_turn(second, "turn-2")
            event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }
            audit.prepare_tool(first, event)
            registry_path = audit.ownership_path(first)
            original = json.loads(registry_path.read_text(encoding="utf-8"))
            key = audit.ownership_key("A.txt")
            old_token = original["files"][key]["generation"]
            registry_path.write_text('{"files":', encoding="utf-8")

            audit.prepare_tool(second, event)
            rebuilt = json.loads(registry_path.read_text(encoding="utf-8"))
            new_token = rebuilt["files"][key]["generation"]
            self.assertNotEqual(old_token, new_token)
            self.assertEqual(rebuilt["schemaVersion"], audit.OWNERSHIP_SCHEMA_VERSION)
            self.assertNotIn("generations", rebuilt)
            target.write_text("second session\n", encoding="utf-8")
            second_session = audit.load_session(second)
            audit.release_turn_paths(second, second_session, "turn-2")

            result = self.finalize(first, "turn-1")
            self.assertEqual(result["auditCoverage"], "partial")
            note = subprocess.run(
                [
                    first.git_exe,
                    "-C",
                    str(project),
                    "notes",
                    "--ref=codex-p4-diff",
                    "show",
                    str(result["commit"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            change = json.loads(note)["files"][0]
            self.assertIn(
                change["historyState"],
                {
                    "concurrent-uncertainty-receipt-only",
                    "ownership-superseded-receipt-only",
                },
            )
            self.assertFalse(change["textPatch"])

    def test_pretool_state_lock_timeout_leaves_partial_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-timeout")
            event = {
                "hook_event_name": "PreToolUse",
                "turn_id": "turn-timeout",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }
            results: list[int] = []

            with audit.project_lock(ctx.state_root, lock_name=".state.lock"):
                with mock.patch.object(audit, "TOOL_LOCK_WAIT_SECONDS", 0.05):
                    thread = threading.Thread(
                        target=lambda: results.append(audit.prepare_tool(ctx, event))
                    )
                    thread.start()
                    thread.join(timeout=2)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(results, [1])
            target.write_text("after timeout\n", encoding="utf-8")
            event["hook_event_name"] = "PostToolUse"
            audit.finish_tool(ctx, event)
            result = self.finalize(ctx, "turn-timeout")
            self.assertEqual(result["auditCoverage"], "partial")
            self.assertIn("state-lock-timeout-unowned", result["coverageWarnings"])

    def test_posttool_state_lock_timeout_keeps_edit_and_delete_receipts(self) -> None:
        for delete_after in (False, True):
            with self.subTest(delete_after=delete_after), tempfile.TemporaryDirectory() as temp:
                data_root = Path(temp).resolve()
                project = data_root / "workspace" / "Project"
                project.mkdir(parents=True)
                target = project / "A.txt"
                target.write_text("before\n", encoding="utf-8")
                ctx = self.make_context(data_root / "local", project)
                turn_id = f"turn-post-timeout-{delete_after}"
                self.start_turn(ctx, turn_id)
                if delete_after:
                    target.unlink()
                else:
                    target.write_text("after\n", encoding="utf-8")
                event = {
                    "hook_event_name": "PostToolUse",
                    "turn_id": turn_id,
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(target)},
                }
                results: list[int] = []

                with audit.project_lock(ctx.state_root, lock_name=".state.lock"):
                    with mock.patch.object(audit, "PROJECT_LOCK_WAIT_SECONDS", 0.05):
                        thread = threading.Thread(
                            target=lambda: results.append(audit.finish_tool(ctx, event))
                        )
                        thread.start()
                        thread.join(timeout=2)
                        self.assertFalse(thread.is_alive())
                self.assertEqual(results, [1])
                result = self.finalize(ctx, turn_id)
                self.assertEqual(result["status"], "committed")
                self.assertEqual(result["auditCoverage"], "partial")
                self.assertIn(
                    "state-lock-timeout-unowned", result["coverageWarnings"]
                )
                note = subprocess.run(
                    [
                        ctx.git_exe,
                        "-C",
                        str(project),
                        "notes",
                        "--ref=codex-p4-diff",
                        "show",
                        str(result["commit"]),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                change = json.loads(note)["files"][0]
                self.assertEqual(change["action"], "unknown")
                self.assertFalse(change["textPatch"])

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

    def test_successful_turn_releases_path_for_another_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }

            self.start_turn(first, "turn-1")
            self.assertEqual(audit.prepare_tool(first, event), 1)
            target.write_text("after\n", encoding="utf-8")
            self.assertEqual(self.finalize(first, "turn-1")["status"], "committed")

            self.start_turn(second, "turn-2")
            self.assertEqual(audit.prepare_tool(second, event), 1)

    def test_no_change_turn_releases_path_for_another_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("same\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }

            self.start_turn(first, "turn-1")
            self.assertEqual(audit.prepare_tool(first, event), 1)
            self.assertEqual(self.finalize(first, "turn-1")["status"], "no-changes")

            self.start_turn(second, "turn-2")
            self.assertEqual(audit.prepare_tool(second, event), 1)

    def test_failed_turn_releases_path_and_next_prompt_retries_frozen_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }

            self.start_turn(first, "turn-1")
            self.assertEqual(audit.prepare_tool(first, event), 1)
            target.write_text("after\n", encoding="utf-8")
            original_run = bridge.run_command
            failed = False

            def fail_first_branch_update(command: list[str], *args: object, **kwargs: object):
                nonlocal failed
                if (
                    not failed
                    and "update-ref" in command
                    and f"refs/heads/{first.branch}" in command
                ):
                    failed = True
                    raise bridge.CodexP4DiffError("injected update-ref failure")
                return original_run(command, *args, **kwargs)

            with mock.patch.object(bridge, "run_command", side_effect=fail_first_branch_update):
                with self.assertRaisesRegex(audit.AuditError, "将自动重试"):
                    self.finalize(first, "turn-1")

            registry = json.loads(audit.ownership_path(first).read_text(encoding="utf-8"))
            self.assertNotIn("A.txt", registry["files"])
            transaction = json.loads(
                audit.transaction_path(first, "turn-1").read_text(encoding="utf-8")
            )
            self.assertEqual(transaction["phase"], "prepared")
            target.write_text("later worktree content\n", encoding="utf-8")

            prompt_event = {
                "hook_event_name": "UserPromptSubmit",
                "turn_id": "turn-2",
                "prompt": "Continue with another change",
            }
            with mock.patch.object(bridge, "readonly_fstat", return_value=None):
                self.assertEqual(audit.record_prompt(first, prompt_event), "turn-2")
            self.assertEqual(prompt_event["_codexP4DiffRecovery"]["status"], "committed")
            session = audit.load_session(first)
            self.assertEqual(session["activeTurnId"], "turn-2")
            self.assertEqual(session["turns"]["turn-1"]["status"], "committed")
            committed = subprocess.run(
                [first.git_exe, "-C", str(project), "show", f"{first.branch}:A.txt"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(committed, "after\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "later worktree content\n")
            self.assertEqual(audit.prepare_tool(first, event), 1)

    def test_failed_recovery_remains_queued_for_a_later_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project, "session-retry")
            self.start_turn(ctx, "turn-1")
            audit.record_before_path(ctx, "turn-1", target)
            target.write_text("after\n", encoding="utf-8")
            original_run = bridge.run_command

            def fail_update(command: list[str], *args: object, **kwargs: object):
                if "update-ref" in command and f"refs/heads/{ctx.branch}" in command:
                    raise bridge.CodexP4DiffError("injected persistent update-ref failure")
                return original_run(command, *args, **kwargs)

            with mock.patch.object(bridge, "run_command", side_effect=fail_update):
                with self.assertRaisesRegex(audit.AuditError, "将自动重试"):
                    self.finalize(ctx, "turn-1")
                second_prompt = {
                    "hook_event_name": "UserPromptSubmit",
                    "turn_id": "turn-2",
                    "prompt": "Continue",
                }
                audit.record_prompt(ctx, second_prompt)
            self.assertEqual(
                second_prompt["_codexP4DiffRecovery"]["status"],
                "audit-failed-released",
            )
            session = audit.load_session(ctx)
            self.assertIn("turn-1", session["pendingRecoveryTurnIds"])

            self.assertEqual(self.finalize(ctx, "turn-2")["status"], "no-changes")
            third_prompt = {
                "hook_event_name": "UserPromptSubmit",
                "turn_id": "turn-3",
                "prompt": "Continue again",
            }
            audit.record_prompt(ctx, third_prompt)
            self.assertEqual(third_prompt["_codexP4DiffRecovery"]["status"], "committed")
            session = audit.load_session(ctx)
            self.assertNotIn("turn-1", session["pendingRecoveryTurnIds"])
            self.assertEqual(session["turns"]["turn-1"]["status"], "committed")

    def test_schema7_migration_uses_active_manifest_and_keeps_pending_add(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            stale_existing = project / "Old.txt"
            pending_add = project / "New.txt"
            stale_existing.write_text("content\n", encoding="utf-8")
            legacy = self.make_context(data_root / "local", project, "legacy-session")
            self.start_turn(legacy, "legacy-turn")
            before = audit.record_before_path(legacy, "legacy-turn", pending_add)
            self.assertFalse(before["exists"])
            now = audit.time.time()
            audit.atomic_write_json(
                audit.ownership_path(legacy),
                {
                    "schemaVersion": 6,
                    "files": {
                        "Old.txt": {
                            "sessionId": legacy.session_id,
                            "updatedAt": audit.utc_now(),
                            "updatedEpoch": now,
                        },
                        "New.txt": {
                            "sessionId": legacy.session_id,
                            "updatedAt": audit.utc_now(),
                            "updatedEpoch": now,
                        },
                    }
                },
            )

            registry, migrated = audit.read_ownership_registry(legacy)
            self.assertTrue(migrated)
            self.assertEqual(registry["schemaVersion"], audit.OWNERSHIP_SCHEMA_VERSION)
            self.assertNotIn("Old.txt", registry["files"])
            self.assertEqual(
                registry["files"][audit.ownership_key("New.txt")]["turnId"],
                "legacy-turn",
            )
            self.assertGreater(
                registry["files"][audit.ownership_key("New.txt")]["leaseUntilEpoch"],
                now,
            )
            audit.atomic_write_json(audit.ownership_path(legacy), registry)
            normalized_again, migrated_again = audit.read_ownership_registry(legacy)
            self.assertFalse(migrated_again)
            self.assertEqual(normalized_again, registry)

    def test_expired_lease_is_reaped_without_waiting_twenty_four_hours(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("content\n", encoding="utf-8")
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
            registry = json.loads(audit.ownership_path(first).read_text(encoding="utf-8"))
            registry["files"][audit.ownership_key("A.txt")]["leaseUntilEpoch"] = (
                audit.time.time() - 1
            )
            audit.atomic_write_json(audit.ownership_path(first), registry)

            self.assertEqual(audit.prepare_tool(second, event), 1)
            updated = json.loads(audit.ownership_path(second).read_text(encoding="utf-8"))
            self.assertEqual(
                updated["files"][audit.ownership_key("A.txt")]["sessionId"],
                second.session_id,
            )

    def test_ended_or_mismatched_owner_is_not_live(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("content\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            self.start_turn(first, "turn-1")
            event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }
            self.assertEqual(audit.prepare_tool(first, event), 1)
            registry = json.loads(audit.ownership_path(first).read_text(encoding="utf-8"))
            owner = registry["files"][audit.ownership_key("A.txt")]
            self.assertTrue(audit.owner_record_is_live(first, "A.txt", owner, audit.time.time()))

            session = audit.load_session(first)
            session["status"] = "ended"
            audit.save_session(first, session)
            self.assertFalse(audit.owner_record_is_live(first, "A.txt", owner, audit.time.time()))
            session["status"] = "active"
            session["activeTurnId"] = "different-turn"
            audit.save_session(first, session)
            self.assertFalse(audit.owner_record_is_live(first, "A.txt", owner, audit.time.time()))

            self.start_turn(second, "turn-2")
            self.assertEqual(audit.prepare_tool(second, event), 1)

    def test_corrupt_ownership_is_quarantined_and_pretool_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("content\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-corrupt")
            registry_path = audit.ownership_path(ctx)
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            registry_path.write_text('{"files":', encoding="utf-8")
            event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }

            self.assertEqual(audit.prepare_tool(ctx, event), 1)
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["schemaVersion"], audit.OWNERSHIP_SCHEMA_VERSION)
            self.assertEqual(
                registry["files"][audit.ownership_key("A.txt")]["sessionId"],
                ctx.session_id,
            )
            self.assertTrue(list(registry_path.parent.glob("ownership.json.corrupt-*")))

    def test_invalid_ownership_field_is_quarantined_and_pretool_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("content\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-invalid-owner")
            audit.record_before_path(ctx, "turn-invalid-owner", target)
            registry_path = audit.ownership_path(ctx)
            audit.atomic_write_json(
                registry_path,
                {
                    "schemaVersion": audit.OWNERSHIP_SCHEMA_VERSION,
                    "files": {
                        "A.txt": {
                            "sessionId": ctx.session_id,
                            "turnId": "turn-invalid-owner",
                            "leaseUntilEpoch": "not-a-number",
                        }
                    },
                },
            )
            event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }

            self.assertEqual(audit.prepare_tool(ctx, event), 1)
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(
                registry["files"][audit.ownership_key("A.txt")]["sessionId"],
                ctx.session_id,
            )
            self.assertTrue(list(registry_path.parent.glob("ownership.json.corrupt-*")))

    def test_slow_snapshot_does_not_block_unrelated_pretool_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            first_target = project / "A.txt"
            second_target = project / "B.txt"
            first_target.write_text("first\n", encoding="utf-8")
            second_target.write_text("second\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            self.start_turn(first, "turn-1")
            self.start_turn(second, "turn-2")
            snapshot_started = threading.Event()
            release_snapshot = threading.Event()
            errors: list[BaseException] = []
            original_snapshot = audit.snapshot_metadata

            def slow_snapshot(path: Path, capture_path: Path | None = None):
                if path.resolve() == first_target.resolve():
                    snapshot_started.set()
                    if not release_snapshot.wait(5):
                        raise TimeoutError("test did not release the slow snapshot")
                return original_snapshot(path, capture_path)

            def prepare_first() -> None:
                try:
                    audit.prepare_tool(
                        first,
                        {
                            "hook_event_name": "PreToolUse",
                            "tool_name": "Write",
                            "tool_input": {"file_path": str(first_target)},
                        },
                    )
                except BaseException as exc:
                    errors.append(exc)

            second_event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(second_target)},
            }
            with mock.patch.object(audit, "snapshot_metadata", side_effect=slow_snapshot):
                thread = threading.Thread(target=prepare_first)
                thread.start()
                self.assertTrue(snapshot_started.wait(5))
                try:
                    self.assertEqual(audit.prepare_tool(second, second_event), 1)
                    self.assertNotIn(
                        "state-lock-timeout-unowned",
                        second_event.get("_codexP4DiffWarnings", []),
                    )
                    registry = json.loads(
                        audit.ownership_path(second).read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        registry["files"][audit.ownership_key("B.txt")]["sessionId"],
                        second.session_id,
                    )
                finally:
                    release_snapshot.set()
                    thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_snapshot_failure_is_partial_after_only_and_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project, "session-snapshot-fail")
            self.start_turn(ctx, "turn-snapshot-fail")
            event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }

            with mock.patch.object(
                audit, "snapshot_metadata", side_effect=PermissionError("injected snapshot failure")
            ):
                self.assertEqual(audit.prepare_tool(ctx, event), 1)
            self.assertIn(
                "before-snapshot-unavailable",
                event.get("_codexP4DiffWarnings", []),
            )
            target.write_text("after\n", encoding="utf-8")
            result = self.finalize(ctx, "turn-snapshot-fail")
            self.assertEqual(result["status"], "committed")
            self.assertEqual(result["auditCoverage"], "partial")
            self.assertIn(
                "before-snapshot-unavailable",
                result["coverageWarnings"],
            )
            note = json.loads(
                subprocess.run(
                    [
                        ctx.git_exe,
                        "-C",
                        str(project),
                        "notes",
                        "--ref=codex-p4-diff",
                        "show",
                        str(result["commit"]),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                ).stdout
            )
            self.assertEqual(note["files"][0]["historyState"], "after-only-receipt")
            committed_path = subprocess.run(
                [
                    ctx.git_exe,
                    "-C",
                    str(project),
                    "cat-file",
                    "-e",
                    f"{result['commit']}:A.txt",
                ],
                check=False,
                capture_output=True,
            )
            self.assertNotEqual(committed_path.returncode, 0)

    def test_snapshot_reservation_blocks_same_path_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            self.start_turn(first, "turn-1")
            self.start_turn(second, "turn-2")
            snapshot_started = threading.Event()
            release_snapshot = threading.Event()
            errors: list[BaseException] = []
            original_snapshot = audit.snapshot_metadata

            def slow_snapshot(path: Path, capture_path: Path | None = None):
                snapshot_started.set()
                if not release_snapshot.wait(5):
                    raise TimeoutError("test did not release the slow snapshot")
                return original_snapshot(path, capture_path)

            def prepare_first() -> None:
                try:
                    audit.prepare_tool(
                        first,
                        {
                            "hook_event_name": "PreToolUse",
                            "tool_name": "Write",
                            "tool_input": {"file_path": str(target)},
                        },
                    )
                except BaseException as exc:
                    errors.append(exc)

            event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }
            with mock.patch.object(audit, "snapshot_metadata", side_effect=slow_snapshot):
                thread = threading.Thread(target=prepare_first)
                thread.start()
                self.assertTrue(snapshot_started.wait(5))
                try:
                    with self.assertRaises(audit.OwnershipConflict):
                        audit.prepare_tool(second, event)
                finally:
                    release_snapshot.set()
                    thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_session_start_cannot_overwrite_concurrent_path_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            starter = self.make_context(data_root / "local", project, "session-start")
            writer = self.make_context(data_root / "local", project, "session-writer")
            self.start_turn(writer, "turn-write")
            registry_read = threading.Event()
            release_start = threading.Event()
            errors: list[BaseException] = []
            original_read = audit.read_ownership_registry

            def delayed_read(ctx: audit.AuditContext):
                value = original_read(ctx)
                if threading.current_thread().name == "session-start-thread":
                    registry_read.set()
                    if not release_start.wait(5):
                        raise TimeoutError("test did not release SessionStart")
                return value

            def mark_start() -> None:
                try:
                    audit.mark_session(starter, "active")
                except BaseException as exc:
                    errors.append(exc)

            def claim_path() -> None:
                try:
                    audit.prepare_tool(
                        writer,
                        {
                            "hook_event_name": "PreToolUse",
                            "tool_name": "Write",
                            "tool_input": {"file_path": str(target)},
                        },
                    )
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch.object(audit, "read_ownership_registry", side_effect=delayed_read):
                start_thread = threading.Thread(
                    target=mark_start, name="session-start-thread"
                )
                claim_thread = threading.Thread(target=claim_path, name="claim-thread")
                start_thread.start()
                self.assertTrue(registry_read.wait(5))
                claim_thread.start()
                release_start.set()
                start_thread.join(5)
                claim_thread.join(5)
            self.assertFalse(start_thread.is_alive())
            self.assertFalse(claim_thread.is_alive())
            self.assertEqual(errors, [])
            registry = json.loads(
                audit.ownership_path(writer).read_text(encoding="utf-8")
            )
            self.assertEqual(
                registry["files"][audit.ownership_key("A.txt")]["sessionId"],
                writer.session_id,
            )

    def test_concurrent_context_resolution_migrates_legacy_pointer_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            owned = data_root / "local" / "CodexP4Diff" / "Native" / "old" / "git"
            owned.mkdir(parents=True)
            (project / ".git").write_text(f"gitdir: {owned}\n", encoding="utf-8")
            p4_context = {
                "p4Exe": "p4",
                "serverAddress": "p4.example.test:1666",
                "userName": "tester",
                "client": "test-client",
                "clientRoot": str(project.parent),
                "projectRoot": str(project),
            }
            barrier = threading.Barrier(3)
            contexts: list[audit.AuditContext] = []
            errors: list[BaseException] = []

            def resolve(session_id: str) -> None:
                try:
                    barrier.wait()
                    contexts.append(
                        audit.resolve_context(
                            {"cwd": str(project), "session_id": session_id}
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            with (
                mock.patch.object(bridge, "user_data_root", return_value=data_root / "local"),
                mock.patch.object(bridge, "discover_p4_context", return_value=p4_context),
            ):
                first = threading.Thread(target=resolve, args=("session-one",))
                second = threading.Thread(target=resolve, args=("session-two",))
                first.start()
                second.start()
                barrier.wait()
                first.join(15)
                second.join(15)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(contexts), 2)
            self.assertTrue((project / ".git").is_dir())
            commit_count = subprocess.run(
                ["git", "-C", str(project), "rev-list", "--all", "--count"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(commit_count, "1")


class RecoveryAndSafetyTest(AuditTestCase):
    def test_invalid_before_manifest_is_quarantined_and_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-invalid-manifest")
            audit.record_before_path(ctx, "turn-invalid-manifest", target)
            manifest_path = audit.before_manifest_path(ctx, "turn-invalid-manifest")
            manifest_path.write_text("[]", encoding="utf-8")
            target.write_text("after\n", encoding="utf-8")

            result = self.finalize(ctx, "turn-invalid-manifest")
            self.assertEqual(result["status"], "no-changes")
            self.assertEqual(result["auditCoverage"], "partial")
            self.assertIn("before-manifest-invalid", [
                item["code"] for item in result["coverageWarnings"]
            ])
            self.assertTrue(list(manifest_path.parent.glob("before.json.corrupt-*")))

    def test_invalid_coverage_record_is_quarantined_and_committed_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-invalid-coverage")
            audit.record_before_path(ctx, "turn-invalid-coverage", target)
            coverage_path = audit.coverage_record_path(ctx, "turn-invalid-coverage")
            coverage_path.parent.mkdir(parents=True, exist_ok=True)
            coverage_path.write_text("[]", encoding="utf-8")
            target.write_text("after\n", encoding="utf-8")

            result = self.finalize(ctx, "turn-invalid-coverage")
            self.assertEqual(result["status"], "committed")
            self.assertEqual(result["auditCoverage"], "partial")
            self.assertIn("coverage-state-invalid", result["coverageWarnings"])
            self.assertTrue(list(coverage_path.parent.glob("coverage.json.corrupt-*")))

    def test_fresh_session_has_no_false_state_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            ctx = self.make_context(data_root / "local", project)

            session = audit.load_session(ctx)
            self.assertNotIn("stateWarnings", session)
            self.start_turn(ctx, "turn-fresh")
            self.assertNotIn("stateWarnings", audit.load_session(ctx))

    def test_invalid_nested_session_is_quarantined_and_turn_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-invalid-session")
            audit.record_before_path(ctx, "turn-invalid-session", target)
            target.write_text("after\n", encoding="utf-8")
            session_path = audit.session_path(ctx)
            session = json.loads(session_path.read_text(encoding="utf-8"))
            session["nextSequence"] = {"invalid": True}
            audit.atomic_write_json(session_path, session)

            result = self.finalize(ctx, "turn-invalid-session")
            self.assertEqual(result["status"], "committed")
            self.assertTrue(list(session_path.parent.glob("session.json.corrupt-*")))
            repaired = audit.load_session(ctx)
            self.assertEqual(
                repaired["turns"]["turn-invalid-session"]["status"], "committed"
            )

    def test_legacy_pointer_initialization_is_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            legacy_git = data_root / "CodexP4Diff" / "Native" / "legacy.git"
            legacy_git.mkdir(parents=True)
            (project / ".git").write_text(
                f"gitdir: {legacy_git}\n", encoding="utf-8"
            )
            p4_context = {
                "p4Exe": "p4",
                "serverAddress": "p4.example.test:1666",
                "userName": "tester",
                "client": "test-client",
                "clientRoot": str(project.parent),
                "projectRoot": str(project),
            }
            contexts: list[audit.AuditContext] = []
            errors: list[BaseException] = []
            barrier = threading.Barrier(2)

            def resolve(session_id: str) -> None:
                try:
                    barrier.wait(timeout=2)
                    contexts.append(
                        audit.resolve_context(
                            {"cwd": str(project), "session_id": session_id}
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch.object(
                bridge, "discover_p4_context", return_value=p4_context
            ), mock.patch.object(bridge, "user_data_root", return_value=data_root):
                threads = [
                    threading.Thread(target=resolve, args=(f"session-{index}",))
                    for index in range(2)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(contexts), 2)
            self.assertTrue((project / ".git").is_dir())
            marker = project / ".git" / bridge.TOOL_NAME / "project.json"
            self.assertTrue(marker.is_file())
            count = subprocess.run(
                ["git", "-C", str(project), "rev-list", "--count", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(count, "1")

    def test_project_lock_does_not_write_or_leak_empty_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_root = Path(temp).resolve()
            lock_path = state_root / ".empty.lock"

            with audit.project_lock(state_root, lock_name=lock_path.name):
                self.assertTrue(lock_path.is_file())
                self.assertEqual(lock_path.stat().st_size, 0)

            lock_path.unlink()
            self.assertFalse(lock_path.exists())

    def test_slow_stop_does_not_block_other_path_pretool(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            first_target = project / "A.txt"
            second_target = project / "B.txt"
            first_target.write_text("before a\n", encoding="utf-8")
            second_target.write_text("before b\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            self.start_turn(first, "turn-a")
            self.start_turn(second, "turn-b")
            first_event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(first_target)},
            }
            second_event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(second_target)},
            }
            audit.prepare_tool(first, first_event)
            first_target.write_text("after a\n", encoding="utf-8")
            entered = threading.Event()
            release = threading.Event()
            results: list[dict[str, object]] = []

            def slow_fstat(*_args: object, **_kwargs: object) -> None:
                entered.set()
                release.wait(timeout=3)
                return None

            def stop_first() -> None:
                results.append(
                    audit.finalize_turn(
                        first, {"hook_event_name": "Stop", "turn_id": "turn-a"}
                    )
                )

            with mock.patch.object(bridge, "readonly_fstat", side_effect=slow_fstat):
                thread = threading.Thread(target=stop_first)
                thread.start()
                self.assertTrue(entered.wait(timeout=3))
                started = audit.time.monotonic()
                self.assertEqual(audit.prepare_tool(second, second_event), 1)
                elapsed = audit.time.monotonic() - started
                release.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertLess(elapsed, 1.0)
            self.assertEqual(results[0]["status"], "committed")
            self.assertEqual(self.finalize(second, "turn-b")["status"], "no-changes")

    def test_invalid_session_root_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            ctx = self.make_context(data_root / "local", project)
            session_path = audit.session_path(ctx)
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text("[]", encoding="utf-8")

            session = audit.load_session(ctx)
            self.assertEqual(session["sessionId"], ctx.session_id)
            self.assertEqual(session["turns"], {})
            self.assertTrue(list(session_path.parent.glob("session.json.corrupt-*")))

    def test_invalid_session_root_rebuilds_pending_transaction_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-lost-session")
            audit.record_before_path(ctx, "turn-lost-session", target)
            target.write_text("after\n", encoding="utf-8")
            original_run = bridge.run_command
            failed = False

            def fail_first_update(command: list[str], *args: object, **kwargs: object):
                nonlocal failed
                if not failed and "update-ref" in command:
                    failed = True
                    raise bridge.CodexP4DiffError("injected update-ref failure")
                return original_run(command, *args, **kwargs)

            with mock.patch.object(bridge, "run_command", side_effect=fail_first_update):
                with self.assertRaisesRegex(audit.AuditError, "将自动重试"):
                    self.finalize(ctx, "turn-lost-session")
            audit.session_path(ctx).write_text("[]", encoding="utf-8")
            prompt_event = {
                "hook_event_name": "UserPromptSubmit",
                "turn_id": "turn-after-recovery",
                "prompt": "Continue",
            }

            audit.record_prompt(ctx, prompt_event)
            self.assertEqual(prompt_event["_codexP4DiffRecovery"]["status"], "committed")
            session = audit.load_session(ctx)
            self.assertEqual(session["turns"]["turn-lost-session"]["status"], "committed")
            self.assertNotIn("turn-lost-session", session["pendingRecoveryTurnIds"])
            self.assertEqual(
                subprocess.run(
                    [ctx.git_exe, "-C", str(project), "rev-list", "--count", ctx.branch],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                "2",
            )

    def test_complete_transaction_rebuild_does_not_rewrite_note_or_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-complete")
            audit.record_before_path(ctx, "turn-complete", target)
            target.write_text("after\n", encoding="utf-8")
            result = self.finalize(ctx, "turn-complete")
            commit = str(result["commit"])

            note_command = [
                ctx.git_exe,
                "-C",
                str(project),
                "notes",
                "--ref=codex-p4-diff",
                "show",
                commit,
            ]
            note_before = subprocess.run(
                note_command, check=True, capture_output=True
            ).stdout
            target.write_text("user staged\n", encoding="utf-8")
            subprocess.run(
                [ctx.git_exe, "-C", str(project), "add", "--", "A.txt"],
                check=True,
                capture_output=True,
            )
            staged_before = audit.index_entries(ctx, ["A.txt"])["A.txt"]
            audit.session_path(ctx).write_text("[]", encoding="utf-8")
            prompt_event = {
                "hook_event_name": "UserPromptSubmit",
                "turn_id": "turn-after-complete",
                "prompt": "Continue",
            }

            audit.record_prompt(ctx, prompt_event)
            self.assertEqual(prompt_event["_codexP4DiffRecovery"]["status"], "committed")
            self.assertEqual(
                subprocess.run(note_command, check=True, capture_output=True).stdout,
                note_before,
            )
            self.assertEqual(audit.index_entries(ctx, ["A.txt"])["A.txt"], staged_before)
            self.assertEqual(
                subprocess.run(
                    [ctx.git_exe, "-C", str(project), "rev-list", "--count", ctx.branch],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                "2",
            )

    def test_frozen_transaction_resumes_before_worktree_no_change_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-frozen")
            audit.record_before_path(ctx, "turn-frozen", target)
            target.write_text("after\n", encoding="utf-8")
            original_run = bridge.run_command
            failed = False

            def fail_first_update(command: list[str], *args: object, **kwargs: object):
                nonlocal failed
                if not failed and "update-ref" in command:
                    failed = True
                    raise bridge.CodexP4DiffError("injected update-ref failure")
                return original_run(command, *args, **kwargs)

            with mock.patch.object(bridge, "run_command", side_effect=fail_first_update):
                with self.assertRaisesRegex(audit.AuditError, "将自动重试"):
                    self.finalize(ctx, "turn-frozen")
            target.write_text("before\n", encoding="utf-8")

            result = self.finalize(ctx, "turn-frozen")
            self.assertEqual(result["status"], "committed")
            committed = subprocess.run(
                [ctx.git_exe, "-C", str(project), "show", f"{ctx.branch}:A.txt"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(committed, "after\n")

    def test_history_timeout_before_transaction_is_recovered_by_later_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            self.start_turn(first, "turn-1")
            audit.record_before_path(first, "turn-1", target)
            target.write_text("after\n", encoding="utf-8")
            lock_acquired = threading.Event()
            release_lock = threading.Event()

            def hold_history_lock() -> None:
                with audit.project_lock(first.state_root):
                    lock_acquired.set()
                    release_lock.wait(5)

            holder = threading.Thread(target=hold_history_lock)
            holder.start()
            self.assertTrue(lock_acquired.wait(5))
            try:
                with self.assertRaises(audit.ProjectLockTimeout):
                    audit.finalize_turn(
                        first,
                        {
                            "hook_event_name": "Stop",
                            "turn_id": "turn-1",
                            "_historyLockWaitSeconds": 0.05,
                        },
                    )
                self.assertFalse(audit.transaction_path(first, "turn-1").exists())
                marker = audit.recovery_marker_path(first, first.session_id, "turn-1")
                self.assertTrue(marker.is_file())
                next_prompt = {
                    "hook_event_name": "UserPromptSubmit",
                    "turn_id": "turn-2",
                    "prompt": "Continue while history is busy",
                }
                audit.record_prompt(first, next_prompt)
                self.assertEqual(
                    next_prompt["_codexP4DiffRecovery"]["status"],
                    "recovery-deferred-history-busy",
                )
                session = audit.load_session(first)
                self.assertIn("turn-1", session["pendingRecoveryTurnIds"])
                self.assertEqual(session["activeTurnId"], "turn-2")
                target.write_text("later worktree value\n", encoding="utf-8")
            finally:
                release_lock.set()
                holder.join(5)
            self.assertFalse(holder.is_alive())

            later_prompt = {
                "hook_event_name": "UserPromptSubmit",
                "turn_id": "turn-new-session",
                "prompt": "Recover prior session",
            }
            audit.record_prompt(second, later_prompt)
            self.assertEqual(
                later_prompt["_codexP4DiffRecovery"]["status"], "committed"
            )
            recovered = audit.load_session(first)
            self.assertEqual(recovered["turns"]["turn-1"]["status"], "committed")
            self.assertFalse(
                audit.recovery_marker_path(first, first.session_id, "turn-1").exists()
            )
            self.assertEqual(
                subprocess.run(
                    [first.git_exe, "-C", str(project), "show", f"{first.branch}:A.txt"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "after\n",
            )

    def test_history_timeout_releases_claim_but_recovers_sealed_after(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            self.start_turn(first, "turn-1")
            audit.prepare_tool(
                first,
                {
                    "hook_event_name": "PreToolUse",
                    "turn_id": "turn-1",
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(target)},
                },
            )
            target.write_text("frozen after\n", encoding="utf-8")
            lock_acquired = threading.Event()
            release_lock = threading.Event()

            def hold_history_lock() -> None:
                with audit.project_lock(first.state_root):
                    lock_acquired.set()
                    release_lock.wait(10)

            holder = threading.Thread(target=hold_history_lock)
            holder.start()
            self.assertTrue(lock_acquired.wait(5))
            try:
                with self.assertRaises(audit.ProjectLockTimeout):
                    audit.finalize_turn(
                        first,
                        {
                            "hook_event_name": "Stop",
                            "turn_id": "turn-1",
                            "_historyLockWaitSeconds": 0.05,
                        },
                    )
                deferred = json.loads(
                    audit.deferred_after_record_path(first, "turn-1").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertTrue(deferred["ownershipSealedAt"])
                self.assertEqual(
                    deferred["files"]["A.txt"]["ownershipAttribution"], "exact"
                )
                registry = json.loads(
                    audit.ownership_path(first).read_text(encoding="utf-8")
                )
                self.assertNotIn(audit.ownership_key("A.txt"), registry["files"])

                self.start_turn(second, "turn-2")
                self.assertEqual(
                    audit.prepare_tool(
                        second,
                        {
                            "hook_event_name": "PreToolUse",
                            "turn_id": "turn-2",
                            "tool_name": "Write",
                            "tool_input": {"file_path": str(target)},
                        },
                    ),
                    1,
                )
                target.write_text("later second-session content\n", encoding="utf-8")
            finally:
                release_lock.set()
                holder.join(10)
            self.assertFalse(holder.is_alive())

            first_result = self.finalize(first, "turn-1")
            self.assertEqual(
                subprocess.run(
                    [
                        first.git_exe,
                        "-C",
                        str(project),
                        "show",
                        f"{first_result['commit']}:A.txt",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "frozen after\n",
            )
            first_note = json.loads(
                subprocess.run(
                    [
                        first.git_exe,
                        "-C",
                        str(project),
                        "notes",
                        "--ref=codex-p4-diff",
                        "show",
                        str(first_result["commit"]),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            self.assertEqual(
                first_note["files"][0]["ownershipAttribution"], "exact"
            )
            registry = json.loads(
                audit.ownership_path(first).read_text(encoding="utf-8")
            )
            self.assertEqual(
                registry["files"][audit.ownership_key("A.txt")]["turnId"], "turn-2"
            )

            second_result = self.finalize(second, "turn-2")
            self.assertEqual(
                subprocess.run(
                    [
                        second.git_exe,
                        "-C",
                        str(project),
                        "show",
                        f"{second_result['commit']}:A.txt",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "later second-session content\n",
            )

    def test_concurrent_deferred_capture_cannot_overwrite_sealed_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            turn_id = "turn-concurrent-deferred"
            self.start_turn(ctx, turn_id)
            self.assertEqual(
                audit.prepare_tool(
                    ctx,
                    {
                        "hook_event_name": "PreToolUse",
                        "turn_id": turn_id,
                        "tool_name": "Write",
                        "tool_input": {"file_path": str(target)},
                    },
                ),
                1,
            )
            frozen_data = b"frozen after\n"
            target.write_bytes(frozen_data)

            first_snapshot_ready = threading.Event()
            first_capture_done = threading.Event()
            allow_second_snapshot = threading.Event()
            capture_barrier = threading.Barrier(2)
            errors: list[BaseException] = []
            original_after_snapshot = audit.after_snapshot

            def controlled_after_snapshot(path: Path) -> tuple[dict, bytes | None]:
                result = original_after_snapshot(path)
                name = threading.current_thread().name
                if name == "deferred-first":
                    first_snapshot_ready.set()
                try:
                    capture_barrier.wait(timeout=1)
                except threading.BrokenBarrierError:
                    pass
                if name == "deferred-second":
                    if not allow_second_snapshot.wait(5):
                        raise TimeoutError("second deferred capture was not released")
                return result

            def capture() -> None:
                try:
                    audit.capture_deferred_after(ctx, turn_id)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    if threading.current_thread().name == "deferred-first":
                        first_capture_done.set()

            with mock.patch.object(
                audit, "after_snapshot", side_effect=controlled_after_snapshot
            ):
                first = threading.Thread(target=capture, name="deferred-first")
                second = threading.Thread(target=capture, name="deferred-second")
                first.start()
                self.assertTrue(first_snapshot_ready.wait(5))
                target.write_text("later content\n", encoding="utf-8")
                second.start()
                try:
                    self.assertTrue(first_capture_done.wait(5))
                    with audit.project_lock(
                        ctx.state_root,
                        lock_name=".state.lock",
                        wait_seconds=audit.PROJECT_LOCK_WAIT_SECONDS,
                    ):
                        session = audit.load_session(ctx)
                        self.assertTrue(
                            audit.seal_deferred_after_ownership_locked(ctx, turn_id)
                        )
                        audit.release_turn_paths_locked(ctx, session, turn_id)
                        audit.save_session(ctx, session)
                finally:
                    allow_second_snapshot.set()
                    first.join(5)
                    second.join(5)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            record = audit.load_deferred_after_record(ctx, turn_id)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertTrue(record["ownershipSealedAt"])
            entry = record["files"]["A.txt"]
            self.assertEqual(entry["ownershipAttribution"], "exact")
            self.assertEqual(entry["after"]["sha256"], audit.sha256_bytes(frozen_data))
            snapshot_path = Path(entry["after"]["snapshotPath"])
            self.assertEqual(snapshot_path.read_bytes(), frozen_data)

    def test_deferred_lock_timeout_keeps_claim_until_snapshot_is_sealed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            turn_id = "turn-slow-deferred"
            self.start_turn(ctx, turn_id)
            self.assertEqual(
                audit.prepare_tool(
                    ctx,
                    {
                        "hook_event_name": "PreToolUse",
                        "turn_id": turn_id,
                        "tool_name": "Write",
                        "tool_input": {"file_path": str(target)},
                    },
                ),
                1,
            )
            target.write_text("frozen after\n", encoding="utf-8")
            history_lock_acquired = threading.Event()
            release_history_lock = threading.Event()
            first_snapshot_ready = threading.Event()
            release_first_snapshot = threading.Event()
            first_errors: list[BaseException] = []
            original_after_snapshot = audit.after_snapshot

            def hold_history_lock() -> None:
                with audit.project_lock(ctx.state_root):
                    history_lock_acquired.set()
                    release_history_lock.wait(10)

            def slow_after_snapshot(path: Path) -> tuple[dict, bytes | None]:
                result = original_after_snapshot(path)
                first_snapshot_ready.set()
                if not release_first_snapshot.wait(5):
                    raise TimeoutError("first deferred snapshot was not released")
                return result

            event = {
                "hook_event_name": "Stop",
                "turn_id": turn_id,
                "_historyLockWaitSeconds": 0.01,
            }

            def first_finalize() -> None:
                try:
                    audit.finalize_turn(ctx, event)
                except BaseException as exc:
                    first_errors.append(exc)

            holder = threading.Thread(target=hold_history_lock)
            holder.start()
            self.assertTrue(history_lock_acquired.wait(5))
            first = threading.Thread(target=first_finalize)
            try:
                with mock.patch.object(
                    audit, "PROJECT_LOCK_WAIT_SECONDS", 0.05
                ), mock.patch.object(
                    audit, "after_snapshot", side_effect=slow_after_snapshot
                ):
                    first.start()
                    self.assertTrue(first_snapshot_ready.wait(5))
                    with self.assertRaises(audit.ProjectLockTimeout):
                        audit.finalize_turn(ctx, event)
                    registry = json.loads(
                        audit.ownership_path(ctx).read_text(encoding="utf-8")
                    )
                    self.assertIn(audit.ownership_key("A.txt"), registry["files"])
                    self.assertTrue(
                        audit.recovery_marker_path(ctx, ctx.session_id, turn_id).is_file()
                    )
                    release_first_snapshot.set()
                    first.join(5)
            finally:
                release_first_snapshot.set()
                release_history_lock.set()
                first.join(5)
                holder.join(5)

            self.assertFalse(first.is_alive())
            self.assertFalse(holder.is_alive())
            self.assertEqual(len(first_errors), 1)
            self.assertIsInstance(first_errors[0], audit.ProjectLockTimeout)
            deferred = audit.load_deferred_after_record(ctx, turn_id)
            self.assertIsNotNone(deferred)
            assert deferred is not None
            self.assertTrue(deferred["ownershipSealedAt"])
            self.assertEqual(
                deferred["files"]["A.txt"]["ownershipAttribution"], "exact"
            )
            registry = json.loads(
                audit.ownership_path(ctx).read_text(encoding="utf-8")
            )
            self.assertNotIn(audit.ownership_key("A.txt"), registry["files"])
            result = self.finalize(ctx, turn_id)
            self.assertEqual(
                subprocess.run(
                    [ctx.git_exe, "-C", str(project), "show", f"{result['commit']}:A.txt"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "frozen after\n",
            )

    def test_deferred_after_failure_never_reads_later_worktree_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-deferred-unavailable")
            audit.record_before_path(ctx, "turn-deferred-unavailable", target)
            target.write_text("after at stop\n", encoding="utf-8")
            lock_acquired = threading.Event()
            release_lock = threading.Event()

            def hold_history_lock() -> None:
                with audit.project_lock(ctx.state_root):
                    lock_acquired.set()
                    release_lock.wait(5)

            holder = threading.Thread(target=hold_history_lock)
            holder.start()
            self.assertTrue(lock_acquired.wait(5))
            try:
                with mock.patch.object(
                    audit, "after_snapshot", side_effect=OSError("injected snapshot failure")
                ):
                    with self.assertRaises(audit.ProjectLockTimeout):
                        audit.finalize_turn(
                            ctx,
                            {
                                "hook_event_name": "Stop",
                                "turn_id": "turn-deferred-unavailable",
                                "_historyLockWaitSeconds": 0.05,
                            },
                        )
            finally:
                release_lock.set()
                holder.join(5)
            self.assertFalse(holder.is_alive())
            frozen = json.loads(
                audit.deferred_after_record_path(
                    ctx, "turn-deferred-unavailable"
                ).read_text(encoding="utf-8")
            )
            self.assertIsNone(frozen["files"]["A.txt"]["after"]["exists"])

            target.write_text("later foreign content\n", encoding="utf-8")
            with mock.patch.object(
                audit,
                "after_snapshot",
                side_effect=AssertionError("recovery must not read the live worktree"),
            ):
                result = self.finalize(ctx, "turn-deferred-unavailable")

            self.assertEqual(result["status"], "committed")
            self.assertEqual(result["auditCoverage"], "partial")
            self.assertIn(
                "deferred-after-snapshot-unavailable", result["coverageWarnings"]
            )
            note = json.loads(
                subprocess.run(
                    [
                        ctx.git_exe,
                        "-C",
                        str(project),
                        "notes",
                        "--ref=codex-p4-diff",
                        "show",
                        str(result["commit"]),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            change = note["files"][0]
            self.assertEqual(change["action"], "unknown")
            self.assertEqual(
                change["ownershipAttribution"], "deferred-after-unavailable"
            )
            self.assertEqual(
                change["historyState"],
                "deferred-after-unavailable-receipt-only",
            )
            self.assertFalse(change["textPatch"])
            self.assertIsNone(change["after"]["sha256"])
            self.assertNotEqual(
                subprocess.run(
                    [
                        ctx.git_exe,
                        "-C",
                        str(project),
                        "cat-file",
                        "-e",
                        f"{result['commit']}:A.txt",
                    ],
                    capture_output=True,
                ).returncode,
                0,
            )

    def test_recovery_terminal_merge_preserves_concurrent_prompt_and_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target_a = project / "A.txt"
            target_b = project / "B.txt"
            target_a.write_text("before a\n", encoding="utf-8")
            target_b.write_text("before b\n", encoding="utf-8")
            first = self.make_context(data_root / "local", project, "session-one")
            second = self.make_context(data_root / "local", project, "session-two")
            self.start_turn(first, "turn-1")
            audit.prepare_tool(
                first,
                {
                    "hook_event_name": "PreToolUse",
                    "turn_id": "turn-1",
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(target_a)},
                },
            )
            target_a.write_text("after a\n", encoding="utf-8")
            with audit.project_lock(
                first.state_root,
                lock_name=".state.lock",
                wait_seconds=audit.PROJECT_LOCK_WAIT_SECONDS,
            ):
                session = audit.load_session(first)
                audit.queue_turn_recovery(first, session, "turn-1", "test-recovery")
                audit.save_session(first, session)

            recovery_ctx = audit.recovery_context(second, first.session_id)
            original_renew = audit.renew_turn_ownership
            recovery_loaded = threading.Event()
            continue_recovery = threading.Event()
            results: list[dict[str, object]] = []
            errors: list[BaseException] = []

            def block_after_session_load(
                ctx: audit.AuditContext, turn_id: str
            ) -> dict[str, str]:
                if ctx.session_id == first.session_id and turn_id == "turn-1":
                    recovery_loaded.set()
                    if not continue_recovery.wait(5):
                        raise AssertionError("timed out waiting to continue recovery")
                return original_renew(ctx, turn_id)

            def recover_old_turn() -> None:
                try:
                    results.append(
                        audit.finalize_turn(
                            recovery_ctx,
                            {
                                "hook_event_name": "Stop",
                                "session_id": first.session_id,
                                "turn_id": "turn-1",
                            },
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch.object(
                audit, "renew_turn_ownership", side_effect=block_after_session_load
            ), mock.patch.object(bridge, "readonly_fstat", return_value=None):
                worker = threading.Thread(target=recover_old_turn)
                worker.start()
                self.assertTrue(recovery_loaded.wait(5))
                try:
                    prompt_event = {
                        "hook_event_name": "UserPromptSubmit",
                        "turn_id": "turn-2",
                        "prompt": "Start a new turn while recovery is finishing",
                    }
                    audit.record_prompt(first, prompt_event)
                    audit.prepare_tool(
                        first,
                        {
                            "hook_event_name": "PreToolUse",
                            "turn_id": "turn-2",
                            "tool_name": "Write",
                            "tool_input": {"file_path": str(target_b)},
                        },
                    )
                    self.assertIn("B.txt", audit.load_session(first)["ownedFiles"])
                finally:
                    continue_recovery.set()
                    worker.join(10)

            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results[0]["status"], "committed")
            final_session = audit.load_session(first)
            self.assertEqual(final_session["activeTurnId"], "turn-2")
            self.assertEqual(final_session["turns"]["turn-1"]["status"], "committed")
            self.assertEqual(final_session["turns"]["turn-2"]["status"], "active")
            self.assertTrue(audit.prompt_record_path(first, "turn-2").is_file())
            self.assertEqual(final_session["nextSequence"], 2)
            registry = json.loads(
                audit.ownership_path(first).read_text(encoding="utf-8")
            )
            self.assertNotIn(audit.ownership_key("A.txt"), registry["files"])
            self.assertEqual(
                registry["files"][audit.ownership_key("B.txt")]["turnId"], "turn-2"
            )
            self.assertIn("B.txt", final_session["ownedFiles"])
            self.assertFalse(
                audit.recovery_marker_path(first, first.session_id, "turn-1").exists()
            )

    def test_prepared_transaction_rebases_after_different_path_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-rebase-safe")
            audit.record_before_path(ctx, "turn-rebase-safe", target)
            target.write_text("after\n", encoding="utf-8")
            original_run = bridge.run_command
            failed = False

            def fail_first_update(command: list[str], *args: object, **kwargs: object):
                nonlocal failed
                if not failed and "update-ref" in command:
                    failed = True
                    raise bridge.CodexP4DiffError("injected update-ref failure")
                return original_run(command, *args, **kwargs)

            with mock.patch.object(bridge, "run_command", side_effect=fail_first_update):
                with self.assertRaises(audit.AuditError):
                    self.finalize(ctx, "turn-rebase-safe")
            concurrent = self.advance_history(ctx, {"B.txt": b"other\n"})

            result = self.finalize(ctx, "turn-rebase-safe")
            self.assertEqual(
                subprocess.run(
                    [ctx.git_exe, "-C", str(project), "rev-parse", f"{result['commit']}^"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                concurrent,
            )
            self.assertEqual(
                subprocess.run(
                    [ctx.git_exe, "-C", str(project), "show", f"{result['commit']}:A.txt"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "after\n",
            )

    def test_prepared_transaction_becomes_receipt_after_same_path_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-rebase-drift")
            audit.record_before_path(ctx, "turn-rebase-drift", target)
            target.write_text("after\n", encoding="utf-8")
            original_run = bridge.run_command
            failed = False

            def fail_first_update(command: list[str], *args: object, **kwargs: object):
                nonlocal failed
                if not failed and "update-ref" in command:
                    failed = True
                    raise bridge.CodexP4DiffError("injected update-ref failure")
                return original_run(command, *args, **kwargs)

            with mock.patch.object(bridge, "run_command", side_effect=fail_first_update):
                with self.assertRaises(audit.AuditError):
                    self.finalize(ctx, "turn-rebase-drift")
            concurrent = self.advance_history(ctx, {"A.txt": b"other\n"})

            result = self.finalize(ctx, "turn-rebase-drift")
            transaction = json.loads(
                audit.transaction_path(ctx, "turn-rebase-drift").read_text(encoding="utf-8")
            )
            self.assertEqual(transaction["parent"], concurrent)
            self.assertEqual(transaction["updateEntries"], {})
            self.assertEqual(
                transaction["auditRecord"]["files"][0]["historyState"],
                "recovery-drifted-receipt-only",
            )
            self.assertEqual(
                subprocess.run(
                    [ctx.git_exe, "-C", str(project), "show", f"{result['commit']}:A.txt"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "other\n",
            )

    def test_ref_updated_transaction_recovers_once_and_repairs_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-recover")
            audit.record_before_path(ctx, "turn-recover", target)
            target.write_text("after\n", encoding="utf-8")
            original_attach = audit.attach_audit_note
            failed = False

            def fail_first_note(*args: object, **kwargs: object) -> None:
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("injected note failure")
                original_attach(*args, **kwargs)

            with mock.patch.object(audit, "attach_audit_note", side_effect=fail_first_note):
                with self.assertRaisesRegex(audit.AuditError, "将自动重试"):
                    self.finalize(ctx, "turn-recover")

            transaction = json.loads(
                audit.transaction_path(ctx, "turn-recover").read_text(encoding="utf-8")
            )
            self.assertEqual(transaction["phase"], "ref-updated")
            candidate = transaction["candidateCommit"]
            result = self.finalize(ctx, "turn-recover")
            self.assertEqual(result["commit"], candidate)
            self.assertEqual(self.finalize(ctx, "turn-recover")["status"], "already-committed")
            count = subprocess.run(
                [ctx.git_exe, "-C", str(project), "rev-list", "--count", ctx.branch],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(count, "2")
            note = subprocess.run(
                [
                    ctx.git_exe,
                    "-C",
                    str(project),
                    "notes",
                    "--ref=codex-p4-diff",
                    "show",
                    candidate,
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(json.loads(note)["turnId"], "turn-recover")
            final_transaction = json.loads(
                audit.transaction_path(ctx, "turn-recover").read_text(encoding="utf-8")
            )
            self.assertEqual(final_transaction["phase"], "complete")

    def test_ref_updated_transaction_recovers_under_descendant_head(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-descendant")
            audit.record_before_path(ctx, "turn-descendant", target)
            target.write_text("after\n", encoding="utf-8")
            original_attach = audit.attach_audit_note
            failed = False

            def fail_first_note(*args: object, **kwargs: object) -> None:
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("injected note failure")
                original_attach(*args, **kwargs)

            with mock.patch.object(audit, "attach_audit_note", side_effect=fail_first_note):
                with self.assertRaises(audit.AuditError):
                    self.finalize(ctx, "turn-descendant")
            transaction = json.loads(
                audit.transaction_path(ctx, "turn-descendant").read_text(encoding="utf-8")
            )
            candidate = transaction["candidateCommit"]
            descendant = self.advance_history(ctx, {"B.txt": b"later\n"})

            result = self.finalize(ctx, "turn-descendant")
            self.assertEqual(result["commit"], candidate)
            self.assertEqual(audit.current_history_head(ctx), descendant)
            self.assertTrue(audit.commit_is_ancestor(ctx, candidate, descendant))
            self.assertEqual(self.finalize(ctx, "turn-descendant")["status"], "already-committed")
            self.assertEqual(
                subprocess.run(
                    [ctx.git_exe, "-C", str(project), "rev-list", "--count", ctx.branch],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                "3",
            )

    def test_corrupt_transaction_is_quarantined_and_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-corrupt")
            audit.record_before_path(ctx, "turn-corrupt", target)
            target.write_text("after\n", encoding="utf-8")
            transaction_path = audit.transaction_path(ctx, "turn-corrupt")
            transaction_path.write_text('{"transactionId":', encoding="utf-8")

            result = self.finalize(ctx, "turn-corrupt")
            self.assertEqual(result["status"], "committed")
            self.assertTrue(list(transaction_path.parent.glob("transaction.json.corrupt-*")))
            count = subprocess.run(
                [ctx.git_exe, "-C", str(project), "rev-list", "--count", ctx.branch],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(count, "2")

    def test_structurally_invalid_transaction_is_quarantined_and_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-invalid-transaction")
            audit.record_before_path(ctx, "turn-invalid-transaction", target)
            target.write_text("after\n", encoding="utf-8")
            transaction_path = audit.transaction_path(ctx, "turn-invalid-transaction")
            audit.atomic_write_json(
                transaction_path, {"transactionId": "malformed-but-valid-json"}
            )

            result = self.finalize(ctx, "turn-invalid-transaction")
            self.assertEqual(result["status"], "committed")
            self.assertTrue(list(transaction_path.parent.glob("transaction.json.corrupt-*")))

    def test_index_sync_warning_surfaces_in_final_result_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-index-warning")
            audit.record_before_path(ctx, "turn-index-warning", target)
            target.write_text("after\n", encoding="utf-8")

            with mock.patch.object(bridge, "readonly_fstat", return_value=None):
                with mock.patch.object(
                    audit, "sync_main_index_safely", return_value=["A.txt"]
                ):
                    result = self.finalize(ctx, "turn-index-warning")

            self.assertEqual(result["status"], "committed")
            self.assertEqual(result["auditCoverage"], "partial")
            self.assertIn("index-changed-paths-left-untouched", result["coverageWarnings"])
            session = audit.load_session(ctx)
            turn_state = session["turns"]["turn-index-warning"]
            self.assertEqual(turn_state["auditCoverage"], "partial")
            self.assertIn(
                "index-changed-paths-left-untouched", turn_state["coverageWarnings"]
            )
            note = subprocess.run(
                [
                    ctx.git_exe,
                    "-C",
                    str(project),
                    "notes",
                    "--ref=codex-p4-diff",
                    "show",
                    str(result["commit"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            note_record = json.loads(note)
            note_warning_codes = {
                item["code"] for item in note_record["coverageWarnings"]
            }
            self.assertIn("index-changed-paths-left-untouched", note_warning_codes)

    def test_atomic_index_sync_preserves_existing_and_racing_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            ctx = self.make_context(data_root / "local", project)

            def hash_blob(content: bytes) -> str:
                return subprocess.run(
                    [ctx.git_exe, "-C", str(project), "hash-object", "-w", "--stdin"],
                    input=content,
                    check=True,
                    capture_output=True,
                ).stdout.decode("ascii").strip()

            codex_blob = hash_blob(b"codex\n")
            user_blob = hash_blob(b"user\n")
            expected = audit.index_entries(ctx, ["A.txt"])
            subprocess.run(
                [
                    ctx.git_exe,
                    "-C",
                    str(project),
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "100644",
                    user_blob,
                    "A.txt",
                ],
                check=True,
                capture_output=True,
            )
            skipped = audit.sync_main_index_safely(
                ctx,
                {"A.txt": codex_blob},
                expected,
                {"A.txt": codex_blob},
            )
            self.assertEqual(skipped, ["A.txt"])
            self.assertEqual(audit.index_entries(ctx, ["A.txt"])["A.txt"], user_blob)

            subprocess.run(
                [ctx.git_exe, "-C", str(project), "update-index", "--force-remove", "A.txt"],
                check=True,
                capture_output=True,
            )
            original_index_entries = audit.index_entries
            contention_return_codes: list[int] = []

            def inspect_while_locked(
                context: audit.AuditContext,
                paths: object,
                env: dict[str, str] | None = None,
            ) -> dict[str, str | None]:
                result = original_index_entries(context, paths, env)
                if env is not None and not contention_return_codes:
                    attempted = subprocess.run(
                        [
                            ctx.git_exe,
                            "-C",
                            str(project),
                            "update-index",
                            "--add",
                            "--cacheinfo",
                            "100644",
                            user_blob,
                            "A.txt",
                        ],
                        check=False,
                        capture_output=True,
                    )
                    contention_return_codes.append(attempted.returncode)
                return result

            with mock.patch.object(
                audit, "index_entries", side_effect=inspect_while_locked
            ):
                skipped = audit.sync_main_index_safely(
                    ctx,
                    {"A.txt": codex_blob},
                    {"A.txt": None},
                    {"A.txt": codex_blob},
                )
            self.assertEqual(skipped, [])
            self.assertEqual(len(contention_return_codes), 1)
            self.assertNotEqual(contention_return_codes[0], 0)
            self.assertEqual(audit.index_entries(ctx, ["A.txt"])["A.txt"], codex_blob)

            subprocess.run(
                [ctx.git_exe, "-C", str(project), "update-index", "--force-remove", "A.txt"],
                check=True,
                capture_output=True,
            )
            original_replace = audit.os.replace
            replace_attempts: list[int] = []

            def transient_index_contention(source: object, destination: object) -> None:
                if (
                    Path(source).name == "index.lock"
                    and Path(destination).name == "index"
                    and len(replace_attempts) < 2
                ):
                    replace_attempts.append(1)
                    raise PermissionError("injected transient index contention")
                original_replace(source, destination)

            with mock.patch.object(
                audit.os, "replace", side_effect=transient_index_contention
            ):
                skipped = audit.sync_main_index_safely(
                    ctx,
                    {"A.txt": codex_blob},
                    {"A.txt": None},
                    {"A.txt": codex_blob},
                )
            self.assertEqual(skipped, [])
            self.assertEqual(len(replace_attempts), 2)
            self.assertEqual(audit.index_entries(ctx, ["A.txt"])["A.txt"], codex_blob)

            with mock.patch.object(
                audit, "PROJECT_LOCK_WAIT_SECONDS", 0.05
            ), mock.patch.object(
                audit.os,
                "replace",
                side_effect=PermissionError("injected persistent index contention"),
            ):
                with self.assertRaisesRegex(
                    PermissionError, "injected persistent index contention"
                ):
                    audit.sync_main_index_safely(
                        ctx,
                        {"A.txt": user_blob},
                        {"A.txt": codex_blob},
                        {"A.txt": user_blob},
                    )
            self.assertFalse((project / ".git" / "index.lock").exists())
            self.assertEqual(audit.index_entries(ctx, ["A.txt"])["A.txt"], codex_blob)

            with mock.patch.object(
                audit.tempfile, "mkstemp", side_effect=OSError("injected temp failure")
            ):
                with self.assertRaisesRegex(OSError, "injected temp failure"):
                    audit.sync_main_index_safely(
                        ctx,
                        {"A.txt": codex_blob},
                        {"A.txt": codex_blob},
                        {"A.txt": codex_blob},
                    )
            self.assertFalse((project / ".git" / "index.lock").exists())

    def test_shell_coverage_warning_is_committed_with_read_only_p4_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp).resolve()
            project = data_root / "workspace" / "Project"
            project.mkdir(parents=True)
            target = project / "A.txt"
            target.write_text("before\n", encoding="utf-8")
            ctx = self.make_context(data_root / "local", project)
            self.start_turn(ctx, "turn-warning")
            event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "shell_command",
                "tool_input": {
                    "workdir": str(project),
                    "command": "Set-Content -LiteralPath 'A.txt' -Value $(Get-Content dynamic.txt)",
                },
            }
            self.assertEqual(audit.prepare_tool(ctx, event), 1)
            self.assertIn("shell-dynamic-syntax-partial", event["_codexP4DiffWarnings"])
            target.write_text("after\n", encoding="utf-8")
            p4_metadata = {
                "depotFile": "//depot/Project/A.txt",
                "haveRev": "1",
                "headRev": "2",
            }
            with mock.patch.object(bridge, "readonly_fstat", return_value=p4_metadata) as fstat:
                result = audit.finalize_turn(ctx, {"hook_event_name": "Stop", "turn_id": "turn-warning"})
            fstat.assert_called_once_with(ctx.p4, target)
            self.assertEqual(result["auditCoverage"], "partial")
            note = subprocess.run(
                [
                    ctx.git_exe,
                    "-C",
                    str(project),
                    "notes",
                    "--ref=codex-p4-diff",
                    "show",
                    str(result["commit"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            receipt = json.loads(note)
            warning_codes = {item["code"] for item in receipt["coverageWarnings"]}
            self.assertIn("shell-dynamic-syntax-partial", warning_codes)
            self.assertEqual(receipt["auditCoverage"], "partial")
            self.assertEqual(receipt["p4Mode"], "read-only")
            self.assertEqual(receipt["files"][0]["p4"], p4_metadata)
            with mock.patch.object(audit, "resolve_context", return_value=ctx):
                feedback = audit.handle_event(
                    {
                        "hook_event_name": "Stop",
                        "session_id": ctx.session_id,
                        "turn_id": "turn-warning",
                    }
                )
            self.assertIn("partial", feedback["hookSpecificOutput"]["additionalContext"])

    def test_project_lock_never_steals_an_active_old_mtime_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_root = Path(temp).resolve()
            errors: list[BaseException] = []

            def contend() -> None:
                try:
                    with audit.project_lock(state_root):
                        pass
                except BaseException as exc:  # Captured for assertion in the test thread.
                    errors.append(exc)

            with mock.patch.object(audit, "PROJECT_LOCK_WAIT_SECONDS", 1):
                with audit.project_lock(state_root):
                    lock_path = state_root / ".project.lock"
                    os.utime(lock_path, (1, 1))
                    thread = threading.Thread(target=contend)
                    thread.start()
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], audit.AuditError)
                with audit.project_lock(state_root):
                    pass
            self.assertTrue((state_root / ".project.lock").is_file())


if __name__ == "__main__":
    unittest.main()
