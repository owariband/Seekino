import type { LexicalFieldName, VideoChunkSearchHit } from "../../api/contracts";
import { translate, type Locale, type MessageParams } from "../../shared/messages";

export interface HitExplanation {
  summary: string;
  matchedFacts: string[];
  sources: string[];
  limitations: string[];
}

const LEXICAL_SOURCE_LABELS: Record<LexicalFieldName, string> = {
  dialogue: "对白词项命中",
  screen_text: "画面文字词项命中",
  person_names: "人物名词项命中",
  event_description: "事件描述词项命中",
  keywords: "关键词词项命中",
  questions: "生成问题词项命中（仅召回线索）",
  summary: "摘要词项命中",
};

function transcriptText(hit: VideoChunkSearchHit): string {
  return hit.evidence.transcript
    .map((entry) => [entry.speaker, entry.text].filter(Boolean).join("："))
    .filter(Boolean)
    .join(" ");
}

export function explainHit(hit: VideoChunkSearchHit, locale: Locale = "zh"): HitExplanation {
  const t = (message: string, params?: MessageParams) => translate(locale, message, params);
  const { candidate, selectionReason } = hit;
  return {
    summary: selectionReason.summary,
    matchedFacts: selectionReason.matchedEvidence.map((item) => item.text),
    sources: [
      candidate.lexicalRank != null && t("词项召回第 {value0}", { value0: candidate.lexicalRank }),
      ...(candidate.lexicalRank != null
        ? [...new Set((selectionReason.lexicalMatches ?? []).map(
            (match) => t(LEXICAL_SOURCE_LABELS[match.field]),
          ))]
        : []),
      candidate.transcriptRank !== null &&
        t("ASR 原话 · 词法召回第 {value0}", { value0: candidate.transcriptRank }),
      candidate.textRank !== null && t("剧情语义 · 语义召回第 {value0}", { value0: candidate.textRank }),
      candidate.visionRank !== null && t("画面事件 · 视觉召回第 {value0}", { value0: candidate.visionRank }),
      selectionReason.matchedEvidence.some((item) => item.kind === "CONTEXT") &&
        t("前后语境"),
    ].filter((value): value is string => Boolean(value)),
    limitations: selectionReason.limitations,
  };
}

export function readableTranscript(hit: VideoChunkSearchHit, locale: Locale = "zh"): string {
  return transcriptText(hit) || translate(locale, "该时间片没有可展示的对白文本。");
}
