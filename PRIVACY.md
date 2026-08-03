# Privacy Policy

Effective date: 2026-08-03

Codex P4 Diff runs locally on the user's computer. The plugin itself does not operate a hosted service, collect analytics, transmit telemetry, or send repository contents to BillyCSZhao.

The plugin may invoke locally installed `p4`, `git`, and P4V/P4VC programs. Those programs use the Perforce and diff-tool configuration already present on the user's computer. Any network traffic produced by Perforce is between the user's Perforce client and the server configured by that user or their organization.

Installing or updating the plugin downloads source files from GitHub through Codex's plugin marketplace mechanism. GitHub's own privacy terms apply to that download.

The local sidecar contains Git metadata and baseline blobs derived from the selected files' Perforce `#have` revisions. It is stored outside the P4 client root under the current user's local application-data directory by default. Users can remove it after disabling the bridge if they no longer need the local review history.

The local audit ledger is stored separately under `%LOCALAPPDATA%\CodexP4Diff\Audit` on Windows (or the platform-equivalent local state directory). It may contain full user prompts, local file paths, Perforce connection identifiers, file actions, before/after hashes and sizes, before snapshots for text-sized files, and unified text patches. Binary and large-file audit events store hashes and sizes without copying the file content into the final event. The pending changelist description receives only a redacted and truncated summary plus a local audit path; the full prompt is not sent to the Perforce server by the plugin.

Audit ledgers and pending changelists are not automatically deleted when a Codex task ends. Users should retain or remove local audit data according to their organization's source-code and data-retention policies. The plugin does not automatically submit, shelve, revert, or delete Perforce changelists.

For questions or security reports, open an issue at <https://github.com/BillyCSZhao/codex-p4-diff/issues>.
