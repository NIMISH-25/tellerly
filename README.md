# tellerly — Computer-Use Automation System

An LLM discovers how to complete a goal in a legacy back-office UI once; the
successful run is compiled into a **typed, versioned capability artifact**;
that artifact **replays deterministically** — no model in the decision loop —
with human-in-the-loop escalation and safety guardrails throughout.

> The model discovers. The artifact becomes a reusable capability.
> Deterministic replay is how an AI agent invokes it in production.


## Setup

Python 3.11+.

```bash
python -m venv .venv
```

Activate it (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate`
elsewhere), then:

```bash
pip install -e ".[discovery,dev]"
```

```bash
playwright install chromium
```

Install editable (`-e`) — the CLI locates the repo (the target app, the
evidence and capability directories) from the source tree; a plain install
falls back to the current working directory.

The `discovery` extra holds `google-genai` and `playwright`. **Replay must
never need them** — that will be enforced structurally when the replay engine
lands (import-graph test, `llm_calls=0` assertion, stubbed-SDK replay); the
mechanism is specified in `src/tellerly/replay/__init__.py`.

Copy `.env.example` to `.env`. `GOOGLE_API_KEY` (a Google AI Studio key) is
only needed for discovery runs; everything currently in the repo runs without
it. The planner model defaults to `gemini-3.5-flash-lite` — 500 requests/day
and 15/min on the free tier, where full Flash models allow only ~20/day and
5/min (a single discovery run is ~16 calls). Override via
`TELLERLY_GEMINI_MODEL`; pass `--throttle 12` if you pick a full Flash model.

## The mock target: Tellerly Teller Console

A deliberately legacy, fictional credit-union console — the stand-in for the
real environment: server-rendered tables, **no test IDs**, element ids that
**rotate on every render**, mixed labelling, and the transfer form inside an
**iframe** (frame traversal required).

```bash
tellerly start-app
```

Then open http://127.0.0.1:8000 — any operator ID, access key `demo`.
Flow: sign in → member search (try `101555` or `Whitfield`) → member record →
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
for a quiet demo, or `python -m target_app --help` for the same flags.

## Demo path

Run the tests (they include a full end-to-end discovery against a live
browser with a scripted planner — no API key needed):

```bash
pytest
```

Start the target in one terminal:

```bash
tellerly start-app
```

Run a real LLM discovery in another (needs `GOOGLE_API_KEY` in `.env`; the
job's secret input resolves from the environment — for the mock console the
access key is `demo`; the default `--throttle 4` fits the free-tier rate
limit). PowerShell:

```powershell
$env:TELLERLY_TARGET_ACCESS_KEY = "demo"; tellerly discover --job jobs/transfer_between_shares.json
```

bash:

```bash
TELLERLY_TARGET_ACCESS_KEY=demo tellerly discover --job jobs/transfer_between_shares.json
```

The run saves a capability under `capabilities/` and full evidence
(structured event log, per-turn screenshots, trace, result) under
`evidence/discovery-<timestamp>/`. Inspect the catalog:

```bash
tellerly capabilities list
```

```bash
tellerly capabilities show transfer_between_shares
```

**Deterministically replay** the saved capability with new inputs — zero
model calls, a different member than was recorded (PowerShell shown; same
`env:` convention keeps the secret off the command line):

```powershell
$env:TELLERLY_TARGET_ACCESS_KEY = "demo"; tellerly replay transfer_between_shares -i operator_id=op-replay -i access_key=env:TELLERLY_TARGET_ACCESS_KEY -i member_id=101556 -i from_share=S00 -i to_share=S01 -i amount=15.00 --approve
```

`--approve` authorizes the run's mutating step (the confirm click); without
it the replay stops with `policy_blocked` *before* posting anything —
`require_confirmation` ships on. Exit codes: 0 = SUCCESS (outputs printed),
3 = a business outcome ("no such member" is an answer, not a crash),
6 = hard failure with step / expected / observed / screenshot evidence.

See the taxonomy in action by varying inputs — each ends the run with a
typed, evidenced result:

| Change | Result | Exit |
|---|---|---|
| `member_id=99999` | BUSINESS_OUTCOME `no_such_record` | 3 |
| `from_share=S02` (member 101555) | BUSINESS_OUTCOME `operation_refused` (hold) | 3 |
| `amount=99999.00` | BUSINESS_OUTCOME `insufficient_funds` | 3 |
| `member_id=55555` | HARD_FAILURE `permission_denied` (+ escalation-seam note) | 6 |
| omit `--approve` | HARD_FAILURE `policy_blocked`, nothing posted | 6 |

### What discovery looks like under the hood

The Gemini planner is never shown markup — it sees visible text plus controls
described as human-meaningful facts (`c3: [textbox] label="Amount (USD):"`),
referenced by ephemeral uids. It enters values as `{{input.name}}`
placeholders, so sensitive values never reach the model **and** the recorded
steps are parameterized by construction. At the moment each action succeeds,
every plausible locator (role → label → form-`name` → text → anchored → CSS;
element ids are unrepresentable) is probed against the live page and only
locators matching exactly one element survive into the artifact. The compiler
then attaches the job's typed contract and the app's outcome catalogue,
derives the safety envelope from what the run actually did, and refuses to
compile a run with no held checkpoint.

## Layout

```
src/tellerly/
  schema/      capability artifact + result contracts (pure data; no browser, no model)
  surface/     the perceive/act seam; Playwright-over-CDP impl lands here
  discovery/   LLM planner loop + trace→capability compiler (only package that may import the Gemini SDK)
  replay/      deterministic execution path (structurally unable to reach a model)
  kernel/      guardrails, value-based redaction, evidence, session-control token
  cli.py       tellerly <command>
target_app/    the mock legacy console (test double, not part of the product)
tests/         smoke tests for target hostility + one test per failure-matrix row
evidence/      discovery & replay run evidence (curated, committed)
```
