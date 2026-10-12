"""Event-specific output contracts: https://learn.chatgpt.com/docs/hooks ."""

import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/codex-p4-diff/scripts"))
import codex_p4_audit as audit


class HookOutputTest(unittest.TestCase):
    EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd",
              "SubagentStart", "SubagentStop")

    def run_hook(self, event, error=None, finalize_status="no-changes"):
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(event).encode()), encoding="utf-8")
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(audit, "resolve_context", side_effect=error) as context,
            mock.patch.object(audit, "mark_session"),
            mock.patch.object(audit, "record_prompt", return_value="turn-test"),
            mock.patch.object(audit, "prepare_tool", return_value=1),
            mock.patch.object(audit, "finish_tool"),
            mock.patch.object(audit, "finalize_turn", return_value={"status": finalize_status}),
            mock.patch.object(audit, "retry_previous_turn"),
            mock.patch.object(audit, "record_subagent_event", return_value={"status": "subagent-recorded"}),
            mock.patch.object(audit, "end_subagent_tools"),
        ):
            context.return_value.branch = "codex/local-history"
            context.return_value.history_root = "test-history"
            context.return_value.project_root = Path("test-project")
            context.return_value.session_id = "session-test"
            self.assertEqual(audit.hook_main(), 0)
        return json.loads(stdout.getvalue()) if stdout.getvalue() else None

    def assert_contract(self, name, result):
        if name in ("Stop", "SessionEnd", "SubagentStart", "SubagentStop"):
            self.assertEqual(set(result), {"systemMessage"})
            self.assertIsInstance(result["systemMessage"], str)
        else:
            self.assertEqual(set(result), {"hookSpecificOutput"})
            specific = result["hookSpecificOutput"]
            self.assertEqual(specific["hookEventName"], name)
            self.assertEqual(set(specific), {"hookEventName", "additionalContext"})
            self.assertIsInstance(specific["additionalContext"], str)

    def test_normal_and_partial_output_is_tagged_for_the_event(self):
        for name in self.EVENTS:
            for warnings in ([], ["shell-dynamic-syntax-partial"]):
                with self.subTest(event=name, warnings=warnings):
                    result = self.run_hook({"hook_event_name": name, "_codexP4DiffWarnings": warnings})
                    if name == "SessionEnd" or (name == "PostToolUse" and not warnings):
                        self.assertIsNone(result)
                    else:
                        self.assert_contract(name, result)

    def test_session_start_limits_read_only_scope_to_the_plugin(self):
        result = self.run_hook({"hook_event_name": "SessionStart"})
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("P4 只读约束仅适用于 codex-p4-diff 插件自身", text)
        self.assertIn("不限制插件之外经用户授权的 P4 操作", text)
        self.assertIn("其他独立权限与审批要求仍须遵守", text)
        self.assertIn("codex/local-history", text)
        self.assertIn("test-history", text)
        self.assertIn("原生“上一轮”卡由 Codex 单独记录", text)
        self.assertIn("最终文件写入由主会话执行", text)
        self.assertIn("并行直接写入后应检查完整 Git 审阅", text)
        self.assertNotIn("；P4 仅做只读查询。", text)

    def test_stop_reports_pending_children_instead_of_no_changes(self):
        result = self.run_hook({"hook_event_name": "Stop"}, finalize_status="awaiting-child-tools")
        self.assertIn("子任务写入尚未结束", result["systemMessage"])
        self.assertNotIn("没有文件变化", result["systemMessage"])

    def test_prompt_instructs_executor_to_prepare_bound_description(self):
        result = self.run_hook({"hook_event_name": "UserPromptSubmit"})
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("codex_p4_description.py", text)
        self.assertIn('--session "session-test" --turn "turn-test"', text)
        self.assertIn("commit-descriptions.md", text)
        self.assertIn("reportPath", text)
        self.assertIn("构建、自动测试、人工验证分别", text)
        self.assertIn("重新 prepare", text)
        self.assertLess(len(text), 1600)

    def test_fail_open_errors_preserve_event_and_do_not_deny(self):
        for name in self.EVENTS:
            for error in (audit.AuditError("审计失败"), RuntimeError("unexpected")):
                with self.subTest(event=name, error=type(error).__name__):
                    self.assert_contract(name, self.run_hook({"hook_event_name": name}, error))

    def test_ownership_conflict_still_denies_only_pretool(self):
        for name in self.EVENTS:
            with self.subTest(event=name):
                result = self.run_hook({"hook_event_name": name}, audit.OwnershipConflict("owned"))
                if name == "PreToolUse":
                    self.assertEqual(result, {"hookSpecificOutput": {
                        "hookEventName": "PreToolUse", "permissionDecision": "deny",
                        "permissionDecisionReason": "owned",
                    }})
                else:
                    self.assert_contract(name, result)


if __name__ == "__main__":
    unittest.main()
