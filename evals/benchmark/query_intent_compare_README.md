# Query intent routing comparison

`query_intent_compare.py` runs the three Chat intent paths on the same local
Supervisor, tools, history and series data:

| arm | query path |
| --- | --- |
| `deepseek_decider` | combined DeepSeek decision (`models.query_hook`) |
| `jev_rewriter` | JEV route, then `models.query_rewriter` when needed |
| `supervisor_direct` | no hook; the current Supervisor decides |

The parent supplies `--cases` and `--configs-dir`; the directory must contain
`deepseek_decider.yaml`, `jev_rewriter.yaml` and `supervisor_direct.yaml`. The
harness never reads `.env` and never prints raw configs or credentials.

```bash
# Offline: schedule, case hashes, config hashes and selected policy metadata.
python -m evals.benchmark.query_intent_compare \
  --cases cases.json --configs-dir configs --output outputs/run --plan

# Live: one fresh conversation per arm/step, real local services.
python -m evals.benchmark.query_intent_compare \
  --cases cases.json --configs-dir configs --output outputs/run --live

# Resume one frozen step, or cancel the conversation owned by that step.
python -m evals.benchmark.query_intent_compare \
  --cases cases.json --configs-dir configs --output outputs/run --only-index 4
```

`--live` freezes `protocol.json` first, then runs each step in a child process.
Existing `result.json` files are skipped. Round one covers every case and
rotates arm order by case; round two covers `repeat_case_ids` in reversed case
order with a different rotation. The child enforces a 300 second turn cutoff and
the parent has a separate watchdog. A timed-out child only cancels its own
conversation via `active.json`; global services are never killed.

## Files per step

- `input.json`: exact request and inserted fixture history.
- `model_inputs.jsonl`: Supervisor prompt/hash, history and tool schemas.
- `tools.jsonl`: tool name, arguments, result text and timestamps.
- `hook.jsonl`: hook input, actual returned message, elapsed time and whether it
  changed the query (to identify fail-open).
- `usage.jsonl`: one row per provider call (deduplicated by `call_id`).
- `events.jsonl`: event type/sequence/receipt monotonic seconds; reasoning text
  is not retained.
- `final.json`: the committed user/assistant messages.
- `result.json`: compact observation with FTT, TT, timeout/error status, hook
  summary, tool/model-input summaries, usage totals and persistence check.
- `runtime.log`, `failure.json`, `cancellation.json` when applicable.

FTT starts immediately before `start_message` and stops at the first nonempty
`answer.delta` or `answer.snapshot` content. Reasoning, status and empty tool
snapshots do not count. `text_stream_before_model_completed` and the full
`text_stream_flags` list retain the provider `beforeModelCompleted` flag.
`tt_seconds` is measured after the execution future commits the assistant
message; timeout failures keep `timeout: true` and no fabricated latency.
