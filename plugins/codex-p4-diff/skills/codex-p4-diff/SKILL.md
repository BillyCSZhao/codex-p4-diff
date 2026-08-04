---
name: codex-p4-diff
description: 在 P4 工程根的固定本地 Git 分支中记录所有 Codex 会话，并在每个有文件变化的用户回合结束时自动创建一个本地 Git commit；记录会话、回合、需求摘要、文件动作、前后哈希和文本补丁，同时保证 Perforce 仅被只读查询。适用于在 Codex Review 或 GitHub Desktop 追溯不同 Codex 会话和回合的修改、诊断自动提交、迁移旧版 P4 CL/外置历史模式，或核对插件是否完全不写入 P4。
---

# Codex P4 Diff

把 Codex 修改历史保存到 P4 工程根的本地 Git 仓库：

- 所有 `session_id` 共用固定 `codex/local-history` 分支；不按会话切换分支。
- 一个有文件变化的 `turn_id` 对应一个自动本地 commit；无变化时不创建空 commit。
- commit message 保存脱敏需求摘要、session/turn ID、文件动作、前后 SHA-256 和大小。
- 完整审计收据保存在 `refs/notes/codex-p4-diff`，包含时间、P4 只读元数据和精确文本补丁。
- 默认提交不超过 8 MiB 的文件；更大的文件只记录哈希、大小和 P4 元数据。用 `CODEX_P4_DIFF_MAX_BLOB_BYTES` 调整阈值。
- 仓库不配置 remote，并用 `pre-push` 拒绝 push；不要发布到远程。

## P4 边界

只允许插件调用 P4 只读命令：`info`、`where`、`fstat`、`diff`、`opened`、`print`。

不要通过插件调用 `change`、`edit`、`add`、`delete`、`reconcile`、`reopen`、`revert`、`shelve` 或 `submit`。P4 checkout、变更列表归属和提交继续由用户、P4V、Unreal Source Control 或项目工具负责。

跨会话同路径冲突只使用 `%LOCALAPPDATA%\CodexP4Diff\State` 的本地所有权记录判断，不读取或修改 P4 changelist 归属。另一个活跃 Codex 会话已记录同一路径时，停止写入并报告 session ID。

## 自动生命周期

1. `SessionStart` 验证 P4 映射，在项目根安全初始化或验证插件拥有的 `.git`，检查固定分支、无 remote 和 push 阻断，并建立会话账本。
2. `UserPromptSubmit` 创建回合记录，保存脱敏摘要和 prompt SHA-256。
3. `PreToolUse` 在 `apply_patch`、`Edit`、`Write` 或明确写入型 shell 工具执行前保存目标文件状态并取得本地会话所有权。
4. `PostToolUse` 补记执行后才暴露的路径；明确标记写入前内容不可用，不冒充精确差异。
5. `Stop` 比较前后状态，在全局固定分支串行创建一个 commit，并仅同步本回合路径的 index。目标路径已有 staged 内容时停止；不相关 staged 内容保持不变。
6. `SessionEnd` 结束本地账本并释放本地路径所有权，不执行 P4 写操作。

Hooks 只覆盖经过通用 Tool Hooks 的写入。专用工具绕过 Hook 时，不要声称记录了精确写入前内容；明确报告覆盖缺口。

## 诊断

从当前 `SKILL.md` 所在目录解析 `SkillRoot`。Windows 运行：

~~~powershell
$Runner = Join-Path $SkillRoot 'scripts\run_codex_p4_diff.ps1'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Runner `
  --workspace $ProjectRoot `
  --project-root $ProjectRoot `
  --doctor
~~~

macOS/Linux 运行：

~~~bash
sh "$SkillRoot/scripts/run_codex_p4_diff.sh" \
  --workspace "$ProjectRoot" \
  --project-root "$ProjectRoot" \
  --doctor
~~~

检查 `ready`、`p4Mode`、`p4Writes`、`historyRoot`、`branch` 和 `legacyGitPointer`。`historyRoot` 应等于项目根，`branch` 应为 `codex/local-history`。真实/外来 Git 仓库、未知 `.git` 指针、remote 或错误分支都会停止自动提交，插件不会擅自接管或切换。

用 `--history-path` 输出项目 Git 根。GitHub Desktop 添加项目根后保持 `codex/local-history`，从 History 查看 `[Codex]` commit。Codex Review 可查看 `Last turn` 或最新 `Commit`；`Unstaged` 只显示尚未被 Stop hook 记录的变化。

查看完整审计详情：

~~~powershell
git notes --ref=codex-p4-diff show <commit>
~~~

遇到存储布局、首触文件 Diff、大文件、GitHub Desktop 或迁移问题时，读取 [本地 Git 历史架构及故障排查](references/p4-git-mapping.md)。

## 安全规则

- 工程 `.git` 使用真实 P4 工作树。不要用 Git checkout、switch、discard、restore、reset、clean、merge 或 rebase 操作项目文件；通过正常 P4 流程恢复。
- 不要给仓库添加 remote。发现 remote 时停止自动提交并报告。
- 不要恢复旧版 `pre-commit` 阻断；本地回合 commit 是核心行为。
- 不要删除或修改旧版 P4 pending CL；升级只停止插件干预 P4。
- 不要删除 0.4 外置历史仓库；0.5 不自动导入或清理旧历史。
- 插件更新后重启 Codex、重新信任 Hooks，并新建任务；旧任务不会热加载新版 Hook。
