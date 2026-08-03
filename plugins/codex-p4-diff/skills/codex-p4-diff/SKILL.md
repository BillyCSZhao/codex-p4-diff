---
name: codex-p4-diff
description: 为 P4 工作区中的每个 Codex 会话建立独立的本地 Git 分支，并在每个有文件变化的用户回合结束时自动创建一个本地 Git commit；记录需求摘要、文件动作、前后哈希和文本补丁，同时保证 Perforce 仅被只读查询。适用于追溯不同 Codex 会话或回合的修改、在 GitHub Desktop 查看本地 Codex 历史、诊断回合自动提交、迁移旧版 P4 CL/原生 Review 模式，或核对插件是否完全不写入 P4。
---

# Codex P4 Diff

把 Codex 的修改历史保存到 P4 工作区之外的本地 Git 仓库：

- 一个 `session_id` 对应一个 `codex/session/<session-id>` 分支。
- 一个有文件变化的 `turn_id` 对应一个自动 Git commit；无变化时不创建空 commit。
- commit 保存脱敏需求摘要、文件动作、前后 SHA-256、大小、只读 P4 元数据和精确文本补丁。
- 默认复制不超过 8 MiB 的文件到历史镜像；更大的文件只保存哈希、大小和 P4 元数据。用 `CODEX_P4_DIFF_MAX_BLOB_BYTES` 调整阈值。
- 历史仓库不配置 remote，并用 `pre-push` 拒绝 push；不要把它发布到远程。

## P4 边界

只允许插件调用以下 P4 只读命令：`info`、`where`、`fstat`、`diff`、`opened`、`print`。

不要通过插件调用 `change`、`edit`、`add`、`delete`、`reconcile`、`reopen`、`revert`、`shelve` 或 `submit`。P4 checkout、变更列表归属和提交继续由用户、P4V、Unreal Source Control 或项目工具负责。

跨会话文件冲突只使用 `%LOCALAPPDATA%\CodexP4Diff\State` 下的本地所有权记录判断，不读取或修改 P4 changelist 归属。另一个活跃 Codex 会话已记录同一路径时，停止写入并报告会话 ID。

## 自动生命周期

1. `SessionStart` 验证 P4 映射，创建外置历史仓库，建立会话账本，并安全移除旧版工具拥有的项目 `.git` 指针。
2. `UserPromptSubmit` 创建本回合记录，保存脱敏摘要和 prompt SHA-256。
3. `PreToolUse` 在 `apply_patch`、`Edit`、`Write` 或明确写入型 shell 工具执行前保存目标文件状态并取得本地会话所有权。
4. `PostToolUse` 补记工具执行后才暴露的路径；这类路径会明确标记写入前内容不可用，不冒充精确差异。
5. `Stop` 比较前后状态并在会话分支创建一个本地 Git commit。提交失败时保留真实工作区文件和本地回合状态，报告错误以便重试。
6. `SessionEnd` 结束本地账本并释放本地路径所有权，不执行任何 P4 写操作。

Hooks 只覆盖经过通用 Tool Hooks 的写入。若专用工具绕过 Hook，不能声称已记录精确的写入前内容；明确报告覆盖缺口。

## 诊断

从当前 `SKILL.md` 所在目录解析 `SkillRoot`，不要相对项目 cwd 猜脚本位置。Windows 运行：

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

检查 `ready`、`p4Mode`、`p4Writes`、`historyRoot` 和 `legacyGitPointer`。若只剩旧版 `.git` 指针，可运行 `--detach-legacy-review`；该命令只移除指向本工具旧 `Native`/`Sidecars` 目录的 `.git` 文件，拒绝删除真实 Git 仓库或未知指针。

用 `--history-path` 输出外置历史仓库路径。把该路径作为现有本地仓库添加到 GitHub Desktop，然后选择 `codex/session/<session-id>` 分支查看 commit、描述、文件镜像和 `turns/.../changes.patch`。

遇到存储布局、首触文件 Diff、大文件策略、GitHub Desktop 或迁移问题时，读取 [本地 Git 历史架构及故障排查](references/p4-git-mapping.md)。

## 安全规则

- 不要把外置历史 Git 的 discard、checkout 或 reset 操作作用到真实 P4 工作区；历史仓库与工作区没有 `.git` 连接。
- 不要给历史仓库添加 remote。发现 remote 时停止自动提交并报告。
- 不要恢复旧版 `pre-commit` 阻断；本地回合 commit 是本插件的核心行为。
- 不要删除旧版 P4 pending CL。升级只停止插件继续干预 P4，旧 CL 由用户按现有 P4 流程处理。
- 插件更新后重启 Codex、重新信任 Hooks，并新建任务；旧任务不会热加载新版 Hook。
