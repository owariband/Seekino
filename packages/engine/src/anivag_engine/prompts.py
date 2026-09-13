from __future__ import annotations

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
当前视觉输入是从原视频按时间顺序采样的静态帧，每张图前都标有相对于当前 AnalysisWindow 起点的时间；这些图片不包含音轨。

帧降级约束：
1. 只根据采样帧中实际可见的画面、硬字幕和候选 ASR 记录事件；候选 ASR 为空时，不得猜测仅靠音频才能确认的对白。
2. 使用帧标签建立先后顺序和近似时间范围，不得把稀疏采样伪装成逐帧观察，也不得虚构采样间隔中没有视觉证据的动作。
3. 单帧只能证明该时刻可见的状态；只有多张相邻帧共同支持时，才把事件扩展为持续时间范围。
4. 即使采样导致信息不足，也必须保持事实克制；宁可省略不确定事件，不要用背景资料补齐画面证据。
5. 每个事件都必须满足 end_ms > start_ms；单帧点状证据统一写成 [t, t+1ms)，这只表示在该采样时刻可见，不表示事件仅持续 1 ms。
""".strip()

FRAME_SEQUENCE_INTRO = (
    "以下图片来自同一个 AnalysisWindow。每张图片紧跟在自己的"
    "时间标签后，所有标签按相对毫秒严格递增。"
)

ARK_FRAME_SEQUENCE_INTRO = (
    "以下图片来自同一个 AnalysisWindow，按标签中的相对毫秒严格递增排列。"
)


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
