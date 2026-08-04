from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
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
    def test_platform_state_directory_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            variable = "LOCALAPPDATA" if MODULE.os.name == "nt" else "XDG_STATE_HOME"
            with mock.patch.dict(MODULE.os.environ, {variable: temp}, clear=False):
                self.assertEqual(MODULE.user_data_root(), Path(temp))

    def test_p4_filespec_characters_are_escaped(self) -> None:
        self.assertEqual(
            MODULE.p4_escape_filespec_path("C:/client/A@B#C%20*D.txt"),
            "C:/client/A%40B%23C%2520%2AD.txt",
        )

    def test_p4v_candidates_prefer_workspace_path_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project = root / "TDDEMO_ZCS" / "Project"
            project.mkdir(parents=True)
            p4qt = root / ".p4qt"
            p4qt.mkdir()
            connection_map = p4qt / "connectionmap.xml"
            connection_map.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<ConnectionMapList>
  <ConnectionMap><Name>0001</Name><User>tester</User><P4Port>p4-a:1666</P4Port></ConnectionMap>
  <ConnectionMap><Name>0002</Name><User>tester</User><P4Port>p4-b:1666</P4Port></ConnectionMap>
</ConnectionMapList>
""",
                encoding="utf-8",
            )
            for key, client, selected in (
                ("0001", "WRONG_CLIENT", root / "Other"),
                ("0002", "TDDEMO_ZCS", project),
            ):
                folder = p4qt / f"{key}Clients"
                folder.mkdir()
                (folder / "WorkspaceSettings.xml").write_text(
                    f"""<Settings>
