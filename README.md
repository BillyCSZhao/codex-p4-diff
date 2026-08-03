# Codex P4 Diff

Codex P4 Diff gives every writing Codex task its own Perforce pending changelist and records an immutable local audit event for every user turn. It also lets Codex's native **Review** page display changes from the task's changelist by mapping the selected files' Perforce `#have` revisions to an external Git baseline.

The plugin does **not** copy the user's project into a fixed path. It discovers the active P4 client, server, user, client root, project root, changelist, tools, and safe local storage at runtime.

## Install

Prerequisites:

- Codex Desktop or CLI with plugin support.
- Python 3.10 or newer.
- Perforce Helix Core CLI (`p4`) with a working login and client mapping.
- Git 2.x.
- P4V is optional and only required for external `p4vc diffhave` / Beyond Compare behavior.

Add this marketplace and install the plugin:

```powershell
codex plugin marketplace add BillyCSZhao/codex-p4-diff
codex plugin add codex-p4-diff@billycszhao-plugins
```

Restart Codex, approve the plugin's Hooks review, open the P4 project, create a **new task**, and ask:

> 检查当前 Codex 会话的 P4 changelist 和回合审计记录。

The plugin validates the P4 workspace when the task starts but creates no empty changelist. The first managed write lazily creates the task-specific pending changelist.

## Session changelists and turn audit

- One Codex `session_id` owns one lazily created pending changelist.
- One user `turn_id` produces one local audit event with the request, file actions, before/after SHA-256 values, and sizes.
- Text changes include a unified patch. Binary and large files record hashes and sizes without copying their content into the final audit event.
- Audit data is stored outside the workspace under `%LOCALAPPDATA%\CodexP4Diff\Audit` on Windows.
- The changelist description contains a redacted, truncated turn summary and a pointer to the local audit ledger. The full prompt is not uploaded to the Perforce server.
- If another changelist already owns a target file, the plugin rejects the write before modification. It never silently runs `p4 reopen`.
- Read-only tasks keep a local session ledger but do not create a changelist.
- Session end never submits, shelves, reverts, or deletes the pending changelist.

## Native Review bridge

- Creates an external sidecar under the current user's local application-data directory by default.
- Creates or replaces a project-root `.git` **file** only when the existing slot is empty or already owned by Codex P4 Diff.
- Builds a Git `HEAD` from the exact P4 `#have` revisions of files opened in the task changelist.
- Uses the real P4 project as the Git worktree, so Review staging and restore operations directly affect real P4 files.
- Installs Git hooks that reject Git commits and pushes. Submit through Perforce.
- Optionally opens text files with `p4vc diffhave`; the user's configured P4V diff application remains in control.

The tool refuses to overwrite a real Git repository, an unknown `.git` pointer, a sidecar from another P4 server/client/user/changelist, or a `.git` path that is not ignored by Perforce. `--changelist auto` resolves the current task's managed changelist and verifies its server, client, project, and ownership markers.

## Troubleshooting

Run the doctor from PowerShell when you need the full structured report:

```powershell
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\run_codex_p4_diff.ps1" `
  --workspace "C:\path\to\p4-project" `
  --project-root "C:\path\to\p4-project" `
  --changelist auto `
  --doctor
```

`$SkillRoot` means the installed `skills/codex-p4-diff` directory. The Codex skill resolves it automatically; users normally do not need to type this command.

| Doctor check | Resolution |
| --- | --- |
| `python` missing or older than 3.10 | Install a current Python from python.org, make `python` or `py -3` available, then restart Codex. |
| `p4` missing | Install Helix Core CLI/P4V and verify `p4 info` from the target project. |
| P4 login/client error | Run `p4 login`, verify `P4CONFIG`/P4V connection, and ensure the project is mapped by the selected client. |
| `git` missing | Install Git 2.x or pass the full executable path. |
| `p4vc` warning | Native Review still works. Install P4V only if external Diff is wanted. |
| Current task has no changelist | Complete one managed file write first, or pass an explicit numeric changelist. |
| File is open in another changelist | Stop and coordinate ownership. The plugin intentionally refuses a silent `p4 reopen`. |
| `opened-files` error | Verify the task changelist and confirm its files are open with `p4 edit`, `p4 add`, or `p4 delete`. |
| `p4-ignore` error | Add `.git` to the active `P4IGNORE` file and verify with `p4 ignores -i -v <project>\.git`. |
| Existing real Git repository | Do not overwrite it. Use only `--open-p4-diff` for that project. |
| Sidecar identity mismatch | Omit `--sidecar` to use automatic isolation, or choose a new empty directory outside the P4 client root. |
| Review is initially empty | Enable the bridge, create a new Codex task rooted at the same project, then open Review. Ensure the selected P4 files actually differ from `#have`. |

On Windows, only if Codex reports `SetNamedSecurityInfoW failed: 5` or `sandbox setup refresh had errors` involving `C:\tmp`, add this to `%USERPROFILE%\.codex\config.toml`, restart Codex, and create a new task:

```toml
[sandbox_workspace_write]
exclude_slash_tmp = true
```

Do not apply that setting for unrelated errors.

To disconnect a project, ask Codex to disable Codex P4 Diff, or run the launcher with `--disable-native-review`. It removes only a `.git` pointer owned by this tool and leaves the external sidecar for audit/recovery.

## Update or remove

```powershell
codex plugin marketplace upgrade billycszhao-plugins
codex plugin add codex-p4-diff@billycszhao-plugins
codex plugin list --json
```

Restart Codex and create a new task after the reinstall. Existing tasks do not hot-load new skills or Hook definitions.

To remove the plugin and marketplace:

```powershell
codex plugin remove codex-p4-diff@billycszhao-plugins
codex plugin marketplace remove billycszhao-plugins
```

## Security and data handling

The plugin executes local `p4`, `git`, and optionally P4V commands. It has no telemetry and does not send project content to a plugin-owned service. Its local audit ledger may contain full user prompts and text patches; see [PRIVACY.md](PRIVACY.md), [TERMS.md](TERMS.md), and [LICENSE](LICENSE).

## Development

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s plugins/codex-p4-diff/tests -v
python -m py_compile plugins/codex-p4-diff/skills/codex-p4-diff/scripts/codex_p4_diff.py plugins/codex-p4-diff/skills/codex-p4-diff/scripts/codex_p4_audit.py plugins/codex-p4-diff/hooks/codex_p4_session.py
```

The tests use temporary directories and mocks; they do not connect to a live Perforce server.
