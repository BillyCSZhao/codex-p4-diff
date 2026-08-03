# 本地 Git 历史架构及故障排查

## 存储拓扑

~~~text
<P4 ProjectRoot>/                    # 真实工作区，不放置 Git 元数据

%LOCALAPPDATA%/CodexP4Diff/
├─ History/<project-name-hash>/      # 可添加到 GitHub Desktop 的本地 Git 仓库
│  ├─ .git/
│  ├─ .codex-p4-diff/project.json
│  ├─ files/<project-relative-path>  # 小文件的滚动镜像
│  └─ turns/<sequence-turn-id>/
│     ├─ event.json                  # 摘要、动作、哈希、大小、P4 只读元数据
│     └─ changes.patch               # 精确的本回合文本补丁
└─ State/<project-name-hash>/
   ├─ ownership.json                 # 跨会话本地路径所有权
   └─ sessions/<session-id>/
      ├─ session.json
      └─ turns/<turn-id>/            # commit 前的临时快照和重试状态
~~~

项目键由 P4 server、client 和项目绝对路径共同生成。历史仓库与真实工作区没有 `.git` 连接，因此 GitHub Desktop 的分支切换或丢弃操作不会修改 P4 文件。

## Commit 拓扑

`codex/base` 只包含一次初始化记录。每个会话分支从它开始：

~~~text
codex/base
  └─ [Codex] turn 1
       └─ [Codex] turn 2
            └─ [Codex] turn 3
~~~

每个有变化的回合只创建一个 `[Codex]` commit。无变化回合写入本地会话状态但不创建空 commit。

首次接触一个已有文件时，`files/...` 在 Git 中表现为新增完整文件，因为分支父提交尚无该文件；同一 commit 的 `turns/.../changes.patch` 始终保存精确的写入前后文本差异。后续回合在镜像状态与写入前状态一致时显示普通 Git 文件 Diff。若文件在回合之间被外部修改，插件不把外部变化冒充 Codex 修改，而把镜像标记为 `drifted-receipt-only`，以补丁和哈希作为审计依据。

## 大文件和二进制

- 默认阈值为 8 MiB。
- 阈值以内的文件可写入 `files/...` 镜像；GitHub Desktop 对二进制显示 binary changed。
- 超过阈值的文件只在 `event.json` 保存前后 SHA-256、大小和 P4 元数据，不把完整资产写入 Git 对象库。
- 设置 `CODEX_P4_DIFF_MAX_BLOB_BYTES` 可调整阈值。降低阈值可以限制 Unreal 资产历史增长。

## P4 只读保证

运行时代码用白名单拒绝非只读 P4 命令。允许集合固定为：

~~~text
p4 info
p4 where
p4 fstat
p4 diff
p4 opened
p4 print
~~~

插件不创建或更新 P4 changelist，不 checkout 文件，不 reconcile，不 reopen，不 revert，不 shelve，也不 submit。旧版插件创建的 pending CL 不会自动迁移或删除；由用户继续在 P4V 中处理。

## GitHub Desktop

1. 运行 `--history-path` 获取目录。
2. 在 GitHub Desktop 选择 **File > Add Local Repository** 并选择该目录。
3. 从 **Current Branch** 选择 `codex/session/<session-id>`。
4. 从 History 查看 `[Codex]` commit；打开 `event.json` 查看详情，打开 `changes.patch` 查看首触或漂移文件的精确差异。

历史仓库没有 remote。不要使用 Publish repository；`pre-push` 也会拒绝 push。

## 迁移旧版 0.3

0.3 可能在项目根创建 `.git` 指针并把文件放入 P4 pending CL。0.4 的 `SessionStart` 会删除且只删除指向 `%LOCALAPPDATA%\CodexP4Diff\Native`、`Sidecars` 或旧 `Audit` 目录的 `.git` 文件，不删除目标目录，也不修改 P4 文件或 CL。

若自动迁移没有运行，执行 `--detach-legacy-review`。真实 `.git` 目录、未知 `.git` 文件和外部 Git 指针都保持不变并报告原因。

## 常见问题

### doctor 显示 `ready=false`

按 `checks[].hint` 修复。常见原因是 P4 登录失效、项目未映射、Git 缺失、历史仓库被添加 remote，或仍存在旧版 `.git` 指针。

### 回合结束没有 commit

先确认本回合确实改变了 Hook 在写入前识别到的文件。无文件变化不会创建空 commit。插件升级后必须重启 Codex、信任新版 Hooks 并新建任务；旧任务不会热加载 Hook。

### shell 命令修改了文件但没有精确补丁

优先让 Codex 使用 `apply_patch`、`Edit`、`Write`，或在 shell 命令中传入明确文件路径。通用 Hook 无法可靠推断不透明脚本在运行后内部选择的所有路径；执行后才发现的路径只记录收据，不能伪造写入前快照。

### 同一路径被另一个会话锁定

结束原 Codex 会话以触发 `SessionEnd`。异常退出留下的所有权记录在 24 小时后视为过期。不要通过 P4 reopen 解决，因为冲突记录完全是本地 Codex 会话状态。

### commit 失败

真实 P4 工作区修改保持不变。修复 `event.json` 中报告的 Git/权限/remote 问题后重试回合收尾。不要删除 `State/.../sessions`，其中包含重试所需的写入前快照。
