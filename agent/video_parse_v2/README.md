# Video Parse V2 Agent Harness

This package coordinates the persisted V2 task state machine. It never imports
Server repositories or Gateway clients. The host implements
[`VideoParseRuntime`](runtime.py), injects it into [`ParseAgent`](agent.py), and
retains database and model ownership.

```python
agent = ParseAgent(runtime=runtime)

await agent.run(video_parse_task_id)                    # Video coordinator
await agent.chunk_workflow.run(chunk_parse_task_id)     # claimed Chunk worker
await agent.interval_refine.run(interval_parse_task_id) # claimed Refine worker
await agent.prefix_context_agent.run(chunk_task_id)     # claimed Prefix worker
```

The default missing runtime fails at the first external operation. It never
reports a fake success.

## Persisted recovery

`ParseAgent.run` first loads `VideoRunSnapshot` and routes from its durable
status:

- `PREPARING`: plan/reuse topology, ensure children, then schedule Chunks;
- `PARSING`: resume Chunk scheduling and wait for required Refine/index work;
- `INDEXING`: resume aggregation/index work without Prepare, ASR,
  Understanding, or Refine;
- `COMPLETE`: return immediately.

Failed children are restored by the Repository/Queue before execution. Chunk
`FAILED`, Interval `PARSING_FAILED`, and Interval `INDEXING_FAILED` retain their
separate recovery points. Prefix is independent and never blocks Video
completion.

Every claimed execution carries an immutable `LeaseToken(owner, version,
expires_at)`. It is present on `VideoRunSnapshot`, `ChunkExecution`,
`RefineInput`, and `PrefixContext`. Runtime commit and cleanup methods must fence
on that exact token; rereading a task ID and accepting its newer lease would let
an expired Worker submit as another attempt.

## Fixed configuration

`ParseConfig` is immutable, rejects unknown fields, and is stored with the task.
It includes:

- Chunk/Interval duration, batch size, and dispatch interval;
- visual input mode and bounds, OCR/Understanding/Prefix switches;
- retry, concurrency, and durable model-call budgets;
- credential-free effective model identities and request controls for ASR,
  Understanding/Refine, optional OCR/Prefix, and embedding.

Credentials remain in application configuration. Recovery uses the task's model
snapshot rather than current global defaults. A missing explicit Refine model
means reuse the fixed Understanding binding.

## Main execution

```text
VideoParsePrepare
  fixed Chunk plan + complete/short Interval plan
        ↓
ChunkScheduler
  ordered batches; next batch opens at 90% base COMPLETE
        ↓
ChunkUnderstandingWorkflow workers
  ASR || visual/OCR → Understanding → Engine validation
  → content + initial scope + ready masks + Chunk COMPLETE in one transaction
        ↓
IntervalRefine workers
  current member content + member scopes → explicit persons/timeline updates
  → apply actual fields to current locked rows + scope union + INDEXING
        ↓
host indexing scan/notification
  all containing Intervals submitted Refine → current content/version
  → projection/embedding/ES → Interval COMPLETE → Video aggregation
```

Long videos receive only complete sliding windows. A video shorter than the
configured Interval receives one `[0,n)` short window and still passes through
Refine and indexing.

## Runtime and transaction rules

[`runtime.py`](runtime.py) contains only domain DTOs and host operations.
Synchronous repositories/queues may be adapted by an async service, but model
and media calls run outside row-locking transactions.

Chunk commit receives `listener.update_interval_mask` and must invoke it inside
the same transaction as content and `COMPLETE`. Notifications occur after
commit and can be duplicated or lost; durable scans use the same readiness
checks as compensation.

Refine input includes each member's content, version, bounds, sequence, and
ordered scope. Engine returns only fields actually changed relative to that
input. Commit checks the Interval lease and unsubmitted state, locks member rows
in fixed order, applies those explicit fields to current content, unions current
scope, and enters `INDEXING`. A concurrent content/version change alone does not
rerun the model.

Prefix tools are bound to the one current Chunk and only produce a proposal.
The host validates its independent lease and target content version before
commit. A factual update increments content version and returns already-refined
containing Intervals to `INDEXING`; compact-only and scope-only writes do not.

Temporary media is owned by the Task/Lease workspace and never enters Graph
state. `ChunkUnderstandingWorkflow.run` always calls runtime cleanup with the
same fenced `ChunkExecution`; orphan cleanup after process death belongs to the
Worker reaper.

The active design contract is [`design/trd.md`](design/trd.md).
