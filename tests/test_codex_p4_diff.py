from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-p4-diff"
SCRIPT_PATH = PLUGIN_ROOT / "skills" / "codex-p4-diff" / "scripts" / "codex_p4_diff.py"

SPEC = importlib.util.spec_from_file_location("codex_p4_diff", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load {SCRIPT_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ExecutableResolutionTests(unittest.TestCase):
    def test_resolve_executable_accepts_explicit_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "tool.exe"
            executable.write_bytes(b"")
            self.assertEqual(
                MODULE.resolve_executable(str(executable), "p4", "install"),
                str(executable.resolve()),
            )

    def test_resolve_executable_reports_install_hint(self) -> None:
        missing = str(Path(tempfile.gettempdir()) / "codex-p4-diff-missing-tool.exe")
        with mock.patch.object(MODULE.shutil, "which", return_value=None):
            with self.assertRaisesRegex(MODULE.CodexP4DiffError, "安装测试工具"):
                MODULE.resolve_executable(missing, "p4", "安装测试工具。")

    def test_run_command_missing_program_is_friendly(self) -> None:
        missing = str(Path(tempfile.gettempdir()) / "codex-p4-diff-never-exists.exe")
        with self.assertRaisesRegex(MODULE.CodexP4DiffError, "找不到命令"):
            MODULE.run_command([missing])


class PlatformPathTests(unittest.TestCase):
    def test_local_app_data_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(MODULE.os.environ, {"LOCALAPPDATA": temp}, clear=False):
                self.assertEqual(MODULE.user_data_root(), Path(temp))

    @unittest.skipIf(MODULE.os.name == "nt", "XDG state path is for non-Windows platforms")
    def test_xdg_state_home_is_used_outside_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(
                MODULE.os.environ,
                {"XDG_STATE_HOME": temp},
                clear=True,
            ):
                self.assertEqual(MODULE.user_data_root(), Path(temp))

    def test_p4_filespec_characters_are_escaped(self) -> None:
        self.assertEqual(
            MODULE.p4_escape_filespec_path("C:/client/A@B#C%20*D.txt"),
            "C:/client/A%40B%23C%2520%2AD.txt",
        )


class SafetyTests(unittest.TestCase):
    def test_real_git_directory_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            project.mkdir()
            (project / ".git").mkdir()
            with self.assertRaisesRegex(MODULE.CodexP4DiffError, "真实 Git 仓库"):
                MODULE.validate_project_slot(project, Path(temp) / "sidecar" / "git")

    def test_sidecar_identity_accepts_same_context(self) -> None:
        root = Path(tempfile.gettempdir()) / "p4-client"
        marker = {
            "serverAddress": "ssl:perforce.example:1666",
            "userName": "alice",
            "client": "alice-main",
            "clientRoot": str(root),
            "changelist": "default",
            "baselineCommit": "abc",
        }
        MODULE.validate_sidecar_identity(
            marker,
            {"serverAddress": "ssl:perforce.example:1666", "userName": "alice"},
            "alice-main",
            "default",
            root,
        )

    def test_sidecar_identity_rejects_different_client(self) -> None:
        root = Path(tempfile.gettempdir()) / "p4-client"
        marker = {
            "serverAddress": "perforce.example:1666",
            "userName": "alice",
            "client": "alice-other",
            "clientRoot": str(root),
            "changelist": "default",
            "baselineCommit": "abc",
        }
        with self.assertRaisesRegex(MODULE.CodexP4DiffError, "不同的 P4 上下文"):
            MODULE.validate_sidecar_identity(
                marker,
                {"serverAddress": "perforce.example:1666", "userName": "alice"},
                "alice-main",
                "default",
                root,
            )

    def test_replace_text_atomically_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "pointer"
            self.assertEqual(MODULE.replace_text_atomically(target, "gitdir: x\n"), "atomic-git-pointer-replace")
            self.assertEqual(MODULE.replace_text_atomically(target, "gitdir: x\n"), "atomic-git-pointer-replace")
            self.assertEqual(target.read_text(encoding="utf-8"), "gitdir: x\n")


class PerforceMappingTests(unittest.TestCase):
    def test_opened_query_runs_from_requested_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            scope = project / "Source"
            client_file = scope / "Tool.cpp"
            scope.mkdir(parents=True)
            client_file.write_text("changed\n", encoding="utf-8")
            records = [
                {
                    "depotFile": "//depot/project/Source/Tool.cpp",
                    "action": "edit",
                    "rev": "12",
                    "type": "text",
                    "change": "default",
                }
            ]
            with mock.patch.object(MODULE, "p4_tagged", return_value=records) as tagged:
                with mock.patch.object(MODULE, "resolve_where", return_value=client_file) as where:
                    files = MODULE.opened_files(
                        "p4",
                        "alice-main",
                        scope,
                        "default",
                        project,
                        project,
                    )
            self.assertEqual(files[0]["relativePath"], "Source/Tool.cpp")
            self.assertEqual(tagged.call_args.kwargs["cwd"], scope)
            self.assertEqual(where.call_args.args[-1], scope)


class P4VTests(unittest.TestCase):
    def test_diffhave_passes_connection_and_uses_only_text_files(self) -> None:
        files = [
            {"clientFile": "C:/p4/A.cpp", "fileType": "text+k"},
            {"clientFile": "C:/p4/B.uasset", "fileType": "binary+l"},
        ]
        command, selected = MODULE.p4vc_diffhave_command(
            "p4vc",
            "alice-main",
            files,
            {"p4Port": "ssl:perforce.example:1666", "p4User": "alice"},
        )
        self.assertEqual(selected, ["C:/p4/A.cpp"])
        self.assertEqual(
            command,
            [
                "p4vc",
                "-p",
                "ssl:perforce.example:1666",
                "-u",
                "alice",
                "-c",
                "alice-main",
                "diffhave",
                "C:/p4/A.cpp",
            ],
        )

    def test_p4v_executable_uses_p4vc_mode(self) -> None:
        self.assertEqual(
            MODULE.p4vc_launcher("C:/Program Files/Perforce/p4v.exe"),
            [str(Path("C:/Program Files/Perforce/p4v.exe")), "-p4vc"],
        )


class DistributionTests(unittest.TestCase):
    def test_manifest_and_marketplace_are_consistent(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads(
            (REPO_ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
        )
        entry = marketplace["plugins"][0]
        self.assertEqual(manifest["name"], "codex-p4-diff")
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertEqual(manifest["author"]["name"], "BillyCSZhao")
        self.assertEqual(manifest["repository"], "https://github.com/BillyCSZhao/codex-p4-diff")
        self.assertEqual(entry["name"], manifest["name"])
        self.assertEqual(entry["source"]["path"], "./plugins/codex-p4-diff")
        self.assertTrue((PLUGIN_ROOT / "assets" / "icon.png").is_file())
        self.assertTrue((PLUGIN_ROOT / "assets" / "logo.png").is_file())


if __name__ == "__main__":
    unittest.main()
