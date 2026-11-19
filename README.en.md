# Agent Nexus

<p align="center">
  <img src="docs/images/logo.svg" alt="Agent Nexus Logo" width="520">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-22c55e?style=flat-square" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.11">
  <a href="docs/INSTALLATION.md"><img src="https://img.shields.io/badge/Deploy-Self--Hosted-8b5cf6?style=flat-square" alt="Self-hosted deployment"></a>
  <a href="docs/images/README.md"><img src="https://img.shields.io/badge/Access-Mobile_Browser-06b6d4?style=flat-square" alt="Mobile browser access"></a>
  <img src="https://img.shields.io/badge/Agents-Codex_%7C_Claude_Code_%7C_WorkBuddy-f97316?style=flat-square" alt="Codex, Claude Code and WorkBuddy adapters">
  <a href="https://github.com/Sulanya/Agent-Nexus/stargazers"><img src="https://img.shields.io/github/stars/Sulanya/Agent-Nexus?style=flat-square&amp;logo=github&amp;label=Stars" alt="GitHub stars"></a>
</p>

<p align="center">
  <a href="docs/ARCHITECTURE.md#agent-能力与边界"><img src="https://img.shields.io/badge/Tool_Use-Native_Agent_Trace-0ea5e9?style=flat-square" alt="Native agent tool execution traces"></a>
  <a href="docs/ARCHITECTURE.md#agent-能力与边界"><img src="https://img.shields.io/badge/Context-Budget_Aware-a855f7?style=flat-square" alt="Context budget metadata support"></a>
  <a href="docs/ARCHITECTURE.md"><img src="https://img.shields.io/badge/Queue-Durable_Submission-10b981?style=flat-square" alt="Durable submission and idempotent receipts"></a>
  <a href="docs/ARCHITECTURE.md"><img src="https://img.shields.io/badge/Recovery-Event_Replay-14b8a6?style=flat-square" alt="Bounded event replay and session reconciliation"></a>
  <a href="docs/API.md"><img src="https://img.shields.io/badge/Workflow-Schedule_%26_Handoff-f59e0b?style=flat-square" alt="One-shot schedules and explicit handoffs"></a>
</p>

**View and take over AI coding sessions on your computer from a browser on any mobile device, while keeping your existing working environment.**

Agent Nexus is a self-hosted remote workspace for existing coding agents. Execution, files, tools and agent authentication remain on your computer. Phones, tablets and other mobile devices with modern JavaScript/WebSocket-capable browsers can act as clients.

Hi, I'm Chuqí. Agent Nexus is my attempt to make my local AI coding workflow accessible from a browser on any mobile device while keeping existing projects and agent sessions on the computer. This generic source snapshot does not include my account, work archive or private configuration.

Downloaders can give the [local setup prompt](docs/LOCAL_AGENT_SETUP_PROMPT.md) to a coding agent already installed on their own computer so it can configure AN there and connect to their own session archive.

The repository contains no embedded author API key, provider secret or access token. Source includes adapters that read a user's own local provider configuration, but contains no author credentials. At runtime, AN creates local access credentials on each user's own computer. Never paste secrets into this repository, GitHub, issues or setup prompts.

This source preview includes the Python gateway, browser UI, desktop launcher source, tests and platform scripts. It does not include agent subscriptions, API quotas, chat histories, bundled runtimes or third-party executables.

See **[desktop, tablet and mobile screenshots](docs/images/README.md)**. Images are collected in `docs/images/`.

## Start from source

Install Python 3.11 and a supported local coding agent (Codex is the primary integration). Create a virtual environment, install `requirements.txt`, copy `an.config.example.json` to `an.config.json`, then run:

```bash
python tools/dev.py start
python tools/dev.py open
```

The source launcher binds to loopback by default. Run `python tools/dev.py status` to inspect it and `python tools/dev.py stop` to stop only a verified owned gateway process. Remote access requires a network path you configure, preferably HTTPS and a private VPN or tunnel. `python tools/dev.py connect --base-url https://your-an-host.example` generates a local private connection page; never publish its contents.

