# Chat Performance and Algorithm Plan

Date: 2026-10-18. Scope: Chat end-to-end latency, the Chat agent architecture, speaker attribution accuracy, and new query types (multimodal input, broad questions). Measurements come from the local stack (series `mygo-ep04-speaker-review-20261016`), the API log and `model_usage` rows. They describe this machine and this data, not a production benchmark.

## 1. Summary

| Problem | Root cause | Direction |
| --- | --- | --- |
| A simple quote takes 100–160 s | Tools take ~2 s in total. The remaining time is ~25–30 model round trips through four nested agents, three of which review or rewrite the same answer | Keep one ReAct loop (retrieval). Make review and answer writing a single pass. Move verification to deterministic host checks |
| Speaker ID is unreliable | Short lines and close voice matches; ASR merges two speakers into one line; the agent decides whether to compare voices at all | Score every line deterministically and accept by margin. Add text and visual priors. Leave the uncertain tail to the agent or to `null` |
| Unknown accuracy | No labeled set | ~150 labeled ep04 lines. Report accuracy against how many lines are left unlabelled |
| New query types | Every type would add loops to an already slow pipeline. "Funny" etc. are not in the indexed text | Fast path first. Visual retrieval is a deterministic tool; broad questions use parse-time moment tags. One small test set per type |

## 2. QA latency

### 2.1 What the trace shows

Message `0ee62820-c63d-41aa-88c2-178a9b900360` ("我觉得也不是你的错吧 —— 这个出自于哪里？"), 161 s total:

