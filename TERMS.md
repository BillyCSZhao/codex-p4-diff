# Terms of Use

Effective date: 2026-08-03

Codex P4 Diff is provided under the MIT License and without warranty.

The plugin creates task-specific pending changelists, opens or reconciles files in the user's real Perforce workspace, and lets Codex's native Review staging and restore operations act on those files. Users are responsible for reviewing commands, Hook permissions, workspace mappings, changelist ownership, local audit retention, backups, and Perforce operations before changing or reverting files.

Git commit and push hooks are installed as a safety guard, but Perforce remains the authoritative submission system. The plugin does not automatically submit, shelve, revert, or delete pending changelists. Users must review and submit changes through their organization's normal Perforce process.

By installing or using the plugin, the user accepts these terms and the terms that apply to Codex, GitHub, Perforce, Git, P4V, and any configured external diff tool.
