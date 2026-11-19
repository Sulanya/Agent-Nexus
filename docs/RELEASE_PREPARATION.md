# Source release preparation

Prepared 2026-11-18 and README/prompt updated 2026-11-19 as a separate source-only directory. The live installation, private agent state and running services were not modified by this export.

## Included

Readable Python gateway modules, browser UI, desktop launcher source and icon generation scripts, regression tests, platform shell wrappers, a portable source-development launcher, installation/security/contribution documentation, MIT license, CI and issue templates.

## Excluded

All original _run/, .secrets/, private connection pages and QR codes, tokens/API credentials, .env, chat histories, local SQLite/JSON indexes, private settings, personal AGENTS/memory, historical backups/logs, bundled runtimes, binary dependencies, release ZIP/EXE, research/debug dumps and previous Git history. This export is a new source snapshot; it contains no hidden Git history.

## Changes for this source snapshot

Private workstation paths, personal billing/expiry copy and private provider labels were removed/generalized; README intentionally uses the public author voice requested for this release. Private executable discovery shortcuts and workstation router dependencies were removed or made opt-in. Loopback and reduced execution permissions are defaults. Dead compatibility trial modules remain readable but do not enable licensing enforcement, expiry or self-deletion. The documented main development entry does not auto-create a public tunnel. README includes a prompt to configure AN against each downloader's own local session archive without copying history into Git.

## Validation and limits

docs/SOURCE_MANIFEST.json records exported source hashes and intentional transformation filenames. tools/audit_source.py checks forbidden runtime files and common credential patterns; it is a screening tool, not proof against every possible secret. The preparation validation report records the actual checks and failures. Platform installers, vendor compatibility and third-party license provenance still require review before publishing a signed binary release.

No GitHub repository has been created or pushed by this preparation step. Before public upload: review all source diffs and this inventory, configure private vulnerability reporting, choose the actual GitHub owner/repository, and inspect staged files and git history. This snapshot is designed to be reviewed before that publishing action.
