# Privacy Policy

Effective date: 2026-08-04

Codex P4 Diff runs locally. It does not operate a hosted service, collect analytics, transmit telemetry, or send repository contents to BillyCSZhao.

The plugin invokes locally installed `p4` and `git` programs. Perforce is restricted at runtime to the read-only `info`, `where`, `fstat`, `diff`, `opened`, and `print` commands. Any resulting network traffic is between the user's Perforce client and server. The plugin does not create, update, shelve, revert, or submit Perforce changelists.

Installing or updating downloads plugin source from GitHub through Codex's marketplace mechanism; GitHub's privacy terms apply.

Local Git history is stored in `.git` at the P4 project root. Commits may contain source file content up to the configured size threshold. Commit messages and `refs/notes/codex-p4-diff` may contain local project paths, redacted prompt summaries, prompt SHA-256 values, text patches, file actions, before/after hashes and sizes, Perforce connection identifiers, depot paths, and revision metadata.

The default content threshold is 8 MiB. Larger files are represented by hashes, sizes, and Perforce metadata. Temporary write-before snapshots, session ledgers, and ownership records remain under `%LOCALAPPDATA%\CodexP4Diff\State` on Windows or the platform-equivalent local state directory.

The plugin does not configure a remote and installs a `pre-push` hook that rejects pushes. Users should not publish the repository because commits and notes may contain source code and local metadata.

History and state are not automatically deleted when a task ends or the plugin is removed. Users are responsible for retention under their organization's policies.

For questions or security reports, open an issue at <https://github.com/BillyCSZhao/codex-p4-diff/issues>.
