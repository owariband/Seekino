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
- retry and concurrency policies; lease-fenced model-call accounting;
- credential-free effective model identities and request controls for ASR,
  Understanding/Refine, optional OCR/Prefix/Voiceprint, speaker embedding and retrieval embedding.

Credentials remain in application configuration. Recovery uses the task's model
snapshot rather than current global defaults. A missing explicit Refine model
means reuse the fixed Understanding binding.

Whole-video ASR cues retain their source timestamps across Chunk boundaries.
The Server supplies `ChunkExecution.dialogue_bounds` from the frozen ASR scope
and original video duration; Understanding Prompt, Canvas, result validation and
the independent database commit gate all honor that same DIALOGUE authority.
Visual events stay within the Chunk's visual authority, and dialogue outside the
source video is still invalid. Refinement resolves overlapping complete cues.
Schema and genuine time errors return the full normalized candidate plus exact
locations within the existing retry/deadline budget; a legal cross-Chunk cue
requires no model repair.

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
  independent member snapshot + original evidence → complete members JSON → business result gate
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

Understanding subgraph nodes (`asr`, `visual`, `ocr`, `understanding`) own
bounded retries for timeouts and transport failures, using the frozen execution
retry configuration. Successful upstream observations stay in graph state;
retrying Understanding does not rerun ASR. The execution deadline does not reset.
Understanding keeps the complete model candidate until the Agent validates the canonical
`VideoParseResult`. A schema rejection supplies the original JSON and exact field paths
for up to `max_retry_count` text-only repairs with the same frozen model; images and ASR
are not sent or prepared again. The initial generation and repairs share the original
request deadline. Media bounds, Canvas and transactional commit gates remain strict.

When pictures establish a camera move, the ordinary Understanding prompt asks for a Chinese
description of its direction, pace and starting and ending composition. Refine preserves these details.
Uploaded material routed or explicitly set to `clip` also records the structured observation.
A `SCENE.camera_ref` travels with that event through Refine and publication. For corroborated
horizontal pans the host registers background features across the ordered samples. When the model
splits one verified pan into adjacent visual beats, those beats retain their descriptions and the
editing client gets one continuous camera interval. See the [camera reference contract](../../docs/design/03-product-reassessment/clip-camera-reference-trd.md).

Model requests remain lease-fenced and counted for usage, without a cumulative
per-video call limit. Commit is not automatically replayed.
A final Parse exception stops dispatch for that video, fences sibling writes,
and publishes `PARSE_NODE_FAILED` through task API/SSE. Consumer records the
failure without requeuing the Chunk. Existing process-crash lease recovery and
Index/Refine retry policies remain separate. Native ASR already executing may
finish its blocking call before cancellation takes effect.

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

Direct Refine consumes retained voice evidence and host-prepared speaker labels.
It does not invoke `compare_speaker`, inspect new frames, or start a speaker encoder.
Existing preparation and comparison implementations remain available to their other
callers. Prepared references remain static task evidence; Refine commits are not
rejected merely because a referenced timeline event later changed. Host-owned
`speaker_source` survives unchanged audio ranges and attribution; changed/new
attribution or audio bounds are marked `refine_edit` when provenance is tracked.


The ASR provider returns seconds; the service converts them once to absolute
integer milliseconds before using the Engine-owned transcript contract. OCR
uses Engine's `OCRResult` and Agent's OCR prompt. Original normalized ASR/OCR
are retained in Chunk detail and exposed as read-only Canvas evidence. The
service prepares media and executes the model. With Understanding
disabled, the workflow directly calls Engine's pure observation-to-event
conversion and applies the same time-range validation before committing.

With `models.asr.scope=video`, the host transcribes the original audio once before
publishing episode media, then the same Chunk ASR entry selects overlapping cues.
Cues retain complete text and absolute times as boundary context; write bounds
and TimelineEvent stay unchanged. This mode requires Understanding and an audio
provider. Its cache belongs to the current coordinator lease in the shared
RuntimeService; the Server first validates durable `video_parse_content.asr`
and rebuilds the cache without another transcription when media, producer and schema match.
Historical snapshots default to `chunk`.
Chunk admission requires the current coordinator's preparation receipt, written
only after both transcript and media are ready. Recovery invalidates the old
receipt; ASR preparation errors and expired preparation leases share the finite
Prepare retry budget. New on-demand playback tasks retain Chunk-scoped ASR.

