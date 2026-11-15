import { ThumbsDown, ThumbsUp, TriangleAlert } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type {
  AgentMessageFeedback,
  AgentMessageFeedbackReason,
  AgentMessageView,
} from "../../api/contracts";
import {
  clearAgentMessageFeedback,
  errorMessage,
  setAgentMessageFeedback,
} from "../../api/client";
import { useI18n } from "../../app/i18n";
import styles from "./MessageFeedback.module.css";

const COMPLETED_REASONS: { code: AgentMessageFeedbackReason; label: string }[] = [
  { code: "IRRELEVANT", label: "答非所问" },
  { code: "WRONG_EVIDENCE", label: "片段或出处不对" },
  { code: "MISSING_RESULT", label: "没找到想要的内容" },
  { code: "INCOMPLETE", label: "回答不完整" },
  { code: "TOO_SLOW", label: "太慢" },
  { code: "PLAYBACK_ISSUE", label: "视频或播放有问题" },
  { code: "OTHER", label: "其他" },
];

export function MessageFeedback({ message }: { message: AgentMessageView }) {
  const { t, locale } = useI18n();
  const [feedback, setFeedback] = useState<AgentMessageFeedback | null>(message.feedback);
  const [open, setOpen] = useState(false);
  const [reasons, setReasons] = useState<AgentMessageFeedbackReason[]>([]);
  const [comment, setComment] = useState("");
  const [pending, setPending] = useState(false);
  const [thanks, setThanks] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const failed = message.status === "FAILED";

  useEffect(() => {
    setFeedback(message.feedback);
  }, [message.feedback]);

  useEffect(() => {
    if (open) textareaRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!thanks) return;
    const timer = window.setTimeout(() => setThanks(false), 4000);
    return () => window.clearTimeout(timer);
  }, [thanks]);

  if (message.status === "PROCESSING") return null;

  function openEditor() {
    setThanks(false);
    setReasons(feedback?.reasons ?? (failed ? ["FAILURE"] : []));
    setComment(feedback?.comment ?? "");
    setError(null);
    setOpen(true);
  }

  function toggleReason(reason: AgentMessageFeedbackReason) {
    setReasons((current) => {
      if (current.includes(reason)) return current.filter((item) => item !== reason);
      if (current.length >= 3) return current;
      return [...current, reason];
    });
  }

  async function save(rating: "UP" | "DOWN", selectedReasons: AgentMessageFeedbackReason[], note: string | null) {
    if (pending) return;
    const previous = feedback;
    const now = Date.now();
    setThanks(false);
    setFeedback({
      rating,
      reasons: selectedReasons,
      comment: note,
      createdAt: previous?.createdAt ?? now,
      updatedAt: now,
    });
    setPending(true);
    setError(null);
    try {
      const saved = await setAgentMessageFeedback(message.conversationId, message.messageId, {
        rating,
        reasons: selectedReasons,
        comment: note,
      });
      setFeedback(saved);
      setThanks(true);
      setOpen(false);
    } catch (cause) {
      setFeedback(previous);
      setError(errorMessage(cause, locale));
    } finally {
      setPending(false);
    }
  }

  async function clear() {
    if (pending) return;
    const previous = feedback;
    setThanks(false);
    setFeedback(null);
    setOpen(false);
    setPending(true);
    setError(null);
    try {
      await clearAgentMessageFeedback(message.conversationId, message.messageId);
    } catch (cause) {
      setFeedback(previous);
      setError(errorMessage(cause, locale));
    } finally {
      setPending(false);
    }
  }

  if (failed) {
    return (
      <div className={styles.feedback}>
        {feedback ? (
          <div className={styles.actions}>
            <button type="button" className={styles.textButton} onClick={openEditor} disabled={pending}>
              {t("修改")}
            </button>
          </div>
        ) : (
          <div className={styles.actions}>
            <button type="button" className={styles.reportButton} onClick={openEditor} disabled={pending}>
              <TriangleAlert size={15} aria-hidden="true" />
              {t("报告这个问题")}
            </button>
          </div>
        )}
        {thanks ? <p className={styles.thanks} role="status">{t("谢谢反馈喵，我们已收到后续会继续改进服务")}</p> : null}
        {open ? (
          <div
            className={styles.panel}
            role="group"
            aria-label={t("报告回答问题")}
            onKeyDown={(event) => { if (event.key === "Escape" && !pending) setOpen(false); }}
          >
            <p className={styles.panelTitle}>{t("发生了什么？")}</p>
            <textarea
              ref={textareaRef}
              className={styles.comment}
              value={comment}
              maxLength={2000}
              aria-label={t("反馈说明")}
              placeholder={t("可以补充你期望的回答或片段，不需要填写对话内容。")}
              onChange={(event) => setComment(event.target.value)}
            />
            {error ? <p className={styles.error} role="alert">{error}</p> : null}
            <div className={styles.panelActions}>
              <button type="button" className={styles.textButton} onClick={() => setOpen(false)} disabled={pending}>
                {t("取消")}
              </button>
              {feedback ? (
                <button type="button" className={styles.textButton} onClick={clear} disabled={pending}>
                  {t("撤销报告")}
                </button>
              ) : null}
              <button
                type="button"
                className={styles.submitButton}
                onClick={() => void save("DOWN", ["FAILURE"], comment.trim() || null)}
                disabled={pending}
              >
                {pending ? t("提交中…") : t("提交报告")}
              </button>
            </div>
          </div>
        ) : null}
        {error && !open ? <p className={styles.error} role="alert">{error}</p> : null}
      </div>
    );
  }

  return (
    <div className={styles.feedback}>
      <div className={styles.actions}>
        <button
          type="button"
          className={`${styles.ratingButton} ${feedback?.rating === "UP" ? styles.selected : ""}`}
          aria-label={t("这个回答有帮助")}
          title={t("这个回答有帮助")}
          aria-pressed={feedback?.rating === "UP"}
          onClick={() => {
            if (feedback?.rating === "UP") void clear();
            else void save("UP", [], null);
          }}
          disabled={pending}
        >
          <ThumbsUp size={16} aria-hidden="true" />
        </button>
        <button
          type="button"
          className={`${styles.ratingButton} ${feedback?.rating === "DOWN" ? styles.selected : ""}`}
          aria-label={t("这个回答没帮助")}
          title={t("这个回答没帮助")}
          aria-pressed={feedback?.rating === "DOWN"}
          onClick={openEditor}
          disabled={pending}
        >
          <ThumbsDown size={16} aria-hidden="true" />
        </button>
      </div>
      {thanks ? <p className={styles.thanks} role="status">{t("谢谢反馈喵，我们已收到后续会继续改进服务")}</p> : null}
      {open ? (
        <div
          className={styles.panel}
          role="group"
          aria-label={t("告诉我们哪里不对")}
          onKeyDown={(event) => { if (event.key === "Escape" && !pending) setOpen(false); }}
        >
          <p className={styles.panelTitle}>{t("哪里不对？")}</p>
          <div className={styles.reasons}>
            {COMPLETED_REASONS.map((reason) => (
              <button
                key={reason.code}
                type="button"
                className={`${styles.reason} ${reasons.includes(reason.code) ? styles.reasonSelected : ""}`}
                aria-pressed={reasons.includes(reason.code)}
                onClick={() => toggleReason(reason.code)}
                disabled={pending || (!reasons.includes(reason.code) && reasons.length >= 3)}
              >
                {t(reason.label)}
              </button>
            ))}
          </div>
          <textarea
            ref={textareaRef}
            className={styles.comment}
            value={comment}
            maxLength={2000}
            aria-label={t("反馈说明")}
            placeholder={t("补充说明（可选）")}
            onChange={(event) => setComment(event.target.value)}
          />
          {error ? <p className={styles.error} role="alert">{error}</p> : null}
          <div className={styles.panelActions}>
            <button type="button" className={styles.textButton} onClick={() => setOpen(false)} disabled={pending}>
              {t("取消")}
            </button>
            {feedback ? (
              <button type="button" className={styles.textButton} onClick={clear} disabled={pending}>
                {t("撤销评价")}
              </button>
            ) : null}
            <button
              type="button"
              className={styles.submitButton}
              onClick={() => void save("DOWN", reasons, comment.trim() || null)}
              disabled={pending}
            >
              {pending ? t("提交中…") : t("提交反馈")}
            </button>
          </div>
        </div>
      ) : null}
      {error && !open ? <p className={styles.error} role="alert">{error}</p> : null}
    </div>
  );
}
