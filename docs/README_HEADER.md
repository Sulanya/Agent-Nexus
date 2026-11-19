# README 顶部 Logo 和能力徽章

把 docs/images/logo.svg 上传到 GitHub 对应目录，再将下面代码粘贴在根目录 README.md 的标题下方。保留线上已修改的正文，只更新页头。

```html
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
  <a href="docs/ARCHITECTURE.md#agent-能力与边界"><img src="https://img.shields.io/badge/Tool_Use-Native_Agent_Trace-0ea5e9?style=flat-square" alt="本机 Agent 工具执行与输出追踪"></a>
  <a href="docs/ARCHITECTURE.md#agent-能力与边界"><img src="https://img.shields.io/badge/Context-Budget_Aware-a855f7?style=flat-square" alt="上下文预算感知，需要配置元数据"></a>
  <a href="docs/ARCHITECTURE.md"><img src="https://img.shields.io/badge/Queue-Durable_Submission-10b981?style=flat-square" alt="持久提交队列与幂等收据"></a>
  <a href="docs/ARCHITECTURE.md"><img src="https://img.shields.io/badge/Recovery-Event_Replay-14b8a6?style=flat-square" alt="有界事件重放与会话对账"></a>
  <a href="docs/API.md"><img src="https://img.shields.io/badge/Workflow-Schedule_%26_Handoff-f59e0b?style=flat-square" alt="一次性定时任务与明确的跨会话交接"></a>
</p>
```

技术标签：MIT、Python 3.11、自托管、移动浏览器、三引擎；Agent 能力：本机工具执行追踪、上下文预算、持久队列、事件恢复、定时任务与会话交接。Stars 使用仓库实际数值。徽章对应能力边界见 docs/ARCHITECTURE.md。没有加入未经验证的覆盖率或线上构建成功标签。
