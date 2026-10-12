---
name: codex-p4-diff
description: 在 P4 工程根的固定本地 Git 分支中记录所有 Codex 会话，并在每个有文件变化的用户回合结束时自动创建一个本地 Git commit；记录实际改动摘要、原始需求、会话、回合、文件动作、前后哈希和文本补丁。本插件自身仅执行 P4 只读查询，不限制插件之外经用户授权的 P4 操作。适用于在 Codex Review 或 GitHub Desktop 追溯不同 Codex 会话和回合的修改、诊断自动提交或路径锁、迁移旧版 P4 CL/外置历史模式，或核对插件是否完全不写入 P4。
---

# Codex P4 Diff

把 Codex 修改历史保存到 P4 工程根的本地 Git 仓库：

- 所有 `session_id` 共用固定 `codex/local-history` 分支；不按会话切换分支。
- 一个有文件变化的用户回合对应一个自动本地 commit；关联子任务的写入并入该回合，保留实际 writer 身份；无变化时不创建空 commit。
- commit 标题描述实际完成的改动；正文保存实际变更、分类验证、脱敏需求及必要追溯信息，精确文件哈希和大小保存在审计 note。
- 完整审计收据保存在 `refs/notes/codex-p4-diff`，包含时间、P4 只读元数据、审计覆盖状态、恢复告警和可获取的精确文本补丁。
- 默认提交不超过 8 MiB 的文件；更大的文件只记录哈希、大小和 P4 元数据。用 `CODEX_P4_DIFF_MAX_BLOB_BYTES` 调整阈值。
- 仓库不配置 remote，并用 `pre-push` 拒绝 push；不要发布到远程。
- 从工程子目录启动时向上复用最近的插件 owned history；遇到更近的外来仓库或未知 `.git` 指针时停止，不穿透该边界。已知插件旧指针把所在目录确定为工程根，并沿用既有迁移规则。

## P4 边界

本节只约束 codex-p4-diff 插件自身的 P4 调用，不是整个 Codex 会话的 P4 只读模式。插件之外的任务可在用户授权范围内执行 P4 操作，仍须遵守其他独立权限与审批要求；不得把任务授权扩展为允许本历史插件写入 P4。

只允许插件调用 P4 只读命令：`info`、`where`、`fstat`、`diff`、`opened`、`print`。

不要通过插件调用 `change`、`edit`、`add`、`delete`、`reconcile`、`reopen`、`revert`、`shelve` 或 `submit`。P4 checkout、变更列表归属和提交继续由用户、P4V、Unreal Source Control 或项目工具负责。

跨会话同路径冲突只使用 `%LOCALAPPDATA%\CodexP4Diff\State` 的本地所有权记录判断，不读取或修改 P4 changelist 归属。schema 8 所有权使用默认十分钟的可续期租约和 registry-epoch 随机 claim token，并校验所属 session、当前回合、状态和写入前清单；registry 重建、token 接管、过期或失活都会让旧回合降级为收据。Windows ownership key 忽略大小写但保留原始显示形式。只有另一个仍活跃的回合已认领同一真实文件时才停止写入并报告 session/turn ID。

## 自动生命周期

1. `SessionStart` 从当前目录向上寻找最近的 owned marker。已有 `.git/codex-p4-diff/project.json` 时复用并通过只读查询验证其项目根、P4 client 和 server；nested cwd 继续使用父级 history/State，外来仓库或未知 `.git` 指针是不可穿透边界，已知插件旧指针则把所在目录确定为工程根。没有 marker 时才使用环境/P4V 发现并初始化。首次多 client 歧义或 owned identity 无法验证时显示 fail-open 审计告警，不建立错误账本，也不阻断正常工具操作。随后在 `.repo-setup.lock` 下安全迁移、初始化或验证插件拥有的 `.git`，检查固定分支、无 remote 和 push 阻断，并建立会话账本。
2. `UserPromptSubmit` 先补偿项目级恢复队列中的未完成事务或延迟回合，再创建新回合记录，保存脱敏摘要、脱敏请求正文（最多 12,000 字符，明确标记截断）和 prompt SHA-256。补偿仍失败时保留跨会话恢复标记，不阻断新回合。
3. `PreToolUse` 在 `apply_patch`、`Edit`、`Write` 或明确写入型 shell 工具执行前取得本地回合所有权，并在短共享状态锁内发布写入前清单预留项；文件内容改由“回合 + 路径”独立锁读取，再短暂合并元数据，不能让慢快照阻塞无关会话认领。shell 支持常见命名/位置参数、命令链、调用运算符、重定向、目录和通配符展开；动态、未解析或快照失败的目标写入 `partial` 覆盖告警，不猜测路径，也不阻断命令。
4. `PostToolUse` 补记执行后才暴露的路径；写入前内容不可用时只生成 partial `after-only-receipt`，不得冒充新增、删除或精确文本补丁。不同 writer 的 uncertainty 证据压缩到有界 scope 摘要；timeout `pending.json` 在压缩时原子取走并提升匿名高水位，乱序覆盖或并发 cleanup 不得漏掉外来写入。
5. `Stop` 优先恢复已有事务，否则比较前后状态，校验本回合专用变更报告，在全局固定分支串行创建一个 commit。`.project.lock` 有界等待，分阶段事务日志保证 ref、note 和 index 更新可幂等恢复；若事务创建前超时，在回合级 `.deferred-after.lock` 内只发布第一次 after 快照，再于 `.state.lock` 内验证并密封逐路径 ownership 归因。只有密封成功才释放旧回合认领；另一处理器仍在发布记录时保留认领和项目级恢复标记。冻结失败时只能生成 partial `deferred-after-unavailable-receipt-only`，不得回读后来工作树。Git 收尾完成后在短 `.state.lock` 内重新加载并合并最新 session，只清理本回合，不能覆盖并发新 prompt 或 ownership。主 index 的最终比较与原子替换持有 Git `index.lock`。目标路径原有 staged 内容保持不动并标为 `staged-receipt-only`；不相关或并发变化的 staged 内容同样保持不变。
6. `SessionEnd` 先尝试补偿尚未完成的回合，再结束本地账本并释放该会话所有权；未完成恢复标记仍可由后续其他会话接力，不执行 P4 写操作。

