# Codex P4 Diff

[English](README.md) | 当前公开版本：[v0.5.3](https://github.com/BillyCSZhao/codex-p4-diff/tree/v0.5.3)

Codex P4 Diff 在 P4 工程根建立一个仅供本地审阅的 Git 历史，不修改 Perforce：

- 所有 Codex 会话共用固定的 `codex/local-history` 分支；
- 每个有文件变化的用户回合自动创建一个本地 Git commit；
- commit 标题描述实际完成的改动，正文保留脱敏后的原始需求、会话、回合、路径、前后哈希和大小；
- `refs/notes/codex-p4-diff` 保存完整审计收据和可获取的精确文本补丁；
- P4 只允许只读查询，不创建、修改或提交 changelist。

工程内的本地 Git 仓库没有 remote，并安装 `pre-push` hook 阻止发布。真实工程相对路径可以直接在 Codex Review 和 GitHub Desktop 中审阅。

## v0.5.3 更新

shell 审计现在只认领明确的文件写入目标。PowerShell 的 `$ErrorActionPreference = 'Stop'`、正则中的 `>`、普通引号字符串以及目录/Junction 创建不再被误判为工程文件锁。同路径所有权缩小到正在执行的回合：成功 commit 或无变化 Stop 后立即释放；收尾失败时保留以便安全重试。升级后的第一个新任务还会清理旧解析器留下的无效字符串记录。

## 安装

前置条件：支持插件和 Hooks 的 Codex、Python 3.10+、Git 2.x，以及已登录并正确映射 workspace 的 `p4`。

```powershell
codex plugin marketplace add BillyCSZhao/codex-p4-diff
codex plugin add codex-p4-diff@billycszhao-plugins
codex plugin list --json
```

确认 `codex-p4-diff@billycszhao-plugins` 的版本为 `0.5.3`。如果 Windows PowerShell 执行策略阻止 `codex.ps1`，把命令中的 `codex` 改成 `codex.cmd`。

安装后重启 Codex，信任插件 Hooks，在 P4 工程目录中创建新任务。已经打开的旧任务不会热加载新版 Hook。

## 升级

```powershell
codex plugin marketplace upgrade billycszhao-plugins
codex plugin add codex-p4-diff@billycszhao-plugins
codex plugin list --json
```

如果 `plugin add` 提示已经安装，或者版本仍低于 `0.5.3`，只重装公开 marketplace 下的这个插件：

```powershell
codex plugin remove codex-p4-diff@billycszhao-plugins
codex plugin add codex-p4-diff@billycszhao-plugins
codex plugin list --json
```

不要误删其他 marketplace 中的本地开发版插件。升级后需要重启 Codex、重新信任 Hooks，并新建任务。

## 自动记录规则

任务开始时，插件在 P4 工程根初始化或验证自己拥有的 `.git`，并检查固定分支、无 remote 和 push 阻断。插件不会接管已有的外来 Git 仓库，也不会自动切换分支。

每个有文件变化的回合会生成一条 `[Codex]` commit。不同会话按回合完成时间依次进入同一分支，无变化回合不创建空 commit。插件只处理当前回合记录到的路径，不运行 `git add -A`；同一路径已有 staged 内容时会拒绝提交，不相关 staged 内容保持不变。

shell 命令只有在 Hook 能识别出高置信文件写入目标时才参与同路径保护，例如 `-LiteralPath`、`-Path`、`-Destination`、`-FilePath` 或引号外的重定向目标。普通消息、正则、枚举值以及目录/Junction 目标不会被认领为文件。

commit 标题格式为 `[Codex] <实际完成的改动>`。插件从助手最终回复中提取第一条有效内容，并跳过空泛标题、纯状态行、代码块和 Codex 指令。无法提取时，按本回合文件动作和文件名生成确定性标题。

commit 正文包含 `Change`、`Change-Summary-Source`、脱敏后的原始 `Request`、session/turn ID、文件动作、哈希、大小和历史状态。完整 note 还包含 `changeSummary`、`changeSummarySource`、`assistantMessageSha256`、`promptSummary`、prompt SHA-256、P4 只读元数据和可获取的精确文本补丁。note 只保存助手回复的哈希，不复制完整回复正文。

默认只把不超过 8 MiB 的文件内容写入 Git。更大的文件仍会在 notes 中记录哈希、大小和 P4 元数据。可通过 `CODEX_P4_DIFF_MAX_BLOB_BYTES` 调整阈值。

## 查看提交记录

在 GitHub Desktop 中选择 **File > Add Local Repository**，添加 P4 工程根，保持在 `codex/local-history` 分支，然后从 **History** 查看各回合 commit。不要点击 Publish。

在 Codex Review 中查看 `Last turn` 或最新 `Commit`。Stop hook 已经记录的修改不会继续出现在 `Unstaged` 中，因此 `Unstaged` 为空通常是正常现象。

查看一条 commit 的完整审计收据：

```powershell
git notes --ref=codex-p4-diff show <commit>
```

## Doctor

先运行 `codex plugin list --json`，找到 `codex-p4-diff@billycszhao-plugins`，将其 `source.path` 填入 `$PluginRoot`。然后在 P4 工程根执行：

```powershell
$ProjectRoot = (Get-Location).Path
$PluginRoot = 'C:\path\shown-by-codex-plugin-list'
$SkillRoot = Join-Path $PluginRoot 'skills\codex-p4-diff'
$Runner = Join-Path $SkillRoot 'scripts\run_codex_p4_diff.ps1'

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Runner `
  --workspace $ProjectRoot `
  --project-root $ProjectRoot `
  --doctor
```

应重点确认：

- `ready=true`
- `p4Mode=read-only`
- `p4Writes=false`
- `historyRoot` 等于 P4 工程根
- `branch=codex/local-history`
- 工程 Git 没有 remote

## P4 与 Git 安全边界

插件只允许调用 `p4 info`、`where`、`fstat`、`diff`、`opened` 和 `print`。P4 checkout、changelist 归属、shelve 和 submit 继续由用户、P4V、Unreal Source Control 或项目工具负责。

工程 `.git` 直接使用真实 P4 工作树。不要对它执行 Git checkout、switch、discard、restore、reset、clean、merge 或 rebase，这些操作可能改写工程文件。文件恢复继续使用正常 P4 流程。

不要给该 Git 仓库添加 remote，也不要绕过 `pre-push` hook。commit 和 notes 可能包含项目源码及本地元数据，只应用于本地审阅。

## 迁移与常见问题

- 0.3 可能创建过 P4 pending CL；0.5 不修改这些 CL，由用户继续在 P4V 中管理。
- 0.4 的外置历史不会被 0.5 导入或删除，可作为旧记录保留。
- 0.5.3 的第一个新任务会迁移本地所有权状态，删除旧 shell 解析器产生的非路径记录。
- 回合结束没有 commit：确认该回合确实改了文件，并确认升级后已经重启 Codex、信任 Hooks、新建任务。
- 新 commit 标题仍显示用户回复：旧任务不会热加载新版 Hook；确认版本至少为 `0.5.2` 后重启 Codex、重新信任 Hooks并新建任务。旧 commit 保持原样。
- `Unstaged` 为空：切换到 `Last turn` 或最新 `Commit`。
- Doctor 显示 `ready=false`：检查 P4 登录和映射、Git 是否可用、工程是否已有外来 `.git`、是否配置了 remote，以及当前分支是否为 `codex/local-history`。
- 同一路径被另一会话锁定：先确认确实是同一真实文件。正常 commit 或无变化 Stop 会立即释放；只有收尾失败或异常中断才保留，异常记录 24 小时后过期。

更详细的架构与故障排查见插件内的 `skills/codex-p4-diff/references/p4-git-mapping.md`。