<PropertyList varName="ClientList"><PropertyList varName="{client}" /></PropertyList>
<PropertyList varName="Recent"><PropertyList varName="{client}"><String>{selected}</String></PropertyList></PropertyList>
</Settings>
""",
                    encoding="utf-8",
                )
            candidates = MODULE.load_p4v_candidates(project, connection_map)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["client"], "TDDEMO_ZCS")
            self.assertEqual(candidates[0]["port"], "p4-b:1666")


class HistoryRepositoryTests(unittest.TestCase):
    def context(self, project: Path) -> dict[str, str]:
        return {
            "p4Exe": "p4",
            "serverAddress": "p4.example.test:1666",
            "userName": "tester",
            "client": "test-client",
            "clientRoot": str(project.parent),
            "projectRoot": str(project),
        }

    def test_history_repo_has_no_remote_and_blocks_push(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project = root / "workspace" / "Project"
            project.mkdir(parents=True)
            with mock.patch.object(MODULE, "user_data_root", return_value=root / "local"):
                history = MODULE.ensure_history_repo(self.context(project), project)
            history_root = Path(history["historyRoot"])
            self.assertEqual(history_root, project)
            branch = subprocess.run(
                [history["gitExe"], "-C", str(history_root), "branch", "--show-current"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(branch, MODULE.HISTORY_BRANCH)
            remotes = subprocess.run(
                [history["gitExe"], "-C", str(history_root), "remote"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(remotes, "")
            git_dir = subprocess.run(
                [history["gitExe"], "-C", str(history_root), "rev-parse", "--absolute-git-dir"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            pre_push = Path(git_dir) / "hooks" / "pre-push"
            self.assertTrue(pre_push.is_file())
            self.assertIn("push is blocked", pre_push.read_text(encoding="utf-8"))
            self.assertFalse((Path(git_dir) / "hooks" / "pre-commit").exists())
            marker = Path(git_dir) / MODULE.TOOL_NAME / "project.json"
            self.assertTrue(marker.is_file())
            exclude = (Path(git_dir) / "info" / "exclude").read_text(encoding="utf-8")
            self.assertIn("BEGIN codex-p4-diff", exclude)
            self.assertIn("\n*\n", exclude)
            self.assertNotIn("!*/", exclude)

    def test_history_repo_refuses_foreign_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project = root / "workspace" / "Project"
            project.mkdir(parents=True)
            subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
            with mock.patch.object(MODULE, "user_data_root", return_value=root / "local"):
                with self.assertRaisesRegex(MODULE.CodexP4DiffError, "拒绝接管"):
                    MODULE.ensure_history_repo(self.context(project), project)

    def test_history_repo_refuses_enclosing_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / "workspace"
            project = workspace / "Project"
            project.mkdir(parents=True)
            subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
            with mock.patch.object(MODULE, "user_data_root", return_value=root / "local"):
                with self.assertRaisesRegex(MODULE.CodexP4DiffError, "嵌套历史仓库"):
                    MODULE.ensure_history_repo(self.context(project), project)
            self.assertFalse((project / ".git").exists())

    def test_history_repo_never_switches_changed_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project = root / "workspace" / "Project"
            project.mkdir(parents=True)
            with mock.patch.object(MODULE, "user_data_root", return_value=root / "local"):
                history = MODULE.ensure_history_repo(self.context(project), project)
                subprocess.run(
                    [history["gitExe"], "-C", str(project), "switch", "-c", "user/other"],
                    check=True,
                    capture_output=True,
                )
                with self.assertRaisesRegex(MODULE.CodexP4DiffError, "不会自动切换"):
                    MODULE.ensure_history_repo(self.context(project), project)
            branch = subprocess.run(
                [history["gitExe"], "-C", str(project), "branch", "--show-current"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(branch, "user/other")

    def test_history_repo_rejects_added_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project = root / "workspace" / "Project"
            project.mkdir(parents=True)
            with mock.patch.object(MODULE, "user_data_root", return_value=root / "local"):
                history = MODULE.ensure_history_repo(self.context(project), project)
                subprocess.run(
                    [
                        history["gitExe"],
                        "-C",
                        str(history["historyRoot"]),
                        "remote",
                        "add",
                        "origin",
                        "https://example.invalid/history.git",
                    ],
                    check=True,
                )
                with self.assertRaisesRegex(MODULE.CodexP4DiffError, "不允许配置 Git remote"):
                    MODULE.ensure_history_repo(self.context(project), project)

    def test_detach_removes_only_owned_legacy_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project = root / "workspace" / "Project"
            project.mkdir(parents=True)
            owned = root / "local" / "CodexP4Diff" / "Native" / "old" / "git"
            owned.mkdir(parents=True)
            (project / ".git").write_text(f"gitdir: {owned}\n", encoding="utf-8")
            with mock.patch.object(MODULE, "user_data_root", return_value=root / "local"):
                result = MODULE.detach_legacy_git_pointer(project)
            self.assertTrue(result["removed"])
            self.assertFalse((project / ".git").exists())

    def test_detach_preserves_foreign_pointer_and_real_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project = root / "Project"
            project.mkdir()
            (project / ".git").write_text("gitdir: C:/foreign/repo.git\n", encoding="utf-8")
            result = MODULE.detach_legacy_git_pointer(project)
            self.assertFalse(result["removed"])
            self.assertTrue((project / ".git").is_file())
            (project / ".git").unlink()
            (project / ".git").mkdir()
            result = MODULE.detach_legacy_git_pointer(project)
            self.assertFalse(result["removed"])
            self.assertTrue((project / ".git").is_dir())


class PerforceSafetyTests(unittest.TestCase):
    def test_runtime_contains_no_perforce_mutation_commands(self) -> None:
        self.assertEqual(
            MODULE.READ_ONLY_P4_COMMANDS,
            frozenset({"info", "where", "fstat", "diff", "opened", "print"}),
        )
        with self.assertRaisesRegex(MODULE.CodexP4DiffError, "拒绝非只读 P4 命令"):
            MODULE.p4_tagged("p4", ["change", "-i"], cwd=Path.cwd())

    def test_readonly_fstat_uses_only_fstat(self) -> None:
        context = {
            "p4Exe": "p4",
            "client": "client",
            "projectRoot": "C:/workspace/Project",
        }
        with mock.patch.object(MODULE, "p4_tagged", return_value=[]) as tagged:
            MODULE.readonly_fstat(context, Path("C:/workspace/Project/A.txt"))
        self.assertEqual(tagged.call_args.args[1][0], "fstat")


class DistributionTests(unittest.TestCase):
    def test_manifest_and_marketplace_are_consistent(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads(
            (REPO_ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
        )
        entry = marketplace["plugins"][0]
        self.assertEqual(manifest["name"], "codex-p4-diff")
        self.assertEqual(manifest["version"], "0.5.0")
        self.assertEqual(manifest["author"]["name"], "BillyCSZhao")
        self.assertEqual(manifest["repository"], "https://github.com/BillyCSZhao/codex-p4-diff")
        self.assertEqual(entry["name"], manifest["name"])
        self.assertEqual(entry["source"]["path"], "./plugins/codex-p4-diff")
        self.assertTrue((PLUGIN_ROOT / "assets" / "icon.png").is_file())
        self.assertTrue((PLUGIN_ROOT / "assets" / "logo.png").is_file())


if __name__ == "__main__":
    unittest.main()
