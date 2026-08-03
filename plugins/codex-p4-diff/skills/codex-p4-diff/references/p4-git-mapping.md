# P4 与 Git 映射及故障排查

## 文件状态

| P4 操作 | Git `HEAD` 基线 | 真实工作树 | 原生 Review |
| --- | --- | --- | --- |
| `edit` | `depotFile#haveRev` | `clientFile` | 已修改或无内容差异 |
| `add` | 无 | `clientFile`，intent-to-add | 新增 |
| `delete` | `depotFile#haveRev` | 文件不存在 | 已删除 |
| `move/delete` | `depotFile#haveRev` | 文件不存在 | 删除/重命名源 |
| `move/add` | 无 | `clientFile`，intent-to-add | 新增/重命名目标 |
| `integrate` / `branch` | 有 `haveRev` 时使用 | `clientFile` | 已修改 |

只跟踪目标 P4 变更列表的文件，并设置 `status.showUntrackedFiles=no`。

## Review 拓扑

~~~text
<P4 ProjectRoot>
├─ .git  ->  gitdir: <sidecar>/git
└─ 真实 P4 文件

<sidecar>/git
├─ HEAD -> refs/heads/codex-p4-have
├─ index
├─ codex-p4-diff.json
└─ hooks/{pre-commit,pre-push}
~~~

`git rev-parse --show-toplevel` 必须等于项目根；`--absolute-git-dir` 必须位于 P4 client root 外。

## 会话审计拓扑

~~~text
%LOCALAPPDATA%/CodexP4Diff/Audit/<client-server>/<project>/
└─ sessions/<session_id>/
   ├─ session.json                 # 会话与 pending CL 的稳定绑定
   └─ turns/<turn_id>/
      ├─ prompt.txt                # 仅本机保存的原始需求
      ├─ prompt.json               # 摘要与哈希
      ├─ before.json               # 回合前文件状态
      ├─ event.json                # 文件动作、前后哈希和 P4 元数据
      ├─ changes.patch             # 可解码文本的 unified diff
      └─ tools/*.json              # apply_patch / shell 工具归属记录
~~~

`session_id` 对应一个 pending CL，`turn_id` 对应一条不可变审计事件。CL Description 只保留脱敏摘要和审计路径，不把完整 prompt 上传到 P4 Server。

同一 client 中一个文件只能归属一个 CL。另一个 Codex 会话命中已打开文件时默认拒绝；pending CL 本身不保存逐回合内容，因此不能用连续 `p4 reopen` 代替回合补丁。

## 自动发现

- `p4`：显式路径、PATH、Windows 标准 Perforce 目录。
- Git：显式路径、PATH、Windows 标准 Git 目录、Codex Desktop 捆绑 Git。
- P4V：显式路径、PATH、Windows 标准 Perforce 目录；接受 `p4vc.bat` 或 `p4v.exe`。
- P4 连接：在 `--workspace` 目录运行 `p4 info` 以加载 P4CONFIG；失败后只读扫描 `~/.p4qt/connectionmap.xml`。
- 多个 server/client 匹配时拒绝猜测。
- 已完成 sidecar 校验 server、user、client、client root 和 changelist，拒绝跨上下文复用。
- 会话 Hook 使用相同的连接/client 解析，不直接信任 shell 中可能指向其他 workspace 的 `P4CLIENT`。

## Git 操作语义

- 暂存只修改 sidecar index。
- Review 还原会写入/删除真实 P4 文件，P4 元数据不会自动同步。
- Git commit/push 由 sidecar 的 pre-commit/pre-push hooks 拦截；sidecar 默认也不配置 remote。
- 还原后只用 `p4 revert -a` 清理等于 `#have` 的文件。

## 基线刷新

真实文件内容变化不重建基线。P4 打开集合、目标 CL 或 `#have` 改变时重建。若 Git 已暂存、存在未合并项或 `HEAD` 偏离 marker，默认停止；确认后才使用 `--discard-git-review-state`。

成功后原子重写项目内工具拥有的 `.git` 指针，产生项目目录事件以提示 Review 扫描新的 `HEAD` 和 index。

`--changelist auto` 从当前 `CODEX_THREAD_ID` 的 `session.json` 读取数字 CL，并校验 server、client 和项目根。账本不存在或上下文不一致时停止，不回退到 `default`。

## Troubleshooting

### Python

Windows 使用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File run_codex_p4_diff.ps1`；macOS/Linux 使用 `sh run_codex_p4_diff.sh`。启动器要求 Python 3.10+，失败时会给出安装提示。

### 找不到 p4 / Git / P4V

运行 `--doctor` 查看解析路径。安装 Helix Core CLI 与 Git；P4V 仅为外部 Diff 所需。也可传 `--p4`、`--git`、`--p4vc` 完整路径。

### 没有已打开文件

`--enable-native-review` 拒绝生成空 Review。用 `p4 opened -c <CL> <scope>/...` 核对 workspace、client 和 changelist。

### P4 未忽略 .git

在当前 P4IGNORE 中加入 `.git`，再运行 `p4 ignores -i -v '<ProjectRoot>\.git'`。插件不会自动修改全局 P4IGNORE。

### 项目已有 Git

插件拒绝覆盖真实 `.git`。保留现有 Git，只使用 `--open-p4-diff`；不要删除真实 Git 来迁就插件。

### Review 为空或延迟

确认目标为 **未暂存**；检查 `visibleGitChangeCount` 和 `gitStatus`；首次启用/升级后用 `newTaskUrl` 新建任务；P4 文件集合变化后重新启用。

### Windows sandbox refresh / C:\tmp ACL

只有日志同时出现 `SetNamedSecurityInfoW failed: 5`、`C:\tmp` 和 `sandbox setup refresh` 时，才在 `~/.codex/config.toml` 添加：

~~~toml
[sandbox_workspace_write]
exclude_slash_tmp = true
~~~

保存后新建任务。不要把它当作通用 Review 修复。

### sidecar/权限

自定义 sidecar 必须可写、为空或由插件标记，并位于 P4 client root 外；不要多人共享 sidecar。

### Hook 未触发

插件 Hooks 在安装或定义变化后需要重新信任，并从新任务加载。检查 Codex Hooks 页面中 `codex-p4-diff/hooks/hooks.json` 的状态。旧任务不会自动获得新 Hook。

### Shell 写入与专用工具

`apply_patch` 可以在写入前精确解析文件并建立归属。对明确的写入型 shell 命令，Hook 会在执行前确认不存在未归属 workspace 漂移，并在执行后 reconcile。少数专用工具可能绕过通用 Tool Hooks；发现这种写入时停止并报告，不要事后吞并已有改动。
