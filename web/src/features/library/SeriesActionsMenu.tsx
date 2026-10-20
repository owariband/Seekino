import { useQueryClient } from "@tanstack/react-query";
import { Check, Download, EllipsisVertical, Globe, Lock } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { errorMessage, updateSeries } from "../../api/client";
import type { SeriesSummary } from "../../api/contracts";
import { useI18n } from "../../app/i18n";
import { seriesTitle } from "../../shared/format";
import { ParseExportDialog } from "./ParseExportDialog";
import styles from "./SeriesActionsMenu.module.css";

export function SeriesActionsMenu({ series }: { series: SeriesSummary }) {
  const { t, locale } = useI18n();
  const queryClient = useQueryClient();
  const menuId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const close = () => { setOpen(false); trigger.current?.focus(); };

  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      if (!trigger.current || !menu.current) return;
      const anchor = trigger.current.getBoundingClientRect();
      const { width, height } = menu.current.getBoundingClientRect();
      setPosition({
        left: Math.max(8, Math.min(anchor.right - width, window.innerWidth - width - 8)),
        top: Math.max(8, anchor.bottom + height + 8 <= window.innerHeight ? anchor.bottom + 6 : anchor.top - height - 6),
      });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => { window.removeEventListener("resize", place); window.removeEventListener("scroll", place, true); };
  }, [open, error, pending]);

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !menu.current?.contains(event.target) && !trigger.current?.contains(event.target)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); setOpen(false); trigger.current?.focus(); }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [open]);

  const changeVisibility = async (visibility: SeriesSummary["visibility"]) => {
    if (pending) return;
    if (visibility === series.visibility) { close(); return; }
    setPending(true);
    setError(null);
    try {
      const updated = await updateSeries({ tenantId: series.tenantId, seriesId: series.seriesId, visibility });
      queryClient.setQueryData(["series-detail", series.seriesId, series.tenantId], updated);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["series"] }),
        queryClient.invalidateQueries({ queryKey: ["series-detail"] }),
      ]);
      close();
    } catch (failure) {
      setError(errorMessage(failure, locale));
    } finally {
      setPending(false);
    }
  };

  if (series.access !== "member") return null;
  return <>
    <button ref={trigger} className={styles.trigger} type="button" aria-label={t("{name} 的系列选项", { name: seriesTitle(series) })}
      aria-haspopup="menu" aria-expanded={open} aria-controls={open ? menuId : undefined}
      onClick={() => { setError(null); setOpen((value) => !value); }}>
      <EllipsisVertical size={20} aria-hidden="true" />
    </button>
    {open ? createPortal(<div ref={menu} id={menuId} className={styles.menu} style={position} role="menu" aria-label={t("系列操作")} aria-busy={pending}
      onBlur={(event) => {
        if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget) && !trigger.current?.contains(event.relatedTarget)) setOpen(false);
      }}
      onKeyDown={(event) => {
        if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
        const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
        const index = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
        buttons[index]?.focus();
      }}>
      <span className={styles.label}>{t("系列可见性")}</span>
      {(["private", "public"] as const).map((visibility) => <button key={visibility} type="button" role="menuitemradio"
        aria-checked={series.visibility === visibility} disabled={pending} onClick={() => void changeVisibility(visibility)}>
        {visibility === "public" ? <Globe size={17} aria-hidden="true" /> : <Lock size={17} aria-hidden="true" />}
        <span>{visibility === "public" ? t("公开") : t("私有")}<small>{visibility === "public" ? t("所有人可见") : t("仅自己可见")}</small></span>
        {series.visibility === visibility ? <Check size={16} aria-hidden="true" /> : null}
      </button>)}
      <button type="button" role="menuitem" disabled={pending} onClick={() => { close(); setExportOpen(true); }}>
        <Download size={17} aria-hidden="true" /><span>{t("导出解析数据")}</span>
      </button>
      {pending ? <p className={styles.status} role="status">{t("正在保存…")}</p> : null}
      {error ? <p className={styles.error} role="alert">{error}</p> : null}
    </div>, document.body) : null}
    {exportOpen ? <ParseExportDialog key={`${series.tenantId}:${series.seriesId}`} series={series}
      onClose={() => { setExportOpen(false); trigger.current?.focus(); }} /> : null}
  </>;
}
