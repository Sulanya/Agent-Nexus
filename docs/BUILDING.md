# Source packaging and desktop builds

## Reviewable source ZIP

`python tools/build_source.py` creates a ZIP beside the source directory using only entries from `docs/SOURCE_MANIFEST.json`. Every reviewed source hash must match. New private files and ignored runtime data are never discovered recursively by the packaging command. It refuses to overwrite an existing archive.

If you edit source, regenerate the manifest as a reviewed release step, then rerun `tools/audit_source.py` and tests. Do not add runtime files merely to make a manifest match. The manifest excludes itself because a file cannot contain its own final hash.

## Desktop application source

`launcher/app.py` is the desktop control panel, `launcher/web/` is its interface, and `launcher/make_icon.py` / `launcher/make_icon2.py` generate the icon assets. Install `requirements-desktop.txt` in an isolated development environment before running `python launcher/app.py` on Windows.

The historical platform wrappers (`tools/an.ps1`, `tools/an.sh`, `.cmd` / `.command`) support packaged runtimes and tunnels. The documented source entry (`tools/dev.py`) is local-first and does not require bundled runtimes or a tunnel. The original workstation-only pyc sealing, trial renewal tooling and opaque binary/runtime caches are intentionally not included as build prerequisites.

Signed Windows/macOS installers are not a deliverable of this source export. To produce them, choose and lock exact Python/pywebview/backend versions, generate icons, install the chosen bundler in an isolated build environment, package the complete required gateway module set and UI, include dependency licenses, and run clean-machine lifecycle/remote access/upgrade/uninstall regressions. macOS additionally needs platform signing/notarization. Do not advertise this preview as an already signed cross-platform installer.

No Codex, Claude, WorkBuddy, cloudflared, Python runtime or model binary is redistributed by the source ZIP. Fetch third-party components only through their official distribution channels and observe their separate terms.
