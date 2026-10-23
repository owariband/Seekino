import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import {
  Activity,
  ArrowUpFromLine,
  ChartNoAxesCombined,
  ChevronDown,
  LibraryBig,
  Menu,
  MessageCircle,
  Moon,
  Play,
  Search,
  Settings,
  Sun,
  UserRound,
  X,
} from "lucide-react";
import { type CSSProperties, Suspense, useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router";

import { FOCUS_SEARCH_EVENT, loadConversations } from "../features/search/conversationNavigation";
import { useUploads } from "../features/uploads/UploadProvider";
import { useAssistantBusy } from "../shared/assistantActivity";
import { Mascot } from "../shared/Mascot";
import { useAuth } from "../features/auth/AuthProvider";
import { errorMessage } from "../api/client";
import { useI18n } from "./i18n";
import { useTheme } from "./theme";
import { useWorkspace } from "./workspace";
import styles from "./app-shell.module.css";

const navigation = [
  { to: "/library", label: "资料库", icon: LibraryBig },
];

// Operator pages live in the account menu so viewers see a quiet header.
const managementLinks = [
  { to: "/tasks", label: "解析任务", icon: Activity },
  { to: "/usage", label: "模型消耗", icon: ChartNoAxesCombined },
];

const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

export type WatchPanelLayout = { open: boolean; width: number; resizing: boolean };

function AccountControls({ onLogin, mobile = false }: { onLogin: () => void; mobile?: boolean }) {
  const { t, locale } = useI18n();
  const { theme, toggleTheme } = useTheme();
  const { accountIdentity, accountStatus, accountError, retryAccount, sources, activeSource, selectSource, logout, canManage } = useAuth();
  const { mode } = useWorkspace();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ failure: unknown } | null>(null);
  const signedIn = accountIdentity && !accountIdentity.isGuest;
  return <section id="anivag-account-panel" className={styles.account} aria-label={t("当前账号")}>
    {!mobile ? <header><strong>{signedIn ? accountIdentity.email || t("已登录") : t("未登录")}</strong><span>{t("账号与资料来源")}</span></header> : null}
    <div className={styles.accountAction}>
      {accountStatus === "unconfigured" ? <span title={t("连接账号服务后，可登录并访问远端资料")}>{t("未连接账号服务")}</span>
        : accountStatus === "checking" ? <span>{t("连接中…")}</span>
        : accountStatus === "unavailable" ? <button type="button" title={accountError ?? undefined} onClick={retryAccount}>{t("重新连接")}</button>
        : !signedIn ? <button className={mobile ? styles.mobileIdentity : undefined} type="button" onClick={onLogin} aria-label={t("登录")}>
          {mobile ? <><span className={styles.avatar}><UserRound size={21} aria-hidden="true" /></span><strong>{t("未登录")}</strong></> : t("登录")}
        </button>
        : <button className={mobile ? styles.mobileIdentity : undefined} type="button" disabled={busy} aria-label={mobile ? t(busy ? "退出中…" : "退出") : undefined} onClick={async () => {
          setBusy(true); setError(null);
          try { await logout(); } catch (failure) { setError({ failure }); }
          finally { setBusy(false); }
        }}>{mobile ? <><span className={styles.avatar}>{accountIdentity.email?.slice(0, 1).toUpperCase() || <UserRound size={21} aria-hidden="true" />}</span>
          <span className={styles.identityLabel}><strong>{accountIdentity.email || t("已登录")}</strong><small>{busy ? t("退出中…") : t("退出")}</small></span>
        </> : busy ? t("退出中…") : t("退出")}</button>}
    </div>
    {sources.length > 1 ? <label><span>{t("资料来源")}</span><select className="field" aria-label={t("选择资料来源")}
      value={activeSource.key} onChange={(event) => selectSource(event.target.value)}>
      {sources.map((source) => <option key={source.key} value={source.key}>{t(source.kind === "local" ? "本机资料" : source.kind === "public" ? "公开资料" : "我的资料")}</option>)}
    </select></label> : <div className={styles.sourceLabel}><span>{t("当前资料")}</span><strong>{t(activeSource.kind === "local" ? "本机资料" : activeSource.kind === "public" ? "公开资料" : "我的资料")}</strong></div>}
    {error ? <p role="alert">{errorMessage(error.failure, locale)}</p> : null}
    <nav className={styles.accountLinks} aria-label={t("管理")}>
      {canManage ? managementLinks.map(({ to, label, icon: Icon }) => <NavLink key={to} className={styles.accountLink} to={to}><Icon size={17} aria-hidden="true" />{t(label)}</NavLink>) : null}
      <NavLink className={styles.accountLink} to="/settings"><Settings size={17} aria-hidden="true" />{t("设置")}</NavLink>
    </nav>
    <div className={clsx(styles.runtime, styles[`runtime${mode}`])}><i aria-hidden="true" /><span>{t(mode === "live" ? "资料已连接" : mode === "offline" ? "资料连接不可用" : "检查资料连接")}</span></div>
    {mobile ? <button className={clsx(styles.iconButton, styles.accountTheme)} type="button" onClick={toggleTheme}
      aria-label={t(theme === "light" ? "切换至黑夜模式" : "切换至白天模式")} title={t(theme === "light" ? "切换至黑夜模式" : "切换至白天模式")}>
      {theme === "light" ? <Moon size={20} aria-hidden="true" /> : <Sun size={20} aria-hidden="true" />}
    </button> : null}
  </section>;
}

