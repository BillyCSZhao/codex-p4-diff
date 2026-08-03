# Changelog

## 0.4.0 - 2026-08-03

- Replace Perforce changelist creation and reconciliation with one external local Git branch per Codex session.
- Create one automatic local Git commit per changed user turn, including a redacted request summary, file actions, hashes, sizes, read-only P4 metadata, and unified text patches.
- Enforce a runtime read-only P4 command whitelist and remove every P4 mutation path.
- Store history outside the P4 workspace so GitHub Desktop operations cannot modify real P4 files.
- Keep history local-only with no remote and a `pre-push` blocker; remove the old `pre-commit` blocker so automatic turn commits can succeed.
- Add local cross-session path ownership without using P4 changelist ownership.
- Add an 8 MiB default blob threshold and receipt-only large-file history for Unreal assets.
- Migrate safely from 0.3 by detaching only legacy tool-owned `.git` pointers while leaving old P4 changelists untouched.

## 0.3.0 - 2026-08-03

- Create one Perforce pending changelist per writing Codex task, with lazy creation on the first managed write.
- Record one local audit event per user turn, including file actions, hashes, sizes, and unified text patches.
- Add Codex lifecycle Hooks for session validation, pre-write ownership checks, post-write reconciliation, and turn finalization.
- Reject writes to files already owned by another changelist instead of silently moving them with `p4 reopen`.
- Add `--changelist auto` resolution with session, client, project, and changelist ownership validation.
- Add session and audit summaries to pending changelist descriptions without uploading full prompts.
- Preserve the external Git baseline and native Codex Review workflow from 0.2.0.
- Document the required marketplace refresh, plugin reinstall, Codex restart, and new-task update flow.

## 0.2.0 - 2026-07-22

- Publish the first public marketplace version of the Perforce-to-Codex Review bridge.
