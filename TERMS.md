# Terms of Use

Effective date: 2026-08-04

Codex P4 Diff is provided under the MIT License and without warranty.

The plugin records Codex changes in a local Git repository at the P4 project root and uses read-only Perforce queries for workspace and baseline metadata. Users remain responsible for Perforce checkout, changelist ownership, reviews, backups, and submission through their normal tools and policies.

Because Git uses the real P4 working tree, checkout, switch, discard, restore, reset, clean, merge, and rebase operations can modify project files. Users are responsible for using the repository as local history and for managing restoration through their normal P4 workflow.

The repository contains source-code audit material. It is created without a remote and with a `pre-push` hook, but users remain responsible for protecting, retaining, or deleting it and for not publishing it accidentally.

Hook-based path detection cannot guarantee a write-before snapshot for tools that bypass Codex Tool Hooks or hide output paths. Such changes may be represented by hashes and receipts rather than exact patches.

By installing or using the plugin, the user accepts these terms and the terms that apply to Codex, GitHub, Perforce, and Git.
