# Codex P4 Diff

Codex P4 Diff lets Codex's native **Review** page display changes from a real Perforce workspace. It creates Git metadata outside the P4 client root, records the selected files' `#have` revisions as `HEAD`, and connects that metadata to the real workspace with a small `.git` pointer file.

The plugin does **not** copy the user's project into a fixed path. It discovers the active P4 client, server, user, client root, project root, changelist, tools, and safe sidecar location at runtime.

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

Restart Codex, open the P4 project, create a **new task**, and ask:

> 使用 Codex P4 Diff 检查当前 P4 环境并启用原生 Review。

The skill runs a read-only doctor first. If it reports an error, follow the `hint` next to that check and run the same request again.

## What it changes

- Creates an external sidecar under the current user's local application-data directory by default.
- Creates or replaces a project-root `.git` **file** only when the existing slot is empty or already owned by Codex P4 Diff.
- Builds a Git `HEAD` from the exact P4 `#have` revisions of files opened in the selected changelist.
- Uses the real P4 project as the Git worktree, so Review staging and restore operations directly affect real P4 files.
- Installs Git hooks that reject Git commits and pushes. Submit through Perforce.
- Optionally opens text files with `p4vc diffhave`; the user's configured P4V diff application remains in control.

The tool refuses to overwrite a real Git repository, an unknown `.git` pointer, a sidecar from another P4 server/client/user/changelist, or a `.git` path that is not ignored by Perforce.

## Troubleshooting

Run the doctor from PowerShell when you need the full structured report:

```powershell
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\run_codex_p4_diff.ps1" `
  --workspace "C:\path\to\p4-project" `
  --project-root "C:\path\to\p4-project" `
  --changelist default `
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
| `opened-files` error | Open files with `p4 edit`, `p4 add`, or `p4 delete`, or select the correct changelist. |
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
codex plugin remove codex-p4-diff@billycszhao-plugins
codex plugin marketplace remove billycszhao-plugins
```

## Security and data handling

The plugin executes local `p4`, `git`, and optionally P4V commands. It has no telemetry and does not send project content to a plugin-owned service. See [PRIVACY.md](PRIVACY.md), [TERMS.md](TERMS.md), and [LICENSE](LICENSE).

## Development

```powershell
python -m unittest discover -s tests -v
python -m py_compile plugins/codex-p4-diff/skills/codex-p4-diff/scripts/codex_p4_diff.py
```

The tests use temporary directories and mocks; they do not connect to a live Perforce server.
