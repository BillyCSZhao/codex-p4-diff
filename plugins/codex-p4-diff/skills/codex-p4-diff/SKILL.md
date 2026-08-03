---
name: codex-p4-diff
description: 为每个 Codex 会话建立独立的 Perforce pending changelist，记录回合级修改详情，并把目标 CL 的 #have 基线接入系统原生 Codex Review；也可调用用户本机 P4V Diff。适用于 Codex 修改或审阅 P4 文件、跨会话追溯文件归属、自动选择当前会话 CL、在原生“审阅”页面查看左右 Diff、使用 Git 暂存/还原真实 P4 文件，或诊断 P4 server/client/工程映射问题。
---

# Codex P4 Diff

每个发生实际文件写入的 Codex 会话使用一个独立的 P4 pending changelist；每个用户回合在工作区外保存需求、文件哈希和文本补丁。外置 Git 元数据继续把目标 CL 的 P4 `#have` 映射为 Git `HEAD`，Codex Review 直接读取真实 P4 文件。

## 依赖与边界

- 需要 Python 3.10+、Perforce CLI（`p4`）和 Git 2.x。
- P4V/P4VC 仅用于外部 Diff；缺少它不影响 Codex 原生 Review。
- 项目根不能已有真实 Git 仓库或来源不明的 `.git` 指针。
- 生效的 `P4IGNORE` 必须忽略项目根 `.git`。
- sidecar 必须位于 P4 client root 外，默认按 server、client、changelist 和项目根自动生成。
- 只支持本机工作区；不要对远程挂载、只读目录或多人共用同一工作树启用。
- 插件 Hooks 必须在 Codex 中完成信任审核；安装或更新后使用新任务验证。
- 同一 P4 workspace 的一个文件同一时刻只能属于一个 pending CL。检测到其他 CL 已打开目标文件时默认拒绝修改，不要静默 `p4 reopen`。
- pending CL 不是内容快照。回合历史保存在 `%LOCALAPPDATA%\CodexP4Diff\Audit`；文本保存 unified diff，二进制默认只保存前后哈希和大小。

## 会话 CL 与回合审计

启用插件后，生命周期 Hooks 自动执行以下流程：

1. `SessionStart` 只验证当前 `cwd` 对应的 P4 server/client/root，不创建空 CL。
2. `UserPromptSubmit` 记录当前 `session_id`、`turn_id` 和本回合需求，并对已有会话 CL 文件保存修改前快照。
3. `PreToolUse` 在首次 `apply_patch` 写入前懒创建当前会话专属 pending CL，打开目标文件并检查其他 CL 的所有权。
4. `PostToolUse` 对新增、删除、移动和明确的写入型 shell 命令执行 P4 reconcile 与归属检查。
5. `Stop` 写入回合 `event.json`、文本 `changes.patch`、前后 SHA-256，并更新 CL Description 的回合及文件摘要。
6. `SessionEnd` 只把本地账本标记为结束；绝不自动 submit、shelve、revert 或删除 CL。

Hook 是强约束护栏，但 Codex 的少数专用工具路径可能不经过通用 Tool Hooks。发现未归属写入时要停止并报告，不能把已有用户改动事后归到当前会话。

## 默认流程

1. 从当前任务的 `cwd` 确定 `ProjectRoot`，并把本 `SKILL.md` 所在目录解析为绝对路径 `SkillRoot`。脚本路径必须相对 `SkillRoot` 解析，不能相对项目 cwd 猜测。
2. 新任务首次写入后，核对 Hook 返回的会话 CL 编号。手工管理的文件仍需确保已通过 `p4 edit`、`p4 add`、`p4 delete` 等进入目标变更列表。
3. Windows 优先通过启动器运行只读诊断：

   ~~~powershell
   $Runner = Join-Path $SkillRoot 'scripts\run_codex_p4_diff.ps1'
   & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Runner --workspace $ProjectRoot --project-root $ProjectRoot --changelist auto --doctor
   ~~~

   macOS/Linux 使用启动器检查 Python 3.10+：

   ~~~bash
   sh "$SkillRoot/scripts/run_codex_p4_diff.sh" --workspace "$ProjectRoot" --project-root "$ProjectRoot" --changelist auto --doctor
   ~~~

