from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
from unittest import mock

from test_codex_p4_audit import AuditTestCase, ROOT, audit, bridge


class BrokerCompatibilityTest(AuditTestCase):
    def test_legacy_broker_preserves_marker_state_and_commits(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp).resolve()
            project = data / "workspace"
            project.mkdir()
            original = self.make_context(data / "local", project)
            marker_path = project / ".git" / bridge.TOOL_NAME / "project.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker.pop("p4Port")
            audit.atomic_write_json(marker_path, marker)
            marker_before = marker_path.read_bytes()
            endpoint = original.p4["serverAddress"]
            backend = "commit.internal:1666"

            def discover(_root, **kwargs):
                return {
                    **original.p4,
                    "p4Port": kwargs.get("port"),
                    "serverAddress": backend,
                    "info": {"brokerAddress": endpoint, "serverAddress": backend},
                }

            with (
                mock.patch.object(bridge, "discover_p4_context", side_effect=discover) as calls,
                mock.patch.object(bridge, "user_data_root", return_value=data / "local"),
            ):
                ctx = audit.resolve_context({"cwd": str(project), "session_id": "broker-test"})
                report = bridge.doctor_report(project, initialize=False)
            self.assertTrue(report["ready"], report)
            self.assertEqual(ctx.state_root, original.state_root)
            self.assertEqual(ctx.p4["serverAddress"], endpoint)
            self.assertEqual(ctx.p4["resolvedServerAddress"], backend)
            self.assertEqual(ctx.p4["serverIdentitySource"], "validated-legacy-broker")
            self.assertEqual(calls.call_args_list[0].kwargs["port"], None)
            self.assertEqual(calls.call_args_list[1].kwargs["port"], endpoint)
            self.assertEqual(marker_path.read_bytes(), marker_before)
            self.start_turn(ctx, "broker-turn")
            target = project / "example.txt"
            audit.record_before_path(ctx, "broker-turn", target)
            target.write_text("recorded\n", encoding="utf-8")
            self.assertEqual(self.finalize(ctx, "broker-turn")["status"], "committed")

    def test_unproven_or_modern_broker_identity_still_rejected(self):
        for case in ("modern", "wrong-broker", "missing-broker", "different-port", "wrong-client"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                data = Path(temp).resolve()
                project = data / "workspace"
                project.mkdir()
                ctx = self.make_context(data / "local", project)
                marker_path = project / ".git" / bridge.TOOL_NAME / "project.json"
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                if case != "modern":
                    marker.pop("p4Port")
                audit.atomic_write_json(marker_path, marker)
                endpoint = ctx.p4["serverAddress"]
                candidate = {
                    **ctx.p4,
                    "serverAddress": "other.internal:1666",
                    "info": {"brokerAddress": endpoint},
                }
                if case == "wrong-broker":
                    candidate["info"]["brokerAddress"] = "other-broker:1666"
                if case == "missing-broker":
                    candidate["info"] = {}
                if case == "wrong-client":
                    candidate["client"] = "another-client"
                port = "different-endpoint:1666" if case == "different-port" else endpoint
                with mock.patch.object(bridge, "discover_p4_context", return_value=candidate):
                    with self.assertRaises(bridge.OwnedHistoryContextError):
                        bridge.resolve_project_p4_context(project, port=port)


class ExecCommandTest(AuditTestCase):
    def test_shell_event_records_before_and_commits_after(self):
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        for hook in ("PreToolUse", "PostToolUse"):
            self.assertIsNotNone(re.fullmatch(hooks["hooks"][hook][0]["matcher"], "exec_command"))
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp).resolve()
            project = data / "workspace"
            project.mkdir()
            ctx = self.make_context(data / "local", project)
            target = project / "example.txt"
            target.write_text("before\n", encoding="utf-8")
            self.start_turn(ctx, "exec-turn")
            event = {
                "tool_name": "exec_command", "turn_id": "exec-turn", "cwd": str(project),
                "tool_input": {"cmd": "Set-Content -LiteralPath example.txt -Value after", "workdir": str(project)},
            }
            analysis = audit.analyze_tool_paths(ctx, event)
            self.assertEqual(analysis.paths, [target])
            self.assertTrue(analysis.mutating)
            audit.prepare_tool(ctx, event)
            manifest = audit.load_before_manifest(ctx, "exec-turn")
            self.assertEqual(manifest["files"]["example.txt"]["sha256"], audit.sha256_bytes(target.read_bytes()))
            target.write_text("after\n", encoding="utf-8")
            audit.finish_tool(ctx, event)
            result = self.finalize(ctx, "exec-turn")
            self.assertEqual(result["status"], "committed")
            self.assertEqual(bridge.git_output(ctx.git_exe, project, ["show", "HEAD:example.txt"]), "after")
            readonly = {**event, "tool_input": {"cmd": "Get-Content example.txt"}}
            self.assertFalse(audit.analyze_tool_paths(ctx, readonly).mutating)