| Stage label | Seconds | What happened |
| --- | ---: | --- |
| QUEUED | 10 | The Supervisor's first model call (deciding to call `search_video_graph`) |
| RETRIEVING | 45 | Query: first call 10 s, `search_video_events` 2 s (correct line at #1), then 6 Canvas reads, 2 frame inspections, draft write |
| EVALUATING | 32 | Evaluator: 5 Canvas reads, 2 frame inspections. **Rejected a correct draft** because the clip started at 22.1 s instead of 17.7 s |
| RETRIEVING + EVALUATING | 38 | A second full loop, only to move the clip start |
| RESPONDING | 36 | The inner Chat agent writes an answer (~23 s), then the Supervisor rereads the Canvas and writes its own answer (~13 s) |

Total tool time was about 2 s. The rest is model latency (Codex app-server, `gpt-5.6-sol`, ~3–5 s per round trip).

The waste falls into five patterns:

1. **The inner Chat render is thrown away.** Under the Supervisor, only `answer_preview` reaches the Supervisor; it writes its own answer (`agent/chat/supervisor.py` says so explicitly).
2. **Three reviewers.** The Evaluator, Chat and the Supervisor each re-check the same evidence. The Evaluator can send back a draft for issues the Supervisor is allowed to fix anyway (playback range, wording).
3. **Progressive disclosure costs round trips.** Each agent pages the Canvas (catalog → overview → section ranges), which takes 3–4 model calls before it can decide anything. This saves tokens, but we pay for it in latency.
4. **Unneeded frames.** Pure dialogue questions trigger `inspect_video_frames`, and stills cannot prove who said a line anyway.
5. **A tool that is bound to fail.** The Supervisor calls `understand_current_video` without a playback position (also seen in `engine/design/event-retrieval-eval-20261018/README.md`).

### 2.2 Changes already made (uncommitted, needs an API restart)

| # | Change | Files | Verified |
| --- | --- | --- | --- |
| 1 | Under the Supervisor, skip the inner Chat stage and pass the approved Query draft through | `agent/chat/video_search/runtime.py`, `state.py`, `supervisor.py` | Unit tests. zh quote **147 s → 100 s** (RESPONDING 49 s → 19 s), one run |
| 2 | The Evaluator is told that the Supervisor owns final ranges. Range/wording issues return `complete` with the suggestion in `advice`; `continue` only for missing or unsupported evidence | `evaluator.py` | Unit tests only |
| 3 | The Evaluator prompt preloads each draft item's `draft`, `evidence.transcript` and `evidence.events` (4 items, 8 000 chars per section, truncation marked) | `evaluator.py` | Unit tests only |
| 4 | `search_video_graph` returns `approved_draft` and `review_advice` inline | `supervisor.py` | Unit tests only |
| 5 | `understand_current_video` is only offered when `extra_context.playback` exists | `supervisor.py` | Unit tests only |
| 6 | Query and Evaluator skills: no frame inspection for dialogue, quote or speaker claims | `skills/query/SKILL.md`, `skills/evaluator/SKILL.md` | Unit tests only |

`tests/agent/chat`: 437 passed. The harness [`evals/latency/chat_latency.py`](../../evals/latency/chat_latency.py) runs the real pipeline in its own process (no parse consumer, no server restart). Cases are in [`ep04_cases.json`](../../evals/latency/ep04_cases.json).

### 2.3 Next: quote fast path (target < 60 s)

1. **Host pre-retrieval.** Before the Query agent's first model call, the host runs the Event search on the raw query (DIALOGUE, lexical-weighted) and, in parallel, the Chunk search. It records both as real retrievals in the Canvas and preloads the top hits with a bounded transcript window (neighbouring lines with speaker and ms) into the Query prompt.
2. **Draft in 1–2 calls.** With the evidence already in the prompt, Query writes the draft immediately when the hit is clear, and keeps the option to search further.
3. **Skip the Evaluator for strong lexical matches.** Use a conservative, testable rule (normalized character overlap with an ORIGINAL or translated text variant above a threshold, plus a margin over the second hit) behind a config flag. The Supervisor remains the reviewer.

Expected budget: Supervisor 6–8 s + Query 12–18 s + Supervisor final 8–10 s ≈ 30–40 s.

## 3. Should the Chat architecture be simplified?

Yes. The latency is structural: one request goes through four model-driven agents, and three of them (Evaluator, Chat, Supervisor) review or rewrite the same answer, each through its own multi-step tool loop. Prompt tuning cannot remove that cost.

### 3.1 The reference design (other team)

```
User → ① MemoryInit (parallel L1/L2/L3 load, small-model query rewrite only for multi-turn)
     → ② Skill-Router (intent → skill_id + injected prompt, progressive disclosure)
     → ③ Plan (the only ReAct loop, parallel fan-out to ④ Tools and ⑤ RAG, MaxReactSteps)
     → ⑥ Result (single pass: synthesis + self_check, no ReAct)
          pass → Final-Polish → user → MemoryWriter (async)
          fail A (ambiguous) → ask the user → second summary
          fail B (evidence missing) → back to ③ with self_check.suggestions
          LoopCount ≥ MaxLoops → degrade (best-scoring history + hand-off hint)
Loop Control (deterministic): MaxLoops, per-node timeouts, score monotonicity, high-risk bypass
```

### 3.2 Why it is faster

| Property | Reference | AniVAG today | Latency effect |
| --- | --- | --- | --- |
| ReAct loops per request | 1 (Plan) | 4 (Supervisor, Query, Evaluator, Chat) | Biggest lever: each removed loop saves 3–6 round trips |
| Review / answer | One single-pass call with evidence in the prompt | Evaluator + Chat + Supervisor, each paging the Canvas | ~40–60 s → ~8–12 s |
| Routing | Small model or rules; skill prompt only for the chosen intent | The Supervisor's first call is a full model call with all skills (~25 KB) in every agent prompt | Smaller prompts, faster first token |
| Tool fan-out | Parallel by design | Sequential tool calls are common | Lower tail latency |
| Loop termination | Deterministic (MaxLoops, timeout, score monotonicity) | Model decides `continue` with no stop rule (see C15) | Prevents runaway loops |
| Memory write | Async after reply | n/a | Off the critical path |

Latency estimate for the same quote question:

| Stage | Today (measured) | Reference-style target |
| --- | ---: | ---: |
| Route / first decision | 10 s | 1–2 s (rules + small model) |
| Retrieval + plan | 45 s (+ 38 s second loop) | 12–18 s (host pre-retrieval, 1–3 ReAct steps) |
| Review | 32 s | 0 s (host checks) or part of Result |
| Answer | 36 s | 6–10 s (single pass, streamed) |
| **Total** | **~160 s** | **~25–35 s** |

### 3.3 What to adopt, adapt or skip

**Adopt**

- **One ReAct loop.** The Query agent becomes the Plan node, the only one allowed to iterate with tools.
- **Single-pass Result.** One structured call returns the answer Markdown, video items and `self_check {pass, reason, missing_evidence, needs_user_confirmation}`. The evidence goes into the prompt, not behind Canvas paging. The answer streams to the user.
- **Deterministic Loop Control.** `MaxLoops = 2` outer loops; fail B only re-plans when the self-check names missing evidence that a new search could find; score monotonicity (stop if a loop does not improve retrieval or the self-check); degrade to the best draft with an explicit uncertainty note. This also resolves C15 (no stop decision).
- **Skill router.** Our query types map naturally to skills, each with its own prompt and tool subset:

  | Skill | Trigger | Tools | Verification |
  | --- | --- | --- | --- |
  | quote / who-said | quotation marks, "出自 / 谁说", short text | Event search (lexical) | Host checks only |
  | scene / plot | events, relationships | Chunk + Event search | Result self-check |
  | visual (image, GIF, MP4) | attachments | `retrieve_visual`, frames | Frame check by model |
  | broad / subjective | "funny / 燃 / best moments" | moment-tag filter + rank | Result self-check |
  | current video | playback context present | `understand_current_video` | Host checks |
  | chit-chat / web | no video intent | none / web | none |

- **Fail A (ask the user).** For ambiguous questions ("which line?", several matches across episodes), asking is cheaper than looping.

**Adapt**

- **Keep the Evaluator, but only as a high-risk bypass.** Speaker attribution, visual-similarity claims and low-confidence retrieval still benefit from an independent check with frames. Trigger it by rule, not on every request.
- **Move verification into the host where possible.** IDs exist, ranges lie inside canonical evidence, adjacent chunks are contiguous: much of this is already validated in `output.py`. The model's self-check only judges whether the evidence supports the claim.
- **Final-Polish.** A separate polish call adds a round trip. Fold it into Result unless a different tone or format model is required.
- **Memory.** L1 (conversation history) already exists. An L3 "similar Q&A" cache keyed by (series scope, normalized query) can return a repeated question instantly. L2 (user profile) has little value for us yet.

**Skip for now**

- A separate query-rewrite model on every turn: only multi-turn questions with pronouns need it, and Plan can resolve those from history.

### 3.4 Target pipeline for AniVAG

```
request
 ├─ rules: playback? attachments? quote-like? greeting?            (0 model calls)
 ├─ Router (small model, only when rules are ambiguous)            (0–1 call)
 ├─ host pre-retrieval in parallel: Event lexical, Chunk hybrid,
 │  visual (if media), moment tags (if broad)                      (0 calls, ~1–2 s)
 ├─ Plan = Query agent, skill-scoped tools, evidence preloaded,
 │  MaxReactSteps                                                  (1–4 calls)
 ├─ host validation: IDs, ranges, scope, contiguity                (0 calls)
 ├─ [high-risk only] Evaluator with frames                         (0–3 calls)
 ├─ Result: answer + video items + self_check, streamed            (1 call)
 └─ Loop Control: pass → done; fail A → ask; fail B → Plan (≤ 2);
    no improvement → degrade with best draft
```

### 3.5 Migration (incremental, each step shippable)

1. **Done in the working tree:** skip inner Chat, Evaluator range rule, evidence preload, inline handoff, realtime gating, no frames for dialogue (§2.2).
2. **Quote fast path:** host pre-retrieval plus a conditional Evaluator skip (§2.3).
3. **Single-pass Result:** replace the Supervisor's post-search ReAct tail with one structured call that receives the approved draft and evidence. The Supervisor keeps routing and web search.
4. **Rules + skill router:** split `skills/` into per-intent prompts and tool subsets; send only the selected one.
5. **Deterministic Loop Control:** MaxLoops, per-node timeouts, monotonicity, degrade path. Make the Evaluator a high-risk bypass.

Check each step with one or two harness runs per query type (latency + correct target range); no long benchmark loops.

**Status 2026-10-18 (steps 3 and 5, uncommitted):**

- `agent/chat/video_search/result.py`: one structured call (no tools, `max_model_calls=1`) gets the question, the draft items and bounded evidence (±15 s transcript window around each range, adjacent retrieved chunks, matched evidence and Events, review advice). It returns `answer_markdown`, `video_items` (ms) and `self_check {pass, reason, missing_evidence, needs_user_confirmation}`. Host validation uses `video_range_sources` (anchor must be a drafted ID; adjacent-chunk contiguity). An invalid range falls back to the drafted range, and an unknown ID is dropped.
- `agent/chat/video_search/loop_control.py`: pass → deliver; `needs_user_confirmation` → ask the user (fail A); fail B re-plans only with non-empty `missing_evidence` (or Evaluator advice), at most `result_max_loops` and only if the loop improved the self-check score or drafted a new chunk; otherwise degrade to the best attempt with a host-written "not fully verified" note.
- The Evaluator runs only for speaker questions, visual questions or attachments, or a drafted chunk whose best fused rank is worse than `evaluator_low_confidence_rank`. A rejection re-plans without spending a Result call.
- The Supervisor no longer gets `get_context_canvas` / `submit_video_item` / `remove_video_item`. `search_video_graph` publishes the Result's answer and cards as soon as it returns, and the Supervisor only closes the turn (one short call). Codex streams per completed message, not per token, so the answer arrives in one piece.
- Config (`agent_runtime`): `single_pass_result` (false restores the old tail), `result_max_loops`, `evaluator_policy` (`risk` / `always`), `evaluator_low_confidence_rank`. The Result reuses the Chat model binding and `chat_timeout_seconds`.

### 3.6 Risks

- Fewer independent checks can let wrong speakers or visual claims through. That is why the Evaluator stays for high-risk skills and why host validation must stay strict.
- A single-pass Result needs the evidence in the prompt. Bound it (top 3–4 items, transcript windows around the hit), or token costs grow.
- The router can misroute. Fall back to the general scene/plot skill when it is unsure, never to "no video".

## 4. Speaker attribution

### 4.1 Measurements (ep04, existing data)

**CAM++ vs. final labels** (v11 task `0f681291`, 270 lines whose label has references; script [`evals/speaker/voiceprint_agreement.py`](../../evals/speaker/voiceprint_agreement.py), output `outputs/verification/speaker-agreement-20261018/v11-lines.jsonl`):

| Gap between best and second-best voice match | Lines | Agrees with label |
| --- | ---: | ---: |
| ≥ 0.15 | 116 | 96% |
| 0.05–0.15 | 100 | 78% |
| < 0.05 | 54 | 48% |

| Line length | < 1 s | 1–2 s | 2–4 s | ≥ 4 s |
| --- | ---: | ---: | ---: | ---: |
| Agreement (centroid) | 60% | 82% | 83% | 94% |

Aggregation: centroid 79.6%, mean of cosines 75.9%, **nearest single reference 66.3%**. Matching against individual prototypes (the k-means direction) is worse than the centroid with 5 references per character.

**Consistency across 6 completed parses (v5, v7–v11):** of 196 lines that align in all runs, 73.5% receive the same name in all six and 14.8% receive two or more different names, so at least one run is wrong on each of those. A further 143 of 339 lines are segmented differently between runs.

Caveat: the labels are agent output and partly derived from CAM++ evidence. Agreement is a consistency measure, not ground-truth accuracy.

### 4.2 Answers

- **Is the emotion effect unavoidable?** Partly. Expressive speech and very short clips carry less speaker information, so voice alone will not reach 100%. But most failures are elsewhere: close matches on short lines, and ASR merging two speakers into one cue (the v8 failure ran no voice comparison on the target line at all).
- **Is an agent the only option?** No, and it should not be the main path. The margin is a usable confidence signal, and a deterministic rule can settle most lines.
- **What accuracy to expect:** roughly 95% on clear lines (≥ 1 s, margin ≥ 0.15) and about 85–90% overall with 10–15% left `null`. This is an estimate until the labeled set below exists.

### 4.3 Plan

1. **Score every DIALOGUE line at parse time** once `voiceprint.ready` (~44 ms per line, ~15 s per episode). Store the top-3 candidates, margin, duration and encoder revision as evidence on the event.
2. **Decide by rule.** Constants live in `server/constants/parse.py`:
   - margin ≥ 0.15 and ≥ 1 s → accept the voice result (flag a conflict if the label differs);
   - margin < 0.05 or < 1 s → low confidence: Refine decides, otherwise `null`;
   - otherwise keep the label and show the scores to Refine.

   Refine then receives a list of conflicts and uncertain lines, instead of choosing which lines to compare.
3. **Cheap priors for the middle band:**
   - a line addressing someone by name (`灯ちゃん`) was likely not spoken by that person;
   - speakers alternate in a back-and-forth;
   - score a whole run of same-voice lines within a scene instead of a single 1 s clip;
   - who is on screen / mouth movement as supporting evidence.
4. **Fix segmentation first** where ASR merges speakers: split cues at diarization turn boundaries, or mark them as mixed.
5. **Score normalization** (per-speaker cohort / AS-norm) if some voices keep "attracting" low-margin lines.
6. **Labeled set:** about 150 ep04 lines, starting with conflicts and low-margin lines, then a random sample. Use a local HTML sheet with per-line audio clips. Report accuracy against how many lines are left unlabelled, by margin and length bucket. About an hour of listening turns the estimate in §4.2 into a measured number.

## 5. New query types

### 5.1 Multimodal input (image / GIF / MP4)

- Follow `retrieve_visual` from [visual-retrieval-trd.md](../design/visual-retrieval-trd.md), but keep the first answer fully deterministic: embed the query media and return the nearest Events or keyframes. A model only reranks and verifies the top-k.
- **GIF / MP4:** sample several frames, embed each, and match the sequence against consecutive keyframes. A sequence is much more distinctive than one frame.
- **Composition similarity** needs a persistent frame-level index: per-keyframe embeddings plus stored images. Today an Event keeps only up to 3 frames averaged into one vector and the JPEGs are temporary (C16).

### 5.2 Broad questions ("really funny moments")

- Similarity search cannot find "funny"; the word is not in the indexed text. Tag moments at parse time instead. The Understanding step already sees every chunk, so it can emit moment tags (comedic, emotional peak, conflict, reveal, performance highlight) with a one-line reason and an intensity score into the Event index.
- A broad query then becomes: filter by tag, rank by intensity, spread the picks across episodes, and have Result write the explanation.
- Episode-level summaries (the RAPTOR-style layer already discussed in `engine/design/multimodel-retrieve-trd.md`) cover questions about the whole series.

### 5.3 Evidence per query type

- About 10 cases per type: exact quote, paraphrased quote, speaker, screenshot, GIF/MP4, broad/subjective, current video. Each case has expected (episode, start–end ms) targets.
- Metrics: hit@1 / hit@3 by time overlap with the target, latency p50, model calls per request. For subjective types: the share of picks the user accepts, not recall.
- Extend `evals/latency/chat_latency.py` with an `expected_ranges` field so one run gives both latency and correctness.

## 6. Order of work

1. Restart the API after the Event backfill finishes, so the §2.2 changes take effect. Run one check per quote case.
2. Quote fast path (§2.3).
3. Parse-time voice scoring + margin rule + labeled set (§4.3).
4. Single-pass Result + deterministic Loop Control (§3.5 steps 3 and 5).
5. Skill router (§3.5 step 4), then `retrieve_visual` and moment tags (§5), each with its small test set.
