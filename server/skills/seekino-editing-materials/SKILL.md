---
name: seekino-editing-materials
description: Find source performances and editing references with Seekino MCP, review the original footage, prepare local layers, and save reusable editing techniques and feedback in the editor's library. Use for footage selection, style research, and updating a motion template after a cut.
---

# Editing with Seekino

## Choose footage

1. Use `list_series` on the connected server. A local and a Cloud connection can be open together. Record the connection, series, video, and source milliseconds for every take. Resolve an `evidence_ref` on the connection that issued it.
2. Use `retrieve` for dialogue, gestures and changes of expression. Include observable cues and spelling variants of the character's name. For example, to cut Anon's reaction in MyGO, search for 千早愛音, clenched teeth, and raised fists. Compare several takes. To survey all published appearances use `browse_events`, copy the recorded names, and follow `next_cursor` until it is null. Omit the name filter to include untagged events.
3. Open each candidate with `read_evidence`. Watch the lead-in, the action and its release. Set your own inclusive `start_ms` and exclusive `end_ms` on the source video and call again. Download `media.clip_url` promptly. Cloud clips are limited to 60 seconds per request. Preserve motion and audio when the performance depends on them. A still works for a deliberate held beat.

## Borrow a visual technique

Use `find_edit_templates` to search for a visible change, such as a diagonal wipe that reveals a title in two cuts. The `techniques` list contains indexed source observations. Play `media.play_url`, inspect the sequence frame by frame, and record the source range and the specific choice worth borrowing. `get_style_card(video_id)` gives the whole video's recurring signals with exemplar references. Open those exemplars with `read_evidence` to compare pacing, palette, composition and transitions. An empty result means this library has no matching published observation. A different series or a locally connected reference library can supply another example.

Choose what fits the current film. Keep borrowed timing, composition or motion distinct from your own variations. Keep the implementation, rendering, rights decisions and creative judgment on the editing computer.

## Prepare a cutout when the shot calls for it

For an animated subject, start with the source clip. Watch for flicker, edge loss and drift if matting successive frames. For a freeze frame or graphic insert, the bundled `scripts/split_frame.py` uses the CPU and the original canvas. Install its optional dependencies in a local Python 3.12 environment (`numpy`, `pillow`, `onnxruntime`). Download the [pinned anime-seg ONNX weights](https://huggingface.co/skytnt/anime-seg/resolve/a0a563c41338cbe0d23dfb4bfc3e243c518e5768/isnetis.onnx) once. The script verifies the file's SHA-256.

```sh
python scripts/split_frame.py frame.png --weights isnetis.onnx --output layers
```

Place `foreground.png` and `background-hole.png` at the same origin. `mask.png` holds the soft matte. Review `red-preview.png` for missing hair, outlines or unwanted scenery. The background layer has a transparent hole where the character was. Moving the character may require a separate clean plate or local inpainting.

## Reuse and update a template

* Search the `templates` list with `find_edit_templates`. Use `include_parse_techniques=false` to search only your own templates. Read a specific revision using `get_edit_template`. When a template has no default, use `list_edit_template_versions` and choose one. Pin its exact version in the edit.
* Rebuild the effect locally from the revision's entry, files, runtime, slots and parameters. Check the rendered frames at the target frame rate and canvas size. Source snapshots preserve what the author originally saw. If a source changed, inspect current footage separately.
* Save a reusable effect with `save_edit_template_version`. Include the contract, editable text files, entry point, runtime, and evidence references from this server. To revise an effect, supply its template and parent version and explain what changed in a `revision_rationale`. Record the viewer's feedback separately from your interpretation, the changes made, what you verified, and what remains unverified.
* Use `append_edit_template_note` for later feedback or a client-reported render check. Promote a verified version with `set_default_edit_template_version` only when that choice is intended. Read the current template revision first and record the reason. Use the same `request_id` when retrying the same write.

Local writes belong to the local library. For Cloud editing, obtain a guest editing token with `POST /api/auth/mcp-token?scope=editing`. No email is required. Keep the same bearer for later sessions. Requesting another anonymous token without a browser cookie opens a different guest library. The guest can read public footage, save 32 private revisions, and keep up to 64 entries per revision including its first evaluation, checks and default selections. The account menu can copy an editing token for the current guest or signed-in member. Signing in from the same guest browser carries those template IDs into the member's library. A public-read token can still fetch footage, style references and the guide.
