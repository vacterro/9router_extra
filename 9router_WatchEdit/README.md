# 9router_WatchEdit

Real-time provider/model health scanner & combo editor control plane for local 9Router installations (`http://127.0.0.1:20128`).

---

## 1. Product Boundary & Core Purpose

`9router_WatchEdit` is **NOT** a replacement for the 9Router routing engine. 9Router remains the authoritative execution and routing engine. WatchEdit acts as its companion **Control Plane**:

```
+-------------------------------------------------------------+
|                      9router_WatchEdit                       |
|  - Real-time provider/model health scanner                   |
|  - Authoritative upstream live discovery                     |
|  - Safe combo ordering & 3-way conflict management           |
|  - Standalone routing presets manager                        |
|  - Fail-closed safety boundary & zero silent SQLite fallback |
+-------------------------------------------------------------+
                               |
               REST API (x-9r-cli-token)
                               v
+-------------------------------------------------------------+
|                           9Router                           |
|  - Routing engine, rate limiting, token refresh              |
|  - Node proxying, provider execution, combo rotations        |
+-------------------------------------------------------------+
```

---

## 2. Safety Boundaries & Concurrency

### Fail-Closed API Boundary
- `update_combo()`, `rename_combo()`, and `delete_combo()` communicate strictly via the official 9Router HTTP API (`PUT /api/combos/{id}`, `DELETE /api/combos/{id}`).
- If the 9Router API fails, the operation **fails closed** and surfaces a clear error dialog to the operator.
- **Zero Automatic SQLite Fallback**: WatchEdit will **NEVER** automatically mutate the active WAL SQLite database while 9Router is running.
- **Offline Recovery Mode**: Direct mutation of `data.sqlite` exists *only* in an explicit manual recovery mode requiring `allow_offline_wal_mutation=True` and strict confirmation that the 9Router server process is offline.

### Optimistic Concurrency & Conflict Detection
- When a combo is selected, WatchEdit tracks its baseline model sequence and timestamp (`baseline_models`, `baseline_updated_at`).
- Before saving changes, WatchEdit re-reads current engine state. If the combo was modified concurrently outside WatchEdit, a **3-Way Conflict Dialog** is presented:
  1. **Baseline**: Models when combo was loaded.
  2. **Server**: Current models on the 9Router engine.
  3. **Local**: Your pending edits.
- Allows the operator to explicitly choose: **Overwrite 9Router**, **Keep 9Router State**, or **Cancel**.
- A passive background timer checks for external modifications and surfaces a notification bar if an external edit occurs during an editing session.

---

## 3. Decoupled Classification & Evidence Streaks

Availability is strictly decoupled from billing cost:

Provider health is also represented as independent dimensions:
- `REACHABILITY`: `REACHABLE` or `UNREACHABLE`.
- `AUTH`: `AUTH_OK`, `AUTH_REJECTED`, or `UNKNOWN`.
- `CATALOG`: `MODELS_AVAILABLE`, `EMPTY_MODEL_CATALOG`, or `DISCOVERY_UNAVAILABLE`.
- `COMPLETION`: the completion probe state (`LIVE`, `WAF_BLOCKED`, `MODEL_INVALID`, etc.).
- `USABLE`: true only when reachability, authentication, a non-empty catalog, and a verified live completion all pass.

An HTTP 200 `/models` response with `data: []` is displayed as `Models API: EMPTY (0 models)`, not as a normal PASS. The provider entry remains available for later discovery, while its models are excluded from active routing until a non-empty catalog is observed.

### Availability States
- `LIVE`: Endpoint responded with valid inference choice or token.
- `PENDING`: Request currently in-flight.
- `AUTH`: Rejection due to invalid API key or permission denied.
- `BALANCE`: Insufficient quota, depleted credit, or billing arrears.
- `RATE_LIMIT`: HTTP 429 or rate limit keyword. Backoff applied.
- `TIMEOUT`: Request exceeded time budget.
- `TEMP_ERROR`: HTTP 500/502/503/504 or network disconnect.
- `ROUTE_ERROR`: HTTP 404 route failure without semantic model failure.
- `MODEL_MISSING`: Upstream explicitly reported `model_not_found` or unsupported entity.
- `DEAD`: Requires $\ge 3$ consecutive verified missing/dead evidences.
- `UNKNOWN`: Not yet tested.

