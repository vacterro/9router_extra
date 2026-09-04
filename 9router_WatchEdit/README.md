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

## 5. Launch & Verification

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
