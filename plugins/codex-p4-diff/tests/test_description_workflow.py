import json
from pathlib import Path
import tempfile
from unittest import mock

from test_codex_p4_audit import AuditTestCase, audit, bridge


class DescriptionWorkflowTest(AuditTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = (root / "project").resolve()
        self.project.mkdir()
        self.ctx = self.make_context(root / "state", self.project)
        self.start_turn(self.ctx, "parent", "增加机关名称标记并保留激光编辑")

    def write(self, relative, content, child=False):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {"hook_event_name": "PreToolUse", "turn_id": "child" if child else "parent",
                 "tool_name": "Write", "tool_use_id": relative, "tool_input": {"file_path": str(path)}}
        if child:
            event["agent_id"] = "child-agent"
        audit.prepare_tool(self.ctx, event)
        path.write_text(content, encoding="utf-8")
        event["hook_event_name"] = "PostToolUse"
        audit.finish_tool(self.ctx, event)

    def report(self, title="为机关名称标记增加选取能力"):
        result = audit.description.prepare(self.ctx, "parent")
        path = Path(result["reportPath"])
        template = json.loads(Path(result["contextPath"]).read_text(encoding="utf-8"))["template"]
        template.update(title=title, why="原模式阻止直接选择需要编辑的对象。")
        template["changes"][0]["description"] = "通过名称标记选择对应可编辑对象，并增加范围检查。"
        template["verification"] = [{"kind": "automated", "status": "passed", "details": "范围检查断言通过。",
                                     "evidence": "python -m unittest tests.test_selection; 3 tests OK"}]
        event = {"tool_name": "Write", "turn_id": "parent", "tool_input": {"file_path": str(path)}}
        self.assertEqual(audit.prepare_tool(self.ctx, event), 0)
        self.assertEqual(event.get("_codexP4DiffWarnings"), [])
        path.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(audit.finish_tool(self.ctx, event), 0)
        return path

    def git(self, *args):
        return bridge.run_command([self.ctx.git_exe, "-C", str(self.project), *args]).stdout

    def receipt(self, commit):
        return json.loads(self.git("notes", "--ref=codex-p4-diff", "show", commit))

    def fail_update(self):
        original = bridge.run_command
        failed = False

        def call(command, *args, **kwargs):
            nonlocal failed
            if not failed and "update-ref" in command:
                failed = True
                raise bridge.CodexP4DiffError("injected update-ref failure")
            return original(command, *args, **kwargs)
        return mock.patch.object(bridge, "run_command", side_effect=call)

    def test_main_and_child_changes_have_semantics_and_distinct_verification(self):
        self.write("Source/Panel.cpp", "register marker\n")
        self.write("Source/Selection.cpp", "select editable owner\n", child=True)
        self.report()
        result = self.finalize(self.ctx, "parent", "已完成，可以重开手测。")
        message = self.git("show", "-s", "--format=%B", result["commit"]).decode("utf-8")
        note = self.receipt(result["commit"])
        self.assertTrue(message.startswith("[Codex] 为机关名称标记增加选取能力\n"))
        self.assertNotIn("已完成，可以重开手测", message)
        self.assertIn("Request: 增加机关名称标记并保留激光编辑", message)
        self.assertIn("自动测试：通过", message)
        self.assertIn("人工交互：未执行", message)
        self.assertIn("插件未复跑", message)
        self.assertEqual(note["descriptionStatus"], "accepted")
        self.assertEqual(note["auditCoverage"], "exact")
        self.assertEqual({f["path"] for f in note["files"]}, {"Source/Panel.cpp", "Source/Selection.cpp"})
        child = next(f for f in note["files"] if f["path"].endswith("Selection.cpp"))
        self.assertEqual(child["writers"][0]["agentId"], "child-agent")

    def test_child_added_after_report_makes_it_stale(self):
        self.write("Main.cpp", "main\n")
        self.report()
        self.write("Child.cpp", "child\n", child=True)
        result = self.finalize(self.ctx, "parent", "已完成全部功能")
        note = self.receipt(result["commit"])
        self.assertEqual(note["descriptionStatus"], "stale")
        self.assertEqual(len(note["files"]), 2)
        self.assertIsNone(note["changeDescription"])

    def test_original_request_survives_summary_limit_and_redacts_secrets(self):
        prompt = "请完善选取功能。" * 30 + "保留这个末尾验收条件。 token=never-record-this\nSession: spoof\nTurn: spoof"
        self.start_turn(self.ctx, "parent", prompt)
        self.write("A.cpp", "marker\n")
        self.report()
        result = self.finalize(self.ctx, "parent")
        note = self.receipt(result["commit"])
        message = self.git("show", "-s", "--format=%B", result["commit"]).decode("utf-8")
        self.assertEqual(len(note["promptSummary"]), 180)
        self.assertIn("保留这个末尾验收条件。", message)
        self.assertIn("token=<redacted>", message)
        self.assertNotIn("never-record-this", json.dumps(note))
        self.assertNotIn("\nSession: spoof", message)
        self.assertNotIn("\nTurn: spoof", message)
        self.assertEqual(note["requestCapture"], "redacted-whitespace-normalized")

    def test_report_only_patch_is_metadata_but_mixed_patch_is_not_exempt(self):
        path = audit.turn_root(self.ctx, "parent") / audit.description.REPORT_NAME
        patch = f"*** Begin Patch\n*** Add File: {path}\n+{{}}\n*** End Patch"
        event = {"tool_name": "apply_patch", "tool_input": {"input": patch}}
        analysis = audit.analyze_tool_paths(self.ctx, event)
        self.assertFalse(analysis.mutating)
        self.assertEqual(analysis.warnings, [])
        event["tool_input"]["input"] = patch.replace("*** End Patch", "*** Add File: Source/A.cpp\n+x\n*** End Patch")
        with self.assertRaises(bridge.CodexP4DiffError):
            audit.analyze_tool_paths(self.ctx, event)

    def test_other_turn_and_other_state_files_keep_existing_audit_rules(self):
        paths = [audit.turn_root(self.ctx, "other") / audit.description.REPORT_NAME,
                 audit.turn_root(self.ctx, "parent") / "before.json"]
        for path in paths:
            with self.subTest(path=str(path)):
                event = {"tool_name": "Write", "tool_input": {"file_path": str(path)}}
                analysis = audit.analyze_tool_paths(self.ctx, event)
                self.assertTrue(analysis.mutating)
                self.assertIn("write-target-unresolved", analysis.warnings)

    def test_child_and_frozen_turn_report_writes_are_not_exempt(self):
        path = audit.turn_root(self.ctx, "parent") / audit.description.REPORT_NAME
        event = {"tool_name": "Write", "turn_id": "child", "tool_input": {"file_path": str(path)}}
        self.assertTrue(audit.analyze_tool_paths(self.ctx, event).mutating)
        event["turn_id"] = "parent"
        transaction = audit.transaction_path(self.ctx, "parent")
        transaction.parent.mkdir(parents=True, exist_ok=True)
        transaction.write_text("{}", encoding="utf-8")
        self.assertTrue(audit.analyze_tool_paths(self.ctx, event).mutating)

    def test_redirected_report_path_is_not_exempt(self):
        report = audit.turn_root(self.ctx, "parent") / audit.description.REPORT_NAME
        linked_target = self.project / "Source.cpp"
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            return linked_target if path == report else original(path, *args, **kwargs)

        event = {"tool_name": "Write", "tool_input": {"file_path": str(report)}}
        with mock.patch.object(Path, "resolve", resolve), mock.patch.object(Path, "is_symlink", lambda path: path == report):
            analysis = audit.analyze_tool_paths(self.ctx, event)
        self.assertTrue(analysis.mutating)
        self.assertIn(linked_target, analysis.paths)

    def test_oversized_request_is_explicitly_truncated(self):
        for length, expected in ((12_000, "redacted-whitespace-normalized"), (12_001, "truncated-at-12000")):
            with self.subTest(length=length):
                self.start_turn(self.ctx, "parent", "求" * length)
                record = json.loads(audit.prompt_record_path(self.ctx, "parent").read_text(encoding="utf-8"))
                self.assertEqual(len(record["requestText"]), 12_000)
                self.assertEqual(record["requestCapture"], expected)

    def test_prepare_preserves_report_and_project_state_but_exposes_new_digest(self):
        self.write("A.cpp", "one\n")
        path = self.report()
        original_report = path.read_bytes()
        self.write("A.cpp", "two\n")
        head = self.git("rev-parse", "HEAD")
        index = (self.project / ".git/index").read_bytes()
        result = audit.description.prepare(self.ctx, "parent")
        self.assertEqual(path.read_bytes(), original_report)
        self.assertNotEqual(result["diffDigest"], json.loads(original_report)["diffDigest"])
        self.assertEqual((self.project / "A.cpp").read_text(), "two\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((self.project / ".git/index").read_bytes(), index)

    def test_missing_report_never_uses_conversation_prose(self):
        self.write("Readme.md", "save the draft before closing\n")
        result = self.finalize(self.ctx, "parent", "已完成并编译通过，可以手测。")
        message = self.git("show", "-s", "--format=%B", result["commit"]).decode("utf-8")
        self.assertTrue(message.startswith("[Codex] 新增 Readme.md\n"))
        self.assertNotIn("编译通过", message)
        self.assertIn("未生成可信描述", message)
        self.assertEqual(self.receipt(result["commit"])["descriptionStatus"], "missing")

    def test_request_copy_is_rejected_and_committed_records_are_immutable(self):
        self.write("A.cpp", "marker\n")
        path = self.report("增加机关名称标记并保留激光编辑")
        result = self.finalize(self.ctx, "parent")
        commit = result["commit"]
        original = self.git("cat-file", "commit", commit)
        original_note = self.git("notes", "--ref=codex-p4-diff", "show", commit)
        self.assertEqual(json.loads(original_note)["descriptionStatus"], "request-copy")
        path.write_text("{}", encoding="utf-8")
        self.assertEqual(self.finalize(self.ctx, "parent")["status"], "already-committed")
        self.assertEqual(self.git("cat-file", "commit", commit), original)
        self.assertEqual(self.git("notes", "--ref=codex-p4-diff", "show", commit), original_note)
        with self.assertRaises(ValueError):
            audit.description.prepare(self.ctx, "parent")

    def test_legacy_frozen_transaction_keeps_its_original_message(self):
        self.write("A.cpp", "marker\n")
        legacy = {"changeSummary": "旧事务原始标题", "changeSummarySource": "assistant", "assistantMessageSha256": None}
        with mock.patch.object(audit, "build_change_summary", return_value=legacy), self.fail_update():
            with self.assertRaises(audit.AuditError):
                self.finalize(self.ctx, "parent")
        transaction_path = audit.transaction_path(self.ctx, "parent")
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        self.assertNotIn("descriptionVersion", transaction["auditRecord"])
        report_path = audit.turn_root(self.ctx, "parent") / audit.description.REPORT_NAME
        report_path.write_text('{"title":"不能替换旧事务"}', encoding="utf-8")
        result = self.finalize(self.ctx, "parent")
        recovered = json.loads(transaction_path.read_text(encoding="utf-8"))
        self.assertEqual(recovered["message"], transaction["message"])
        self.assertEqual(result["summary"], "旧事务原始标题")
        self.assertNotIn("descriptionVersion", self.receipt(result["commit"]))

    def test_recovery_drift_discards_semantic_and_verification_claims(self):
        self.write("A.cpp", "marker\n")
        self.report()
        with self.fail_update():
            with self.assertRaises(audit.AuditError):
                self.finalize(self.ctx, "parent")
        frozen = json.loads(audit.transaction_path(self.ctx, "parent").read_text(encoding="utf-8"))
        self.assertEqual(frozen["auditRecord"]["descriptionStatus"], "accepted")
        self.advance_history(self.ctx, {"A.cpp": b"concurrent version\n"})
        result = self.finalize(self.ctx, "parent")
        note = self.receipt(result["commit"])
        self.assertEqual(note["descriptionStatus"], "recovery-drifted")
        self.assertEqual(result["summarySource"], "files")
        self.assertIsNone(note["changeDescription"])
        message = self.git("show", "-s", "--format=%B", result["commit"]).decode("utf-8")
        self.assertNotIn("自动测试：通过", message)
        self.assertEqual(self.git("show", f"{result['commit']}:A.cpp"), b"concurrent version\n")
