from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from .schemas import TranscriptAnalysis

TRANSCRIPT_PROMPT = """
你只负责从这段视频的音轨中转写可听见的人声，不负责理解画面。

要求：
1. 所有 start_ms 和 end_ms 都是相对于当前视频片段起点的毫秒数。
2. transcript 只保留能听清的原语言台词，不翻译，不补全听不清的内容。
3. 不要根据画面、字幕、角色外观或背景知识猜测或修正台词。
4. 不要识别说话者身份，不要输出画面描述、动作、情绪或剧情推断。
5. 没有可确认的人声时返回空 transcript。
""".strip()

VIDEO_PARSE_PROMPT = """
你负责把候选 ASR 与自己从视频画面、音频和硬字幕中确认的信息，整理成一条统一、合理的事件时间线。

要求：
1. 所有 start_ms 和 end_ms 都是相对于当前视频片段起点的毫秒数。
2. 候选 ASR 可能有错漏和时间偏差，必须结合视频重新判断，不能机械复制。
3. 逐条核对候选 ASR；只要音频或硬字幕能够确认，就必须生成对应的 DIALOGUE，只有确认是误识别时才省略。
4. 将场景变化、人物动作、可见情绪、关系变化和对白统一放入 events，并按时间排序。
5. 对白可结合音频和清晰可见的硬字幕纠错；普通 DIALOGUE 的 text 写确认后的台词，其他类型写 null。
6. 身份无法从视频或背景资料确认时，使用“白发角色”等中性称呼，不强行猜测真名。
7. 所有事件必须有当前视频画面、音频或硬字幕证据；候选 ASR 和背景资料只能帮助校正与消歧，不能单独创造事件。
8. 同一个持续事件不要按每一帧重复输出；时间范围应覆盖事件真正发生的部分。
9. 每条输出都必须能脱离前后文单独正确理解。不要逐字输出被截断的半句话、省略关键宾语的问句，或孤立阅读时可能产生其他含义的表达。
10. 遇到第 9 条情况时仍保留 DIALOGUE 事件和准确时间范围：description 用“人物 + 询问/说明 + 明确且有画面证据的无害对象”补全语义，例如“高松灯询问要乐奈开始学习吉他的年龄”；text 写 null，不猜测、不展开任何其他含义。
11. summary、description 和 text 使用完整、克制的事实表述；若无法同时保证准确和无歧义，优先保留可确认的动作与时间，省略原句。
""".strip()

VIDEO_ONLY_PARSE_PROMPT = """
你负责把候选 ASR 与自己从视频画面和硬字幕中确认的信息，整理成一条统一、合理的事件时间线。

当前视频由本地视觉运行时解码和按时间顺序采样；模型能够使用帧顺序与时间信息，但不能读取原视频音轨。

要求：
1. 所有 start_ms 和 end_ms 都是相对于当前视频片段起点的毫秒数。
2. 候选 ASR 可能有错漏和时间偏差，必须结合视频画面与硬字幕重新判断，不能机械复制。
3. 逐条核对候选 ASR；只要清晰可见的硬字幕能够确认，就必须生成对应的 DIALOGUE，无法由视觉证据确认时不要猜测。
4. 将场景变化、人物动作、可见情绪、关系变化和可确认的对白统一放入 events，并按时间排序。
5. 普通 DIALOGUE 的 text 写由清晰硬字幕确认后的台词，其他类型写 null。
6. 身份无法从视频或背景资料确认时，使用“白发角色”等中性称呼，不强行猜测真名。
7. 所有事件必须有当前视频画面或硬字幕证据；候选 ASR 和背景资料只能帮助校正与消歧，不能单独创造事件。
8. 同一个持续事件不要按每一帧重复输出；时间范围应覆盖事件真正发生的部分。
9. 每条输出都必须能脱离前后文单独正确理解。不要逐字输出被截断的半句话、省略关键宾语的问句，或孤立阅读时可能产生其他含义的表达。
10. 遇到第 9 条情况时仍保留 DIALOGUE 事件和准确时间范围：description 用“人物 + 询问/说明 + 明确且有画面证据的无害对象”补全语义，例如“高松灯询问要乐奈开始学习吉他的年龄”；text 写 null，不猜测、不展开任何其他含义。
11. summary、description 和 text 使用完整、克制的事实表述；若无法同时保证准确和无歧义，优先保留可确认的动作与时间，省略原句。
""".strip()

