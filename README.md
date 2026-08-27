# tellerly — Computer-Use Automation System

An LLM discovers how to complete a goal in a legacy back-office UI **once**;
the successful run is compiled into a typed, versioned **capability
artifact**; that artifact **replays deterministically** — no model in the
decision loop — with human-in-the-loop escalation and safety guardrails
throughout.

> The model discovers. The artifact becomes a reusable capability.
> Deterministic replay is how an AI agent invokes it in production.

## What is in here

- **The Tellerly Teller Console** (`target_app/`) — a deliberately legacy,
  fictional credit-union console: server-rendered tables, no test IDs,
  element ids that rotate every render, mixed labelling, the transfer form
  inside an iframe, and nine injectable runtime-failure conditions. Two
  tenant skins of the same product (`ridgeline`, `bluepeak`).
- **Discovery** (`src/tellerly/discovery/`) — a Gemini planner drives the
  console through an observe→decide→act loop; it never sees a selector and
  never sees a secret. Locators are *measured* against the live page at the
  moment of action, and a compiler turns the trace into a capability.
- **The capability artifact** (`src/tellerly/schema/`) — typed inputs with
  sensitivity levels, typed outputs, ranked locator ladders with recorded
  robustness rationale, declared business outcomes, a safety envelope, and a
  success checkpoint. Load-time validators refuse incoherent artifacts.
- **Deterministic replay** (`src/tellerly/replay/`) — executes a saved
  capability with **zero model calls**, structurally enforced. A four-tier
  result contract separates success / business outcomes / recoverable
  conditions / hard failures.
- **Escalation & handoff** (`src/tellerly/kernel/operator.py`) — a stuck
  replay pages a human who takes over the *same live session* through a
  terminal operator console, then hands control back with a decision.
- **Multi-tenant overlays** (`src/tellerly/schema/overlay.py`) — one
  recording serves many institutions; a tenant overlay patches only what
  differs and *cannot express* a dangerous change.
- **The capability API** (`src/tellerly/api.py`) — the agent-facing surface:
  discover capabilities by name, read their typed contracts, invoke them
  over HTTP.
- **Evidence** (`evidence/`) — real, committed runs of everything above,
  indexed in [evidence/README.md](evidence/README.md). The discovery
  evidence is from a **real model run**, not a scripted stand-in.

## 1. Install

Python 3.11+.

```bash
python -m venv .venv
```

Activate it — PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

bash:

```bash
source .venv/bin/activate
```

Then:

```bash
pip install -e ".[discovery,dev]"
```

```bash
playwright install chromium
```

Install editable (`-e`) — the CLI locates the repo (target app, evidence and
capability directories) from the source tree. The `discovery` extra holds
`google-genai` and `playwright`; **replay never needs the model SDK** —
enforced structurally (import-graph test, poisoned-SDK replay, and a result
contract that refuses nonzero model usage — `tests/test_replay_isolation.py`).

## 2. Configure

Copy `.env.example` to `.env` (gitignored). `GOOGLE_API_KEY` (a Google AI
Studio key) is needed **only** for discovery runs; everything else — tests,
replay, escalation, the API — runs without it. The planner model defaults to
`gemini-3.5-flash-lite`.


## 3. Start the target application

```bash
tellerly start-app
```

Open http://127.0.0.1:8000 — any operator ID, access key `demo`. Flow: sign
in → member search (try `101555` or `Whitfield`) → member record →
share-to-share transfer in the action panel → confirmation → receipt.

Every runtime condition the replay engine must handle is reachable on demand:

| Trigger | What happens | Category |
|---|---|---|
| search `99999` | "No records found" | business outcome |
| open member `55555` | NOT AUTHORIZED (403) | hard failure |
| transfer **from** share `S02` (member 101555) | refused — administrative hold | business outcome |
| amount greater than the source balance | refused — insufficient funds | business outcome |
| amount `-1` or `1000000000` | TELLERLY INTERNAL FAULT (500) | hard failure |
| any URL with `?slow=1` | 8-second load | recoverable |
| every 3rd member-record load | maintenance interstitial | recoverable |
| idle past the session TTL (180 s) | session expired, back to sign-in | recoverable |
| re-submitting a posted confirmation | TRANSFER ALREADY PROCESSED | business outcome |

Tune the chaos: `tellerly start-app --interstitial-every 0 --session-ttl 3600`
for a quiet demo. A second tenant skin of the same product:
`tellerly start-app --tenant bluepeak --port 8010`.

## 4. Commands

Commands that set environment variables differ per shell, so both PowerShell
and bash forms are shown; every other command is identical in either shell.

### 4.1 Run the tests

```bash
pytest
```

202 tests, **no API key and no model calls needed** — including full
end-to-end discovery and replay against a real browser with a scripted
planner standing in for the model. See "Testing strategy" below.

### 4.2 Discovery (a real LLM run)

With the target running and `GOOGLE_API_KEY` set — the job's secret input
resolves from the environment, keeping it off the command line. PowerShell:

```powershell
$env:TELLERLY_TARGET_ACCESS_KEY = "demo"; tellerly discover --job jobs/transfer_between_shares.json
```

