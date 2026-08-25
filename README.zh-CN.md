# Codex P4 Diff

[English](README.md) | 当前公开版本：[v0.5.5](https://github.com/BillyCSZhao/codex-p4-diff/tree/v0.5.5)

Codex P4 Diff 在 P4 工程根建立一个仅供本地审阅的 Git 历史，不修改 Perforce：

- 所有 Codex 会话共用固定的 `codex/local-history` 分支；
- 每个有文件变化的用户回合自动创建一个本地 Git commit；
- commit 标题描述实际完成的改动，正文保留脱敏后的原始需求、会话、回合、路径、前后哈希和大小；
- `refs/notes/codex-p4-diff` 保存完整审计收据和可获取的精确文本补丁；
- P4 只允许只读查询，不创建、修改或提交 changelist。

工程内的本地 Git 仓库没有 remote，并安装 `pre-push` hook 阻止发布。真实工程相对路径可以直接在 Codex Review 和 GitHub Desktop 中审阅。

## v0.5.5 更新

已有插件本地历史会保持稳定的 P4 身份。插件优先复用并重新验证 `.git/codex-p4-diff/project.json` 中记录的 client 和 server，再考虑环境或 P4V 自动发现，因此错误的默认 `P4CLIENT` 或两个同时映射该目录的 P4V workspace 不会再把既有历史和 State 静默切到另一套身份。

从已有 owned project 的子目录启动任务时，会向上复用最近的父级历史根；如果途中遇到更近的外来仓库或未知 `.git` 指针，就停止向上查找，绝不穿透或接管。已确认由插件创建的旧式指针会把所在目录确定为工程根，并沿用既有的串行迁移规则。首次建立历史且没有 owned marker 时，如果多个 P4 client 都能映射当前目录，Hook 会显示可操作的 fail-open 告警，不再静默跳过；正常工具操作仍继续。普通未映射目录仍视为不适用。P4 运行时白名单保持不变且始终只读。

## 安装

前置条件：支持插件和 Hooks 的 Codex、Python 3.10+、Git 2.x，以及已登录并正确映射 workspace 的 `p4`。

```powershell
codex plugin marketplace add BillyCSZhao/codex-p4-diff
codex plugin add codex-p4-diff@billycszhao-plugins
codex plugin list --json
```

确认 `codex-p4-diff@billycszhao-plugins` 的版本为 `0.5.5`。如果 Windows PowerShell 执行策略阻止 `codex.ps1`，把命令中的 `codex` 改成 `codex.cmd`。

安装后重启 Codex，信任插件 Hooks，在 P4 工程目录中创建新任务。已经打开的旧任务不会热加载新版 Hook。

## 升级

```powershell
codex plugin marketplace upgrade billycszhao-plugins
codex plugin add codex-p4-diff@billycszhao-plugins
codex plugin list --json
```

如果 `plugin add` 提示已经安装，或者版本仍低于 `0.5.5`，只重装公开 marketplace 下的这个插件：

```powershell
codex plugin remove codex-p4-diff@billycszhao-plugins
codex plugin add codex-p4-diff@billycszhao-plugins
codex plugin list --json
```

不要误删其他 marketplace 中的本地开发版插件。升级后需要重启 Codex、重新信任 Hooks，并新建任务。

## 自动记录规则

任务开始时，插件从当前目录向上寻找最近的 `.git/codex-p4-diff/project.json`。找到 owned marker 后，以其中的项目根、P4 client 和 server 作为既有身份，并通过只读 P4 查询重新验证；从子目录启动的任务因此继续使用父级 history 和 State。向上查找一旦遇到更近的外来仓库或未知 `.git` 指针就停止，不会穿透该边界；已确认由插件创建的旧式指针则把所在目录确定为工程根，并可按既有规则迁移。首次使用时应从预期的 P4 工程根创建任务。旧指针迁移和仓库初始化由 `.repo-setup.lock` 串行化；插件不会接管外来 Git 仓库，也不会自动切换分支。

每个有文件变化的回合会生成一条 `[Codex]` commit。不同会话按回合完成时间依次进入同一分支，无变化回合不创建空 commit。插件只处理当前回合记录到的路径，不运行 `git add -A`。目标路径原有 staged 内容会保持不动并记为 `staged-receipt-only`，其余符合条件的路径仍可进入 commit；不相关 staged 内容同样保持不变。最终比较和替换使用 Git 标准 `index.lock`：安全更新先在临时 index 中构造，再原子安装；锁前已完成的外部 stage 会被识别并跳过，比较和替换之间不能插入新的标准 Git stage。

shell 命令只有在 Hook 能静态解析真实文件目标时才参与同路径保护。解析器支持常见命名参数和位置参数、命令链、PowerShell 调用运算符、重定向、现有目录目标和通配符。目录目标会展开为其下当前存在的工程文件，不会把目录本身当作文件锁；普通消息、注释、正则和选项值不会被当作路径。动态语法、未解析变量、响应文件、链接或展开上限会产生明确的 `partial` 覆盖告警，不猜测路径，也不中断命令。

各类有界 OS 锁各自只负责短临界区：`.repo-setup.lock` 串行化旧指针迁移和仓库初始化，`.state.lock` 保护 ownership 与 session 账本，`.project.lock` 串行化 Git 收尾，“回合 + 路径”锁隔离 before 快照，`.deferred-after.lock` 固化首次 Stop-time 快照，uncertainty scope 使用独立短锁，主 index 最终替换由 Git `index.lock` 保护。当前实现不会向锁文件写入占位内容，因此并发初始化不会写到另一进程已经锁定的字节。插件在推进分支、附加 note 或同步 index 前保存分阶段事务。若 history 在事务创建前仍繁忙，Stop 会冻结当时的 after 快照、密封逐路径归因、释放旧认领并写项目级恢复标记；后续任意 prompt 或会话都能恢复，重复失败也不阻断新回合。终态会短暂重取 `.state.lock`，重新加载最新账本，只合并完成的旧回合并只释放该回合认领。

`PreToolUse` 只在短暂持有共享状态锁时发布路径所有权和轻量的写入前清单预留项，随后在“回合 + 文件”独立锁下读取文件内容，并短暂重新取得状态锁合并快照元数据。慢速或较大的快照不会再让无关会话因状态锁超时而漏掉认领。仅在 `PostToolUse` 才发现的路径或 before 快照失败会明确降级为 partial `after-only-receipt`，不会伪造新增、删除或文本补丁，也不会阻断工具。每个 uncertainty scope 最多保留两个 writer 摘要和一个常态 timeout `pending.json`；压缩时先原子取走 pending，再把匿名时间提升到压缩时刻，因此乱序 writer 和并发清理都不会隐藏证据。

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
- `historyRoot` 等于 owned P4 工程根；从子目录运行时可以是其父目录
- `branch=codex/local-history`
- 工程 Git 没有 remote

## P4 与 Git 安全边界

插件只允许调用 `p4 info`、`where`、`fstat`、`diff`、`opened` 和 `print`。P4 checkout、changelist 归属、shelve 和 submit 继续由用户、P4V、Unreal Source Control 或项目工具负责。

工程 `.git` 直接使用真实 P4 工作树。不要对它执行 Git checkout、switch、discard、restore、reset、clean、merge 或 rebase，这些操作可能改写工程文件。文件恢复继续使用正常 P4 流程。

不要给该 Git 仓库添加 remote，也不要绕过 `pre-push` hook。commit 和 notes 可能包含项目源码及本地元数据，只应用于本地审阅。

## 迁移与常见问题

- 0.3 可能创建过 P4 pending CL；0.5 不修改这些 CL，由用户继续在 P4V 中管理。
- 0.4 的外置历史不会被 0.5 导入或删除，可作为旧记录保留。
- 0.5.3 的第一个新任务会删除旧 shell 解析器产生的非路径所有权记录。
- 0.5.4 会把所有权状态迁移到 schema 8，只保留所属活跃回合写入前清单中的真实路径，并用可续期短租约和 epoch-scoped token 防止 registry 重建后的旧 token 复用；不再保留随历史路径增长的 generation 计数表。
- 0.5.5 会复用并验证已有 owned marker 的 P4 client/server，并让首次多 client 歧义显示为可见告警；既有 P4 changelist 和历史 commit 不会被改写。
- 回合结束没有 commit：确认该回合确实改了文件，并确认升级后已经重启 Codex、信任 Hooks、新建任务。
- 新 commit 标题仍显示用户回复：旧任务不会热加载新版 Hook；确认版本至少为 `0.5.2` 后重启 Codex、重新信任 Hooks并新建任务。旧 commit 保持原样。
- `Unstaged` 为空：切换到 `Last turn` 或最新 `Commit`。
- 首次发现多个可映射 client：在预期工程根用 `--port` 和 `--client` 显式运行 Doctor，修正 P4 环境或 P4V workspace 选择后再新建任务。Hook 告警按 fail-open 处理，不阻断正常工具操作；歧义未解决时不会建立该任务的历史记录。
- 已有 owned marker 无法验证：修复 marker 所绑定 client/server 的 P4 登录或映射，不要删除 marker，也不要把既有 history 改绑到另一 client。
- Doctor 显示 `ready=false`：检查 P4 登录和映射、Git 是否可用、工程是否已有外来 `.git`、是否配置了 remote，以及当前分支是否为 `codex/local-history`。
- 同一路径被另一会话锁定：只有租约未过期且 session、活跃回合、状态、写入前清单和 claim token 均一致时才视为活跃；默认租约为十分钟并在同回合继续操作时续期。Windows 路径键忽略大小写但保留原始显示形式；接管会更换 token，使旧回合只能生成收据。
- Shell 覆盖为 `partial`：查看 Hook 反馈及 Git note 中的 `coverageWarnings`。这是未能静态确认全部写入目标的可见告警，不会阻断命令，也不会伪造精确写入前内容。
- commit 失败：插件会把冻结事务或 Stop-timeout after 快照放入项目级恢复队列，于后续 prompt、任务结束或其他会话启动后自动重试；重复失败不会阻断新回合，修复 Git 分支、remote、权限等根因后仍会再次恢复。不要删除本地 State 中的事务、恢复标记和快照。

更详细的架构与故障排查见插件内的 `skills/codex-p4-diff/references/p4-git-mapping.md`。
