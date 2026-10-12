# Changelog

## 0.6.0 - 2026-10-12

- Replace conversational commit titles with a dedicated AI-written report bound to the final turn diff. Record why, actual behavior changes, relevant decisions, separate build/automated/manual verification, and known limitations. Keep the request separate; verification is explicitly executor-reported, not rerun by the plugin.
- Validate session/turn identity, the complete captured file set, before/after hashes, and archive attribution. Missing, invalid, stale, incomplete, or uncertain reports fall back to file facts without blocking history. Recovery drift discards unsupported implementation and verification claims. Existing commits and frozen legacy descriptions stay unchanged.
- Preserve the redacted, whitespace-normalized current request up to 12,000 characters with explicit truncation metadata and its original SHA-256. Keep full file evidence and patches in Git notes rather than the commit body.
- Include linked subagent writes in their owning parent turn, retain writer identities and earliest snapshots, and wait for pending child tools before finalization. Add exact before/after review refs for all recorded writers; document that these cannot backfill Codex's native Last turn card.
- Recover only an index lock proven to belong to a terminated plugin writer. Preserve unrelated or unverifiable locks, worktree content, and staged changes.
- Extend lifecycle Hook output and description instructions; treat only standalone writes to the current active report as plugin metadata. Keep all other path auditing and the P4 read-only boundary intact.
- Add regression coverage for parent/child attribution, lock recovery, review refs, feature/fix/docs/test reports, missing evidence, redaction, long requests, stale reports, and immutable history.

## 0.5.6 - 2026-09-27

- Accept a legacy marker that stored the broker endpoint as its server address only after reconnecting to that exact endpoint and verifying the live broker address, client, and project mapping. Preserve the marker and existing State keys; report the resolved backend separately. Modern identity conflicts and unverified aliases still fail validation.
- Capture `exec_command` writes through PreToolUse and PostToolUse, including `cmd`, `workdir`, and explicit Bash/sh/zsh shell selection. Keep read-only commands outside write tracking.
- Add regression coverage for broker validation, unchanged history identity, rejected mismatches, and complete before/after capture through local commit creation.
- Preserve the fixed history branch, P4 read-only boundary, and existing project files. Branch repair remains an explicit maintenance action, not an automatic Hook operation.

## 0.5.5 - 2026-08-25

- Reuse and revalidate the P4 client/server identity stored in an existing plugin-owned `.git/codex-p4-diff/project.json` before ambient environment or P4V discovery, so an unrelated default client or duplicate workspace mapping cannot silently redirect an established local history.
- Resolve a nested task directory to the nearest parent plugin-owned history while stopping at any closer foreign `.git` boundary. A recognized plugin-owned legacy pointer becomes that directory's project boundary and keeps the existing serialized migration behavior; it is never used to traverse into a farther history.
- Surface first-time multi-client ambiguity and invalid owned-history identity as visible fail-open Hook warnings without blocking normal tool operations; ordinary unmapped directories remain out of scope, and the runtime P4 whitelist remains read-only.

## 0.5.4 - 2026-08-08

- Replace age-deleted project lock files with bounded operating-system file locks that require no sentinel writes, and migrate ownership to schema 8 renewable short leases with random registry epochs and per-claim tokens that cannot be reused after registry rebuild.
- Journal Git finalization as an idempotent transaction so interrupted ref, note, and index updates resume without duplicating a turn commit.
- Add project-level recovery markers so any later session can finish an earlier deferred turn; serialize first-writer Stop-time snapshot publication, seal its ownership attribution, and release the old claim only after sealing succeeds so another session can continue immediately. Duplicate or slower timeout handlers cannot replace an already published snapshot or release its claim before sealing. If freezing fails, fall back to `deferred-after-unavailable-receipt-only` without reading later worktree content.
- Merge terminal turn state under the short shared-state lock so cross-session recovery cannot overwrite a concurrently created prompt, active turn, or ownership claim.
- Preserve pre-existing same-path staged content as `staged-receipt-only` instead of refusing the whole turn; serialize compare-and-update with Git's index lock, retry transient Windows replace contention, and atomically install a temporary index so unrelated or concurrent staged entries remain untouched.
- Expand statically resolvable shell writes across command chains, call operators, directories, and globs, while recording explicit `partial` coverage warnings for dynamic or unresolved targets without blocking the command.
- Reserve ownership and before-manifest entries under a short shared-state lock, then capture file bytes under per-turn, per-path locks so a slow snapshot cannot make unrelated tasks miss ownership auditing.
- Compact uncertainty evidence to at most two writer summaries plus one normal timeout fallback; atomically ingest the fallback and promote its anonymous high-water mark at compaction so out-of-order writers and cleanup races cannot hide a concurrent write.
- Keep ordinary audit failures fail-open and preserve the runtime Perforce read-only command boundary.

## 0.5.3 - 2026-08-07

- Parse shell commands with quote awareness so ordinary strings such as PowerShell's `'Stop'` and regex characters such as `>` are not mistaken for project paths or redirections.
- Claim shell paths only when they are explicit write targets, while ignoring directory and Junction creation.
- Scope same-path ownership to the active turn and release it after a successful commit or a no-change Stop; retain it after failed finalization for safe retry.
- Migrate legacy ownership state on the first updated task and discard non-path records produced by older shell parsing.

## 0.5.2 - 2026-08-05

- Build each commit title from the first meaningful completed-change line in the Stop hook's `last_assistant_message` instead of echoing the user's prompt.
- Preserve the redacted original request in `Request` and `promptSummary` while recording `changeSummary`, `changeSummarySource`, and `assistantMessageSha256` for auditability.
- Fall back to deterministic file-action titles when no usable assistant summary is available.
- Leave all existing local history unchanged; only new commits created by updated tasks use the new title format.

## 0.5.1 - 2026-08-04

- Preserve UTF-8 hook input across the Windows PowerShell 5.1 launcher so Chinese prompt summaries remain intact in commit messages and audit notes.

## 0.5.0 - 2026-08-04

- Initialize a real local Git repository directly at each P4 project root.
- Use one fixed `codex/local-history` branch for every Codex session, with no per-session switching.
- Create one local commit per changed user turn using real project-relative paths for Codex Review and GitHub Desktop.
- Put session, turn, request, file action, hash, size, P4 metadata, and exact text patch details in commit messages and `refs/notes/codex-p4-diff`.
- Serialize cross-session commits and retain same-path ownership protection.
- Update only the recorded turn paths in Git's index, refuse same-path staged conflicts, and preserve unrelated staged content.
- Refuse to take over foreign Git repositories, reject remotes, and block push.
- Keep the runtime P4 whitelist read-only and leave all P4 changelists untouched.

## 0.4.0 - 2026-08-03

- Replace Perforce changelist creation and reconciliation with one external local Git branch per Codex session.
- Create one automatic local Git commit per changed user turn with local audit receipts.
- Enforce a runtime read-only P4 command whitelist and remove every P4 mutation path.
- Add local cross-session path ownership and an 8 MiB default blob threshold.

## 0.3.0 - 2026-08-03

- Create one Perforce pending changelist per writing Codex task and one local audit event per user turn.
- Add lifecycle Hooks for ownership, reconciliation, and turn finalization.

## 0.2.0 - 2026-07-22

- Publish the first public marketplace version of the Perforce-to-Codex Review bridge.