bash:

```bash
TELLERLY_TARGET_ACCESS_KEY=demo tellerly discover --job jobs/transfer_between_shares.json
```

~16 model calls, ~$0.11 at list price ($0 on the free tier), ~90 seconds.
Saves a versioned capability under `capabilities/` and full evidence
(event log, per-turn screenshots, trace, result) under
`evidence/discovery-<timestamp>/`.

### 4.3 Inspect an artifact

```bash
tellerly capabilities list
```

```bash
tellerly capabilities show transfer_between_shares
```

Add `--json` for the raw artifact. Every locator rung carries its measured
provenance and robustness rationale; the contract table shows typed inputs
(with sensitivity), outputs, declared outcomes, and which steps mutate.

### 4.4 Deterministic replay

Replay the saved capability with **new inputs** — a different member than
was recorded, zero model calls. PowerShell:

```powershell
$env:TELLERLY_TARGET_ACCESS_KEY = "demo"; tellerly replay transfer_between_shares -i operator_id=op-replay -i access_key=env:TELLERLY_TARGET_ACCESS_KEY -i member_id=101556 -i from_share=S00 -i to_share=S01 -i amount=15.00 --approve
```

bash:

```bash
TELLERLY_TARGET_ACCESS_KEY=demo tellerly replay transfer_between_shares -i operator_id=op-replay -i access_key=env:TELLERLY_TARGET_ACCESS_KEY -i member_id=101556 -i from_share=S00 -i to_share=S01 -i amount=15.00 --approve
```

