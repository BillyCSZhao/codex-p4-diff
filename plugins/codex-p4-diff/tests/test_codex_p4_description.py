from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "codex-p4-diff" / "scripts"))
import codex_p4_description as description


class DescriptionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.report_path = Path(self.temp.name) / "report.json"

    def change(self, path="Source/A.cpp", action="edit"):
        return {
            "path": path, "action": action, "ownershipAttribution": "exact",
            "historyState": {"add": "first-touch", "edit": "exact", "delete": "exact-delete"}[action],
            "before": {"exists": action != "add", "sha256": None if action == "add" else "a" * 64},
            "after": {"exists": action != "delete", "sha256": None if action == "delete" else "b" * 64},
        }

    def report(self, changes, title="修复子任务修改的归档范围"):
        return {
            "schemaVersion": 1, "sessionId": "session", "turnId": "turn",
            "diffDigest": description.diff_digest(changes), "title": title,
            "why": "将子任务已写入的文件纳入对应用户回合，方便完整审阅。",
            "changes": [{"description": "根据实际文件变化记录完整路径。", "paths": [c["path"] for c in changes]}],
            "decisions": ["保留现有事务恢复流程。"], "verification": [],
            "limitations": ["尚未完成编辑器交互手测。"],
        }

    def run_report(self, changes, report=None):
        if report is not None:
            self.report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return description.describe(
            self.report_path, "session", "turn", changes,
            lambda value, limit=180: str(value).replace("secret-value", "<redacted>"),
            lambda items: f"更新 {len(items)} 个文件",
        )

    def assert_fallback(self, result, status):
        self.assertEqual(result["descriptionVersion"], 1)
        self.assertEqual(result["descriptionStatus"], status)
        self.assertEqual(result["changeSummarySource"], "files")
        self.assertIsNone(result["changeDescription"])

    def test_add_edit_delete_reports_are_accepted(self):
        for action, title in (("add", "新增场景标记选择入口"), ("edit", "修复句柄选择拦截"), ("delete", "删除重复选择适配逻辑")):
            with self.subTest(action=action):
                changes = [self.change(action=action)]
                result = self.run_report(changes, self.report(changes, title))
                self.assertEqual(result["descriptionStatus"], "accepted")
                self.assertEqual(result["changeSummarySource"], "structured-ai")
                self.assertEqual(result["changeSummary"], title)

    def test_documentation_and_test_changes_keep_verification_categories_distinct(self):
        changes = [self.change("docs/README.md"), self.change("tests/test_selection.py", "add")]
        report = self.report(changes, "补充选取行为说明和回归测试")
        report["verification"] = [{"kind": "static", "status": "passed", "details": "已核对文档与测试断言。", "evidence": "AI reported: reviewed docs/README.md and tests/test_selection.py"}]
        result = self.run_report(changes, report)
        self.assertEqual(result["descriptionStatus"], "accepted")
        verification = {v["kind"]: v for v in result["changeDescription"]["verification"]}
        self.assertEqual(verification["static"]["status"], "passed")
        for kind in ("build", "automated", "manual"):
            self.assertEqual(verification[kind]["status"], "not_run")

    def test_omitted_subagent_path_is_incomplete_even_with_current_digest(self):
        changes = [self.change("Source/Root.cpp"), self.change("Source/Child.cpp", "add")]
        report = self.report(changes)
        report["changes"][0]["paths"] = ["Source/Root.cpp"]
        self.assert_fallback(self.run_report(changes, report), "incomplete")

    def test_documentation_only_and_test_only_changes_need_no_build_claim(self):
        for path, title in (("docs/selection.md", "说明标记选取与激光句柄的优先级"),
                            ("tests/test_selection.py", "增加激光句柄优先级回归检查")):
            with self.subTest(path=path):
                changes = [self.change(path)]
                report = self.report(changes, title)
                report["verification"] = [{"kind": "build", "status": "not_applicable",
                                           "details": "无编译目标变更。", "evidence": ""}]
                result = self.run_report(changes, report)
                self.assertEqual(result["descriptionStatus"], "accepted")
                self.assertEqual(result["changeDescription"]["verification"][0]["status"], "not_applicable")

    def test_report_becomes_stale_after_file_content_changes(self):
        changes = [self.change()]
        report = self.report(changes)
        changes[0]["after"]["sha256"] = "c" * 64
        self.assert_fallback(self.run_report(changes, report), "stale")

    def test_missing_report_and_empty_or_closing_titles_fall_back(self):
        changes = [self.change()]
        self.assert_fallback(self.run_report(changes), "missing")
        for title in ("", "已完成", "可以手测", "建议先测试", "请确认", "done"):
            with self.subTest(title=title):
                self.assert_fallback(self.run_report(changes, self.report(changes, title)), "invalid")

    def test_uncertain_or_receipt_only_changes_do_not_claim_implementation(self):
        for key, value in (("ownershipAttribution", "uncertain"), ("historyState", "drifted-receipt-only")):
            with self.subTest(field=key):
                changes = [self.change()]
                changes[0][key] = value
                self.assert_fallback(self.run_report(changes, self.report(changes)), "uncertain")

    def test_deeply_nested_json_falls_back_without_interrupting_archive(self):
        self.report_path.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
        self.assert_fallback(self.run_report([self.change()]), "invalid")

    def test_completed_verification_requires_evidence(self):
        changes = [self.change()]
        for status in ("passed", "failed", "partial"):
            with self.subTest(status=status):
                report = self.report(changes)
                report["verification"] = [{"kind": "automated", "status": status, "details": "检查结果", "evidence": ""}]
                self.assert_fallback(self.run_report(changes, report), "invalid")

    def test_all_narrative_fields_are_redacted(self):
        changes = [self.change()]
        report = self.report(changes, "修复 secret-value 的配置读取")
        report["why"] = "避免 secret-value 配置失效"
        report["changes"][0]["description"] = "处理 secret-value"
        report["decisions"] = ["保留 secret-value 行为"]
        report["limitations"] = ["未验证 secret-value"]
        report["verification"] = [{"kind": "build", "status": "passed", "details": "构建 secret-value", "evidence": "AI reported command output secret-value"}]
        result = self.run_report(changes, report)
        self.assertEqual(result["descriptionStatus"], "accepted")
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertIn("<redacted>", result["changeSummary"])

    def test_digest_is_order_independent_and_bound_to_file_identity(self):
        changes = [self.change(), self.change("Source/B.cpp", "add")]
        digest = description.diff_digest(changes)
        self.assertEqual(digest, description.diff_digest(list(reversed(changes))))
        self.assertEqual(len(digest), hashlib.sha256().digest_size * 2)
        changed = copy.deepcopy(changes)
        changed[1]["path"] = "Source/Other.cpp"
        self.assertNotEqual(digest, description.diff_digest(changed))


if __name__ == "__main__":
    unittest.main()
