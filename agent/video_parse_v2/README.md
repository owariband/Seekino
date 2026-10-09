# Video Parse V2 Agent Harness

This package coordinates the persisted V2 task state machine. It never imports
Server repositories or Gateway clients. The service injects its runtime object
into [`ParseAgent`](runtime.py) and retains database and model ownership.

## Entry point and module layout

`runtime.py` assembles the stages and builds the Video coordinator Graph.
`state.py` holds the task and execution DTOs consumed by the stages. The stages
call the injected runtime directly. Stage-local state stays alongside its stage.

```text
video_parse_v2/
├── __init__.py             # public ParseAgent export
├── runtime.py              # stage assembly, main Graph and ParseAgent.run
├── input.py                # task inputs and frozen configuration
├── state.py                # task/execution DTOs and main Graph state
├── timelineEvent_canvas.py # Parse facts, evidence and validated local operations
├── errors.py               # parse errors
├── prepare.py              # topology and child-task initialization
├── scheduler.py            # ordered batches and Chunk dispatch
├── prompts.py              # OCR, Understanding, Refine and Prefix prompt builders
├── understanding/
│   ├── __init__.py          # ChunkUnderstandingWorkflow export
│   ├── workflow.py          # Chunk subgraph and execution cleanup
│   └── state.py             # Chunk-local transient state
├── refine/
│   ├── __init__.py          # IntervalRefine export
│   ├── agent.py             # Interval generation, validation and commit
│   └── input.py             # member content/version/scope snapshot
└── prefix/
    ├── __init__.py          # PrefixContextAgent export
    ├── agent.py             # optional background Prefix execution
    ├── context.py           # bound Chunk and prefix context snapshot
    └── tools.py             # shared Canvas tools bound to the current target
```

The public entry point remains `ParseAgent`. The host enters claimed Chunk,
Refine, indexing and Prefix work independently; they are not serialized into
the Video coordinator Graph.

Configuration callers import `ParseInput` directly from
`agent.video_parse_v2.input`. That import stays lightweight and does not load
the Agent runtime or LangGraph. The package keeps `ParseAgent` as its single
public entry point; loading that entry initializes the executable stages.

```python
from agent.video_parse_v2 import ParseAgent

agent = ParseAgent(runtime=runtime)

await agent.run(video_parse_task_id)                    # Video coordinator
await agent.chunk_workflow.run(chunk_parse_task_id)     # claimed Chunk worker
await agent.interval_refine.run(interval_parse_task_id) # claimed Refine worker
await agent.prefix_context_agent.run(chunk_task_id)     # claimed Prefix worker
```

Executing a stage requires an injected runtime. Constructing the coordinator
alone does not execute external operations.

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
  ASR || visual/OCR → Understanding or Engine observation conversion → validation
  → content + initial scope + ready masks + Chunk COMPLETE in one transaction
        ↓
IntervalRefine workers
  independent member snapshot + original evidence → local Canvas corrections
  → check all member versions/fingerprints + validate complete results
  → transactionally save changed content + scope union + INDEXING
        ↓
host indexing scan/notification
  all containing Intervals submitted Refine → current content/version
  → projection/embedding/ES → Interval COMPLETE → Video aggregation
```

Long videos receive only complete sliding windows. A video shorter than the
configured Interval receives one `[0,n)` short window and still passes through
Refine and indexing.

## Runtime and transaction rules

The runtime object supplies the operations actually consumed by each stage.
Synchronous repositories/queues may be adapted by an async service, but model
and media calls run outside row-locking transactions.

Chunk commit gives the host the validated result. The Server repository saves
content, initial scope, every containing Interval's ready mask, and Chunk
`COMPLETE` in one lease-fenced transaction. The DB Queue scans persisted ready
Intervals and rechecks readiness while claiming them; no Agent callback writes
masks or enqueues Refine. Post-commit notifications wake the Video coordinator,
and durable scans continue work even when notifications are lost.

The ASR provider returns seconds; the service converts them once to absolute
integer milliseconds before using the Engine-owned transcript contract. OCR
uses Engine's `OCRResult` and Agent's OCR prompt. Original normalized ASR/OCR
are retained in Chunk detail and exposed as read-only Canvas evidence. The
service prepares media and executes the model. With Understanding
disabled, the workflow directly calls Engine's pure observation-to-event
conversion and applies the same time-range validation before committing.

Refine input includes each member's content, version, bounds, sequence, and
ordered scope, plus original evidence and full content fingerprints. Understanding,
[`ParseContextCanvas`](timelineEvent_canvas.py) in Refine and Prefix uses the same [`ContextCanvas`](../utils/context_canvas.py)
implementation and shared `get_context_canvas` / `write_context_canvas` tools as
Chat. Refine operations add or correct events; omitted events and fields remain
unchanged, and event deletion is unavailable. Each batch validates an independent
draft before publishing it. The program assembles the final timeline from Canvas;
the model returns only a completion receipt.

Commit checks the Interval lease and unsubmitted state, locks member rows in
fixed order, and checks every input member version and fingerprint before any
updates. It validates full results, saves changed content, unions current scope,
and enters `INDEXING` in one transaction. A changed input snapshot triggers fresh
refinement of that window while retaining completed ASR and Understanding work.
Content conflicts are distinct from lease loss and cancellation.

Prefix tools are bound to one current Chunk Canvas. The service validates its
independent lease and input content version/fingerprint before commit; a content
conflict refreshes that Prefix review. A factual update increments content
version and returns already-refined
containing Intervals to `INDEXING`; compact-only and scope-only writes do not.
The Server consumer's bounded background scan claims Prefix work through its
repository. It advances the persisted cursor without Agent readiness callbacks.

Temporary media is owned by the Task/Lease workspace and never enters Graph
state. `ChunkUnderstandingWorkflow.run` always calls runtime cleanup with the
same fenced `ChunkExecution`; orphan cleanup after process death belongs to the
Worker reaper.

The task-state contract is [`design/trd.md`](design/trd.md); the shared Canvas
contract and implementation evidence are in
[`../design/utils_context_canvas.md`](../design/utils_context_canvas.md).
