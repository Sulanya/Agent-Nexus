# Third-party dependencies and compatibility

Agent Nexus source is MIT licensed. Dependencies and connected products retain their own licenses and terms; MIT does not relicense them.

| Component | Use | License / terms to check |
|---|---|---|
| Python | Runtime, not bundled | Python Software Foundation license |
| psutil | Process and ownership checks | BSD-3-Clause; verify installed version metadata |
| qrcode | Optional private connection QR | BSD; verify installed version metadata |
| Pillow | Image/QR/icon support | HPND-style license; consult installed distribution |
| pywebview | Optional desktop webview | BSD-3-Clause; native backends may have additional terms |
| faster-whisper / CTranslate2 | Optional local transcription | Consult package/dependency licenses; model weights have separate terms |
| cloudflared | Optional tunnel, not bundled | Apache-2.0 software plus service terms |
| Codex / Claude Code / WorkBuddy | External coding engines, not bundled | Each vendor's code, service and account terms |

The source UI uses local/system rendering and generated assets; bundled proprietary agent binaries, third-party runtime distributions and downloaded model weights are excluded. The original launcher demo QR image is omitted; regenerate only with fake demo data.

T3 Code inspired architectural discussion about command durability and control surfaces. This package does not include T3 Code source or binaries. This is not a comprehensive external legal provenance audit: before publishing compiled installers, inventory exact locked dependency versions and their transitive native components, obtain required notices, and verify ownership of contributed artwork/code.
