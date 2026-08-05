# 本地 Git 历史架构及故障排查

## 存储拓扑

~~~text
<P4 ProjectRoot>/
└─ .git/
   ├─ codex-p4-diff/project.json    # 插件所有权、项目和初始 commit
   ├─ info/exclude                  # 默认忽略全部未追踪文件；回合路径显式加入
   ├─ hooks/pre-push                # 阻止发布本地审计历史
   └─ refs/notes/codex-p4-diff      # 每条 commit 的完整回合收据

%LOCALAPPDATA%/CodexP4Diff/State/<project-name-hash>/
├─ ownership.json                   # 跨会话同路径所有权
└─ sessions/<session-id>/
   ├─ session.json
   └─ turns/<turn-id>/              # commit 前快照和重试状态
~~~

项目键由 P4 server、client 和项目绝对路径生成，只用于外置 State。0.4 的 `%LOCALAPPDATA%/CodexP4Diff/History` 不迁移、不删除，仍可作为旧记录查看。

## Commit 拓扑

所有会话按回合完成时间推进固定分支：

~~~text
codex/local-history
  Initialize Codex local history
    └─ [Codex] 修复 NPC 资产命名规则
         └─ [Codex] 新增 Config.ini
              └─ [Codex] 更新 3 个文件：A.cpp、B.h 等
~~~

project lock 序列化读取 HEAD、生成 commit、更新 notes/index/ref 的流程。commit 标题优先来自助手最终回复中的实际完成内容，无法提取时按文件动作生成；原始用户请求保留在正文和 note 中。commit message 和 note 都包含 session/turn，因此无需分支隔离也能追溯归属。

## 精确路径与漂移

- 小文件直接以真实工程相对路径进入 commit，Codex Review 和 GitHub Desktop 显示真实文件 Diff。
- 首次接触已有 P4 文件时，Git 父提交没有该路径，因此 commit Diff 显示完整新增；note 中的 `textPatch` 保存精确写入前后差异。
- 若 Git HEAD 内容与 Hook 捕获的写入前内容不一致，标记 `drifted-receipt-only`，不把外部变化冒充 Codex 修改。
- 删除仅在 Git HEAD 与捕获前内容一致时进入 commit，否则用 `delete-receipt-only` 记录。
- 超过 8 MiB 的文件默认只记录收据，不写入 Git blob。

## Git index 边界

插件使用临时 index 构造 commit，从不运行 `git add -A`。提交前检查本回合目标路径：若已有 staged 变化则停止，避免覆盖用户或其他工具的暂存状态。不相关路径的 staged 内容保留且不会进入 Codex commit。commit 成功后只把本回合实际提交路径同步到主 index，使工作树保持干净并保留其他 staged 状态。

## P4 只读保证

允许集合固定为 `info`、`where`、`fstat`、`diff`、`opened`、`print`。插件不创建或更新 P4 changelist，不 checkout、reconcile、reopen、revert、shelve 或 submit。旧 pending CL 由用户在 P4V 中处理。

## 查看历史

在 Codex Review 选择 `Last turn` 或最新 `Commit`。在 GitHub Desktop 添加项目根，保持 `codex/local-history`，从 History 选择 commit。标题表示实际完成的改动；原始请求查看正文 `Request`。完整收据运行：

~~~powershell
git notes --ref=codex-p4-diff show <commit>
~~~

不要 Publish。因为 `.git` 直接绑定真实 P4 工作树，Git checkout、switch、discard、restore、reset、clean、merge 和 rebase 会修改项目文件。

## 迁移

0.3 可能创建 pending CL 和指向插件本地数据的 `.git` 文件；0.5 只会移除已知插件拥有的旧指针，不修改 P4 CL。0.4 外置历史保留原样。若项目已有真实 `.git` 或外来指针，0.5 拒绝接管。升级后的标题规则只影响新任务创建的新 commit，不改写已有历史。

## 常见问题

### doctor 显示 `ready=false`

常见原因是 P4 登录失效、项目未映射、Git 缺失、项目已有外来仓库、配置了 remote 或当前不在 `codex/local-history`。插件不会自动切换分支。

### 回合结束没有 commit

无文件变化不会创建空 commit。确认 Hook 在写入前识别了目标路径；升级后需重启 Codex、信任新版 Hooks并新建任务。

### commit 标题仍然是用户回复

确认安装版本至少为 `0.5.2`。旧任务不会热加载新版 Stop hook；重启 Codex、重新信任 Hooks 并新建任务。已有 commit 保持不可变，可在正文或 note 中区分实际改动和原始请求。

### Review 的 Unstaged 为空

Stop hook 已把本回合改动提交后，`Unstaged` 为空是正常的。切换到 `Last turn` 或最新 `Commit`。

### shell 修改没有精确补丁

优先使用 `apply_patch`、`Edit`、`Write`，或传入明确文件路径。执行后才发现的路径只记录收据，不能伪造写入前快照。

### 同一路径被另一个会话锁定

结束原任务以触发 `SessionEnd`。异常退出的所有权记录 24 小时后过期。不要通过 P4 reopen 解决。

### commit 失败

真实 P4 文件保持修改状态。修复 Git 分支、remote、staged 冲突或权限问题后重试 Stop；不要删除 State 会话目录，其中有重试所需快照。
