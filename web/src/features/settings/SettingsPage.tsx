import { Check, Database, Moon, Server, ShieldCheck, Sun } from "lucide-react";

import { API_BASE_URL, WORKSPACE_ID } from "../../api/client";
import { useTheme } from "../../app/theme";
import { useWorkspace } from "../../app/workspace";
import styles from "./settings.module.css";

export function SettingsPage() {
  const { theme, setTheme } = useTheme();
  const { mode } = useWorkspace();
  const shownApiUrl = API_BASE_URL || "同源 / Vite 代理 → http://127.0.0.1:9621";

  return (
    <main className="page">
      <header>
        <h1 className="page-title">设置</h1>
      </header>

      <div className={styles.layout}>
        <section className={styles.settingsSection}>
          <div className={styles.sectionIntro}><span>01</span><div><h2>外观</h2><p>白天清透，夜里安静；两套主题使用同一组青绿色语义。</p></div></div>
          <div className={styles.themeChoices} role="radiogroup" aria-label="主题模式">
            <button type="button" role="radio" aria-checked={theme === "light"} className={theme === "light" ? styles.themeActive : undefined} onClick={() => setTheme("light")}>
              <div className={styles.themePreviewLight}><i /><i /><i /></div>
              <span><Sun size={15} />白天模式</span>
              {theme === "light" ? <Check size={15} /> : null}
            </button>
            <button type="button" role="radio" aria-checked={theme === "dark"} className={theme === "dark" ? styles.themeActive : undefined} onClick={() => setTheme("dark")}>
              <div className={styles.themePreviewDark}><i /><i /><i /></div>
              <span><Moon size={15} />黑夜模式</span>
              {theme === "dark" ? <Check size={15} /> : null}
            </button>
          </div>
        </section>

        <section className={styles.settingsSection}>
          <div className={styles.sectionIntro}><span>02</span><div><h2>连接</h2><p>页面始终请求真实 AniVAG API；服务不可用时展示错误和空态，不注入演示数据。</p></div></div>
          <dl className={styles.connectionList}>
            <div><dt><Server size={16} />API 状态</dt><dd data-mode={mode}><i />{mode === "live" ? "已连接" : mode === "offline" ? "不可用" : "正在检查"}</dd></div>
            <div><dt><Database size={16} />API 地址</dt><dd>{shownApiUrl}</dd></div>
            <div><dt><ShieldCheck size={16} />Workspace</dt><dd>{WORKSPACE_ID}</dd></div>
          </dl>
          {mode === "offline" ? (
            <p className={styles.modeNote}>当前无法连接 API。请确认 AniVAG Server 已启动；恢复连接后页面会重新读取真实 Series 和任务状态。</p>
          ) : null}
        </section>

        <section className={styles.settingsSection}>
          <div className={styles.sectionIntro}><span>03</span><div><h2>数据边界</h2><p>前端只通过公开 HTTP 接口访问媒体，不尝试读取内部 artifact URI。</p></div></div>
          <div className={styles.boundaries}>
            <article><strong>上传确认</strong><p>分片经过 SHA-256 校验，并由服务端确认后才进入已上传进度。</p></article>
            <article><strong>检索理由</strong><p>只根据 Evidence、召回通道与排名生成可核验摘要，不展示或伪造私有思维链。</p></article>
            <article><strong>断线恢复</strong><p>SSE 只负责通知；恢复后重新获取 MySQL 快照，并以服务端状态为准。</p></article>
          </div>
        </section>
      </div>
    </main>
  );
}
