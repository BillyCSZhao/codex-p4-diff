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
├─ ownership.json                   # 跨会话、回合级同路径所有权
├─ recovery/*.json                  # 项目级跨会话恢复入口
├─ uncertainty/<scope>/
│  ├─ summary.json                  # 最多两个不同 writer 的最新证据
│  ├─ pending.json                  # scope 锁繁忙时的单文件保守 fallback
│  └─ pending-merge-*.json          # 原子取走后的短期压缩输入，成功后清理
└─ sessions/<session-id>/
   ├─ session.json
   └─ turns/<turn-id>/
      ├─ before.json                # commit 前文件清单和快照元数据
      ├─ before-locks/*.lock        # 按文件隔离的短期快照协调锁
      ├─ coverage.json              # Shell/工具覆盖告警
      ├─ .deferred-after.lock       # 固化首次 Stop-time after 快照
      ├─ deferred-after.json        # Stop 在事务前超时时冻结的 after 清单
      ├─ deferred-after/*.bin       # 对应的小文件内容快照
      └─ transaction.json           # 可幂等续跑的 Git 收尾事务
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

所有本地锁都使用 Windows `msvcrt` 或 POSIX `fcntl`，有界等待后 fail-open，不再按锁文件 mtime 删除或抢占。当前实现不向锁文件写入占位内容，避免并发初始化时写入另一进程已经锁定的字节。`.repo-setup.lock` 只保护旧 `.git` 指针迁移和仓库初始化；`.state.lock` 只保护 ownership/session 短更新；`.project.lock` 序列化读取 HEAD、生成 commit、更新 ref/notes/index；before 快照、deferred-after 快照与 uncertainty scope 各有独立短锁；最终主 index 替换还取得 Git `index.lock`。不存在一把覆盖所有慢操作的全局锁。

每次 Git 收尾先写 `transaction.json`，按 prepared、ref-updated、note-updated、index-synced、complete 阶段推进；进程中断或 Hook 重入时从已完成阶段继续，不重复创建同一回合 commit。prepared 后若分支被其他回合推进，不同路径会重建到新 HEAD；同路径已变化则旧回合降级为 `recovery-drifted-receipt-only`。ref-updated 后即使 HEAD 已是 candidate 的后代，也只修复 note/index，不重复推进 ref。若 `.project.lock` 在事务创建前超时，插件在回合级 `.deferred-after.lock` 内冻结并只发布第一次 Stop-time after 快照，再于 `.state.lock` 内把 manifest claim token 与当前 registry 核对后把逐路径 `exact/superseded` 归因密封进 `deferred-after.json`。只有密封成功才释放旧回合 ownership；若另一处理器仍在发布快照则保留认领和项目级 recovery marker。重复或并发处理器只复用已发布记录，不会覆盖已密封归因。密封完成后，后续任意会话可立即认领同一路径，旧回合恢复仍使用密封归因和冻结内容。若某路径的冻结内容不可用，该路径只生成 `deferred-after-unavailable-receipt-only`，after 哈希/大小保持 unknown，不产生 patch 或 tree update，也绝不读取恢复时的 live worktree。

Git 收尾期间不长期占用 `.state.lock`。写入 committed/no-change/failed 终态前会短暂取得 `.state.lock`，重新读取最新 `session.json`，只合并目标旧回合并只释放该回合 ownership。这样其他会话恢复旧回合时，同一原 session 并发创建的新 prompt、`activeTurnId`、其他回合 claim 和 `ownedFiles` 不会被旧 session 对象覆盖。

路径所有权只覆盖正在执行的回合。schema 8 记录使用默认十分钟的可续期租约，并要求 session、`activeTurnId`、回合状态、`before.json` 和 claim token 一致。registry 初始化时生成随机 epoch，每次新 claim 使用新的随机 token；`ownership.json` 损坏、删除、重建或被另一回合接管后，旧 manifest token 不可能被计数器 ABA 复用。状态只保存当前 owners，不保留随历史路径增长的 generation 表。Windows 用大小写无关 canonical key 判断同一路径，同时在记录中保留原始 casing。

`PreToolUse` 在 `.state.lock` 内原子写入 claim token 和 `before.json` 预留项后立即释放共享锁。最多 8 MiB 的内容读取在 `before-locks/<path-hash>.lock` 下完成，不占用共享状态锁；完成后只短暂重取 `.state.lock` 合并元数据。同路径在预留项发布后已经受保护，无认领空窗；不同路径和不同会话不会因慢快照互相阻塞。快照失败或仅由 `PostToolUse` 发现的路径保留未知 before 元数据，最终只生成 partial `after-only-receipt`，不伪造 `/dev/null` patch。

无法在 `.state.lock` 下确认归属或出现执行后冲突时会写 uncertainty 证据。每个 global/path scope 的 `summary.json` 只保留回答“某 before 时间之后是否出现 foreign writer”所需的两个最新不同 writer；scope 锁繁忙时只覆盖一个 unknown-writer `pending.json`。下一次成功读写先把它原子改名为唯一 `pending-merge-*.json`，再把匿名 marker 时间提升到本次压缩时刻并合并清理。原子取走之后写入的新 `pending.json` 不在本轮 cleanup 范围内，因此旧 writer 晚覆盖和 compactor/read-delete 竞态都不会漏掉证据；常态也不会为每次 timeout 永久累积文件。

## 精确路径与漂移

- 小文件直接以真实工程相对路径进入 commit，Codex Review 和 GitHub Desktop 显示真实文件 Diff。
- 首次接触已有 P4 文件时，Git 父提交没有该路径，因此 commit Diff 显示完整新增；note 中的 `textPatch` 保存精确写入前后差异。
- 若 Git HEAD 内容与 Hook 捕获的写入前内容不一致，标记 `drifted-receipt-only`，不把外部变化冒充 Codex 修改。
- 删除仅在 Git HEAD 与捕获前内容一致时进入 commit，否则用 `delete-receipt-only` 记录。
- Stop-time after 冻结失败时使用 `deferred-after-unavailable-receipt-only`，只保留 unknown after 收据，不从后来工作树补内容。
- 目标路径已有 staged 内容时标记 `staged-receipt-only`，保留哈希、动作和 P4 元数据，但不把该路径的工作树内容写入回合树。
- 超过 8 MiB 的文件默认只记录收据，不写入 Git blob。
- Shell 或工具目标不能完整静态解析时，回合标记 `auditCoverage=partial`，`coverageWarnings` 保存缺口代码；不会用猜测路径伪造精确快照。

## Git index 边界

插件使用临时 index 构造 commit，从不运行 `git add -A`。本回合目标路径若已有 staged 变化，该路径转为 `staged-receipt-only`，staged 内容和主 index 保持不动，其余符合条件的回合路径仍可提交。不相关路径的 staged 内容也不会进入 Codex commit。commit 成功后，插件取得 Git 标准 `index.lock`，在锁内重新比较事务开始时的条目，在独立临时 index 中更新安全路径并原子替换主 index；锁前已经变化的路径会被跳过，比较和替换之间不能插入另一次标准 Git stage。跳过路径保持原状，并在 note 的 `coverageWarnings` 与 `transactionWarnings` 中记录 `index-changed-paths-left-untouched`。

## P4 只读保证

允许集合固定为 `info`、`where`、`fstat`、`diff`、`opened`、`print`。插件不创建或更新 P4 changelist，不 checkout、reconcile、reopen、revert、shelve 或 submit。旧 pending CL 由用户在 P4V 中处理。

## 查看历史

在 Codex Review 选择 `Last turn` 或最新 `Commit`。在 GitHub Desktop 添加项目根，保持 `codex/local-history`，从 History 选择 commit。标题表示实际完成的改动；原始请求查看正文 `Request`。完整收据运行：

~~~powershell
git notes --ref=codex-p4-diff show <commit>
~~~

不要 Publish。因为 `.git` 直接绑定真实 P4 工作树，Git checkout、switch、discard、restore、reset、clean、merge 和 rebase 会修改项目文件。

## 迁移

0.3 可能创建 pending CL 和指向插件本地数据的 `.git` 文件；0.5 只会移除已知插件拥有的旧指针，不修改 P4 CL。0.4 外置历史保留原样。若项目已有真实 `.git` 或外来指针，0.5 拒绝接管。0.5.3 首次启动会删除旧 shell 解析器产生的非路径记录。0.5.4 把 `ownership.json` 迁移到 schema 8：只保留当前活跃回合 `before.json` 中的真实路径，增加可续期短租约和 epoch-scoped 随机 token，并移除历史 generation 计数表。升级后的标题规则只影响新任务创建的新 commit，不改写已有历史。

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

优先使用 `apply_patch`、`Edit`、`Write`，或在 shell 中使用静态可解析的命名/位置参数和明确重定向目标。解析器支持常见命令链、PowerShell 调用运算符、目录目标与通配符展开。动态变量、响应文件、链接、复杂脚本或路径展开超过上限时不会猜测或阻断，而是记录 `auditCoverage=partial` 和具体 `coverageWarnings`；执行后才发现的路径只记录收据，不能伪造写入前快照。

### 审计告警会阻断命令吗

不会。锁等待超时、状态文件损坏、Shell 覆盖不完整和普通 Git/P4 只读审计错误都通过 Hook 上下文反馈并按 fail-open 处理。只有 `PreToolUse` 确认另一个仍活跃的 session/turn 正在记录同一真实文件时，才拒绝这次写入。

### 同一路径被另一个会话锁定

先确认提示的是同一真实文件而不是目录或普通字符串。记录只有在租约未过期且 session、活跃回合、状态和 `before.json` 均匹配时才有效；默认租约十分钟，并在同回合继续认领路径时续期。成功、无变化或失败收尾都会释放路径。不要通过 P4 reopen 解决。

### commit 失败

真实 P4 文件和现有 staged 内容保持原样。插件保留 `transaction.json`，或在事务尚未创建时保留 `deferred-after.json`，并写入项目级 `recovery/*.json`。后续 prompt、`SessionEnd` 或其他会话都会尝试恢复；连续失败不会删除入口。事务会识别已经推进的 ref/note/index 阶段，避免重复 commit。不要删除 State 会话目录、恢复标记或快照。
