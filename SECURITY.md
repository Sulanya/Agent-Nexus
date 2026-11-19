# Security and privacy

AN can expose local files, coding-agent sessions, tool execution and approvals. Treat full-access links, QR codes and cookies as credentials. Keep the default loopback binding; explicitly configure any LAN/public access and prefer TLS and private networks.

The gateway reads native agent state on the host. A tunnel or reverse proxy may transport browser traffic; model traffic is governed by each configured agent/provider. “Local execution” does not mean all traffic remains offline, nor that token usage is zero.

Do not upload _run/, gateway/.secrets/, .env, agent configuration/auth files, transcript JSONL, session databases, screenshots with private content, or logs containing request bodies/paths. Generated connection pages must remain private. Authentication is not disabled for the developer in this public edition.

Unknown command outcomes require reconciliation; users should review target sessions before manually marking tasks complete. Protocol adapters and third-party agents can change; do not rely on AN as an isolation boundary for untrusted code.

Do not report working credentials or exploitable details in public Issues. Before enabling public GitHub security reports, the repository owner must configure a private vulnerability reporting channel. Until then, keep details private and contact the maintainer through a separately verified private channel. No private address is included in this source package.