4. 只有 `doctor.ready` 为 `true` 时继续。若为 `false`，按失败项的 `hint` 修复并重跑；不要跳过真实 Git、P4IGNORE 或 sidecar 错误。
5. 启用原生 Review：

   ~~~powershell
   & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Runner --workspace $ProjectRoot --project-root $ProjectRoot --changelist auto --enable-native-review
   ~~~

   macOS/Linux 将同样参数传给 `run_codex_p4_diff.sh`。用户明确要求 P4V/Beyond Compare 时追加 `--open-p4-diff`。若多个 server/client 匹配，按错误提示显式追加 `--port`、`--user` 和 `--client`。
6. 核对 `nativeReview.enabled`、`gitTopLevel`、`gitDir`、`p4Ignore.ignored` 和 `gitStatus`。
7. 首次启用或插件升级后，使用 `nativeReview.newTaskUrl` 新建绑定项目根的任务。然后按 **Ctrl+Shift+G**，选择 **未暂存** 或 **已暂存**。
8. P4 打开文件集合、变更列表或 `#have` 变化后重新执行启用命令。`auto` 从 `CODEX_THREAD_ID` 对应的审计账本解析数字 CL；没有账本时明确失败，不回退到 `default`。
9. 工具最后原子重写 `.git` 指针，向 Review 发出项目内刷新事件。不要在已有活跃任务中从未知 Git checkout 动态切换指针；首次绑定使用新任务。

## 错误处理

- 失败时读取 JSON 的 `error`、`hints` 和 `troubleshooting`，修复后重试。
- `opened-files = 0`：检查 workspace、client、changelist，并确认文件已在 P4 打开。
- `当前会话尚未建立 P4 变更列表`：该任务还没有实际文件写入；先完成一次受 Hook 管理的修改，或显式传入数字 CL。
- Hook 没有运行：在新任务中检查 Hooks 信任状态；插件更新后旧任务不会自动拾取新的 Hook 定义。
- 文件属于另一个 CL：停止修改并报告 CL 编号；只有用户明确要求移交后，才能先保存原会话审计快照再执行 `p4 reopen`。
- P4V 缺失：原生 Review 可继续；只有需要外部 Diff 时才安装。
- Review 为空：确认选择 **未暂存** 并核对 `visibleGitChangeCount`；首次识别时新建任务。
- 遇到 P4/Git/P4V、sandbox、已有 `.git`、编码或生命周期问题时，读取 [P4 与 Git 映射及故障排查](references/p4-git-mapping.md)。

## Git 与 P4 安全

- 暂存/取消暂存只修改外置 Git index。
- Review 的还原、检出和重置会直接改写真实 P4 文件；执行前说明具体文件范围。
- Git commit/push 由 sidecar 的 `pre-commit`/`pre-push` hooks 拦截；正式提交始终使用 Perforce。
- 不要把 `default` 或其他用户 CL 中已打开的文件自动挪入会话 CL。
- 完整用户 prompt 只写入本机审计目录；CL Description 仅写脱敏且截断的摘要。
- P4 范围变化且 Git 已暂存、存在未合并项或 `HEAD` 偏离基线时默认停止。只有用户明确同意后使用 `--discard-git-review-state`。
- Review 还原后需清理无差异 P4 打开项时，取得授权再运行：

  ~~~powershell
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Runner --workspace $ProjectRoot --project-root $ProjectRoot --changelist default --repair-p4-state
  ~~~

## 禁用

关闭使用该项目的 Codex 任务后运行：

~~~powershell
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Runner --workspace $ProjectRoot --project-root $ProjectRoot --disable-native-review
~~~

只移除工具拥有的 `.git` 指针，保留 sidecar、会话 CL 和审计账本；拒绝删除真实 Git 或未知指针。若要停用自动会话 CL，在 Codex 中禁用插件或对应 Hook，不要删除仍含未提交文件的 pending CL。