FRAME_PARSE_EVIDENCE_PROMPT = """
标有 Frame 和时间标签的视觉输入是从原视频按时间顺序采样的静态帧，每张图前都标有相对于当前 AnalysisWindow 起点的时间；这些图片不包含音轨。标有 asset:// 的背景参考图不属于视频采样帧，不能用来确定当前事件或时间。

帧降级约束：
1. 只根据采样帧中实际可见的画面、硬字幕和候选 ASR 记录事件；候选 ASR 为空时，不得猜测仅靠音频才能确认的对白。
2. 使用帧标签建立先后顺序和近似时间范围，不得把稀疏采样伪装成逐帧观察，也不得虚构采样间隔中没有视觉证据的动作。
3. 单帧只能证明该时刻可见的状态；只有多张相邻帧共同支持时，才把事件扩展为持续时间范围。
4. 即使采样导致信息不足，也必须保持事实克制；宁可省略不确定事件，不要用背景资料补齐画面证据。
5. 每个事件都必须满足 end_ms > start_ms；单帧点状证据统一写成 [t, t+1ms)，这只表示在该采样时刻可见，不表示事件仅持续 1 ms。
""".strip()

FRAME_SEQUENCE_INTRO = (
    "以下视频采样帧来自同一个 AnalysisWindow。每张图片紧跟在自己的"
    "时间标签后，所有标签按相对毫秒严格递增。"
)

ARK_FRAME_SEQUENCE_INTRO = (
    "以下视频采样帧来自同一个 AnalysisWindow，按标签中的相对毫秒严格递增排列。"
)

NEUTRAL_VISUAL_PROMPT = """
你是逐帧视觉证据提取器。只依据上面按顺序排列的图片，输出中性、可核验的观察。你没有人物资料、前文或音频。

严格遵守：
1. 不使用作品知识，不输出人物真名。人物只能用当前图片中直接可见、可跨帧辨认的发色、发型、服装、乐器和站位描述。
2. visual_fact 只写直接可见的动作、物体、构图或状态；不要从字幕推导身份、关系、情绪、意图、因果或故事，也不要把字幕改写进 visual_fact。
3. hard_subtitle 必须逐字抄录该观察覆盖图片里所有清楚可读的硬字幕，保留原语言、行序与换行；同一帧若同时有多组字幕，必须全部保留。没有或不能确定时为 null，不补全被遮挡文字。
4. 字幕发生任何变化时，即使画面主体和动作未变，也必须另建一条 observation。只有画面事实和全部字幕都相同时，才可以合并相邻帧。
5. visible_speaker 只有嘴部动作、手势或明确镜头证据足以支持时才填匿名外观描述；字幕出现不等于能确定说话者。
6. evidence_frames 只能使用当前请求里实际存在的 Frame 编号。不要机械逐帧重复，但必须覆盖所有真正变化的画面和字幕。
7. subjects 只列与该条事实直接相关、画面中确实可见的匿名人物或物体。不要计算时间戳，不输出事件类型，不生成完整剧情。

严格返回给定 JSON Schema，不附加解释。
""".strip()

FALLBACK_REFINER_PROMPT = """
请把 Lite 音轨转写与匿名逐帧视觉证据整理为 VideoTimeline。你在本阶段看不到当前片段的原始视频帧、视频或音轨；可能收到带 asset:// 标签的背景参考图，仅用于人物身份与术语消歧，不得假装观察到当前片段证据中未写出的内容。

证据边界：
1. 当前片段的画面、动作、物体和硬字幕只能来自 visual_observations；可听见的台词只能来自 lite_candidate_asr。解析上下文承接上一片段；上下文与背景参考图只能帮助人物与术语消歧，不能创造当前片段事实。背景图中的动作或文字不能作为当前事件或台词证据。
2. visual_fact 是抽帧视觉证据；hard_subtitle 是抽帧时刻可见文字；visible_speaker 只是低置信线索，不能单独认定说话者。Lite ASR 可能有错漏或少量时间偏差，应与硬字幕互相校验。
3. 人物归一不能只凭发色；只有外观、乐器、服装和位置等多项证据与解析上下文共同匹配时才使用标准名。灯光会改变发色观感。证据不足时保留匿名外观描述，明确区分台上人物与台下观众。孤立一帧且相邻观察不支持的高细节描述应省略，不要扩写。

时间与事件：
4. 所有时间均为当前 AnalysisWindow 内相对毫秒，必须位于给定 duration_ms 范围内；events 按 start_ms 非递减排序，end_ms 必须大于 start_ms。support_intervals_ms 只表示抽帧邻近支持范围，不代表动作持续整个区间。台词时间优先参考 Lite ASR。
5. 输出有语义价值的事件，不要为每条 visual observation 机械创建 ACTION。同一时刻的 DIALOGUE 与视觉状态属于同一镜头时，把视觉状态写进 DIALOGUE.description；只有独立动作或镜头变化才另建 ACTION/SCENE。不同硬字幕或真正变化的镜头不能丢，也不要补齐抽帧间未观察到的动作。
6. 逐项覆盖 lite_candidate_asr：每条可听清的 ASR cue 都应对应一条 DIALOGUE，除非硬字幕明确证明它是误识别；不要因为附近另有硬字幕就静默删除 ASR。同一时刻存在两路台词时允许事件重叠。
7. DIALOGUE.text 必须逐字等于 allowed_dialogue_texts 中的一个完整字符串。若 ASR 与附近中文硬字幕语义对应，优先逐字选择中文行；若没有对应中文行，逐字使用 ASR 原文。不得自行翻译、补写、续写或改标点。同一帧有两组独立字幕时应保留为两条可重叠 DIALOGUE。
8. 只有证据足以对应说话者时，description 才写姓名；否则使用匿名人物或“画面出现字幕”。description 和 summary 使用简体中文，保持事实克制，不增加因果、动机、内心或剧情知识。
9. 只返回符合指定 JSON Schema 的对象。
""".strip()