### Cost States
- `FREE`: Verified free-tier provider or model.
- `PAID`: Commercial paid provider or explicit cost override.
- `UNKNOWN`: Unknown cost structure.

### Operator UI Badges
- `LIVE + FREE` $\rightarrow$ `FREE/USE`
- `LIVE + PAID` $\rightarrow$ `PAID`
- `LIVE + UNKNOWN` $\rightarrow$ `USE/?` (Unknown successful models never default to `PAID`)

### Evidence Counters
`ModelHealthRecord` tracks distinct streaks rather than a single counter:
- `consecutive_model_missing`
- `consecutive_timeout`
- `consecutive_route_error`
- `consecutive_auth`
- `consecutive_rate_limit`
Transients (`RATE_LIMIT -> TEMP_ERROR -> 404`) never accumulate toward `DEAD`.

---

## 4. Single-Screen Unified Workspace

```
+------------------------------------+------------------------------------+
|  WATCH VIEW (55% Width)            |  COMBO EDITOR (45% Width)          |
|  - [ALL] [USE] [FREE] [PAID]       |  - Combo Selector + New/Rename/Dup |
|    [ATTENTION] [PENDING] [DEAD]    |  - Revert / Reload / Save buttons  |
|  - Search Filter                   |  - Drag & Drop Ordered Models      |
|  - Real-time Health Table          |  - Available Verified Models List  |
+------------------------------------+------------------------------------+
|  ACTIVITY LOG (60% Width)          |  INSPECTOR PANEL (40% Width)       |
|  - In-flight probe progress        |  - Latency, streaks, HTTP detail   |
|  - Live probe stream               |  - Cost Override + Retest Button   |
+------------------------------------+------------------------------------+
```

- **Instant Startup**: Populates immediately (<100ms) from cached inventory, then triggers non-blocking background discovery.
- **Direct Drag & Drop**: Drag rows directly from WatchView into ComboEditor.
- **Scan Freeze**: Sorting is frozen during active scanning to prevent row jumping.
- **Presets Manager**: Accessible via the top toolbar button for on-demand comparison and application.

---

## 5. FREE Fallback Control Plane (provider-first)

The final main tab (`FREE Fallback`) controls which providers are allowed to
grow the "better than zero" fallback pool. **Providers** are the primary
surface; models are a drilldown. The tab itself performs no I/O: opening,
sorting, filtering and selecting only rearrange local state, and a scan runs
only from an explicit action.

### Scan policy vs. scan mode

They answer different questions and are stored separately:

- **Scan policy** — *when* a provider may be scanned: `ALWAYS`, `STALE_ONLY`
  (refresh only when the metadata is older than the staleness window),
  `MANUAL` (explicit actions only) or `NEVER`.
- **Scan mode** — *what* a scan is allowed to do: `METADATA_ONLY`,
  `METADATA_AND_LIVE_PROBE` or `DISABLED`.

A provider can therefore be "enabled but metadata-only", which is the default
for every provider with a real catalog adapter: discovery is free, inference is
not.

### Metadata discovery vs. live validation

- **Metadata Scan Selected** reads only the adapter's documented catalog or
  local metadata source and executes **zero inference calls**. Proven by
  counter in the tests and enforced in the controller: a metadata result that
  reports an inference call is rejected, not believed.
- **Validate Selected** may execute one bounded official canary, and only when
  the adapter declares a canary path, the provider is selected and enabled,
  trusted-alive is not suppressing it, the policy permits it, and the monetary
  cost class is proven safe or the operator granted the per-provider billing
  consent. `POSSIBLE_BILLING`, `ACCOUNT_CONDITIONAL` and `UNKNOWN` live probes
  are refused by default.
- **Scan Selected** follows each provider's configured policy/mode. If the mode
  requests a live probe that the cost guard refuses, the provider is downgraded
  to a metadata refresh and the refusal is shown — never silently probed.

