from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
from unittest import mock

from test_codex_p4_audit import AuditTestCase, audit, bridge


class SubagentTurnTest(AuditTestCase):
    def event(self, path: Path, turn: str, agent: str | None = None, call: str = "write"):
        event = {
            "hook_event_name": "PreToolUse", "tool_name": "Write",
            "turn_id": turn, "tool_use_id": call,
            "tool_input": {"file_path": str(path)},
        }
        if agent:
            event["agent_id"] = agent
        return event

    def write(self, ctx, path: Path, value: str, turn: str, agent: str | None = None):
        event = self.event(path, turn, agent)
        audit.prepare_tool(ctx, event)
        path.write_text(value, encoding="utf-8")
        post = {key: value for key, value in event.items() if not key.startswith("_codex")}
        post["hook_event_name"] = "PostToolUse"
        audit.finish_tool(ctx, post)

    def lifecycle(self, ctx, turn: str, agent: str, stopping: bool = False):
        with mock.patch.object(bridge, "readonly_fstat", return_value=None):
            return audit.record_subagent_event(ctx, {
                "hook_event_name": "SubagentStop" if stopping else "SubagentStart",
                "turn_id": turn, "agent_id": agent,
            })

    def test_parent_commit_contains_child_new_file_and_writer_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            project = (Path(temp) / "project").resolve()
            project.mkdir()
            ctx = self.make_context(Path(temp) / "state", project)
            self.start_turn(ctx, "parent", "Implement scene markers")
            self.lifecycle(ctx, "child", "agent")
            self.write(ctx, project / "core.cpp", "actual implementation\n", "child", "agent")
            self.lifecycle(ctx, "child", "agent", stopping=True)
            self.write(ctx, project / "panel.cpp", "connect checkbox\n", "parent")
            result = self.finalize(ctx, "parent")
            self.assertEqual(result["status"], "committed")
            note = json.loads(bridge.run_command([
                ctx.git_exe, "-C", str(project), "notes", "--ref=codex-p4-diff", "show", result["commit"]
            ]).stdout)
            files = {entry["path"]: entry for entry in note["files"]}
            self.assertEqual(set(files), {"core.cpp", "panel.cpp"})
            self.assertEqual(files["core.cpp"]["writers"], [{"turnId": "child", "agentId": "agent"}])
            self.assertFalse(files["core.cpp"]["before"]["exists"])
            self.assertFalse(audit.before_manifest_path(ctx, "child").exists())

    def test_post_releases_writer_but_keeps_parent_earliest_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            project = (Path(temp) / "project").resolve()
            project.mkdir()
            path = project / "shared.cpp"
            path.write_text("original\n", encoding="utf-8")
            ctx = self.make_context(Path(temp) / "state", project)
            self.start_turn(ctx, "parent")
            self.write(ctx, path, "child\n", "child", "agent")
            first = audit.load_before_manifest(ctx, "parent")["files"]["shared.cpp"]
            self.write(ctx, path, "parent followup\n", "parent")
            final = audit.load_before_manifest(ctx, "parent")["files"]["shared.cpp"]
            self.assertEqual(first["sha256"], final["sha256"])
            self.assertEqual(Path(final["snapshotPath"]).read_text(), "original\n")
            self.assertEqual(first["ownershipGeneration"], final["ownershipGeneration"])
            self.assertEqual({entry["turnId"] for entry in final["writers"]}, {"child", "parent"})

    def test_concurrent_child_writer_and_foreign_session_remain_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            project = (Path(temp) / "project").resolve()
            project.mkdir()
            path = project / "shared.cpp"
            data = Path(temp) / "state"
            ctx = self.make_context(data, project)
            other = self.make_context(data, project, "other-session")
            self.start_turn(ctx, "parent")
            self.start_turn(other, "other-parent")
            first = self.event(path, "child-1", "agent-1", "call-1")
            audit.prepare_tool(ctx, first)
            with self.assertRaises(audit.OwnershipConflict):
                audit.prepare_tool(ctx, self.event(path, "child-2", "agent-2", "call-2"))
            path.write_text("child 1\n")
            first["hook_event_name"] = "PostToolUse"
            audit.finish_tool(ctx, first)
            with self.assertRaises(audit.OwnershipConflict):
                audit.prepare_tool(other, self.event(path, "other-parent"))
            self.write(ctx, path, "child 2\n", "child-2", "agent-2")

    def test_late_child_does_not_join_next_user_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            project = (Path(temp) / "project").resolve()
            project.mkdir()
            ctx = self.make_context(Path(temp) / "state", project)
            self.start_turn(ctx, "old-parent")
            self.lifecycle(ctx, "old-child", "agent")
            self.finalize(ctx, "old-parent")
            self.start_turn(ctx, "new-parent")
            self.write(ctx, project / "late.cpp", "late result\n", "old-child", "agent")
            self.assertNotIn("late.cpp", audit.load_before_manifest(ctx, "new-parent")["files"])
            self.assertIn("late.cpp", audit.load_before_manifest(ctx, "old-child")["files"])
            result = self.lifecycle(ctx, "old-child", "agent", stopping=True)
            self.assertEqual(result["status"], "committed")
            self.assertEqual(audit.load_session(ctx)["activeTurnId"], "new-parent")

    def test_new_child_turn_of_same_agent_binds_current_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            project = (Path(temp) / "project").resolve()
            project.mkdir()
            ctx = self.make_context(Path(temp) / "state", project)
            self.start_turn(ctx, "parent-1")
            self.write(ctx, project / "one.cpp", "one\n", "child-1", "agent")
            self.lifecycle(ctx, "child-1", "agent", stopping=True)
            self.finalize(ctx, "parent-1")
            self.start_turn(ctx, "parent-2")
            self.write(ctx, project / "two.cpp", "two\n", "child-2", "agent")
            writers = audit.load_session(ctx)["writerTurns"]
            self.assertEqual(writers["child-1"]["parentTurnId"], "parent-1")
            self.assertEqual(writers["child-2"]["parentTurnId"], "parent-2")
            self.assertIn("two.cpp", audit.load_before_manifest(ctx, "parent-2")["files"])

    def test_inflight_post_after_new_prompt_keeps_original_before_and_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            project = (Path(temp) / "project").resolve()
            project.mkdir()
            path = project / "slow.cpp"
            path.write_text("original\n", encoding="utf-8")
            original_sha = audit.sha256_bytes(path.read_bytes())
            ctx = self.make_context(Path(temp) / "state", project)
            self.start_turn(ctx, "old-parent")
            pre = self.event(path, "child", "agent", "slow-write")
            audit.prepare_tool(ctx, pre)
            self.assertEqual(self.finalize(ctx, "old-parent")["status"], "awaiting-child-tools")
            self.assertFalse(audit.transaction_path(ctx, "old-parent").exists())
            self.start_turn(ctx, "new-parent")
            with self.assertRaises(audit.OwnershipConflict):
                audit.prepare_tool(ctx, self.event(path, "new-parent", call="new-write"))
            path.write_text("finished child\n", encoding="utf-8")
            post = self.event(path, "child", "agent", "slow-write")
            post["hook_event_name"] = "PostToolUse"
            audit.finish_tool(ctx, post)
            result = self.lifecycle(ctx, "child", "agent", stopping=True)
            self.assertEqual(result["status"], "committed")
            note = json.loads(bridge.run_command([
                ctx.git_exe, "-C", str(project), "notes", "--ref=codex-p4-diff", "show", result["commit"]
            ]).stdout)
            self.assertEqual(note["turnId"], "old-parent")
            self.assertEqual(note["auditCoverage"], "exact")
            self.assertEqual(note["files"][0]["before"]["sha256"], original_sha)
            self.assertEqual(note["files"][0]["after"]["sha256"], audit.sha256_bytes(path.read_bytes()))
            self.assertFalse(audit.before_manifest_path(ctx, "child").exists())
            self.assertNotIn("slow.cpp", audit.load_before_manifest(ctx, "new-parent")["files"])
            self.assertEqual(audit.load_session(ctx)["activeTurnId"], "new-parent")

    def test_duplicate_child_stop_does_not_create_a_late_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            project = (Path(temp) / "project").resolve()
            project.mkdir()
            ctx = self.make_context(Path(temp) / "state", project)
            self.start_turn(ctx, "parent")
            self.write(ctx, project / "file.cpp", "child\n", "child", "agent")
            self.lifecycle(ctx, "child", "agent", stopping=True)
            self.finalize(ctx, "parent")
            self.start_turn(ctx, "next-parent")
            before = set(audit.load_session(ctx)["turns"])
            self.lifecycle(ctx, "child", "agent", stopping=True)
            self.assertEqual(set(audit.load_session(ctx)["turns"]), before)

    def test_session_end_can_recover_a_child_with_missing_post(self):
        with tempfile.TemporaryDirectory() as temp:
            project = (Path(temp) / "project").resolve()
            project.mkdir()
            path = project / "interrupted.cpp"
            ctx = self.make_context(Path(temp) / "state", project)
            self.start_turn(ctx, "parent")
            audit.prepare_tool(ctx, self.event(path, "child", "agent"))
            path.write_text("written before shutdown\n", encoding="utf-8")
            self.assertEqual(self.finalize(ctx, "parent")["status"], "awaiting-child-tools")
            audit.end_subagent_tools(ctx)
            with mock.patch.object(bridge, "readonly_fstat", return_value=None):
                result = audit.retry_previous_turn(ctx, None)
            self.assertEqual(result["status"], "committed")
            self.assertEqual(result["auditCoverage"], "exact")
            self.assertFalse(audit.recovery_marker_path(ctx, ctx.session_id, "parent").exists())

    def test_unknown_tool_without_agent_identity_is_not_assumed_to_be_child(self):
        with tempfile.TemporaryDirectory() as temp:
            project = (Path(temp) / "project").resolve()
            project.mkdir()
            ctx = self.make_context(Path(temp) / "state", project)
            self.start_turn(ctx, "parent")
            event = self.event(project / "unknown.cpp", "unknown-turn")
            audit.prepare_tool(ctx, event)
            self.assertIn("unregistered-writer-turn", event["_codexP4DiffWarnings"])
            self.assertNotIn("unknown.cpp", audit.load_before_manifest(ctx, "parent")["files"])
            self.assertIn("unknown.cpp", audit.load_before_manifest(ctx, "unknown-turn")["files"])


    def test_session_end_recovers_detached_child_when_subagent_stop_is_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            project = (Path(temp) / "project").resolve()
            project.mkdir()
            path = project / "late.cpp"
            ctx = self.make_context(Path(temp) / "state", project)
            self.start_turn(ctx, "old-parent")
            self.lifecycle(ctx, "child", "agent")
            self.finalize(ctx, "old-parent")
            self.start_turn(ctx, "new-parent")
            self.write(ctx, path, "late child result\n", "child", "agent")
            self.assertFalse(audit.recovery_marker_path(ctx, ctx.session_id, "child").exists())
            audit.end_subagent_tools(ctx)
            marker = audit.read_json_if_exists(audit.recovery_marker_path(ctx, ctx.session_id, "child"), {})
            self.assertEqual(marker["reason"], "awaiting-child-tools")
            with mock.patch.object(bridge, "readonly_fstat", return_value=None):
                result = audit.retry_previous_turn(ctx, None)
            results = result.get("results", [result])
            committed = [item for item in results if item["status"] == "committed"]
            self.assertEqual(len(committed), 1)
            note = json.loads(bridge.run_command([
                ctx.git_exe, "-C", str(project), "notes", "--ref=codex-p4-diff", "show", committed[0]["commit"]
            ]).stdout)
            self.assertEqual(note["turnId"], "child")
            self.assertEqual(note["parentTurnId"], "old-parent")
            self.assertFalse(note["files"][0]["before"]["exists"])
            self.assertEqual(note["files"][0]["after"]["sha256"], audit.sha256_bytes(path.read_bytes()))
            self.assertEqual(audit.load_session(ctx)["turns"]["child"]["status"], "committed")

    def test_history_timeout_does_not_freeze_inflight_child_even_if_state_is_busy(self):
        for state_busy in (False, True):
            with self.subTest(state_busy=state_busy), tempfile.TemporaryDirectory() as temp:
                project = (Path(temp) / "project").resolve()
                project.mkdir()
                path = project / "slow.cpp"
                path.write_text("original\n", encoding="utf-8")
                before_sha = audit.sha256_bytes(path.read_bytes())
                ctx = self.make_context(Path(temp) / "state", project)
                self.start_turn(ctx, "parent")
                audit.prepare_tool(ctx, self.event(path, "child", "agent", "slow-write"))
                real_lock = audit.project_lock

                @contextmanager
                def busy_lock(state_root, *, lock_name=".project.lock", wait_seconds=None):
                    if lock_name == ".project.lock" or (state_busy and lock_name == ".state.lock"):
                        raise audit.ProjectLockTimeout("injected lock timeout")
                    with real_lock(state_root, lock_name=lock_name, wait_seconds=wait_seconds):
                        yield

                with mock.patch.object(audit, "project_lock", busy_lock), \
                     mock.patch.object(audit, "capture_deferred_after") as freeze, \
                     mock.patch.object(audit, "release_turn_paths_locked") as release:
                    if state_busy:
                        with self.assertRaises(audit.ProjectLockTimeout):
                            self.finalize(ctx, "parent")
                    else:
                        self.assertEqual(self.finalize(ctx, "parent")["status"], "awaiting-child-tools")
                    freeze.assert_not_called()
                    release.assert_not_called()
                self.assertIsNone(audit.load_deferred_after_record(ctx, "parent"))
                marker = audit.read_json_if_exists(audit.recovery_marker_path(ctx, ctx.session_id, "parent"), {})
                self.assertEqual(marker["reason"], "awaiting-child-tools")
                registry = audit.read_json_if_exists(audit.ownership_path(ctx), {})
                self.assertIn(audit.ownership_key("slow.cpp"), registry["files"])
                path.write_text("finished child\n", encoding="utf-8")
                post = self.event(path, "child", "agent", "slow-write")
                post["hook_event_name"] = "PostToolUse"
                audit.finish_tool(ctx, post)
                result = self.finalize(ctx, "parent")
                self.assertEqual(result["status"], "committed")
                self.assertEqual(result["auditCoverage"], "exact")
                note = json.loads(bridge.run_command([
                    ctx.git_exe, "-C", str(project), "notes", "--ref=codex-p4-diff", "show", result["commit"]
                ]).stdout)
                self.assertEqual(note["files"][0]["before"]["sha256"], before_sha)
                self.assertEqual(note["files"][0]["after"]["sha256"], audit.sha256_bytes(path.read_bytes()))
