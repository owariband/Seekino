import clsx from "clsx";
import {
  Activity,
  ArrowUpFromLine,
  ChartNoAxesCombined,
  LibraryBig,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  PlaySquare,
  Search,
  Settings,
  Sun,
} from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router";

import { taskProgress } from "../features/uploads/upload-types";
import { useUploads } from "../features/uploads/UploadProvider";
import { useTheme } from "./theme";
import { useWorkspace } from "./workspace";
import styles from "./app-shell.module.css";

const navigation = [
  { to: "/watch", label: "视频观看", icon: PlaySquare },
  { to: "/search", label: "名场面检索", icon: Search },
  { to: "/library", label: "资料库", icon: LibraryBig },
  { to: "/tasks", label: "解析任务", icon: Activity },
  { to: "/usage", label: "模型消耗", icon: ChartNoAxesCombined },
  { to: "/settings", label: "设置", icon: Settings },
];

export function AppShell() {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => window.localStorage.getItem("anivag-sidebar-collapsed") === "true");
  const { theme, toggleTheme } = useTheme();
  const { mode } = useWorkspace();
  const { visibleTasks, activeCount, overallProgress, openDialog } = useUploads();
  const previewTasks = visibleTasks
    .filter((task) => !["completed", "cancelled"].includes(task.status))
    .slice(0, 3);

  useEffect(() => {
    window.localStorage.setItem("anivag-sidebar-collapsed", String(sidebarCollapsed));
  }, [sidebarCollapsed]);

  return (
    <div className={clsx(styles.shell, sidebarCollapsed && styles.sidebarCollapsed)}>
      <button
        className={styles.sidebarToggle}
        type="button"
        onClick={() => setSidebarCollapsed((current) => !current)}
        aria-label={sidebarCollapsed ? "展开导航" : "收起导航"}
        title={sidebarCollapsed ? "展开导航" : "收起导航"}
        aria-expanded={!sidebarCollapsed}
        aria-controls="anivag-sidebar"
      >
        {sidebarCollapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}
      </button>
      <aside id="anivag-sidebar" className={styles.sidebar} hidden={sidebarCollapsed}>
        <NavLink className={styles.brand} to="/watch" aria-label="anivag 首页">
          <img src="/anivag-mark.svg" alt="" />
          <strong>anivag</strong>
        </NavLink>

        <nav className={styles.navigation} aria-label="主导航">
          {navigation.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) => clsx(styles.navItem, isActive && styles.navActive)}
            >
              <Icon size={17} strokeWidth={1.8} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>

        <button className={styles.uploadDock} type="button" onClick={openDialog}>
          <div className={styles.uploadDockHeading}>
            <span><ArrowUpFromLine size={15} /> 并行传输</span>
            <b>{overallProgress}%</b>
          </div>
          {previewTasks.length > 0 ? (
            <div className={styles.microTasks}>
              {previewTasks.map((task) => (
                <div key={task.taskId}>
                  <span>EP {String(task.episodeOrder).padStart(2, "0")}</span>
                  <i><em style={{ width: `${taskProgress(task)}%` }} /></i>
                  <small>{taskProgress(task)}%</small>
                </div>
              ))}
            </div>
          ) : (
            <p>可同时放入 3 个视频</p>
          )}
          <div className={styles.uploadDockFooter}>
            <span>{activeCount > 0 ? `${activeCount} 项进行中` : "打开上传案台"}</span>
            <ArrowUpFromLine size={13} />
          </div>
        </button>

        <footer className={styles.sidebarFooter}>
          <div className={clsx(styles.runtime, styles[`runtime${mode}`])}>
            <i />
            <span>{mode === "live" ? "API 已连接" : mode === "offline" ? "API 不可用" : "检查服务"}</span>
          </div>
          <button type="button" onClick={toggleTheme} aria-label={theme === "light" ? "切换至黑夜模式" : "切换至白天模式"}>
            {theme === "light" ? <Moon size={16} /> : <Sun size={16} />}
          </button>
        </footer>
      </aside>

      <div className={styles.content}>
        <header className={styles.mobileHeader}>
          <NavLink className={styles.mobileBrand} to="/watch"><img src="/anivag-mark.svg" alt="" /><strong>anivag</strong></NavLink>
          <div>
            <button type="button" onClick={openDialog} aria-label="打开上传面板"><ArrowUpFromLine size={17} /><span>{activeCount || "上传"}</span></button>
            <button type="button" onClick={toggleTheme} aria-label="切换明暗主题">{theme === "light" ? <Moon size={17} /> : <Sun size={17} />}</button>
          </div>
        </header>
        <Outlet />
      </div>

      <nav className={styles.mobileNav} aria-label="移动端主导航">
        {navigation.map(({ to, label, icon: Icon }) => (
          <NavLink key={to} to={to} className={({ isActive }) => isActive ? styles.mobileActive : undefined}>
            <Icon size={18} /><span>{label}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
