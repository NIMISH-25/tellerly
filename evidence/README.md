# Evidence index

One directory per run: `events.jsonl` (structured, redacted event log),
`result.json` (the typed result contract), per-turn or failure screenshots,
and for discovery a `trace.json`. All paths inside are repo-relative. Every
run below executed against the live mock console via the real CLI.

| Run | Scenario | Result | Model calls | Cost (list) | Wall |
|---|---|---|---|---|---|
| `discovery-20260827T205204Z-807b14` | LLM discovery of the transfer flow (gemini-3.5-flash-lite) | GOAL_MET → compiled `capabilities/transfer_between_shares/v1.0.0.json` | 15 | $0.108 ($0 free tier) | 81.5s |
| `replay-20260827T205329Z-150db5` | Deterministic replay, **different member** than recorded (101556, $15 S00→S01) | SUCCESS, `confirmation_no` captured | 0 | $0.00 | 3.3s |
| `replay-20260827T205336Z-e681e7` | Replay with unknown member `99999` | BUSINESS_OUTCOME `no_such_record` (exit 3) | 0 | $0.00 | 2.1s |
| `replay-20260827T205344Z-e6c7d6` | Replay against restricted member `55555`, no escalation handler | HARD_FAILURE `permission_denied` + screenshots (exit 6) | 0 | $0.00 | 2.1s |
| `replay-20260827T205500Z-bbda2c` | Same restricted member with `--escalate`: intervention raised, piped operator session (`look`, `done abort <note>`) | HARD_FAILURE `aborted_by_operator`, 1 InterventionRecord with the operator's note, intervention screenshot + redacted DOM snapshot, full control-token history in events | 0 | $0.00 | 3.1s |
| `replay-20260827T223507Z-f8a7e7` | The **base** artifact pointed at tenant bluepeak's instance (port 8010) | HARD_FAILURE `policy_blocked` — the artifact's own host binding refuses; the base does not fit this tenant (exit 6). No screenshots: the run was refused before any navigation, so there was nothing to capture | 0 | $0.00 | <1s |
| `replay-20260827T214803Z-0716f3` | Same command with `--tenant bluepeak`: overlay resolved (retargets + inserted VERIFY steps + host re-bind) | SUCCESS through the extra screen, `confirmation_no` captured; relabelled controls resolved via fallback rungs (the drift signal) | 0 | $0.00 | 3.7s |
| `replay-20260827T221943Z-f7be0e` | **Invoked via the capability API**: `POST /api/capabilities/transfer_between_shares/invoke` with typed args and `approve_mutations: true` | SUCCESS, HTTP 200 carrying the full ReplayResult (`confirmation_no` TL-004224) — an AI agent discovering and calling a capability by name | 0 | $0.00 | 4.0s |

The saved example artifact the replays execute is
[`capabilities/transfer_between_shares/v1.0.0.json`](../capabilities/transfer_between_shares/v1.0.0.json)
— every locator rung carries its measured-unique provenance and robustness
rationale in its `note`.
