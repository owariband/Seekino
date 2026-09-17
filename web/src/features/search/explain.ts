import type { VideoChunkSearchHit } from "../../api/contracts";

export interface HitExplanation {
  summary: string;
  matchedFacts: string[];
  sources: string[];
  limitations: string[];
}

function transcriptText(hit: VideoChunkSearchHit): string {
  return hit.evidence.transcript
    .map((entry) => [entry.speaker, entry.text].filter(Boolean).join("："))
    .filter(Boolean)
    .join(" ");
}

export function explainHit(hit: VideoChunkSearchHit): HitExplanation {
  const { candidate, selectionReason } = hit;
  return {
    summary: selectionReason.summary,
    matchedFacts: selectionReason.matchedEvidence.map((item) => item.text),
    sources: [
      candidate.transcriptRank !== null &&
        `ASR 原话 · 词法召回第 ${candidate.transcriptRank}`,
      candidate.textRank !== null && `剧情语义 · 语义召回第 ${candidate.textRank}`,
      candidate.visionRank !== null && `画面事件 · 视觉召回第 ${candidate.visionRank}`,
      selectionReason.matchedEvidence.some((item) => item.kind === "CONTEXT") &&
        "前后语境",
    ].filter((value): value is string => Boolean(value)),
    limitations: selectionReason.limitations,
  };
}

export function readableTranscript(hit: VideoChunkSearchHit): string {
  return transcriptText(hit) || "该时间片没有可展示的对白文本。";
}
