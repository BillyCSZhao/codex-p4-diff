# Privacy Policy

Effective date: 2026-08-03

Codex P4 Diff runs locally on the user's computer. The plugin itself does not operate a hosted service, collect analytics, transmit telemetry, or send repository contents to BillyCSZhao.

The plugin invokes locally installed `p4` and `git` programs. Perforce is restricted at runtime to the read-only `info`, `where`, `fstat`, `diff`, `opened`, and `print` commands. Any network traffic produced by those queries is between the user's Perforce client and the server configured by that user or organization. The plugin does not create, update, shelve, revert, or submit Perforce changelists.

Installing or updating the plugin downloads source files from GitHub through Codex's plugin marketplace mechanism. GitHub's own privacy terms apply to that download.

The local Git history is stored outside the P4 client root under `%LOCALAPPDATA%\CodexP4Diff\History` on Windows, or the platform-equivalent local state directory. It may contain local project paths, redacted prompt summaries, prompt SHA-256 values, source file content up to the configured size threshold, text patches, file actions, before/after hashes and sizes, Perforce connection identifiers, depot paths, and revision metadata.

The default file-content threshold is 8 MiB. Larger files are represented by hashes, sizes, and Perforce metadata rather than complete file content. Temporary write-before snapshots and session ownership data are stored under `%LOCALAPPDATA%\CodexP4Diff\State`.

The plugin does not configure a Git remote and installs a `pre-push` hook that rejects pushes. Users should not remove that protection or publish the history repository because its commits may contain source code and local metadata.

Local history is not automatically deleted when a Codex task ends or the plugin is removed. Users should retain or remove it according to their organization's source-code and data-retention policies.

For questions or security reports, open an issue at <https://github.com/BillyCSZhao/codex-p4-diff/issues>.
