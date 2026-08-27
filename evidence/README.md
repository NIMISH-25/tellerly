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

The saved example artifact the replays execute is
[`capabilities/transfer_between_shares/v1.0.0.json`](../capabilities/transfer_between_shares/v1.0.0.json)
— every locator rung carries its measured-unique provenance and robustness
rationale in its `note`.
