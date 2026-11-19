# 本机安装与档案读取提示词

将下面提示词复制给**运行在你自己的电脑上的编程 Agent**。提示词也包含在中英文 README 中。

```text
I am the user of this computer. Help me install and configure Agent Nexus, then connect it to my own local coding-agent session archives.

First read README.md, docs/INSTALLATION.md, docs/LOCAL_AGENT_SETUP_PROMPT.md and SECURITY.md. Inspect the OS, Python 3.11, installed agents and their actual data paths. Do not guess paths; report which directories you need to access and why before reading them.

Use the agent logins and provider configuration already present on this computer. Do not request or create API keys, read or print secret values, upload transcripts, or write private paths, tokens or session text to Git, logs, screenshots or this repository. Inspect native session files read-only. Do not move, rename, delete, convert or copy them into this repository.

Create an isolated virtual environment in this source directory and a local-only config copied from an.config.example.json. Keep loopback binding, public tunnels off, and extra execution permissions off. Start only this source instance and verify health, authentication and its session list. Do not restart or modify another running AN instance.

First connect my existing Codex/Claude Code/WorkBuddy session indexes read-only so I can find my own projects, conversation titles, dates and status in AN. Limit access to agent data directories I confirm. Verify whether AN can read my historical archives, but do not copy or summarize transcript contents into README, repository docs, issues or persistent memory. If I later request an archive summary, propose a private local output path outside the repository and wait for confirmation before writing.

Report which directories and agents were actually used, project/session counts, archive formats that are unsupported and local access instructions. Never print credentials or transcript contents. Stop and explain before expanding the requested data scope.
```

This prompt is guidance only. It neither runs automatically nor grants filesystem permissions. Agent archive discovery depends on the user's installed agent format, paths and access rights.
