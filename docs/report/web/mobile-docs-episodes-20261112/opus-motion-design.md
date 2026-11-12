> 2026-11-12 第二轮顾问原始输出。模型标识与用量见 [调用元数据](opus-motion.json)。保留原文供追溯；最终实施补齐无过渡/取消时的内容清理，实际行为以当前源码和验收记录为准。

# Seekino 移动端顶栏与抽屉迭代设计（PR #129 同分支）

## (a) 视觉与交互方案

**顶栏 docs 入口**
- 只显示 BookOpen 图标，不显示“文档”文字。
- 保留现有的 `title` 和 `aria-label`，值都是 `t("使用文档")`。
- 手机端点击区域固定为 44×44。当前页高亮 `aria-current` 保持不变。
- 抽屉中的“使用文档”保持原样。
- 本轮新增的 `docsLabel` span、`.docsLabel` CSS 和 `.docsLink .docsLabel` 规则全部删除。
- i18n 字典中若有本轮新增的短词 `"文档"`，请 `grep` 确认只被 docsLabel 引用后删除，其余导航文案不动。

**抽屉运动参数**

| 阶段 | 抽屉 | 遮罩 |
|---|---|---|
| 进入 | `translateX(-100%) → 0`，240ms `var(--ease-out)` | 透明度 0 → .35，同步 240ms |
| 退出 | `0 → -100%`，180ms `cubic-bezier(.4,0,1,1)`（偏 ease-in） | 同步淡出 180ms |

- 退出比进入更短，体感“出得快、进得稳”。
- 不做弹簧效果，不做条目级 stagger，只动 `transform` 和遮罩颜色。
- 没有使用 `--duration-page`（320ms），因为对抽屉偏慢。

**方案比较与选择**

- **只用纯 CSS 原生 dialog**（`@starting-style` + `display`/`overlay` 的 `allow-discrete`）：能解决 top-layer 退出期间仍然可见的问题。但内容由 `mobileMenuOpen` 条件挂载，`close()` 时 `setState(false)` 会立刻卸载内容，退出过程中会出现空白面板滑走。
- **完整的 React 关闭阶段状态机**：可以解决，但代码改动大，也和原生 `close()`/Esc 路径重复。
- **选用：原生 CSS 过渡 + 一个极小的 `drawerMounted` 保留标志。**
  - 打开时置 true。
  - 关闭后，等 dialog 自身 `transform` 的 `transitionend` 才卸载内容。
  - 若浏览器不支持该过渡，或开启了 reduced-motion，`transitionend` 不会触发，内容会继续挂载在已关闭（`display:none`）的 dialog 里。这是无害的，下次打开会直接复用。
- **重复 ID 问题**：手机实例的 AccountControls 不再输出 `id`。`id="seekino-account-panel"` 只给桌面实例，与 `aria-controls` 一一对应。

**原有行为保持不变**
- 会话查询仍为 `enabled: mobileMenuOpen`。
- `showModal`、Esc、焦点陷阱和焦点恢复都走原生 dialog，逻辑不变。
- 背景点击关闭、`location.key` 关闭、桌面断点关闭沿用原逻辑。
- 路由、历史、未发送草稿不涉及，不受影响。

**避免拦截触摸**
- `.mobileDrawer:not([open])` 及其 `::backdrop` 设置 `pointer-events: none`。退出动画期间，触摸会直接穿透到页面。

**快速关闭再开**
- 退出过程中元素仍处于显示状态，再次 `showModal()` 时 transition 会从当前位置反向重定向，不会重新走 `@starting-style`，也不会卡住。
- `drawerMounted` 再次被置 true；退出中途重开时 `transitionend` 的 `open` 判断为 true，因此不会卸载内容。

## (b) 补丁