`--approve` authorizes the run's mutating step (the confirm click); without
it the replay stops with `policy_blocked` **before** posting anything.
Exit codes: 0 = SUCCESS (outputs printed), 3 = business outcome ("no such
member" is an answer, not a crash), 6 = hard failure with
step / expected / observed / screenshot evidence.

### 4.5 Error & outcome demonstrations

Vary the inputs — each run ends with a typed, evidenced result:

| Change | Result | Exit |
|---|---|---|
| `member_id=99999` | BUSINESS_OUTCOME `no_such_record` | 3 |
| `from_share=S02` (member 101555) | BUSINESS_OUTCOME `operation_refused` (hold) | 3 |
| `amount=99999.00` | BUSINESS_OUTCOME `insufficient_funds` | 3 |
| `member_id=55555` | HARD_FAILURE `permission_denied` (escalates when enabled) | 6 |
| omit `--approve` | HARD_FAILURE `policy_blocked`, nothing posted | 6 |

Recoverable conditions (the maintenance interstitial) are cleared by the
engine mid-run using the artifact's declared recovery steps — run against a
default server (`--interstitial-every 3`) to watch it happen in the events.

### 4.6 Human escalation & handoff

Add `--escalate` and a stuck run pages you instead of failing: the run
pauses, an intervention request (reason code, step, URL, screenshot,
redacted DOM snapshot, deadline) prints in the terminal, the browser opens
headed (interactive sessions), and an operator console takes over the
**same live session**. PowerShell:

```powershell
$env:TELLERLY_TARGET_ACCESS_KEY = "demo"; tellerly replay transfer_between_shares -i operator_id=op-esc -i access_key=env:TELLERLY_TARGET_ACCESS_KEY -i member_id=55555 -i from_share=S00 -i to_share=S01 -i amount=15.00 --approve --escalate
```

bash:

```bash
TELLERLY_TARGET_ACCESS_KEY=demo tellerly replay transfer_between_shares -i operator_id=op-esc -i access_key=env:TELLERLY_TARGET_ACCESS_KEY -i member_id=55555 -i from_share=S00 -i to_share=S01 -i amount=15.00 --approve --escalate
```

Console commands: `look`, `click/fill/select/press <uid> …`, `goto <path>`,
`note <text>`, then `done continue|retry|skip|abort [note]` — resume is a
decision, not a signal. Every operator action runs through the same Surface
and policy gate as automation and lands in the same evidence; an unanswered
escalation times out into a loud abort.

### 4.7 Multi-tenant: one recording, many institutions

Start the second tenant of the same vendor product (relabelled controls +
an extra mandatory VERIFY screen):

```bash
tellerly start-app --tenant bluepeak --port 8010
```

The base artifact does not fit tenant bluepeak (try the replay above with
`--target http://127.0.0.1:8010` — it fails cleanly). Apply the tenant's
overlay instead. PowerShell:

```powershell
$env:TELLERLY_TARGET_ACCESS_KEY = "demo"; tellerly replay transfer_between_shares --tenant bluepeak -i operator_id=op-tenant -i access_key=env:TELLERLY_TARGET_ACCESS_KEY -i member_id=101556 -i from_share=S00 -i to_share=S01 -i amount=10.00 --approve --target http://127.0.0.1:8010
```

bash:

```bash
TELLERLY_TARGET_ACCESS_KEY=demo tellerly replay transfer_between_shares --tenant bluepeak -i operator_id=op-tenant -i access_key=env:TELLERLY_TARGET_ACCESS_KEY -i member_id=101556 -i from_share=S00 -i to_share=S01 -i amount=10.00 --approve --target http://127.0.0.1:8010
```

The overlay
([capabilities/transfer_between_shares/overlays/bluepeak.json](capabilities/transfer_between_shares/overlays/bluepeak.json))
retargets two relabelled buttons, inserts the verify-screen steps, and
re-binds the tenant host. It is a typed patch list with **no vocabulary**
for removing steps, weakening checkpoints, or relaxing safety; the resolved
capability re-passes every artifact validator, and the deployment policy
still gates the tenant host. The success run's telemetry shows the drift
signal: relabelled controls resolve via the form-`name` fallback rung.

An overlay is reviewed against exactly one base version, so `--tenant`
without `--version` automatically replays that pinned base — even after
you record a newer version (e.g. by re-running discovery), the tenant
command keeps working and prints a note that the overlay is due a
re-review. An explicit `--version` that contradicts the pin still refuses.

### 4.8 The capability API (agent-facing)

Expose the catalog as a small HTTP surface an AI agent can discover and
invoke — capabilities are derived from the artifacts on disk, never
hand-authored:

```bash
tellerly serve-api
```

| Endpoint | Purpose |
|---|---|
| `GET /api/capabilities` | Catalog: ids, versions, typed inputs/outputs, risk, tenants |
| `GET /api/capabilities/<id>` | Full contract incl. a JSON Schema for the inputs |
| `POST /api/capabilities/<id>/invoke` | Execute a replay with typed args; returns the ReplayResult |

Invoke one (a business outcome returns HTTP 200 — it is an *answer*, not a
transport error; mutating capabilities require `"approve_mutations": true`).
PowerShell:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8800/api/capabilities/transfer_between_shares/invoke -ContentType application/json -Body '{"inputs": {"operator_id": "op-api", "access_key": "demo", "member_id": "101556", "from_share": "S00", "to_share": "S01", "amount": "12.00"}, "approve_mutations": true}'
```

bash:

```bash
curl -s -X POST http://127.0.0.1:8800/api/capabilities/transfer_between_shares/invoke -H "Content-Type: application/json" -d '{"inputs": {"operator_id": "op-api", "access_key": "demo", "member_id": "101556", "from_share": "S00", "to_share": "S01", "amount": "12.00"}, "approve_mutations": true}'
```

## 5. Evidence

`evidence/` holds real, committed runs — a genuine model-driven discovery,
replays for every result tier, a live operator escalation, and the
cross-tenant pair — each with a redacted event log, screenshots, and the
typed result. All paths are repo-relative (portable across machines). Index:
[evidence/README.md](evidence/README.md).

## 6. Layout

```
src/tellerly/
  schema/      capability artifact, result contracts, tenant overlays (pure data)
  surface/     the perceive/act seam + the Playwright implementation
  discovery/   LLM planner loop + trace→capability compiler (only package that may import the Gemini SDK)
  replay/      deterministic execution path (structurally unable to reach a model)
  kernel/      guardrails, value-based redaction, evidence, control token, operator console, store
  api.py       the agent-facing capability API
  cli.py       tellerly <command>
target_app/    the mock legacy console, two tenant skins (test double, not part of the product)
jobs/          discovery job specs (the reviewed contract a run works against)
config/        deployment policy + per-app outcome catalogues
capabilities/  compiled artifacts + tenant overlays
tests/         202 tests — see Testing strategy
evidence/      curated run evidence (see evidence/README.md)
```

## 7. Testing strategy

`pytest` runs everything with no API key and no model calls:

- **Target hostility & failure matrix** — smoke tests keep the mock console
  hostile and prove every failure-matrix row reachable
  (`test_target_smoke.py`, `test_target_failure_matrix.py`).
- **Schema coherence** — every load-time refusal has a test constructing the
  violation (`test_schema_artifact.py`, `test_taxonomy_and_results.py`).
- **Scripted end-to-end discovery** — the full engine/recorder/compiler
  pipeline against a real browser with a deterministic scripted planner
  (`test_discovery_scripted.py`).
- **Replay behavior** — the real recorded artifact replayed for every result
  tier, ladder-fallback drift, and the confirmation gate
  (`test_replay_engine.py`).
- **No-model isolation** — the import-graph walk and a full replay with the
  model SDK poisoned (`test_replay_isolation.py`).
- **Escalation** — abort/skip/continue/timeout paths including a scripted
  operator clicking through the same Surface (`test_escalation.py`,
  `test_control.py`).
- **Multi-tenant** — the closed overlay grammar's refusals and the live
  cross-tenant replay with drift telemetry (`test_overlay.py`,
  `test_cross_tenant.py`).
- **The API** — catalog, contracts, and typed invocation over HTTP
  (`test_api.py`).

## 8. Honest limits

The deliberate cuts, with reasoning and next steps, are in
[REPORT.md](REPORT.md) §Cuts. Headlines: one surface implementation
(the seam for accessibility/desktop surfaces is real, the second
implementation is not); the operator console is a terminal, not a web app;
screenshots are captured raw (structured evidence is value-redacted);
overlay authoring is manual; drift is recorded per run but not yet
aggregated fleet-wide.
