import { ArrowRight, Search, X } from "lucide-react";
import { useState, useSyncExternalStore } from "react";
import { Link } from "react-router";

import { useI18n } from "../../app/i18n";
import { Mascot } from "../../shared/Mascot";
import styles from "./welcome.module.css";

export const WELCOME_DISMISSED_KEY = "seekino-welcome-dismissed";

function wasDismissed(): boolean {
  try { return window.localStorage.getItem(WELCOME_DISMISSED_KEY) === "1"; }
  catch { return false; }
}

const WIDE = "(min-width: 761px)";

/** Wide screens float the card in the hero's open right side; narrow ones stack it under the hero. */
export function useWideLayout(): boolean {
  return useSyncExternalStore(
    (notify) => { const query = window.matchMedia(WIDE); query.addEventListener("change", notify); return () => query.removeEventListener("change", notify); },
    () => window.matchMedia(WIDE).matches,
  );
}

/** First-visit introduction for the public library; one dismissal hides it for good on this browser. */
export function WelcomeCard({ placement }: { placement: "hero" | "belowHero" | "page" }) {
  const { t } = useI18n();
  const [dismissed, setDismissed] = useState(wasDismissed);
  if (dismissed) return null;
  const dismiss = () => {
    setDismissed(true);
    try { window.localStorage.setItem(WELCOME_DISMISSED_KEY, "1"); } catch { /* Hidden for this visit only. */ }
  };
  return <section className={`${styles.welcome} ${styles[placement]}`} aria-label={t("欢迎来到 Seekino")}>
    <Mascot mood="hop" size={60} className={styles.mascot} />
    <div className={styles.body}>
      <h2>{t("41 小时的课，你要的只是其中一段")}</h2>
      <p>{t("我是 Kino。说出你想找的那一段，我直接带你到原片里的那一秒。")}</p>
      <div className={styles.actions}>
        <Link className={styles.primary} to="/search" state={{ focusComposer: true }}>
          <Search size={16} aria-hidden="true" />{t("试一句")}
        </Link>
        <Link className={styles.secondary} to="/docs">{t("怎么用")}<ArrowRight size={15} aria-hidden="true" /></Link>
      </div>
      <p className={styles.community}>{t("欢迎交流👏：1127823767 (QQ)")}</p>
    </div>
    <button className={styles.close} type="button" onClick={dismiss} aria-label={t("关闭欢迎提示")} title={t("关闭欢迎提示")}>
      <X size={18} aria-hidden="true" />
    </button>
  </section>;
}
