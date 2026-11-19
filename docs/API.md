# API and persistence reference

This is a source-level overview, not a promise that all historical adapters share identical behavior. The actual route implementations are in gateway/remote_server.py.

## Authentication and contract

`GET /api/health` returns process/service metadata without credentials. Other API routes require an `X-Auth` token or the current private link/cookie mechanism. Full-access tokens permit state-changing actions; view tokens remain read-only and have additional file/approval restrictions.

`GET /api/rpc/contract` publishes independent API/WebSocket versions, message methods, limits and authorization policy. `/api/ws?rpc=1` negotiates a compatible version before upgrade. Each client message revalidates current credentials. An accepted socket does not authorize arbitrary methods.

## Main route families

| Route | Purpose |
|---|---|
| GET /api/whoami | Current access role |
| GET /api/status | Engines, active turns and pending approvals |
| GET /api/threads?engine=codex | Engine-specific session index |
| POST /api/threads | Create a session/draft according to engine semantics |
| GET /api/threads/:id | Read native session projection |
| POST /api/threads/:id/turns | Submit a message using stable clientUserMessageId |
| POST /api/threads/:id/interrupt | Interrupt execution, respecting engine behavior |
| GET /api/threads/:id/queue | Native queue projection where supported |
| GET /api/threads/:id/recovery | Recover by native item/turn identity |
| POST /api/projects/create | Idempotent native Codex project creation/readback |
| GET /api/providers | Configured engine/provider metadata |
| GET/POST /api/workflows | List/create a one-shot task or explicit handoff |
| GET /api/workflows/:id | Task state and ordered task events |
| POST /api/workflows/:id/cancel | Cancel a task not yet dispatched |
| POST /api/workflows/:id/reconcile | Reconcile unknown outcome with the original request identity |

Use the current UI and route source for engine-specific payload fields. Never infer that HTTP success means a model turn has completed.

## Persistence

All AN-local event journals, workflow state, submission ledgers and connection outputs are private runtime artifacts. Native agent project/session stores remain owned by those agents. The public source ZIP contains neither kind of user data.

Retried creates use stable requestId values, while messages retain clientUserMessageId. Do not replace these IDs when a network timeout leaves the outcome unknown. A confirmed rejection and an unknown external side effect require different recovery paths.
