# T-20 live deployment + real-traffic boundary evidence

T-20 verifies the OpenCode Go `x-opencode-session` implementation is actually
LIVE in the deployed 9Router, not only in the repository tree.

## What ran (2026-09-16)

1. **Offline session suite** (authoritative logic proof):
   `npm --prefix tests test -- unit/opencode-go-session-header.test.js` -> 10/10;
   `unit/opencode-go-models.test.js unit/device-polling.test.js` -> 25/25.
2. **Live deployment** via `apply-update.ps1`:
   - stopped the running instance (PIDs 12680, 21232);
   - created safety backup `%APPDATA%\9router_backup_2026-09-16_190023`
     (pruned to bounded retention);
   - installed `9router-0.5.65-extra.tgz` globally;
   - live DB/providers preserved (state-restore export absent by design);
   - launched and VERIFIED a running instance (pid 18340, `/api/version` 200);
   - exit 0, no success banner before launch verification.
3. **Network-boundary capture** (`t20_boundary_capture.mjs`): a real localhost
   HTTP server records the outbound headers the deployed-source executor emits.
   Two repositories are modeled as two logical conversations.

## Boundary result (PASS)

```
requests: 3   urls: /zen/go/v1/chat/completions (x3)
repoA turn1 session = 424b6188-...-1ef1bb7d01ac
repoA turn2 session = 424b6188-...-1ef1bb7d01ac   (stable, same conversation)
repoB turn1 session = f12e84fa-...-8b26a828f979   (different conversation)
Authorization: Bearer <credential>  (provider-isolated on every request)
repoA_stable: true   repoB_nonempty: true   distinct: true
opaque_uuid: true    provider_isolated: true
```

## What could NOT be proven here

The deployed `opencode-go` provider connection is INACTIVE (`isActive: false`,
no credential in the live DB), so no real upstream request to
`opencode.ai/zen/go` was issued. The boundary capture proves the header the
deployed runtime EMITS; a real upstream round-trip requires the user to
activate the provider connection.

## Re-running

```powershell
node 9router_WatchEdit/tests/live/t20_boundary_capture.mjs <out.json>
```

It reads the deployed source at `%APPDATA%\9router\source` and makes no
external requests (target is 127.0.0.1).
