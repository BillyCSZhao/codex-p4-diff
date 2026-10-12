# Codex P4 Diff

[简体中文](README.zh-CN.md) | Current release: [v0.6.0](https://github.com/BillyCSZhao/codex-p4-diff/tree/v0.6.0)

Codex P4 Diff records Codex changes as local Git history without modifying Perforce:

- the P4 project root becomes one local Git repository on `codex/local-history`;
- every changed user turn from every Codex session becomes one automatic local commit;
- commit titles describe actual behavior changes; the body separates reasons, implementation, verification, limitations, and the redacted request;
- immutable audit details are stored in `refs/notes/codex-p4-diff`;
- Perforce is restricted to read-only queries.

The generated project history has no remote and a `pre-push` hook blocks publication. This restriction applies to recorded project history, not this public plugin-source repository. Real project-relative paths make the commits visible in Codex Review and GitHub Desktop.

## What's new in v0.6.0

- **Useful commit descriptions:** the executing AI reads the final diff and provides a dedicated report. The plugin checks the turn, complete captured file set, before/after hashes, and attribution before saving it. Missing or stale reports fall back to explicit file facts; ordinary final replies are no longer used as titles.
- **Separate intent, implementation, and evidence:** retain the redacted current request, explain actual changes, and report build, automated, and manual checks independently. Verification is executor-reported; the plugin does not rerun tests or call another model. Existing commits and frozen legacy descriptions are unchanged.
- **Parent and subagent coverage:** link child writes to their parent turn, preserve writer identities, and wait for pending tools. Exact review refs cover recorded before/after content. They cannot backfill Codex's native Last turn card.
- **Conservative recovery:** recover only a stale Git index lock proven to belong to a terminated plugin writer; preserve unrelated locks and staged data.

See the [description workflow and examples](plugins/codex-p4-diff/skills/codex-p4-diff/references/commit-descriptions.md) and [changelog](CHANGELOG.md). The v0.5.6 broker and `exec_command` fixes are retained.

## What's new in v0.5.6

Legacy histories that recorded a P4 broker endpoint as their server address can resume after the backend address changes: the plugin reconnects to the original endpoint and verifies the live broker address, client, and project mapping. It preserves the marker and State keys, reports the backend as `resolvedServerAddress`, and continues to reject modern identity mismatches and unverified aliases.

Hooks now capture writes made through `exec_command` as well as `shell_command`, including `cmd`, `workdir`, and explicit Bash/sh/zsh selection. The fixed history branch and P4 read-only restrictions remain unchanged; Hooks do not automatically switch branches.

## Identity handling introduced in v0.5.5

An existing plugin-owned history now keeps its P4 identity stable. The client and server recorded in `.git/codex-p4-diff/project.json` are reused and revalidated before ambient environment or P4V discovery, so an unrelated default `P4CLIENT` or two P4V workspaces that map the same directory cannot silently redirect the history or its state.

A task started below an existing owned project resolves to the nearest parent history root. The search stops at a closer foreign `.git` boundary and never traverses or takes it over. A recognized plugin-owned legacy pointer becomes that directory's project boundary and retains the existing serialized migration behavior. On first setup, when no owned marker exists and multiple P4 clients map the same directory, Hooks now surface an actionable fail-open warning instead of silently skipping the task; normal tool operations continue. Ordinary unmapped directories remain out of scope. The P4 runtime whitelist is unchanged and read-only.

## Install

Prerequisites: Codex with plugin and Hook support, Python 3.10+, `p4` with a working login/client mapping, and Git 2.x.

```powershell
codex plugin marketplace add BillyCSZhao/codex-p4-diff
codex plugin add codex-p4-diff@billycszhao-plugins
codex plugin list --json
```

Confirm that `codex-p4-diff@billycszhao-plugins` reports version `0.6.0`. On Windows, use `codex.cmd` instead of `codex` if PowerShell execution policy blocks `codex.ps1`.

Restart Codex, approve the plugin Hooks, open the P4 project, and create a new task. Existing tasks do not hot-load new Hook definitions.

## Behavior

At task start, the plugin searches from the task directory upward for the nearest `.git/codex-p4-diff/project.json` that identifies an owned history. Its recorded project root, P4 client, and server are treated as the established identity and revalidated with read-only P4 queries. A task started in a subdirectory therefore reuses the parent history and state. The upward search stops at the first closer foreign repository or unknown `.git` pointer and does not cross that boundary. A recognized plugin-owned legacy pointer instead identifies that directory as the project root and can be migrated under the existing serialized rules. For first setup, start the task at the intended P4 project root. Legacy pointer migration and repository initialization are serialized by `.repo-setup.lock`. The plugin refuses to take over a pre-existing foreign Git repository and never switches branches automatically.

Every changed turn advances the shared branch by one `[Codex]` commit. Sessions are interleaved by turn completion time. A no-change turn creates no empty commit. The main Git index is updated only for paths recorded for that turn; the plugin never runs `git add -A`. Pre-existing staged content on a recorded path is left untouched and represented as `staged-receipt-only`, while other eligible paths may still enter the commit. Unrelated staged content is also preserved. Final comparison and replacement use Git's standard `index.lock`: updates are built in a temporary index and atomically installed, so a stage that wins before the lock is skipped and no stage can appear between comparison and replacement.

Shell commands participate in same-path protection only when the hook can identify statically resolvable file targets. It understands common named and positional write arguments, command chains, PowerShell's call operator, redirections, and existing directory or glob targets. Directory targets are expanded to their current project files rather than claimed as a directory lock. Quoted messages, comments, regex patterns, and option values are not treated as paths. Dynamic syntax, unresolved variables, response files, links, or expansion limits produce explicit `partial` coverage warnings instead of guessed paths; the command continues.

Separate bounded OS-level locks have narrow responsibilities: `.repo-setup.lock` serializes legacy-pointer migration and repository initialization, `.state.lock` protects ownership and session ledgers, `.project.lock` serializes Git finalization, per-turn/per-path locks isolate before snapshots, `.deferred-after.lock` makes the first Stop-time snapshot immutable, uncertainty scopes use their own short locks, and Git's `index.lock` protects the final index replacement. The current implementation writes no sentinel payload to lock files, so concurrent initialization never writes into a byte already locked by another process. The phase-based transaction journal is written before the branch, note, or index is advanced. If history is busy before a transaction can be created, the Stop hook freezes the current after snapshot, seals whether each path still belonged to that turn, releases those claims, and writes a project-level recovery marker. Any later prompt or session can resume that turn; repeated failure does not block a new turn. Terminal updates briefly reacquire `.state.lock`, reload the latest ledger, merge only the completed turn, and release only that turn's claims.

`PreToolUse` publishes the ownership claim and a lightweight before-manifest reservation while holding only the short shared-state lock. It then reads file content under a per-turn, per-path lock and briefly reacquires shared state to merge the snapshot metadata. Slow or large snapshots therefore do not make unrelated sessions fall through the state-lock timeout. A path discovered only by `PostToolUse`, or a failed before snapshot, becomes an explicit partial `after-only-receipt`; it never fabricates an add/delete patch or blocks the tool. Each uncertainty scope retains at most two writer summaries plus one normal timeout `pending.json`; compaction atomically ingests that file and promotes its anonymous timestamp to compaction time, preserving evidence across out-of-order writers and concurrent cleanup.

The commit subject is `[Codex] <actual behavior change>`. After the final writes and child tasks finish, the executing AI follows the Hook-provided `prepare` command, reads the final diff context, and fills the turn's dedicated report. The plugin binds the report to the final captured paths, actions, and hashes. Missing, invalid, stale, incomplete, or uncertain reports produce a file-action title and an explicit explanation of the fallback; the plugin never substitutes conversation prose or assumes verification passed. A hash match establishes association with the captured diff, not the truth of every natural-language claim. Partial audit coverage remains visible.

Files up to 8 MiB are eligible for commit content. Larger files retain hashes, sizes, and P4 metadata in the Git note without adding the full blob. Set `CODEX_P4_DIFF_MAX_BLOB_BYTES` to change the threshold.

Each new commit body contains the reason, actual changes with file references, optional decisions and limitations, separate verification categories, the redacted original `Request`, report status and source, diff digest, and session/turn IDs. For example, use “Fix handle selection priority” as the title; put “build passed; manual interaction not run” under verification. Exact file hashes, sizes, P4 metadata, and patches remain in the note. Read it with:

```powershell
git notes --ref=codex-p4-diff show <commit>
```

The note also contains `changeDescription`, `descriptionStatus`, `descriptionDiffDigest`, writer identities, and the compatible `promptSummary`. New requests are redacted and whitespace-normalized, then stored as `requestText` up to 12,000 characters; truncation is explicit in `requestCapture`. The original prompt SHA-256 remains available. Legacy prompts retain their existing summary without fetching old conversations. Only the assistant message hash is stored; the full assistant response is not copied into the note.

## Perforce boundary

The runtime allows only `p4 info`, `where`, `fstat`, `diff`, `opened`, and `print`. It never creates, edits, reconciles, reopens, reverts, shelves, or submits a P4 changelist. P4 checkout and submission remain the responsibility of P4V, Unreal Source Control, or the user.

Cross-session same-path conflicts use a turn-scoped local registry under `%LOCALAPPDATA%\CodexP4Diff\State`; they do not inspect or change P4 changelist ownership. Schema 8 records use a renewable ten-minute lease and a registry-epoch claim token. They are live only while session, active turn, status, before-manifest, and token agree. Windows ownership keys are case-insensitive while the original display casing is retained. A takeover changes the token, so an older turn becomes receipt-only even after the newer owner releases the path. Expired or inactive records are reclaimed.

Incomplete shell coverage and ordinary audit errors are reported as warnings and do not block the underlying tool. A `PreToolUse` denial is reserved for a confirmed live same-path conflict with another Codex session or turn.

## Review and GitHub Desktop

Use Codex's Git commit/branch review or GitHub Desktop History to inspect the full recorded change. `Unstaged` shows only changes not yet captured by the Stop hook. The native Last turn card is maintained by Codex separately and may omit direct child-task writes; plugin commits cannot backfill it. Prefer child tasks for analysis and patches, with the main task applying the final writes. If children write directly, review all writers through Git before delivery.

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

Check `ready`, `p4Mode`, `p4Writes`, `historyRoot`, `branch`, and `legacyGitPointer`. `historyRoot` must equal the owned P4 project root and `branch` must be `codex/local-history`; when the command starts in a nested directory, `historyRoot` may therefore be a parent directory.

Version 0.3 could create a P4 pending changelist and attach a plugin-owned `.git` pointer. Version 0.4 moved history outside the project. Version 0.5 performs no P4 migration and leaves old external history untouched; it only removes a legacy `.git` file when it points into known plugin-owned local-data directories. Version 0.5.3 dropped non-path ownership records produced by older shell parsing. Version 0.5.4 migrates ownership to schema 8, retaining only active before-manifest paths and replacing historical counters with bounded epoch-scoped claim tokens. Version 0.5.5 reuses the validated P4 identity from an existing owned marker and makes first-time client ambiguity visible. Existing P4 changelists remain entirely user-managed.

If first-time discovery finds more than one mapped P4 client, run Doctor with explicit `--port` and `--client` from the intended project root. Hooks report the ambiguity as a fail-open warning and do not block normal tool operations. If an existing owned marker can no longer be validated, repair the P4 login or mapping rather than deleting the marker or allowing the history to drift to another client.

## Update or remove

```powershell
codex plugin marketplace upgrade billycszhao-plugins
codex plugin add codex-p4-diff@billycszhao-plugins
codex plugin list --json
```

Confirm that the installed version is `0.6.0`. If `plugin add` reports that the plugin is already installed or the listed version remains older, reinstall only this public plugin:

```powershell
codex plugin remove codex-p4-diff@billycszhao-plugins
codex plugin add codex-p4-diff@billycszhao-plugins
codex plugin list --json
```

Restart Codex, approve the updated Hooks, and create a new task. Do not remove a separate local-development plugin from another marketplace.

Copyable update request for another Codex task:

> Update the public plugin codex-p4-diff@billycszhao-plugins from https://github.com/BillyCSZhao/codex-p4-diff to its latest published version (this release is v0.6.0). First inspect the installed version, marketplace source, and supported CLI commands. Refresh only that marketplace and install the new package. If needed, reinstall only this public plugin; preserve other marketplaces, local development copies, project .git directories, State, and existing history. If the source is pinned to an older tag, identify that before changing the source. Verify the installed version and source, and run the plugin's read-only Doctor in my intended P4 project. Report what was verified and tell me to restart Codex, trust updated Hooks, and create a new task. Do not claim existing tasks hot-reloaded or old Last turn cards were repaired. Ask only for a missing project path or a decision that cannot be safely inferred.

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
python -m py_compile plugins/codex-p4-diff/skills/codex-p4-diff/scripts/codex_p4_diff.py plugins/codex-p4-diff/skills/codex-p4-diff/scripts/codex_p4_audit.py plugins/codex-p4-diff/skills/codex-p4-diff/scripts/codex_p4_description.py plugins/codex-p4-diff/skills/codex-p4-diff/scripts/codex_p4_review.py plugins/codex-p4-diff/hooks/codex_p4_session.py
```

Tests use temporary directories and mocks; they do not mutate a Perforce server or workspace.
