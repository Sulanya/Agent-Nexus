# Contributor instructions

- Keep runtime credentials, URLs containing access tokens, chat transcripts and local databases out of Git.
- Use isolated temporary data for tests. Never run tests against a contributor's real agent accounts.
- Preserve request identity across retries; reconcile unknown model outcomes before further submission.
- Read back model/project changes before showing success. Polling must not call a model.
- Bind to loopback by default. Do not remove authentication to fix a connection problem.
- Do not batch-kill processes. Stop only a PID verified through health and ownership checks.
- Back up before changing existing user data. Do not delete conversations during cleanup.
- Run Python regression tests and relevant UI checks; report unverified platform/engine behavior explicitly.
