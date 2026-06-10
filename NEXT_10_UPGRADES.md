# Next 10 Upgrades

Implemented:

1. Runtime DB files ignored and removed from package.
2. Gmail OAuth local flow and server callback flow.
3. Chrome DevTools navigation/evaluate/DOM/click/type/screenshot actions.
4. Computer screenshot saves to workspace file by default; base64 is opt-in.
5. Vision grounding tool backed by ModelRouter task=vision.
6. Pull request creation tool; no merge and no auto-merge.
7. Remote approval UI API: create/list/approve/deny approvals.
8. Unit tests for token budget, Gmail OAuth path handling, webhook signatures.
9. Provider live connectivity validation command.
10. Channel webhook signature helper tests.

Commands:

```bash
python3 -m compileall omnidesk_agent
pytest
omnidesk validate-models-live --config examples/config.yaml
omnidesk validate-webhook-signatures --config examples/config.yaml
omnidesk gmail-auth --config examples/config.yaml
```