export function AppShell() {
  const { t, locale } = useI18n();
  const [accountOpen, setAccountOpen] = useState(false);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const mobileMenuRef = useRef<HTMLDialogElement>(null);
  const [watchPanelLayout, setWatchPanelLayout] = useState<WatchPanelLayout>({ open: false, width: 440, resizing: false });
  const accountRef = useRef<HTMLDivElement>(null);
  const avatarRef = useRef<HTMLButtonElement>(null);
  const location = useLocation();
  const navigate = useNavigate();
  const { theme, toggleTheme } = useTheme();
  const assistantBusy = useAssistantBusy();
  const { activeCount, overallProgress, openDialog } = useUploads();
  const { accountIdentity, canUpload, canManage, openLogin, sources, selectSource, scopeKey } = useAuth();
  const conversationList = useQuery({
    queryKey: ["navigation-conversations", scopeKey, locale],
    queryFn: () => loadConversations(locale),
    enabled: mobileMenuOpen,
    staleTime: 0,
  });
  const signedIn = accountIdentity && !accountIdentity.isGuest;
  const visibleNavigation = canManage ? navigation : [];
  const uploadAction = canUpload ? openDialog : !signedIn ? openLogin : () => {
    const personalSource = sources.find((source) => source.kind === "personal");
    if (personalSource) selectSource(personalSource.key);
  };

  const closeMobileMenu = () => {
    mobileMenuRef.current?.close();
    setMobileMenuOpen(false);
  };

  useEffect(() => { setAccountOpen(false); setMobileMenuOpen(false); }, [location.key]);
  useEffect(() => {
    if (!mobileMenuOpen) return;
    const dialog = mobileMenuRef.current!;
    dialog.showModal();
    const desktop = window.matchMedia("(min-width: 761px)");
    const closeOnDesktop = () => { if (desktop.matches) setMobileMenuOpen(false); };
    desktop.addEventListener("change", closeOnDesktop);
    return () => { dialog.close(); desktop.removeEventListener("change", closeOnDesktop); };
  }, [mobileMenuOpen]);
  useEffect(() => {
    const openSearch = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() !== "k" || !(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey) return;
      event.preventDefault();
      if (location.pathname === "/search") window.dispatchEvent(new Event(FOCUS_SEARCH_EVENT));
      else navigate("/search", { state: { focusComposer: true } });
    };
    document.addEventListener("keydown", openSearch);
    return () => document.removeEventListener("keydown", openSearch);
  }, [location.pathname, navigate]);
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
    <div className={styles.shell} data-app-shell data-watch-panel-open={watchPanelLayout.open}
      data-watch-panel-resizing={watchPanelLayout.resizing} style={{ "--watch-panel-width": `${watchPanelLayout.width}px` } as CSSProperties}>
      <div className={styles.primary}>
      <header className={styles.topbar} aria-label={t("全站导航")}>
        <button className={styles.mobileMenuTrigger} type="button" aria-label={t("打开侧边栏")}
          aria-expanded={mobileMenuOpen} aria-controls="mobile-navigation"
          onClick={() => { setAccountOpen(false); setMobileMenuOpen(true); }}>
          <Menu size={22} aria-hidden="true" />
        </button>
        <NavLink className={styles.brand} to="/watch" aria-label={t("Seekino 首页")}>
          <Mascot className={styles.brandMark} mood={assistantBusy ? "seek" : "hop"} size={40} />
          <strong>Seek<span className={styles.brandI}>i</span>no</strong>
        </NavLink>
        <nav className={styles.navigation} aria-label={t("主导航")}>
          {visibleNavigation.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} title={t(label)} className={({ isActive }) => clsx(styles.navItem, isActive && styles.navActive)}>
              <Icon size={16} strokeWidth={1.8} aria-hidden="true" /><span>{t(label)}</span>
            </NavLink>
          ))}
        </nav>
        {location.pathname === "/search" ? <span className={styles.searchSlot}>
          <button className={clsx(styles.searchTrigger, styles.mobileSearchTrigger)} type="button" aria-label={t("搜索名场面")}
            onClick={() => window.dispatchEvent(new Event(FOCUS_SEARCH_EVENT))}><Search size={18} aria-hidden="true" /></button>
        </span> : <NavLink className={styles.searchTrigger} to="/search" state={{ focusComposer: true }} aria-label={t("搜索名场面")} title={t("搜索名场面（{shortcut} K）", { shortcut: isMac ? "⌘" : "Ctrl" })}>
          <Search size={18} strokeWidth={1.8} aria-hidden="true" /><span>{t("描述一个画面，找回那一刻…")}</span><kbd aria-hidden="true">{isMac ? "⌘" : "Ctrl"} K</kbd>
        </NavLink>}
        <div className={styles.headerActions}>
          <span id="watch-header-actions" style={{ display: "contents" }} />
          <button className={styles.uploadButton} type="button" onClick={uploadAction}
            aria-label={canUpload ? activeCount > 0 ? t("并行传输 · {count} 项进行中，{progress}%", { count: activeCount, progress: overallProgress }) : t("并行传输 · 打开上传案台") : t(!signedIn ? "登录后上传远端视频" : "进入我的资料")}
            title={t(canUpload ? "上传视频与管理并行传输" : !signedIn ? "登录后上传远端视频" : "进入我的资料")}>
            <ArrowUpFromLine size={19} strokeWidth={1.7} aria-hidden="true" /><span>{canUpload ? activeCount > 0 ? `${overallProgress}%` : t("上传") : t(!signedIn ? "上传" : "我的资料")}</span>
            {canUpload && activeCount > 0 ? <i style={{ width: `${overallProgress}%` }} aria-hidden="true" /> : null}
          </button>
          <button className={styles.iconButton} type="button" onClick={toggleTheme} aria-label={t(theme === "light" ? "切换至黑夜模式" : "切换至白天模式")} title={t(theme === "light" ? "切换至黑夜模式" : "切换至白天模式")}>
            {theme === "light" ? <Moon size={20} strokeWidth={1.7} aria-hidden="true" /> : <Sun size={20} strokeWidth={1.7} aria-hidden="true" />}
          </button>
          <div className={styles.accountMenu} ref={accountRef} onBlur={(event) => {
            if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) setAccountOpen(false);
          }}>
            <button className={styles.accountTrigger} type="button" ref={avatarRef} onClick={() => setAccountOpen((open) => !open)} aria-label={t("打开账号菜单")} aria-expanded={accountOpen} aria-controls="anivag-account-panel" title={signedIn ? accountIdentity.email || t("我的账号") : t("登录与账号")}>
              <span className={styles.avatar}>{signedIn && accountIdentity.email ? accountIdentity.email.slice(0, 1).toUpperCase() : <UserRound size={21} strokeWidth={1.6} aria-hidden="true" />}</span><ChevronDown size={13} aria-hidden="true" />
            </button>
            {accountOpen ? <AccountControls onLogin={() => { avatarRef.current?.focus(); setAccountOpen(false); openLogin(); }} /> : null}
          </div>
        </div>
      </header>
      <div className={styles.content}>
        <Suspense fallback={<main className="page" aria-busy="true"><Mascot mood="seek" size={56} delayed /></main>}>
          <Outlet context={setWatchPanelLayout} />
        </Suspense>
      </div>
      </div>
      <div id="watch-side-panel-host" className={styles.panelHost} />
      <dialog id="mobile-navigation" ref={mobileMenuRef} className={styles.mobileDrawer} aria-label={t("手机导航")}
        onClose={() => setMobileMenuOpen(false)}
        onClick={(event) => { if (event.target === event.currentTarget) closeMobileMenu(); }}>
        {mobileMenuOpen ? <div className={styles.drawerContent}>
          <header className={styles.drawerHeader}>
            <NavLink className={styles.brand} to="/watch" aria-label={t("Seekino 首页")} onClick={closeMobileMenu}>
              <Mascot className={styles.brandMark} size={32} /><strong>Seek<span className={styles.brandI}>i</span>no</strong>
            </NavLink>
            <button className={styles.iconButton} type="button" onClick={closeMobileMenu} aria-label={t("关闭侧边栏")}><X size={20} aria-hidden="true" /></button>
          </header>
          <nav className={styles.drawerNavigation} aria-label={t("主导航")} onClick={(event) => {
            if ((event.target as Element).closest("a")) closeMobileMenu();
          }}>
            <NavLink className={styles.accountLink} to="/watch"><Play size={20} aria-hidden="true" />{t("观看")}</NavLink>
            <NavLink className={styles.accountLink} to="/search"><MessageCircle size={20} aria-hidden="true" />{t("Seekino 助手")}</NavLink>
            {visibleNavigation.map(({ to, label, icon: Icon }) => (
              <NavLink key={to} className={styles.accountLink} to={to}><Icon size={20} aria-hidden="true" />{t(label)}</NavLink>
            ))}
          </nav>
          <div className={styles.drawerActions}>
            <button className={styles.accountLink} type="button" onClick={() => { closeMobileMenu(); uploadAction(); }}>
              <ArrowUpFromLine size={20} aria-hidden="true" />
              {t(canUpload ? "上传" : !signedIn ? "登录后上传远端视频" : "进入我的资料")}
              {canUpload && activeCount > 0 ? <span>{overallProgress}%</span> : null}
            </button>
          </div>
          <nav className={styles.drawerHistory} aria-label={t("对话记录")}>
            <h2>{t("对话记录")}</h2>
            {conversationList.isPending ? <p role="status">{t("正在读取对话记录")}</p> : conversationList.isError ? <div>
              <p role="alert">{t("读取对话记录失败：{value0}", { value0: errorMessage(conversationList.error, locale) })}</p>
              <button className={styles.accountLink} type="button" onClick={() => void conversationList.refetch()}>{t("重试")}</button>
            </div> : conversationList.data.length ? conversationList.data.map((conversation) => <Link key={conversation.conversationId}
              className={styles.accountLink} to="/search" onClick={closeMobileMenu}
              state={{ conversation: { conversationId: conversation.conversationId, title: conversation.title, scopeKey } }}
              aria-label={t("打开对话 {value0}", { value0: conversation.title })} title={conversation.title}>
              <span>{conversation.title}</span>
            </Link>) : <p>{t("还没有可显示的对话。")}</p>}
          </nav>
          <AccountControls mobile onLogin={() => { closeMobileMenu(); openLogin(); }} />
        </div> : null}
      </dialog>
    </div>
  );
}
