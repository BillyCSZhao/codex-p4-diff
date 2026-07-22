# Privacy Policy

Effective date: 2026-07-22

Codex P4 Diff runs locally on the user's computer. The plugin itself does not operate a hosted service, collect analytics, transmit telemetry, or send repository contents to BillyCSZhao.

The plugin may invoke locally installed `p4`, `git`, and P4V/P4VC programs. Those programs use the Perforce and diff-tool configuration already present on the user's computer. Any network traffic produced by Perforce is between the user's Perforce client and the server configured by that user or their organization.

Installing or updating the plugin downloads source files from GitHub through Codex's plugin marketplace mechanism. GitHub's own privacy terms apply to that download.

The local sidecar contains Git metadata and baseline blobs derived from the selected files' Perforce `#have` revisions. It is stored outside the P4 client root under the current user's local application-data directory by default. Users can remove it after disabling the bridge if they no longer need the local review history.

For questions or security reports, open an issue at <https://github.com/BillyCSZhao/codex-p4-diff/issues>.
