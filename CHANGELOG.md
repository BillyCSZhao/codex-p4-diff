# Changelog

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