Hooks 只覆盖经过通用 Tool Hooks 的写入。复杂 shell 或专用工具无法静态识别全部目标路径时，以不中断正常命令为优先，不得猜测路径；必须把 `auditCoverage=partial` 和 `coverageWarnings` 写入本地状态/收据并反馈给任务，不得声称记录了精确写入前内容。普通未映射目录仍静默视为不适用；首次多 client 歧义、owned identity 无法验证及其他可操作审计错误必须向任务显示 fail-open 告警。只有确认仍活跃的同路径所有权冲突可以拒绝 `PreToolUse`。

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

检查 `ready`、`p4Mode`、`p4Writes`、`historyRoot`、`branch` 和 `legacyGitPointer`。`historyRoot` 应等于 owned 项目根；从子目录运行时可以是父目录。`branch` 应为 `codex/local-history`。真实/外来 Git 仓库、未知 `.git` 指针、remote 或错误分支都会停止自动提交，插件不会擅自接管、穿透或切换。首次多 client 歧义可用 `--port` 和 `--client` 显式诊断；已有 marker 的身份冲突必须修复 P4 登录/映射，不能借此改绑历史。

用 `--history-path` 输出项目 Git 根。GitHub Desktop 添加项目根后保持 `codex/local-history`，从 History 查看 `[Codex]` commit。Codex Review 的 `Commit` / `Branch` 可以读取 Git 历史；`Unstaged` 只显示尚未记录的工作树变化。

### 原生审阅卡与插件历史

Codex 原生 `Last turn` / “上一轮”卡由宿主的工具差异事件生成，插件的 Git commit 和 Hook `systemMessage` 不能回填这张卡。不要把插件 commit 成功等同于原生卡已经完整。

默认让子任务负责分析、检查和提出补丁，由主会话执行最终文件写入，使主会话“上一轮”能够展示本轮修改。若授权子任务直接写入，必须汇总并检查本轮所有 writer 的记录，交付前打开覆盖所有 writer 的完整 Git 审阅，并说明原生卡的限制，不能只引用主会话卡的文件数。

排查漏记时，分别核对原生卡、用户回合 `before.json`、子 writer 归属和 Git note；保留旧历史，不通过反复写回相同源码或编辑 Codex 私有数据库伪造差异。

新提交使用绑定最终差异的专用变更报告，不再从聊天回复抽取标题。每个有实际改动的回合，在最终写入及子任务结束后、最终回复前，按 [提交描述流程](references/commit-descriptions.md) 运行 UserPromptSubmit 给出的 prepare 命令，阅读生成的差异上下文并填写 reportPath。报告覆盖全部已捕获改动，写清实际行为、原因、取舍、分类验证及限制。原始请求保留在正文 `Request` 和 note 的 `requestText`，兼容保留 `promptSummary`；精确文件哈希、写入者和补丁仍保留在 note。

插件校验 session/turn、完整文件集合、前后内容摘要和归档归属；不调用额外 AI，也不把执行者填写的验证结果当成插件复跑。缺失、过期、不完整或不可信报告会降级为文件事实，并记录 `descriptionStatus`。旧 commit 不会重写，已有事务继续使用冻结时的描述。

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
- 不要删除 owned marker 来绕过 P4 identity 校验，也不要把既有 history 改绑到另一 client/server。
- 不要为修正标题而改写已有 commit；新标题规则只应用于升级后新任务创建的 commit。
- 插件更新后重启 Codex、重新信任 Hooks，并新建任务；旧任务不会热加载新版 Hook。