Optional frozen `models.diarization` adds a whole-video preparation stage after ASR.
The host persists its anonymous voice turns independently; either completed stage survives
failure/restart. Both must be ready before Chunk admission. The ASR copy consumed by
Understanding declares `speaker_scope=video`, `speaker_scope_id` and dominant `speaker_ids`;
matching scope and label refer to a consistent candidate voice across chunks. Missing or
mixed dominant turns remain unknown. Anonymous IDs alone do not identify people.
Each Understanding request includes `all_speakers` with the voice scope, complete
voice-group catalog/count and background-provided known speakers. The Series JSON
background may supply `all_speakers` and lecturer/narrator context. An explicit mapping
or a known sole lecturer plus consistent whole-video voice/media can support a name;
a cast list or voice count alone cannot. Reuse supported mappings in Refine;
missing/conflicting identity remains `speaker=null`. Original ASR text and timestamps
stay unchanged; the combination does not imply acoustic forced alignment.

Refine input includes each member's content, version, bounds, sequence, and
ordered scope, plus original evidence and full content fingerprints. Understanding
receives ASR/OCR and background directly and returns one `VideoParseResult` JSON
object without tools. The host validates the complete result and imports persons
and all events in one atomic `ParseContextCanvas.write` batch before the existing
lease-fenced commit. Full ASR word alignment remains in immutable evidence; the
initial prompt includes cue text, times and anonymous speaker IDs.
Refine receives complete member contents and retained observations once and returns
`IntervalRefinementResult` (`members`, each with `sequence_no`, `persons`, `timeline`).
Its voiceprint input contains only the existing per-dialogue prefill summaries bound
by Canvas to that member's event position and audio interval: original speaker,
ranked candidate scores/counts, band and conflict information. Full reference samples,
source details and reference ID lists stay in host evidence. The Prompt projection
does not recompute scores or change prefill rules; see the
[input boundary and measurements](../../docs/design/03-product-reassessment/refine-voiceprint-prefill-context-trd.md).
In `prefill` mode, both ACCEPT and CONFLICT use the top candidate as a reviewable
draft speaker, unless a prior `refine_edit` already owns the identity. The summary
keeps the original Understanding name and top three scores, plus the original
name's scored candidate when it falls outside the top three. NO_VOICE and empty
candidates do not prefill; flag mode only supplies evidence. Refine can retain,
correct or clear the draft identity; see the
[conflict prefill contract](../../docs/design/03-product-reassessment/voiceprint-conflict-prefill-trd.md).
It has no model-facing tools or separate completion receipt. The business result
gate validates the domain Schema, exact member set and time authority, then derives
only actual changes. A Refine-only response transport retains the full JSON value
while requesting the same canonical model schema. The host stably sorts integer
timeline starts, then strictly validates; no other event values are repaired.
Schema/member errors return the entire latest rejected response and matching field
locations for correction. Invalid JSON syntax retains the raw text. Previous failed
responses are replaced rather than accumulated. The existing finite retry policy
and a shared deadline still apply. Network/control/lease failures
do not become Schema repair instructions. Snapshot conflicts refresh only Refine and
do not reset that deadline. No result is committed before all checks pass.

The host subscribes before loading the snapshot. After a successful transaction,
only overlapping executions receive actual member content-version changes; scope-only
and no-op commits do not interrupt generation. The host cancels only the current
request, waits for transport/slot cleanup, and then signals `RefineInputChanged`.
A complete response wins a simultaneous update and can still pass the existing merge.
On refresh the Agent retains its latest complete candidate and precise validation or
merge error alongside the newly loaded snapshot. It neither accumulates old candidates
nor resumes hidden model reasoning. All generations share the original deadline.
Notifications are process-local optimization; durable versions and leases still fence
commits after restarts, missed notifications, Prefix or external-process writes.

[`ParseContextCanvas`](timelineEvent_canvas.py) remains an internal Refine snapshot
for prefill/provenance and diff calculation. Prefix still uses shared
[`ContextCanvas`](../utils/context_canvas.py) tools. Complete Refine output may remove
duplicate/unsupported events; missing or repeated member results are rejected.

With frozen ASR `scope=video`, complete DIALOGUE may cross nominal Chunk/Interval
bounds, but must stay inside the source video. Visual events retain their existing
authorized range. Refine and Prefix input, draft and final transaction validation
share this rule. The current Understanding prompt and initial Chunk commit retain
their existing boundary contract; this change does not deploy the slower experimental
Understanding prompt. Full ASR observations remain immutable.

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
