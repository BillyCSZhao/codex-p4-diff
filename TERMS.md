# Terms of Use

Effective date: 2026-08-03

Codex P4 Diff is provided under the MIT License and without warranty.

The plugin records Codex changes in an external local Git repository and uses read-only Perforce queries for workspace and baseline metadata. Users remain responsible for Perforce checkout, changelist ownership, reviews, backups, and submission through their organization's normal tools and policies.

The local history repository contains source-code audit material. It is created without a remote and with a `pre-push` hook, but users remain responsible for protecting, retaining, or deleting that local data and for not publishing it accidentally.

Hook-based path detection cannot guarantee a write-before snapshot for tools that bypass Codex Tool Hooks or hide their output paths. Such changes may be represented by hashes and receipts rather than exact patches.

By installing or using the plugin, the user accepts these terms and the terms that apply to Codex, GitHub, Perforce, and Git.
