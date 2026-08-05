# Codex P4 Diff

[简体中文](README.zh-CN.md) | Current release: [v0.5.2](https://github.com/BillyCSZhao/codex-p4-diff/tree/v0.5.2)

Codex P4 Diff records Codex changes as local Git history without modifying Perforce:

- the P4 project root becomes one local Git repository on `codex/local-history`;
- every changed user turn from every Codex session becomes one automatic local commit;
- commit titles summarize the completed change, while the body preserves the redacted request, session, turn, paths, hashes, and sizes;
- immutable audit details are stored in `refs/notes/codex-p4-diff`;
- Perforce is restricted to read-only queries.

The repository has no remote and a `pre-push` hook blocks publication. Real project-relative paths make the commits visible in Codex Review and GitHub Desktop.

## What's new in v0.5.2

Commit titles now describe what Codex changed instead of echoing the user's last prompt or confirmation. The Stop hook takes the first meaningful completed-change line from `last_assistant_message`, skips generic Markdown lines such as "Done" or "Tests passed", redacts secrets, and falls back to a deterministic file-action title such as `修改 A.cpp` when needed. The original request remains available as `Request` in the commit body and `promptSummary` in the Git note. Existing commits are not rewritten.

## Install

Prerequisites: Codex with plugin and Hook support, Python 3.10+, `p4` with a working login/client mapping, and Git 2.x.

```powershell
codex plugin marketplace add BillyCSZhao/codex-p4-diff
codex plugin add codex-p4-diff@billycszhao-plugins
codex plugin list --json
```

Confirm that `codex-p4-diff@billycszhao-plugins` reports version `0.5.2`. On Windows, use `codex.cmd` instead of `codex` if PowerShell execution policy blocks `codex.ps1`.

Restart Codex, approve the plugin Hooks, open the P4 project, and create a new task. Existing tasks do not hot-load new Hook definitions.

## Behavior

At task start, the plugin initializes `<P4 ProjectRoot>/.git` and creates the fixed `codex/local-history` branch. It refuses to take over a pre-existing Git repository that does not contain its ownership marker under `.git/codex-p4-diff/project.json`. It never switches branches automatically.

Every changed turn advances the shared branch by one `[Codex]` commit. Sessions are interleaved by turn completion time. A no-change turn creates no empty commit. The main Git index is updated only for paths recorded for that turn; the plugin never runs `git add -A`. If one of those paths already has staged content, the commit is refused. Staged content on unrelated paths is preserved.

The commit subject is `[Codex] <completed change>`. It is derived from the first meaningful line of the final assistant message after generic headings, status-only lines, Markdown markers, fenced code, and Codex directives are skipped. If no usable line exists, the plugin generates a title from file actions, for example `修改 A.cpp`, `新增 Config.ini`, or `更新 3 个文件：A.cpp、B.h 等`.

Files up to 8 MiB are eligible for commit content. Larger files retain hashes, sizes, and P4 metadata in the Git note without adding the full blob. Set `CODEX_P4_DIFF_MAX_BLOB_BYTES` to change the threshold.

Each commit body contains `Change`, `Change-Summary-Source`, the redacted original `Request`, session and turn IDs, project path, file actions, hashes, sizes, and history state. Read the complete note with:

```powershell
git notes --ref=codex-p4-diff show <commit>
```

The note contains `changeSummary`, `changeSummarySource`, `assistantMessageSha256`, `promptSummary`, prompt SHA-256, timestamps, before/after metadata, read-only P4 metadata, and the exact unified text patch when captured. Only the assistant message hash is stored; the full assistant response is not copied into the note.

## Perforce boundary

The runtime allows only `p4 info`, `where`, `fstat`, `diff`, `opened`, and `print`. It never creates, edits, reconciles, reopens, reverts, shelves, or submits a P4 changelist. P4 checkout and submission remain the responsibility of P4V, Unreal Source Control, or the user.

Cross-session same-path conflicts use a local registry under `%LOCALAPPDATA%\CodexP4Diff\State`; they do not inspect or change P4 changelist ownership.

## Review and GitHub Desktop

Codex Review can inspect the latest commit or turn because the project itself is a Git checkout. `Unstaged` shows only changes not yet captured by the Stop hook.

In GitHub Desktop, choose **File > Add Local Repository** and select the P4 project root. Stay on `codex/local-history` and use **History** to inspect commits. Do not publish the repository.

This Git repository uses the real P4 working tree. GitHub Desktop or Git commands such as checkout, switch, discard, restore, reset, clean, merge, and rebase can modify project files. Use the repository for history/review only; manage project-file restoration through the normal P4 workflow.

## Doctor and migration

Run `codex plugin list --json` first. Set `$PluginRoot` to the installed `codex-p4-diff@billycszhao-plugins` item's `source.path`, then run:

```powershell
$ProjectRoot = (Get-Location).Path
$PluginRoot = 'C:\path\shown-by-codex-plugin-list'
$SkillRoot = Join-Path $PluginRoot 'skills\codex-p4-diff'
$Runner = Join-Path $SkillRoot 'scripts\run_codex_p4_diff.ps1'

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Runner `
  --workspace $ProjectRoot `
  --project-root $ProjectRoot `
  --doctor
```

Check `ready`, `p4Mode`, `p4Writes`, `historyRoot`, `branch`, and `legacyGitPointer`. `historyRoot` must equal the P4 project root and `branch` must be `codex/local-history`.

Version 0.3 could create a P4 pending changelist and attach a plugin-owned `.git` pointer. Version 0.4 moved history outside the project. Version 0.5 performs no P4 migration and leaves old external history untouched; it only removes a legacy `.git` file when it points into known plugin-owned local-data directories. Existing P4 changelists remain entirely user-managed.

## Update or remove

```powershell
codex plugin marketplace upgrade billycszhao-plugins
codex plugin add codex-p4-diff@billycszhao-plugins
codex plugin list --json
```

Confirm that the installed version is `0.5.2`. If `plugin add` reports that the plugin is already installed or the listed version remains older, reinstall only this public plugin:

```powershell
codex plugin remove codex-p4-diff@billycszhao-plugins
codex plugin add codex-p4-diff@billycszhao-plugins
codex plugin list --json
```

Restart Codex, approve the updated Hooks, and create a new task. Do not remove a separate local-development plugin from another marketplace.

```powershell
codex plugin remove codex-p4-diff@billycszhao-plugins
codex plugin marketplace remove billycszhao-plugins
```

Removing the plugin does not delete project `.git`, local Git history, state snapshots, external 0.4 history, or P4 changelists.

## Security and development

The plugin has no telemetry and does not send project contents to a plugin-owned service. See [PRIVACY.md](PRIVACY.md), [TERMS.md](TERMS.md), and [LICENSE](LICENSE).

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s plugins/codex-p4-diff/tests -v
python -m py_compile plugins/codex-p4-diff/skills/codex-p4-diff/scripts/codex_p4_diff.py plugins/codex-p4-diff/skills/codex-p4-diff/scripts/codex_p4_audit.py plugins/codex-p4-diff/hooks/codex_p4_session.py
```

Tests use temporary directories and mocks; they do not mutate a Perforce server or workspace.
