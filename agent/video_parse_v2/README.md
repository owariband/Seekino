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
├── runtime.py              # stage assembly, main Graph and InvokeVideoParse
├── input.py                # task inputs and frozen configuration
├── state.py                # task/execution DTOs and main Graph state
├── timelineEvent_canvas.py # Parse facts, evidence and validated local operations
├── errors.py               # parse errors
├── prepare.py              # topology and child-task initialization
├── scheduler.py            # ordered batches and Chunk dispatch
├── prompts.py              # OCR, Understanding, Refine and Prefix prompt builders
├── voiceprint.py            # InvokeVoiceprint and host-bound preparation/comparison tools
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

await agent.InvokeVideoParse(video_parse_task_id)                      # Video coordinator
await agent.chunk_workflow.InvokeUnderstanding(chunk_parse_task_id)    # claimed Chunk worker
await agent.interval_refine.InvokeRefine(interval_parse_task_id)        # claimed Refine worker
await agent.prefix_context_agent.InvokePrefix(chunk_task_id)           # claimed Prefix worker
```

Server-activated Agent business entries use `InvokeXxx`. Internal Graph nodes
such as Prepare and ChunkScheduler keep their existing `run` names. Voiceprint
uses `agent.voiceprint_agent.InvokeVoiceprint(task_id, parent_lease_token)` inside
the coordinator; it has no independent Worker or lease.

Executing a stage requires an injected runtime. Constructing the coordinator
alone does not execute external operations.

## Persisted recovery

`ParseAgent.InvokeVideoParse` first loads `VideoRunSnapshot` and routes from its durable
status:

- `PREPARING`: plan/reuse topology, ensure children, then schedule Chunks;
- `PARSING`: resume Chunk scheduling, optional voice preparation, and required Refine/index work;
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
  Understanding/Refine, optional OCR/Prefix/Voiceprint, speaker embedding and retrieval embedding.

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
VoiceprintAgent (optional, within the existing Video coordinator)
  ceil(chunk_count × 3/4) Understanding commits → freeze all currently committed sources/speakers
  → reference registration → finish Tool validates >50% speaker coverage (empty list permits zero)
  → transaction commits detail.voiceprint.ready=true; remaining Understanding continues
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

When voice preparation is enabled, the atomic Refine claim additionally requires
`detail.voiceprint.ready=true`. Until then it remains WAITING without occupying a
Refine slot or spending its retry budget. Queue batching still opens at 90% base
Understanding completion; the three-quarter threshold only advances the coordinator.

Voice preparation is disabled when `ParseConfig.voiceprint` is absent or null.
Enabling it requires `voiceprint={max_tool_calls, execution_timeout_seconds}`,
`models.voiceprint` (a frozen generative binding), and `models.speaker_embedding`
(`source`, `model`, `request_timeout_seconds`). The speaker binding uses Gateway's
`speaker_embedding` capability and CPU CAM++; its external source/checkpoint and
optional dependencies must already be deployed. Historical tasks retain the old path.

The formal result is `video_parse_task.detail.voiceprint={ready, voiceprintList}`.
Each character has `speaker`, actual `model={name, encoder_revision}`,
per-segment `references`, and optional attribution `confidence`. Engine owns this
Schema; Server persists it, and Gateway computes the vectors. No ES voice index or
cross-video/series sharing is introduced. Reference metadata binds original-video
milliseconds, Chunk/event identity, content version, PCM digest and selection basis.
Partial registration stays unready; each new attempt discards unpublished references.

The five preparation Tools are `get_speaker_list`, readonly `get_context_canvas`,
`register_voiceprint`, `unregister_voiceprint`, and `finish_voiceprint_preparation`.
Only the host resolves media paths, extracts temporary PCM and computes vectors. A text completion alone
cannot make preparation ready. `get_speaker_list` returns each speaker's `refs_count`,
`target_refs_count` and registered `references=[{source_ref,start_ms,end_ms}]` metadata;
vectors are not exposed to the Agent. The default collection target is
six independent references per speaker, defined once by
`server/constants/parse.py::VOICEPRINT_REFS_PER_SPEAKER` and returned by the Tools.
Additional reliable independent references are allowed within the existing tool and
time budgets. Prefer reliable, clean speech with varied scenes, wording and speaking
states; six is a collection goal, not a registration limit, quality guarantee or
required number of emotions.
Before registering, inspect each speaker's available chunks and their sequence numbers.
Sample temporally separated early, middle and late available chunks, preferring one
reliable utterance per distinct chunk before adding more from the same chunk. A speaker
need not occur in every region; reliability takes priority over temporal coverage or count.

Repeated `register_voiceprint` calls append references for the same speaker. Its
receipt returns `added`, counts and source metadata. Exact repeats are idempotent;
another subrange of an already registered source and any overlap with any speaker's
registered audio are declined before embedding and rechecked on commit. Explicit
`replace_source_ref` replaces one reference, including a more precise subrange of
that same source, only after the new embedding succeeds and the transaction rechecks
the remaining references. `unregister_voiceprint(source_ref)` removes one reference
idempotently; neither operation is allowed after ready. Half-open
intervals may touch. Identical PCM from a different source is also declined at the
final registration check, after extraction provides its existing digest.
Tool `message`/`retry_hint` directs the Agent to select a different
independent, non-overlapping utterance; these declines consume Tool budget but do
not count as preparation execution failures.

Before ready, finishing reads the current references in the same transaction and
checks each same-speaker, same-model/revision group for low acoustic agreement.
For each reference, `candidate_mean` is its mean cosine against all other references
in that group, excluding itself. Experimental defaults require at least five
independent references and flag `candidate_mean <= 0.50`; the sole threshold source
is `server/constants/parse.py`. Core agreement and gap are not completion conditions.
A low mean returns `ready=false`, `suspect_references` with source/time and
`candidate_mean`, plus guidance to replace or explicitly remove
the reference. This requests review and does not prove a wrong character label.
No reference is silently deleted or relabelled, and no group must lose its
lowest scorer. Smaller groups do not block on this check; scores are neither
probabilities nor independently calibrated identity accuracy. No new model call,
runtime cache or persistent reference copy is needed.

Finishing still requires valid references for strictly more than half the fixed
speaker list. Every speaker below its reference target, including zero references,
needs a nonblank `skip_reasons` explanation. Missing reasons return `ready=false`,
`missing_reason_speakers`, `reference_counts`, the target and a message. With reasons
and majority coverage, fewer than six references remains normal, including after
explicit removal when no reliable replacement exists. Reasons cannot bypass an outlier that remains
registered. An empty list can finish
with zero references. The Agent should finish before exhausting its finite budget.
Actual model, media and database failures retain the existing failure path. No waiting
for later chunks, background supplementation or aggregation change is introduced.

Parent runtime stores only attempt sources, lease
fence and finite failure/recovery facts; normal cancellation and waiting are free.
Preparation failures/timeouts and active-phase crashes use the existing failure
closure; committed preparation is skipped after restart, even if notification was lost.

Refine can call `compare_speaker(start_ms,end_ms)` for null or conflicting named
speakers. The host excludes self/overlap, repeated PCM and incompatible encoders,
then returns cosine candidates, margin and provenance.
Cosine is not a probability. Refine uses its existing Canvas to change speaker or
retain null. Prepared speaker labels and registered audio ranges are trusted static
results for this task. Refine never revalidates them against later timeline edits
or rejects a commit because a reference event changed; comparison references are
retained only for diagnostics.

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
state. `ChunkUnderstandingWorkflow.InvokeUnderstanding` always calls runtime cleanup with the
same fenced `ChunkExecution`; orphan cleanup after process death belongs to the
Worker reaper.

The task-state contract is [`design/trd.md`](../../docs/design/02-agent-harness/video-parse-harness-trd.md); the shared Canvas
contract and implementation evidence are in
[`../design/utils_context_canvas.md`](../../docs/design/02-agent-harness/context-canvas-design.md).