### Trusted-alive (operator override)

A provider can be marked trusted-alive for 1h / 1d / 7d / 30d or until manually
cleared. Trust suppresses needless live probing while it is valid. It never
fabricates FREE evidence, never overrides a PAID/POSSIBLE_BILLING
classification, never makes an unsupported route routable, and when it expires
the provider simply becomes eligible for live validation again. A positive
authoritative failure (auth revoked, runtime gone) still supersedes it.

### Cost-risk vocabulary

Monetary risk and quota consumption are separate classes on purpose:
`ZERO_MONETARY_METADATA`, `FREE_QUOTA_PROBE`, `ACCOUNT_CONDITIONAL`,
`POSSIBLE_BILLING`, `UNKNOWN`. Metadata cost risk and live-probe cost risk are
stored separately.

Badges are explanatory only ($0 METADATA, FREE QUOTA, ACCOUNT CONDITIONAL,
CLIENT BOUND, POSSIBLE COST, RATE LIMITED, NO FREE ADAPTER); the underlying
policy and evidence fields stay authoritative.

### Strict FREE evidence

Eligibility is classified, never inferred:

- `STRICT_FREE` requires machine-verifiable zero-cost evidence from an
  authoritative source: explicit zero pricing, an official free route
  (`:free`), or an explicit catalogue `is_free` flag.
- `CONDITIONAL_FREE` covers trial, signup/promo credit, coupons and ambiguous
  marketing text. These providers stay **visible** in the table but are never
  auto-synced into the strict FREE tail: credit is not permanent zero cost and
  a coupon can turn into an invoice.
- `UNKNOWN_COST`, `PAID`, `WITHDRAWN` are never auto-added.
- `CLIENT_BOUND_FREE` (for example the OpenCode free tier) reaches SAIFREN only
  through the existing `ocf/*` local-bridge contract, never as a generic
  routable FREE route.

The four dimensions — free evidence, provider health, routing capability and
cost risk — are kept independent. A provider being alive proves no model is
free; a model being free proves nothing about routability; a successful
metadata read proves nothing about inference health; and a failed live probe
never erases authoritative FREE evidence.

### Better-than-zero tail policy

`Sync strict FREE to SAIFREN bottom` appends eligible routes at the very bottom
of SAIFREN and is idempotent:

- existing reliable routes keep their relative order and are never reordered;
- manual entries are preserved verbatim;
- a `STRICT_FREE` route whose health is still `UNKNOWN` stays eligible at the
  absolute tail as long as monetary cost is strictly zero, the routing
  mechanism is supported, it is not positively DEAD/AUTH_FAILED, and its
  timeout/backoff behaviour is bounded;
- `PAID`, `POSSIBLE_BILLING`, `UNKNOWN_COST`, conditional, auth-failed,
  withdrawn and DEAD routes are never auto-added;
- removal happens **only** for scanner-owned entries after a *successful
  authoritative* refresh positively drops their FREE evidence. A provider or
  source outage prunes nothing and last-known-good inventory survives;
- the diff is shown for confirmation before any mutation.

### No implicit mutation

A catalog refresh never rewrites the live combo. Adding or removing SAIFREN
entries is always an explicit operator action, applied through the existing
verified combo-apply path (official API plus read-back verification).

---

## 6. Launch & Verification

### Launch
Run `START_WATCHEDIT.bat` from project root or execute:
```cmd
cd 9router_WatchEdit
python run.py
```

### Run Tests
```cmd
# Default unit suite: fully offline, requires NO localhost 9Router
python -m pytest

# Integration suite: may require the live local 9Router service (localhost:20128)
python -m pytest -m integration
```

### Security Model
The application boots `SECRETS: LOCKED` (safe external-development mode): UI,
presets, cached models and tests work; live provider operations are refused
until you unlock via the header indicator (OS-backed grant or optional
master-password vault). Secrets never live in this repository — see
`../SECURITY_LOCAL.md` for the full model, safe-share building and private
backups.