### Prompt for your own local coding agent

After extracting this source, you may give the following prompt to Codex, Claude Code or another coding agent running on your own computer. Keep credentials and private transcript content out of the repository and prompt.

```text
I am the user of this computer. Help me install and configure Agent Nexus, then connect it to my own local coding-agent session archives.

First read README.md, docs/INSTALLATION.md, docs/LOCAL_AGENT_SETUP_PROMPT.md and SECURITY.md. Inspect the OS, Python 3.11, installed agents and their actual data paths. Do not guess paths; report which directories you need to access and why before reading them.

Use the agent logins and provider configuration already present on this computer. Do not request or create API keys, read or print secret values, upload transcripts, or write private paths, tokens or session text to Git, logs, screenshots or this repository. Inspect native session files read-only. Do not move, rename, delete, convert or copy them into this repository.

Create an isolated virtual environment in this source directory and a local-only config copied from an.config.example.json. Keep loopback binding, public tunnels off, and extra execution permissions off. Start only this source instance and verify health, authentication and its session list. Do not restart or modify another running AN instance.

First connect my existing Codex/Claude Code/WorkBuddy session indexes read-only so I can find my own projects, conversation titles, dates and status in AN. Limit access to agent data directories I confirm. Verify whether AN can read my historical archives, but do not copy or summarize transcript contents into README, repository docs, issues or persistent memory. If I later request an archive summary, propose a private local output path outside the repository and wait for confirmation before writing.

Report which directories and agents were actually used, project/session counts, archive formats that are unsupported and local access instructions. Never print credentials or transcript contents. Stop and explain before expanding the requested data scope.
```

The prompt is guidance for the user's own local agent; AN does not send it automatically or grant extra filesystem access. Actual archive discovery depends on the installed agent format and local permissions.

## Why Agent Nexus exists

This project did not begin as a product plan looking for a problem. It began with a daily inconvenience: Chuqí used AI coding tools on a computer, then needed to leave the desk and still see or take over the work.

The available remote paths were not as open as they looked. Codex remote use mainly depends on its account system rather than acting as a simple self-hosted API. WorkBuddy and tools in the DeepSeek Harness family also tend to tie remote access to their own accounts or hosted services. Projects, sessions, model permissions and remote entry points become scattered across vendor identities.

Agent Nexus takes a different direction: keep the real work on the computer, and let any mobile browser become the viewing and control surface. The longer-term goal is to reduce unnecessary dependence on platform accounts, so people can own their working environment, session archive and connection method.

Chuqí's view is simple: **a communist society should not have these unnecessary restrictions.** Tools should serve people instead of locking them into an account, client or remote plan. Open-sourcing AN is an invitation to inspect, modify and continue that work together.

This source preview does not magically remove every external account requirement. Codex, WorkBuddy, Claude Code, DeepSeek Harness and other agents still define their own login, terms and model permissions. AN currently avoids adding a separate AN cloud account and tries to reuse the user's existing local environment. Full independence depends on whether each external agent provides a local protocol, API or replaceable execution path; we will document that boundary rather than present a slogan as a finished feature.

## Compatibility
Codex uses its local app-server, projects and rollout data. Claude Code and WorkBuddy use their own CLI/transcript/index formats; features are not identical across engines. Unsent new Claude/WorkBuddy sessions are AN drafts until the first message creates the native transcript. Windows has the most existing validation. macOS packaging scripts are included; Linux and clean-machine desktop packaging still need platform validation.

## Development

```bash
python -m unittest discover -s gateway -v
node gateway/test_rpc_ui.cjs
python tools/audit_source.py
```

See [installation](docs/INSTALLATION.md), [architecture](docs/ARCHITECTURE.md), [security](SECURITY.md), [contributing](CONTRIBUTING.md) and [third-party notices](THIRD_PARTY_NOTICES.md). Project code is MIT licensed; third-party tools remain governed by their own terms.
