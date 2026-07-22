---
name: codex-p4-diff
description: 把 Perforce 工作区的 #have 基线接入当前工程的系统原生 Codex Review，并按需调用用户本机 P4V Diff。适用于 Codex 修改或审阅 P4 管理的代码/文本文件、用户要求在原生“审阅”页面查看左右 Diff、使用 Git 暂存/还原真实 P4 文件、打开 Beyond Compare/P4Merge，或诊断不同 P4 server/client/工程为何无法显示原生 Review 时。
---

# Codex P4 Diff

把外置 Git 元数据连接到真实 P4 工程：Git `HEAD` 保存目标变更列表的 P4 `#have`，项目内只放工具拥有的 `.git` 指针，Codex Review 直接读取真实 P4 文件。

## 依赖与边界

- 需要 Python 3.10+、Perforce CLI（`p4`）和 Git 2.x。
- P4V/P4VC 仅用于外部 Diff；缺少它不影响 Codex 原生 Review。
- 项目根不能已有真实 Git 仓库或来源不明的 `.git` 指针。
- 生效的 `P4IGNORE` 必须忽略项目根 `.git`。
- sidecar 必须位于 P4 client root 外，默认按 server、client、changelist 和项目根自动生成。
- 只支持本机工作区；不要对远程挂载、只读目录或多人共用同一工作树启用。

## 默认流程

1. 从当前任务的 `cwd` 确定 `ProjectRoot`，并把本 `SKILL.md` 所在目录解析为绝对路径 `SkillRoot`。脚本路径必须相对 `SkillRoot` 解析，不能相对项目 cwd 猜测。
2. 确保目标文件已通过 `p4 edit`、`p4 add`、`p4 delete` 等进入目标变更列表。
3. Windows 优先通过启动器运行只读诊断：

   ~~~powershell
   $Runner = Join-Path $SkillRoot 'scripts\run_codex_p4_diff.ps1'
   & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Runner --workspace $ProjectRoot --project-root $ProjectRoot --changelist default --doctor
   ~~~

   macOS/Linux 使用启动器检查 Python 3.10+：

   ~~~bash
   sh "$SkillRoot/scripts/run_codex_p4_diff.sh" --workspace "$ProjectRoot" --project-root "$ProjectRoot" --changelist default --doctor
   ~~~

4. 只有 `doctor.ready` 为 `true` 时继续。若为 `false`，按失败项的 `hint` 修复并重跑；不要跳过真实 Git、P4IGNORE 或 sidecar 错误。
5. 启用原生 Review：

   ~~~powershell
   & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Runner --workspace $ProjectRoot --project-root $ProjectRoot --changelist default --enable-native-review
   ~~~

   macOS/Linux 将同样参数传给 `run_codex_p4_diff.sh`。用户明确要求 P4V/Beyond Compare 时追加 `--open-p4-diff`。若多个 server/client 匹配，按错误提示显式追加 `--port`、`--user` 和 `--client`。
6. 核对 `nativeReview.enabled`、`gitTopLevel`、`gitDir`、`p4Ignore.ignored` 和 `gitStatus`。
7. 首次启用或插件升级后，使用 `nativeReview.newTaskUrl` 新建绑定项目根的任务。然后按 **Ctrl+Shift+G**，选择 **未暂存** 或 **已暂存**。
8. P4 打开文件集合、变更列表或 `#have` 变化后重新执行启用命令。工具最后原子重写 `.git` 指针，向 Review 发出项目内刷新事件。

## 错误处理

- 失败时读取 JSON 的 `error`、`hints` 和 `troubleshooting`，修复后重试。
- `opened-files = 0`：检查 workspace、client、changelist，并确认文件已在 P4 打开。
- P4V 缺失：原生 Review 可继续；只有需要外部 Diff 时才安装。
- Review 为空：确认选择 **未暂存** 并核对 `visibleGitChangeCount`；首次识别时新建任务。
- 遇到 P4/Git/P4V、sandbox、已有 `.git`、编码或生命周期问题时，读取 [P4 与 Git 映射及故障排查](references/p4-git-mapping.md)。

## Git 与 P4 安全

- 暂存/取消暂存只修改外置 Git index。
- Review 的还原、检出和重置会直接改写真实 P4 文件；执行前说明具体文件范围。
- Git commit/push 由 sidecar 的 `pre-commit`/`pre-push` hooks 拦截；正式提交始终使用 Perforce。
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

只移除工具拥有的 `.git` 指针，保留 sidecar；拒绝删除真实 Git 或未知指针。
