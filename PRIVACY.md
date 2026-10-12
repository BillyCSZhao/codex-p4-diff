# Privacy Policy

Effective date: 2026-10-12

Codex P4 Diff runs locally. It does not operate a hosted service, collect analytics, transmit telemetry, or send repository contents to BillyCSZhao.

The plugin invokes locally installed `p4` and `git` programs. Perforce is restricted at runtime to the read-only `info`, `where`, `fstat`, `diff`, `opened`, and `print` commands. Any resulting network traffic is between the user's Perforce client and server. The plugin does not create, update, shelve, revert, or submit Perforce changelists.

Installing or updating downloads plugin source from GitHub through Codex's marketplace mechanism; GitHub's privacy terms apply.

Local Git history is stored in `.git` at the P4 project root. Commits may contain source file content up to the configured size threshold. Commit messages and `refs/notes/codex-p4-diff` may contain local project paths, redacted change reports, reasons, decisions, executor-reported verification evidence, known limitations, writer identities, redacted prompt summaries, and the redacted current request (whitespace normalized, limited to 12,000 characters with truncation marked). They also include prompt and assistant-message SHA-256 values, report status and diff digest, text patches, file actions, before/after hashes and sizes, audit-coverage and recovery warnings, Perforce connection identifiers, depot paths, and revision metadata. The full assistant response and unrelated chat history are not copied into the Git note. Existing token/password/secret and Bearer redaction rules apply to report prose and requests; redaction is not a guarantee that all sensitive information will be recognized.

Description templates, bounded diff context, and executor-written reports remain in the local turn State directory. The executing Codex task produces the report using its existing context; the plugin does not make a separate model API call and does not independently rerun the reported checks. Review refs may retain before/after snapshots for recorded writers in the same local Git repository without changing the working tree or main index.

The default content threshold is 8 MiB. Larger files are represented by hashes, sizes, and Perforce metadata. Temporary write-before and deferred-after snapshots, shell coverage records, transaction journals, project-level recovery markers, bounded uncertainty summaries, session ledgers, and turn-scoped ownership records remain under `%LOCALAPPDATA%\CodexP4Diff\State` on Windows or the platform-equivalent local state directory. Eligible small deferred-after snapshots may contain the file bytes present when Stop ran. If that snapshot cannot be captured, recovery stores unknown after metadata and never substitutes later live-worktree content. Ownership uses a renewable ten-minute lease by default and is also invalidated when its session, active turn, status, or before-manifest no longer matches. Successful, no-change, and failed finalization attempts release same-path ownership; a failed turn retains its local transaction and snapshot state for automatic recovery by a later prompt, session end, or another session.

Project commit serialization, before-snapshot coordination, and first-writer deferred-after publication use bounded operating-system file locks. The current implementation writes no project content or sentinel payload to lock files and does not treat them as active based on age. File content is read outside the shared ownership-state lock under turn-scoped locks.

The plugin does not configure a remote and installs a `pre-push` hook that rejects pushes. Users should not publish the repository because commits and notes may contain source code and local metadata.

History and state are not automatically deleted when a task ends or the plugin is removed. Users are responsible for retention under their organization's policies.

For questions or security reports, open an issue at <https://github.com/BillyCSZhao/codex-p4-diff/issues>.
