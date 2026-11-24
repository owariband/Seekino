"""Bind native Kino turns to verified footage, conversation storage and the selected model."""
from time import monotonic

from engine.rag.retrieval.contracts import EventIndexNotReadyError, SeriesNotReadyError
from server.service.mcp_evidence import StaleEvidenceError

from .watch_history import LIBRARY_CHAT, watch_stages, watch_updates
from .watch_settings import watch_connection


def answer_turn(application, action, args):
    library = application.library
    connection = watch_connection(application._root, application.profile, application.keys)
    if connection is None:
        raise ValueError("请先配置 Kino 问答使用的视觉模型。")
    question = args["question"]
    if not isinstance(question, str) or not question.strip() or len(question) > 4000:
        raise ValueError("请输入 1 至 4000 字的问题。")
    in_library = action == "askLibrary"
    owner = LIBRARY_CHAT if in_library else args["videoID"]
    reference = args.get("sourceRef", "")
    store = library.watch_history
    thread = args.get("threadID", "")
    turns = store.view(owner, thread)["messages"] if thread else ([] if "threadID" in args else args.get("history", []))
    publish_stage = watch_stages.get()
    if publish_stage:
        publish_stage("RETRIEVING")
    sources = []
    source_views = []
    if in_library:
        scope = list(dict.fromkeys(args["seriesIDs"]))
        if not scope:
            raise ValueError("请先选择可检索的系列。")
        for series_id in scope:
            library.series(series_id)
        try:
            hits = library.search(question, series_ids=scope, top_k=3,
                                  keywords_only=not application.profile.view(application.keys)["searchReady"]).hits
        except (EventIndexNotReadyError, SeriesNotReadyError):
            hits = []
        refs = [item.source_ref for item in hits]
        if thread:
            # Follow-up questions may refer to earlier footage by pronoun.
            for turn in reversed(turns):
                if turn.get("sources") and not turn.get("failed"):
                    refs.extend(item["sourceRef"] for item in turn["sources"] if item["seriesID"] in scope)
                    break
        if refs and publish_stage:
            publish_stage("EVALUATING")
        for ref in dict.fromkeys(refs):
            try:
                clip = library.open_result(ref)
            except StaleEvidenceError:
                continue
            if clip.series_id in scope:
                item = library.video(clip.video_id)
                title = item.display_name or item.original_filename
                sources.append((ref, title, clip))
                source_views.append(dict(sourceRef=ref, title=title, videoID=clip.video_id,
                                         seriesID=clip.series_id, startMs=clip.start_ms, endMs=clip.end_ms))
            if len(sources) == 3:
                break
        turns = [turn for turn in turns if all(item["seriesID"] in scope for item in turn.get("sources", []))]
        evidence = None
        position = 0
        if sources:
            clip = sources[0][2]
            video, series, position = clip.video_id, clip.series_id, clip.start_ms
    else:
        video, series, position = owner, args["seriesID"], args["positionMs"]
        scope = [series]
        original = library.open_video(video)
        if publish_stage:
            publish_stage("EVALUATING")
        if (original.series_id != series or type(position) is not int or not 0 <= position <= original.duration_ms):
            raise ValueError("提问的系列或播放位置无效。")
        evidence = library.open_result(reference) if reference else None
        if evidence:
            if evidence.video_id != video or evidence.series_id != series:
                raise ValueError("引用的片段不属于当前视频。")
            item = library.video(video)
            source_views.append(dict(sourceRef=reference, videoID=video, seriesID=series,
                                     title=item.display_name or item.original_filename,
                                     startMs=evidence.start_ms, endMs=evidence.end_ms))
    binding = connection[0].resolve_model_binding("vision", environ=connection[1])
    thread, message, attachment = store.start(owner, scope, thread, question, position,
        reference, args.get("attachmentPath"), binding.model, sources=source_views)
    partial = ""
    last_checkpoint = None
    last_update = None
    publish = watch_updates.get()

    def streamed(delta):
        nonlocal partial, last_checkpoint, last_update
        partial += delta
        now = monotonic()
        if publish is not None and (last_update is None or now - last_update >= .032):
            publish(partial)
            last_update = now
        if last_checkpoint is None or now - last_checkpoint >= .35:
            store.checkpoint(message, partial)
            last_checkpoint = now

    try:
        if in_library and not sources:
            answer = {"text": "在所选系列的已发布索引中没有找到相关片段。可以换个关键词或调整系列范围。", "positionMs": 0}
        else:
            answer = library.ask_watch(connection, video_id=video, series_id=series, position_ms=position,
                question=question, history=[turn for turn in turns if not turn.get("failed")],
                evidence=evidence, attachment=attachment, library_sources=sources if in_library else None,
                on_delta=streamed if args.get("stream") else None)
        store.finish(message, answer["text"])
    except Exception as exc:
        if partial:
            store.checkpoint(message, partial)
        store.fail(message, str(exc))
        if "threadID" in args:
            return {"watchFailure": str(exc), "watch": store.view(owner, thread)}
        raise
    return {**answer, "watch": store.view(owner, thread)} if "threadID" in args else answer
