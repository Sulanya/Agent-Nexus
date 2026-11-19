# Contributing

Use Python 3.11 and an isolated virtual environment. Run unittest discovery, node gateway/test_rpc_ui.cjs and tools/audit_source.py. Windows contributors with Edge should also run gateway/test_workflow_ui.cjs.

Keep fixes focused. Add behavior tests for queue identity, persistence, recovery or cross-client synchronization. Use temporary SQLite fixtures and fake engine responses; unit tests must not invoke paid models, mutate personal chat state or start a second production gateway.

Before submitting: remove credentials and personal paths from code/logs/screenshots, document engine/platform limitations, and describe the concrete before/after behavior and validation. Never delete conversation files to make a list look clean. New project/setting operations need idempotency and persistent readback.

Changes are contributed under this repository's MIT license. Contributors must have the right to submit code and preserve third-party notices. Do not paste proprietary CLI bundles or restricted assets into this repository.
