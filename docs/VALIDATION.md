# Source snapshot validation

Validation date: 2026-11-19. These results apply to this prepared source snapshot, not future edits or arbitrary vendor versions.

| Check | Result | Scope |
|---|---|---|
| Python gateway regression | PASS: 43 tests | Temporary fixtures, real extracted route handlers, transaction failure injection; no paid model calls |
| Versioned RPC UI smoke | PASS | Node 22 VM with mocked transport |
| Workflow/sidebar UI smoke | PASS | Local Edge, isolated temporary browser profile, 375×667 viewport plus simulated desktop width |
| Source lifecycle smoke | PASS | Python 3.11; copied source in a temporary directory, isolated empty home and independent loopback port |
| Startup ownership | PASS | Second start returns the same verified gateway PID |
| HTTP authentication | PASS | Unauthenticated whoami returns 401; private token returns full role |
| API requests | PASS | Health, RPC contract, workflows, threads and authenticated UI |
| Process cleanup | PASS | Exact owned gateway stop; temporary child processes verified gone |
| Sensitive artifact screening | PASS | Public scanner plus private local checks for workstation identifiers, old domains and exact credential values; the requested author byline is intentional |
| Local setup/read-archive prompt | PASS | Directs users to their own local agent, confirmed paths, read-only discovery and no credential/transcript publication |
| JavaScript parse | PASS | Inline UI blocks and launcher script |

## Reproduce

```bash
python -m unittest discover -s gateway -v
node gateway/test_rpc_ui.cjs
python tools/test_source_lifecycle.py
python tools/audit_source.py
```

Install `requirements.txt` before the lifecycle smoke. On Windows with Edge, also run `node gateway/test_workflow_ui.cjs`.

The lifecycle smoke deliberately excludes real engine login/config and real model calls. It confirms gateway/source startup and API boundaries when an engine is unavailable; it is not an end-to-end vendor protocol certification. Existing Python tests emit a non-failing invalid-escape DeprecationWarning from a legacy HTML string.

## API credentials

The repository contains generic code that reads a user's own configured provider metadata and supports API-key field names. It contains no embedded author API key, provider secret or access token. The local gateway generates access credentials at runtime for each installation. Public scanning and exact-value checks against local credential files found no matches.

## Not claimed

- No clean-machine Windows/macOS/Linux signed installer certification.
- No real remote mobile-browser network matrix on this source snapshot.
- No real Codex/Claude/WorkBuddy model execution using fresh accounts.
- No third-party legal provenance certification; dependency versions/transitive licenses need review before binary redistribution.
- Automated scanning cannot prove the absence of every possible secret. Review staged content again before GitHub publication.

The preparation did not restart or edit the live Agent Nexus service. No GitHub repository, push, public release or account action was performed.
