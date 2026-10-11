import clsx from "clsx";
import {
  Activity,
  ArrowUpFromLine,
  ChartNoAxesCombined,
  ChevronDown,
  LibraryBig,
  Moon,
  Search,
  Settings,
  Sun,
  UserRound,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router";

import { useUploads } from "../features/uploads/UploadProvider";
import { useAuth } from "../features/auth/AuthProvider";
import { errorMessage } from "../api/client";
import { useTheme } from "./theme";
import { useWorkspace } from "./workspace";
import styles from "./app-shell.module.css";

const navigation = [
  { to: "/search", label: "名场面检索", icon: Search },
  { to: "/library", label: "资料库", icon: LibraryBig },
  { to: "/tasks", label: "解析任务", icon: Activity },
  { to: "/usage", label: "模型消耗", icon: ChartNoAxesCombined },
  { to: "/settings", label: "设置", icon: Settings },
];

function AccountControls({ onLogin }: { onLogin: () => void }) {
  const { accountIdentity, accountStatus, accountError, retryAccount, sources, activeSource, selectSource, logout } = useAuth();
  const { mode } = useWorkspace();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const signedIn = accountIdentity && !accountIdentity.isGuest;
  return <section id="anivag-account-panel" className={styles.account} aria-label="当前账号">
    <header><strong>{signedIn ? accountIdentity.email || "已登录" : "未登录"}</strong><span>账号与资料来源</span></header>
    <div className={styles.accountAction}>
      {accountStatus === "unconfigured" ? <span title="连接账号服务后，可登录并访问远端资料">未连接账号服务</span>
        : accountStatus === "checking" ? <span>连接中…</span>
        : accountStatus === "unavailable" ? <button type="button" title={accountError ?? undefined} onClick={retryAccount}>重新连接</button>
        : !signedIn ? <button type="button" onClick={onLogin}>登录</button>
        : <button type="button" disabled={busy} onClick={async () => {
          setBusy(true); setError(null);
          try { await logout(); } catch (failure) { setError(errorMessage(failure)); }
          finally { setBusy(false); }
        }}>{busy ? "退出中…" : "退出"}</button>}
    </div>
    {sources.length > 1 ? <label><span>资料来源</span><select className="field" aria-label="选择资料来源"
      value={activeSource.key} onChange={(event) => selectSource(event.target.value)}>
      {sources.map((source) => <option key={source.key} value={source.key}>{source.kind === "local" ? "本机资料" : source.kind === "public" ? "公开资料" : "我的资料"}</option>)}
    </select></label> : <div className={styles.sourceLabel}><span>当前资料</span><strong>{activeSource.kind === "local" ? "本机资料" : activeSource.kind === "public" ? "公开资料" : "我的资料"}</strong></div>}
    {error ? <p role="alert">{error}</p> : null}
    <div className={clsx(styles.runtime, styles[`runtime${mode}`])}><i aria-hidden="true" /><span>{mode === "live" ? "资料已连接" : mode === "offline" ? "资料连接不可用" : "检查资料连接"}</span></div>
  </section>;
}

export function AppShell() {
  const [accountOpen, setAccountOpen] = useState(false);
  const accountRef = useRef<HTMLDivElement>(null);
  const avatarRef = useRef<HTMLButtonElement>(null);
  const location = useLocation();
  const { theme, toggleTheme } = useTheme();
  const { activeCount, overallProgress, openDialog } = useUploads();
  const { accountIdentity, canUpload, canManage, canConfigure, openLogin, sources, selectSource } = useAuth();
  const signedIn = accountIdentity && !accountIdentity.isGuest;
  const visibleNavigation = navigation.filter(({ to }) => {
    if (to === "/settings") return canConfigure;
    if (["/library", "/tasks", "/usage"].includes(to)) return canManage;
    return true;
  });
  const uploadAction = canUpload ? openDialog : !signedIn ? openLogin : () => {
    const personalSource = sources.find((source) => source.kind === "personal");
    if (personalSource) selectSource(personalSource.key);
  };

  useEffect(() => { setAccountOpen(false); }, [location.key]);
  useEffect(() => {
    if (!accountOpen) return;
    const closeOutside = (event: PointerEvent) => {
      if (!accountRef.current?.contains(event.target as Node)) setAccountOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setAccountOpen(false); avatarRef.current?.focus(); }
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => { document.removeEventListener("pointerdown", closeOutside); document.removeEventListener("keydown", closeOnEscape); };
  }, [accountOpen]);

  return (
    <div className={styles.shell}>
      <header className={styles.topbar} aria-label="全站导航">
        <NavLink className={styles.brand} to="/watch" aria-label="Seekino 首页">
          <img src="/seekino-logo.png" alt="" /><strong>seekino</strong>
        </NavLink>
        <nav className={styles.navigation} aria-label="主导航">
          {visibleNavigation.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} className={({ isActive }) => clsx(styles.navItem, isActive && styles.navActive)}>
              <Icon size={16} strokeWidth={1.8} aria-hidden="true" /><span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className={styles.headerActions}>
          <span id="watch-header-actions" style={{ display: "contents" }} />
          <NavLink className={styles.iconButton} to="/search" aria-label="搜索名场面" title="搜索名场面"><Search size={21} strokeWidth={1.7} aria-hidden="true" /></NavLink>
          <button className={styles.uploadButton} type="button" onClick={uploadAction}
            aria-label={canUpload ? `并行传输 · ${activeCount > 0 ? `${activeCount} 项进行中，${overallProgress}%` : "打开上传案台"}` : !signedIn ? "登录后上传远端视频" : "进入我的资料"}
            title={canUpload ? "上传视频与管理并行传输" : !signedIn ? "登录后上传远端视频" : "进入我的资料"}>
            <ArrowUpFromLine size={19} strokeWidth={1.7} aria-hidden="true" /><span>{canUpload ? activeCount > 0 ? `${overallProgress}%` : "上传" : !signedIn ? "上传" : "我的资料"}</span>
            {canUpload && activeCount > 0 ? <i style={{ width: `${overallProgress}%` }} aria-hidden="true" /> : null}
          </button>
          <button className={styles.iconButton} type="button" onClick={toggleTheme} aria-label={theme === "light" ? "切换至黑夜模式" : "切换至白天模式"} title={theme === "light" ? "切换至黑夜模式" : "切换至白天模式"}>
            {theme === "light" ? <Moon size={20} strokeWidth={1.7} aria-hidden="true" /> : <Sun size={20} strokeWidth={1.7} aria-hidden="true" />}
          </button>
          <div className={styles.accountMenu} ref={accountRef} onBlur={(event) => {
            if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) setAccountOpen(false);
          }}>
            <button className={styles.accountTrigger} type="button" ref={avatarRef} onClick={() => setAccountOpen((open) => !open)} aria-label="打开账号菜单" aria-expanded={accountOpen} aria-controls="anivag-account-panel" title={signedIn ? accountIdentity.email || "我的账号" : "登录与账号"}>
              <span className={styles.avatar}>{signedIn && accountIdentity.email ? accountIdentity.email.slice(0, 1).toUpperCase() : <UserRound size={21} strokeWidth={1.6} aria-hidden="true" />}</span><ChevronDown size={13} aria-hidden="true" />
            </button>
            {accountOpen ? <AccountControls onLogin={() => { avatarRef.current?.focus(); setAccountOpen(false); openLogin(); }} /> : null}
          </div>
        </div>
      </header>
      <div className={styles.content}><Outlet /></div>
    </div>
  );
}
