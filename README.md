# Codex P4 Diff

Codex P4 Diff records Codex changes as local Git history without modifying Perforce:

- one Codex session becomes one `codex/session/<session-id>` branch;
- one changed user turn becomes one automatic local Git commit;
- Perforce is queried only for workspace and baseline metadata;
- the history repository stays outside the P4 workspace, has no remote, and blocks push.

The commit message contains the redacted request summary, session ID, turn ID, and changed paths. Each commit also includes an immutable `event.json` with file actions, before/after SHA-256 values, sizes, and read-only P4 metadata. Text changes include a unified patch.

## Install

Prerequisites:

- Codex Desktop or CLI with plugin and Hook support;
- Python 3.10 or newer;
- Perforce Helix Core CLI (`p4`) with a working login and client mapping;
- Git 2.x.

```powershell
codex plugin marketplace add BillyCSZhao/codex-p4-diff
codex plugin add codex-p4-diff@billycszhao-plugins
```

Restart Codex, approve the plugin Hooks, open the P4 project, and create a new task. Existing tasks do not hot-load new Hook definitions.

## Behavior

At task start, the plugin creates an external history repository under:

```text
%LOCALAPPDATA%\CodexP4Diff\History\<project-name-hash>
```

Every changed turn advances the task's session branch by one `[Codex]` commit. A no-change turn creates no empty commit. The external repository contains:

```text
files/<project-relative-path>        rolling small-file mirror
turns/<sequence-turn-id>/event.json immutable turn details
turns/<sequence-turn-id>/changes.patch exact text delta
```

Files up to 8 MiB are eligible for the rolling mirror. Larger files are represented by hashes, sizes, and P4 metadata so repeated Unreal binary changes do not grow local Git history without bound. Set `CODEX_P4_DIFF_MAX_BLOB_BYTES` to change the threshold.

The first commit that touches an existing file shows the rolling mirror as a full-file addition because the branch did not previously contain that path. The same commit's `changes.patch` still contains the exact before/after text delta. Later commits show normal Git file diffs while the mirror matches the captured before state.

## Perforce boundary

The runtime enforces this read-only P4 command whitelist:

```text
p4 info
p4 where
p4 fstat
p4 diff
p4 opened
p4 print
```

The plugin never creates or edits P4 changelists, checks out files, reconciles, reopens, reverts, shelves, or submits. P4 file checkout and submission remain the responsibility of P4V, Unreal Source Control, or the user.

File ownership conflicts between Codex sessions use a local registry under `%LOCALAPPDATA%\CodexP4Diff\State`; they do not inspect or move P4 changelist ownership.

## View in GitHub Desktop

Run the installed launcher to get the exact history path:

```powershell
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\run_codex_p4_diff.ps1" `
  --workspace "C:\path\to\p4-project" `
  --project-root "C:\path\to\p4-project" `
  --history-path
```

Add the returned directory with **File > Add Local Repository**, then select the appropriate `codex/session/<session-id>` branch. Do not use **Publish repository**. The history repository has no remote and its `pre-push` hook rejects pushes.

Because this Git repository is outside the P4 workspace, GitHub Desktop checkout, discard, and branch operations do not modify real P4 files.

## Doctor and migration

```powershell
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\run_codex_p4_diff.ps1" `
  --workspace "C:\path\to\p4-project" `
  --project-root "C:\path\to\p4-project" `
  --doctor
```

Check `ready`, `p4Mode`, `p4Writes`, `historyRoot`, and `legacyGitPointer`.

Version 0.3 could attach a plugin-owned `.git` pointer to the P4 workspace and create P4 pending changelists. Version 0.4 stops all P4 mutations. At new-task startup it safely removes only a `.git` file pointing into the old Codex P4 Diff local-data directories. It does not remove a real Git repository, an unknown pointer, the old sidecar data, or any P4 changelist.

To run only that migration manually:

```powershell
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\run_codex_p4_diff.ps1" `
  --workspace "C:\path\to\p4-project" `
  --detach-legacy-review
```

## Update or remove

```powershell
codex plugin marketplace upgrade billycszhao-plugins
codex plugin add codex-p4-diff@billycszhao-plugins
codex plugin list --json
```

Restart Codex, approve the updated Hooks, and create a new task.

```powershell
codex plugin remove codex-p4-diff@billycszhao-plugins
codex plugin marketplace remove billycszhao-plugins
```

Removing the plugin does not delete local history or old P4 changelists.

## Security and data handling

The plugin runs local `p4` and `git` commands. It has no telemetry and does not send project contents to a plugin-owned service. Prompt summaries and source-code patches remain in the local history repository; see [PRIVACY.md](PRIVACY.md), [TERMS.md](TERMS.md), and [LICENSE](LICENSE).

## Development

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s plugins/codex-p4-diff/tests -v
python -m py_compile plugins/codex-p4-diff/skills/codex-p4-diff/scripts/codex_p4_diff.py plugins/codex-p4-diff/skills/codex-p4-diff/scripts/codex_p4_audit.py plugins/codex-p4-diff/hooks/codex_p4_session.py
```

Tests use temporary directories and mocks; they do not mutate a Perforce server or workspace.
