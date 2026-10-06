# Video Parse V2 Engine

`engine.parsev2` is the pure domain boundary for V2 parsing. It contains no
database, queue, model-provider, media-path, or Elasticsearch client.

The current public pieces are:

```text
parsev2/
├── schemas.py       formal persons + timeline result schema
├── planning.py      fixed Chunk topology and complete/short Interval windows
└── validation.py    time/member authority, explicit updates, and scope union
```

Search projection lives in
[`engine/rag/indexing/projection.py`](../rag/indexing/projection.py). The
Server reads the current effective Chunk content and version only after all
Intervals containing that Chunk have submitted Refine, then calls that
projection before encoding and Elasticsearch writes.

## Planning contract

`plan_chunks(duration_seconds, chunk_duration)` produces consecutive half-open
integer-second ranges. The final Chunk may be shorter. An existing topology must
exactly match the same plan; retries reuse it.

`interval_size` uses `ceil(interval_duration / chunk_duration)` and is limited
to 63 members by the persisted bitmap. `plan_intervals` follows two rules:

- `n >= size`: every complete sliding window, with starts `0..n-size`;
- `0 < n < size`: one short Interval `[0,n)`.

No extra short tail is added to a long video, and no Interval has a center
Chunk. `IntervalPlan.full_ready_mask` uses the actual member count.

## Result and update validation

`VideoParseResult` owns the formal persons and timeline schema. The host supplies
trusted media bounds; `validate_result_time_range` rejects an event outside that
authority.

Interval and Prefix models do not return a generic patch or a stored snapshot.
They may propose only the current business fields:

```text
ChunkResultUpdate
├── sequence_no
├── persons?          explicit only when changed
└── timeline?         explicit only when changed
```

`validate_interval_refinement` checks fixed membership, consecutive order,
time bounds, and the resulting `VideoParseResult`. It removes fields equal to
the model input snapshot. The transactional host then applies the remaining
explicit fields to the **current database content**:

- an omitted field preserves the current value;
- if two Intervals change the same field, the later committed value wins;
- another Interval changing content or expanding scope does not force the
  already-running model request to repeat;
- an old member snapshot is never written back as a whole object.

`member_versions` identify what the model saw and help diagnose calls. They are
not a global optimistic-lock requirement for Refine. The host still rejects a
lost Interval lease, a duplicate submitted result, and any target outside the
fixed Interval.

## Scope contract

`chunk_parse_content.detail.scope` is an ordered, unique list of Chunk sequence
numbers. Base Understanding initializes it to the member's own sequence.
Refine commits calculate:

```text
ordered_scope_union(current_database_scope, interval_member_sequences)
```

The union is computed from the row locked at commit time, so concurrent windows
cannot remove entries. Scope is prompt context only. It is not an Interval ID,
ready bitmap, Refine completion proof, or indexing gate, and scope-only changes
do not increment content version or trigger indexing.

The separate retrieval `ChunkIndexInput.scope` contains trusted task/content
identity and version. It must not be confused with this sequence list.

## Ownership boundary

Engine validates deterministic domain data. The Agent decides when to call it.
The Server owns leases, fixed-row locking, transactions, current-content
application, status aggregation, indexing readiness, and recovery. Gateway owns
actual model/embedding requests and cancellation.

The active design contract is
[`design/trd.md`](design/trd.md). Historical Canonical/Patch reducer and atomic
publication proposals are not the current implementation contract.
