import { ImagePlus, Pencil } from "lucide-react";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import Markdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

import { errorMessage, imageContentUrl, mediaUrl, uploadSeriesImage } from "../../api/client";
import type { ImageAsset, SeriesBackground, SeriesUpdateRequest, SeriesView } from "../../api/contracts";
import { useWorkspace } from "../../app/workspace";
import styles from "./library.module.css";

function readBackground(data: SeriesView["importantExtraData"]): SeriesBackground {
  if (!data) return { format: "markdown", content: "" };
  if (Object.keys(data).every((key) => key === "format" || key === "content") && typeof data.content === "string" && (data.format === undefined || data.format === "markdown" || data.format === "json")) {
    return { format: data.format === "json" ? "json" : "markdown", content: data.content };
  }
  if (Object.keys(data).length === 1 && typeof data.seriesBible === "string") {
    return { format: "markdown", content: data.seriesBible };
  }
  // 旧背景含自定义字段时，整体转为 JSON 正文，避免编辑时丢失信息。
  return { format: "json", content: JSON.stringify(data, null, 2) };
}

export function SeriesBackgroundEditor({ series }: { series: SeriesView }) {
  const { mode, updateSeries } = useWorkspace();
  const current = {
    ...readBackground(series.importantExtraData),
    description: series.description ?? "",
    transcriptLanguageHint: series.transcriptLanguageHint ?? "",
  };
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(current);
  const [baseline, setBaseline] = useState(current);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [caption, setCaption] = useState("");
  const [assets, setAssets] = useState<Record<string, ImageAsset>>({});
  const [failedImages, setFailedImages] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const bodyRef = useRef<HTMLTextAreaElement>(null);
  const uploadController = useRef<AbortController | null>(null);
  const pendingCursor = useRef<number | null>(null);
  const hintId = useId();
  const languageOptionsId = useId();
  const document = editing ? draft : current;
  const imageIds = [...new Set([
    ...Array.from(document.content.matchAll(/asset:\/\/([A-Za-z0-9_-]+)/g), (match) => match[1]!),
    ...(editing ? Object.keys(assets) : []),
  ])];
  const changed = draft.description.trim() !== baseline.description.trim()
    || draft.transcriptLanguageHint.trim() !== baseline.transcriptLanguageHint.trim()
    || draft.format !== baseline.format
    || draft.content !== baseline.content;
  const busy = saving || uploading;

  useEffect(() => () => uploadController.current?.abort(), []);
  useEffect(() => {
    if (!uploading && pendingCursor.current !== null && bodyRef.current) {
      bodyRef.current.focus();
      bodyRef.current.setSelectionRange(pendingCursor.current, pendingCursor.current);
      pendingCursor.current = null;
    }
  }, [draft.content, uploading]);

  const startEditing = () => {
    setDraft(current);
    setBaseline(current);
    setAssets({});
    setCaption("");
    setError(null);
    setNotice(null);
    setEditing(true);
  };

  const cancel = () => {
    uploadController.current?.abort();
    uploadController.current = null;
    pendingCursor.current = null;
    setUploading(false);
    setEditing(false);
    setError(null);
    setNotice(null);
  };

  const insertReference = (imageId: string, alt: string, start: number, end: number) => {
    const reference = `asset://${imageId}`;
    let insertion: string;
    if (draft.format === "json") {
      let insideString = false;
      let escaped = false;
      for (const char of draft.content.slice(0, start)) {
        if (escaped) escaped = false;
        else if (char === "\\" && insideString) escaped = true;
        else if (char === '"') insideString = !insideString;
      }
      if (!insideString || escaped) {
        setNotice("图片已上传，请将图片引用放入 JSON 的字符串字段。");
        return;
      }
      insertion = reference;
    } else {
      insertion = `![${alt.replace(/[\[\]\\]/g, "\\$&")}](${reference})`;
    }
    pendingCursor.current = start + insertion.length;
    setDraft((value) => ({ ...value, content: value.content.slice(0, start) + insertion + value.content.slice(end) }));
    setNotice(null);
  };

  const uploadImage = async (file: File) => {
    if (busy || uploadController.current || mode !== "live") return;
    const start = bodyRef.current?.selectionStart ?? draft.content.length;
    const end = bodyRef.current?.selectionEnd ?? start;
    const controller = new AbortController();
    uploadController.current = controller;
    setUploading(true);
    setError(null);
    setNotice(null);
    try {
      const asset = await uploadSeriesImage(series.seriesId, file, controller.signal);
      if (controller.signal.aborted) return;
      setAssets((value) => ({ ...value, [asset.imageId]: asset }));
      setFailedImages((value) => value.filter((id) => id !== asset.imageId));
      insertReference(asset.imageId, caption.trim() || file.name.replace(/\.[^.]+$/, "") || "参考图", start, end);
    } catch (failure) {
      if (!controller.signal.aborted) setError(`图片上传失败：${errorMessage(failure)}`);
    } finally {
      if (uploadController.current === controller) {
        uploadController.current = null;
        setUploading(false);
      }
    }
  };

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || !changed || mode !== "live") return;
    setError(null);
    const changes: Omit<SeriesUpdateRequest, "tenantId"> = { seriesId: series.seriesId };
    if (draft.description.trim() !== baseline.description.trim()) changes.description = draft.description.trim() || null;
    if (draft.transcriptLanguageHint.trim() !== baseline.transcriptLanguageHint.trim()) {
      changes.transcriptLanguageHint = draft.transcriptLanguageHint.trim() || null;
    }
    if (draft.format !== baseline.format || draft.content !== baseline.content) {
      if (draft.content !== "" && draft.format === "json") {
        try { JSON.parse(draft.content); }
        catch { setError("JSON 格式有误，请检查引号、逗号和括号。"); return; }
      }
      changes.importantExtraData = draft.content === "" ? null : { format: draft.format, content: draft.content };
    }
    setSaving(true);
    try {
      await updateSeries(changes);
      setEditing(false);
      setNotice("已保存");
    } catch (failure) {
      setError(`保存失败：${errorMessage(failure)}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className={styles.background} aria-label="系列背景资料">
      <div className={styles.backgroundHeading}>
        <h2>背景资料</h2>
        {!editing ? <button className="button-quiet" type="button" onClick={startEditing} disabled={mode !== "live"}><Pencil size={14} />编辑背景</button> : null}
      </div>
      {editing ? (
        <form className={styles.backgroundForm} onSubmit={save} onKeyDown={(event) => {
          if (event.key === "Escape" && !saving) { event.preventDefault(); cancel(); }
        }}>
          <label><span>简介</span><input className="field" value={draft.description} disabled={busy} onChange={(event) => setDraft((value) => ({ ...value, description: event.target.value }))} placeholder="简单介绍这个视频系列" /></label>
          <label><span>ASR 原文语言</span><input className="field" value={draft.transcriptLanguageHint} list={languageOptionsId} maxLength={64} disabled={busy} onChange={(event) => setDraft((value) => ({ ...value, transcriptLanguageHint: event.target.value }))} placeholder="例如：ja（日语）" /></label>
          <datalist id={languageOptionsId}><option value="ja">日语</option><option value="zh-Hans">简体中文</option><option value="en">英语</option><option value="ko">韩语</option></datalist>
          <div className={styles.editorHeading}>
            <label><span>正文格式</span><select className="field" value={draft.format} disabled={busy} onChange={(event) => { setDraft((value) => ({ ...value, format: event.target.value as SeriesBackground["format"] })); setError(null); }}><option value="markdown">Markdown</option><option value="json">JSON</option></select></label>
            <button className="button-quiet" type="button" disabled={busy || !draft.content} onClick={() => { setDraft((value) => ({ ...value, content: "" })); setError(null); }}>清空背景</button>
          </div>
          <label><span className="sr-only">背景正文</span><textarea ref={bodyRef} className={`field ${styles.backgroundEditor}`} value={draft.content} disabled={busy} rows={7} aria-describedby={hintId} onChange={(event) => { setDraft((value) => ({ ...value, content: event.target.value })); setError(null); }} onPaste={(event) => {
            const file = Array.from(event.clipboardData.files).find((item) => item.type.startsWith("image/"));
            if (file && !busy && mode === "live") { event.preventDefault(); void uploadImage(file); }
          }} placeholder={draft.format === "json" ? "粘贴任意合法 JSON，保留你自己的字段。" : "人物关系、别名、剧情背景… 可直接粘贴参考图。"} /></label>
          <div className={styles.imageUploadControls}>
            <input className="field" aria-label="图片说明（选填）" placeholder="图片说明，如人物姓名" value={caption} disabled={busy} onChange={(event) => setCaption(event.target.value)} />
            <button className="button-secondary" type="button" disabled={busy || mode !== "live"} onClick={() => fileRef.current?.click()}><ImagePlus size={15} />添加参考图</button>
            <input ref={fileRef} className="sr-only" type="file" aria-label="选择背景参考图" accept="image/png,image/jpeg,image/webp" disabled={busy} onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (file) void uploadImage(file);
            }} />
          </div>
          {uploading ? <div className={styles.imageUploading} role="status"><progress aria-label="参考图上传进度" /><span>正在上传参考图…</span></div> : null}
          <p className={styles.backgroundHint} id={hintId}>背景资料用于后续解析；ASR 原文语言保存后立即用于新检索。两者都不会重新解析已完成的视频。</p>
          {error ? <p className={styles.error} role="alert">{error}</p> : null}
          <div className={styles.backgroundActions}>
            <button className="button-quiet" type="button" onClick={cancel} disabled={saving}>取消</button>
            <button className="button-primary" type="submit" disabled={busy || !changed || mode !== "live"}>{saving ? "正在保存…" : "保存背景"}</button>
          </div>
        </form>
      ) : (
        <>
          {series.description ? <p className={styles.seriesDescription}>{series.description}</p> : null}
          <p className={styles.backgroundHint}>ASR 原文语言：{series.transcriptLanguageHint ?? "未设置"}</p>
          {document.format === "markdown" && document.content ? (
            <div className={styles.backgroundMarkdown}>
              <Markdown
                remarkPlugins={[remarkGfm]}
                skipHtml
                urlTransform={(url, key, node) => {
                  if (key === "src" && node.tagName === "img" && url.startsWith("asset://")) {
                    const imageId = url.slice("asset://".length);
                    if (!/^[A-Za-z0-9_-]+$/.test(imageId)) return "";
                    return assets[imageId] ? mediaUrl(assets[imageId].url) ?? "" : imageContentUrl(imageId, series.tenantId);
                  }
                  return defaultUrlTransform(url);
                }}
                components={{
                  img: ({ src, alt, title }) => {
                    const source = typeof src === "string" ? src : "";
                    if (!source || failedImages.includes(source)) {
                      return <span className={styles.inlineImageUnavailable} role="img" aria-label={alt || "背景参考图"}>{alt ? `${alt}：` : ""}<span>图片暂时无法预览</span></span>;
                    }
                    return <img src={source} alt={alt || "背景参考图"} title={title} loading="lazy" onError={() => setFailedImages((value) => [...new Set([...value, source])])} />;
                  },
                  table: ({ children }) => <div className={styles.markdownTable}><table>{children}</table></div>,
                }}
              >{document.content}</Markdown>
            </div>
          ) : <pre className={styles.backgroundText}>{document.content || "暂无背景资料"}</pre>}
          {series.characterBible.length > 0 ? <details className={styles.characters}><summary>人物资料 · {series.characterBible.length}</summary><ul>{series.characterBible.map((character, index) => <li key={`${character.name}-${index}`}><strong>{character.name}</strong>{character.aliases.length ? <span>{character.aliases.join("、")}</span> : null}</li>)}</ul></details> : null}
        </>
      )}
      {(editing || document.format === "json") && imageIds.length ? <div className={styles.backgroundImages} aria-label="背景参考图">{imageIds.map((imageId) => (
        <figure key={imageId}>
          {failedImages.includes(imageId) ? <div className={styles.imageUnavailable}>图片暂时无法预览</div> : <img src={assets[imageId] ? mediaUrl(assets[imageId].url) ?? undefined : imageContentUrl(imageId, series.tenantId)} alt="背景参考图" loading="lazy" onError={() => setFailedImages((value) => [...new Set([...value, imageId])])} />}
          {editing && draft.format === "json" ? <label><span className="sr-only">图片引用</span><input className="field" value={`asset://${imageId}`} readOnly onFocus={(event) => event.currentTarget.select()} /></label> : null}
        </figure>
      ))}</div> : null}
      {notice ? <p className={styles.backgroundHint} role="status">{notice}</p> : null}
    </section>
  );
}