```diff
--- a/web/src/app/AppShell.tsx
+++ b/web/src/app/AppShell.tsx
@@ function AccountControls(
-  return <section id="seekino-account-panel" className={styles.account} aria-label={t("当前账号")}>
+  // Only the desktop popover owns the id targeted by the avatar's aria-controls; the drawer copy may stay mounted.
+  return <section id={mobile ? undefined : "seekino-account-panel"} className={styles.account} aria-label={t("当前账号")}>
@@ export function AppShell() {
   const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
+  // Keeps drawer content rendered while the native close transition plays out.
+  const [drawerMounted, setDrawerMounted] = useState(false);
   const mobileMenuRef = useRef<HTMLDialogElement>(null);
@@
   useEffect(() => {
     if (!mobileMenuOpen) return;
+    setDrawerMounted(true);
     const dialog = mobileMenuRef.current!;
-    dialog.showModal();
+    if (!dialog.open) dialog.showModal();
@@
           <NavLink className={clsx(styles.iconButton, styles.docsLink)} to="/docs" aria-label={t("使用文档")} title={t("使用文档")}>
-            <BookOpen size={20} strokeWidth={1.7} aria-hidden="true" /><span className={styles.docsLabel}>{t("文档")}</span>
+            <BookOpen size={20} strokeWidth={1.7} aria-hidden="true" />
           </NavLink>
@@
       <dialog id="mobile-navigation" ref={mobileMenuRef} className={styles.mobileDrawer} aria-label={t("手机导航")}
         onClose={() => setMobileMenuOpen(false)}
+        onTransitionEnd={(event) => {
+          if (event.target !== event.currentTarget || event.pseudoElement || event.propertyName !== "transform") return;
+          if (!event.currentTarget.open) setDrawerMounted(false);
+        }}
         onClick={(event) => { if (event.target === event.currentTarget) closeMobileMenu(); }}>
-        {mobileMenuOpen ? <div className={styles.drawerContent}>
+        {mobileMenuOpen || drawerMounted ? <div className={styles.drawerContent}>
```

```diff
--- a/web/src/app/app-shell.module.css
+++ b/web/src/app/app-shell.module.css
@@
 .docsLink[aria-current="page"] { color: var(--teal-deep); background: var(--teal-wash); }
-.docsLabel { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); border: 0; white-space: nowrap; }
@@
-.mobileDrawer { position: fixed; inset: 0 auto 0 0; width: min(320px, 85vw); max-width: none; height: 100dvh; max-height: none; margin: 0; padding: 0; color: var(--ink); border: 0; background: var(--surface-raised); }
-.mobileDrawer::backdrop { background: rgb(0 0 0 / .35); }
+.mobileDrawer {
+  --drawer-in: 240ms; --drawer-out: 180ms; --drawer-ease-out: cubic-bezier(.4, 0, 1, 1);
+  position: fixed; inset: 0 auto 0 0; width: min(320px, 85vw); max-width: none; height: 100dvh; max-height: none; margin: 0; padding: 0; color: var(--ink); border: 0; background: var(--surface-raised);
+  transform: translateX(-100%);
+  transition: transform var(--drawer-out) var(--drawer-ease-out), display var(--drawer-out) allow-discrete, overlay var(--drawer-out) allow-discrete;
+}
+.mobileDrawer[open] { transform: none; transition-duration: var(--drawer-in); transition-timing-function: var(--ease-out); }
+.mobileDrawer::backdrop {
+  background-color: rgb(0 0 0 / 0);
+  transition: background-color var(--drawer-out) var(--drawer-ease-out), display var(--drawer-out) allow-discrete, overlay var(--drawer-out) allow-discrete;
+}
+.mobileDrawer[open]::backdrop { background-color: rgb(0 0 0 / .35); transition-duration: var(--drawer-in); transition-timing-function: var(--ease-out); }
+@starting-style {
+  .mobileDrawer[open] { transform: translateX(-100%); }
+  .mobileDrawer[open]::backdrop { background-color: rgb(0 0 0 / 0); }
+}
+/* A closing drawer is still painted in the top layer; never let it swallow taps. */
+.mobileDrawer:not([open]), .mobileDrawer:not([open])::backdrop { pointer-events: none; }
@@ @media (max-width: 760px) {
-  .headerActions > .docsLink { display: inline-flex; width: auto; min-width: 44px; height: 44px; min-height: 44px; padding-inline: 8px; flex-shrink: 0; }
-  .docsLink .docsLabel { position: static; width: auto; height: auto; padding: 0; margin: 0; overflow: visible; clip-path: none; font-size: .8rem; white-space: nowrap; }
+  .headerActions > .docsLink { display: inline-flex; width: 44px; min-width: 44px; height: 44px; padding: 0; flex-shrink: 0; }
 }
@@
 @media (prefers-reduced-motion: reduce) {
   .shell { transition: none; }
+  /* The global rule only covers *, ::before and ::after, so ::backdrop needs an explicit opt-out. */
+  .mobileDrawer, .mobileDrawer::backdrop { transition: none; }
 }
```