def build_video_parse_prompt(
    duration_ms: int,
    transcript: TranscriptAnalysis,
    *,
    context: str | None,
    ordered_frames: bool,
    video_audio_available: bool = True,
) -> str:
    sections = [
        VIDEO_PARSE_PROMPT if video_audio_available else VIDEO_ONLY_PARSE_PROMPT
    ]
    if ordered_frames:
        sections.append(FRAME_PARSE_EVIDENCE_PROMPT)
    candidate_name = "候选 ASR" if ordered_frames else "Lite 候选 ASR"
    sections.append(
        f"当前视频片段实际时长为 {duration_ms} ms；"
        f"所有事件必须完全位于 0 到 {duration_ms} ms 之间。\n\n"
        f"{candidate_name}（可能有错，仅作为辅助证据）：\n"
        f"{transcript.model_dump_json(indent=2)}"
    )
    if context:
        evidence_name = "当前图片证据" if ordered_frames else "当前视频证据"
        sections.append(
            "解析上下文（仅用于连续性与消歧，不能替代"
            f"{evidence_name}）：\n{context.strip()}"
        )
    return "\n\n".join(sections)


def build_neutral_visual_prompt(frame_count: int) -> str:
    if frame_count <= 0:
        raise ValueError("frame_count must be greater than zero")
    return (
        f"当前请求共有 {frame_count} 张图片；evidence_frames 只能使用 "
        f"1 到 {frame_count}。\n\n{NEUTRAL_VISUAL_PROMPT}"
    )


def build_fallback_refiner_prompt(
    duration_ms: int,
    transcript: TranscriptAnalysis,
    observations: Sequence[Mapping[str, object]],
    *,
    context: str | None,
) -> str:
    if duration_ms <= 0:
        raise ValueError("duration_ms must be greater than zero")
    allowed_dialogue_texts = _fallback_evidence_lines(transcript, observations)
    evidence = {
        "time_basis": "analysis-window-relative milliseconds",
        "duration_ms": duration_ms,
        "lite_candidate_asr": [
            {"cue_id": f"a{index:03d}", **cue.model_dump(mode="json")}
            for index, cue in enumerate(transcript.transcript)
        ],
        "visual_observations": list(observations),
        "allowed_dialogue_texts": allowed_dialogue_texts,
    }
    return "\n\n".join(
        [
            FALLBACK_REFINER_PROMPT,
            f"当前 AnalysisWindow 的 duration_ms：{duration_ms}",
            "解析上下文（SeriesBible 与 contextBeforeCompact；只用于连续性和消歧）：\n"
            + (context.strip() if context and context.strip() else "（无）"),
            "当前片段证据：\n"
            + json.dumps(evidence, ensure_ascii=False, indent=2),
        ]
    )


def _fallback_evidence_lines(
    transcript: TranscriptAnalysis,
    observations: Sequence[Mapping[str, object]],
) -> list[str]:
    lines = [_normalize_evidence_text(cue.text) for cue in transcript.transcript]
    for observation in observations:
        subtitle = observation.get("hard_subtitle")
        if not isinstance(subtitle, str):
            continue
        lines.extend(
            _normalize_evidence_text(line)
            for line in subtitle.splitlines()
            if _normalize_evidence_text(line)
        )
    return list(dict.fromkeys(line for line in lines if line))


def _normalize_evidence_text(value: str) -> str:
    return " ".join(value.split())
