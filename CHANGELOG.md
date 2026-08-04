# Changelog

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