**补丁说明**
- **`if (!dialog.open)` 守卫**：退出过程中 dialog 已是非 open 状态，正常可以再次 `showModal`。守卫是为了防止 StrictMode 双调用或其他路径在已经 open 时重复调用而抛出 `InvalidStateError`。
- **`::backdrop` 必须单独处理**：全局 reduced-motion 规则只覆盖 `*`、`::before`、`::after`，不覆盖 `::backdrop`，所以要显式写 `transition: none`。

## (c) 浏览器验收（Chrome 现代版，DevTools 设备模式 390×844）

1. **顶栏图标**
   - 手机宽度下 docs 只显示图标，无文字；测得尺寸为 44×44。
   - 悬停有 `title`，读屏名称为“使用文档”。
   - 在 `/docs` 页面时有高亮。
   - 宽度 580px 和 360px 时尺寸不被压小。
   - 抽屉内“使用文档”文字仍在。
2. **进入中间帧**：DevTools Animations 面板设为 10% 速度后打开抽屉。抽屉从左侧滑入，遮罩同步从透明加深到 .35，首帧没有闪现到终点位置。
3. **退出中间帧（全部关闭路径）**：分别用关闭按钮、背景点击、Esc、点导航链接、点会话记录关闭。每种都要求面板滑出过程中内容完整，不出现空白；结束后 DOM 中 dialog 不在 top layer。
4. **触摸穿透**：在退出动画中途点击页面上的按钮，点击应生效，不被遮罩吞掉。
5. **焦点**：打开后 Tab 只在抽屉内循环；按 Esc 关闭后焦点回到汉堡按钮。
6. **快速开关**：在 10% 速度下关闭到一半时立刻重新打开。抽屉应从当前位置反向滑回，内容不卸载；之后可以正常再次关闭，连续 5 次不出现卡住或常驻遮罩。
7. **重复 ID**：抽屉打开过一次后，切到桌面宽度并打开头像菜单，执行 `document.querySelectorAll('#seekino-account-panel').length === 1`。`aria-controls` 应指向桌面面板。
8. **断点与路由**：抽屉打开时把窗口拉宽到 761px 以上，抽屉自动关闭，遮罩消失。路由跳转后抽屉关闭；搜索页未发送的草稿保留。
9. **会话查询**：在 Network 面板确认会话请求只在打开抽屉时发出，关闭状态下不发请求。
10. **reduced-motion**：在 Rendering 面板模拟 `prefers-reduced-motion: reduce`。开关都应立即完成，遮罩不淡入淡出；再次打开时内容正确，查询会刷新。

## (d) 取舍与未验证

**取舍**
- **选择保留标志，而非内容常驻或完整关闭状态机**：内容只在“打开中或退出中”挂载，首屏不会多渲染一份 AccountControls，也不会让测试里多出一份同名导航。
- **代价一**：无动画环境（不支持的浏览器或 reduced-motion）下，内容会在关闭后继续挂在隐藏的 dialog 内。手机 AccountControls 的 `busy`、`copied`、`error` 状态也会延续到下次打开（以前每次打开都会重置）。如果这一点不可接受，可以给 AccountControls 加一个按打开次数递增的 `key`。
- **代价二**：jsdom 中 `transitionend` 不会触发，单测里关闭后抽屉内容仍在 DOM 中。断言应改为检查 `dialog.open === false`，而不是检查内容消失。

**降级说明**
- `@starting-style` 以及 `display`/`overlay` 的 `allow-discrete` 依赖较新的 Chromium，Safari 支持情况需真机确认。
- 在不支持的浏览器里会降级为立即开关，功能不受影响。不能声称所有浏览器都有动画效果。

**未验证，需实测的边界**
- iOS Safari 和 Android 真机的过渡效果，以及遮罩触摸穿透。
- 安全区（safe-area）下的效果。
- 窗口跨过桌面断点时，关闭动画以桌面样式播放的观感。
- Firefox 对 `overlay` 属性的支持情况。
- StrictMode 开发模式下，effect 双调用与 `showModal` 守卫的配合。