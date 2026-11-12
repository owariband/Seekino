# 1. 问题与现状

2026-11-12 后续决定：本次中性色板造成整站观感偏离原有青瓷背景，按恢复要求由 [配色恢复 TRD](restore-celadon-palette-trd.md) / [PR #127](https://github.com/owariband/Seekino/pull/127) 恢复原色板，保留字体、动效和模型菜单。以下保留 11 月 10 日改版时的方案、取舍与验证边界。

需求：参考 Apple 设计语言，让 Seekino Web 界面更干净、简约、有质感，重点更换字体。提出日期：2026-11-10（Asia/Shanghai）。分支：`feat/apple-style-refresh`（基于 `feat/cloud-user-docs`）；PR：[#90](https://github.com/owariband/Seekino/pull/90)。状态：实现完成、本地截图验证，待合并。

**依赖。** 本分支叠加在 [Cloud 使用文档 TRD](cloud-user-docs-trd.md) 的分支上，使新文档页与欢迎卡片一并换新样式。合并顺序：先合 `feat/cloud-user-docs`，再合本分支；若前者调整，本分支 rebase 后重新截图。

**背景与触发场景。** Cloud 即将推广（比赛需要真实用户），首次访客对界面质感的第一印象直接影响是否继续试用。hypnos 认为当前界面不够简约、不够高级，尤其是字体。

**现状证据（2026-11-10 源码与截图）。**

| 因素 | 现状 | 观感影响 |
| --- | --- | --- |
| 标题字体 | `--display` 为 `ui-rounded` / SF Pro Rounded / Nunito（[global.css](../../../web/src/styles/global.css)） | 圆体偏俏皮，与“高级”相反 |
| 正文字体 | `--sans` 首选 Inter，但仓库只发布了 3.6 kB 的品牌字标子集；未安装 Inter 的设备实际落到各自系统字体 | 跨平台字形不一致 |
| 标签 | 约 50 处使用 `--mono` 等宽字体，多为 0.58–0.68rem 的大写、宽字距小标签（kicker、集数、元信息） | 像开发工具，不像消费级产品 |
| 色板 | 背景与文字带绿灰色调（`#e4eae5`、`#17312b`），Hero 为绿黑 `#10251f` | 不够中性、干净 |
| 质感 | 不透明顶栏，主按钮 10px 圆角并带彩色投影，悬停上浮 | 偏“工具感” |

**本次边界。** 只改视觉层：字体、色板、圆角、阴影、顶栏材质、按钮形状和标签排版。不改布局结构、交互、文案、组件逻辑和 API。不使用 Apple 的商标、图标或 SF Pro 字体文件：SF Pro 授权仅限 Apple 平台，因此在 Apple 设备上通过系统字体 `-apple-system` 调用，其他平台使用自托管 Inter（OFL）。

# 2. 影响因素、假设与评价指标

假设：观感主要由字体、标签排版、色调与表面材质决定；不动布局即可明显提升质感，且回归风险低。

| 指标 | 类型 | 验收标准 | 测量方式 |
| --- | --- | --- | --- |
| 字体一致性 | 结果 | Apple 设备显示 SF；非 Apple 设备加载自托管 Inter；中文落到 PingFang / 微软雅黑 | `document.fonts` 检查；强制 Inter 截图 |
| 视觉质感 | 结果 | hypnos 对改造前后截图的判断 | 同一 mock 数据下的前后截图 |
| 可读性 | 护栏 | 标签字号不低于 0.72rem；深色模式下主按钮文字可读 | 截图检查 |
| 字体体积 | 护栏 | 新增字体不超过 100 kB，且只对拉丁字符生效 | 文件大小与 `unicode-range` |
| 既有行为 | 护栏 | typecheck、全部前端测试、build 通过 | npm 命令 |

# 3. 改造方案与建模

- **字体：** `--sans` 改为 `-apple-system, BlinkMacSystemFont, "Seekino Inter", "Segoe UI Variable Text", "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Noto Sans SC", "Microsoft YaHei", sans-serif`，`--display` 与正文同族。SF 在大字号自动切换 Display 字形；Inter 通过 opsz 轴（`font-optical-sizing: auto`）达到相同效果。新增 `web/public/assets/fonts/inter-latin-opsz.woff2`（Inter 5.3.0，取自 `@fontsource-variable/inter`，72.9 kB，wght+opsz 双轴，仅拉丁 `unicode-range`），沿用仓库已有的 Inter OFL 许可证。不用 Google Fonts：中国大陆访问不稳定。
- **标签：** 44 处装饰性 `--mono` 改为 `--sans`，并加 `font-variant-numeric: tabular-nums` 保持数字对齐；低于 0.72rem 的字号提升到 0.72rem。去掉大写与宽字距。代码块、行内代码、Markdown 编辑器和代码值保留等宽字体（6 处）。
- **色板：** 中性灰白：浅色 canvas `#f5f5f7`、卡片 `#ffffff`、文字 `#1d1d1f` / `#6e6e73`；深色 canvas `#000000`、卡片 `#1c1c1e`、文字 `#f5f5f7`。Teal 保留为唯一强调色。Hero 舞台由绿黑改为中性黑。
- **材质与形状：** 顶栏改为半透明磨砂（`backdrop-filter: saturate(180%) blur(20px)`）+ 发丝线；主要按钮和 Hero 按钮改为胶囊形；Hero 主按钮改为白色胶囊；去掉悬停上浮和彩色投影；阴影改为两层低不透明度。
- **标题：** 大标题字重 700，字距 -0.022em；页面标题略放大。
- **动效（2026-11-10 追加）：** hypnos 反馈页面切换和按钮动画“生硬、突兀”。核对源码：已有 39 处 transition，无 linear 缓动，因此“缺少过渡、使用线性缓动”的说法不成立。实际原因有三：路由切换没有任何动画，页面瞬间替换；顶栏图标按钮、上传、账号菜单、导航项、Hero 与系列页按钮、关闭按钮完全没有 transition；按钮没有按下反馈。改法：新增 `--ease-out: cubic-bezier(.25, 1, .5, 1)`、`--duration-fast: 160ms`、`--duration-press: 110ms`、`--duration-page: 320ms`；48 处短时 `ease` 改用 `--ease-out`；上述控件补上颜色、背景和 transform 过渡，`:active` 缩放 0.92–0.98。路径变化时，`AppShell` 用 Web Animations API 让内容区淡入并上移 10px（320ms）。不引入 framer-motion：路由组件不需要重新挂载，避免播放页和对话状态丢失。不用 React Router 的 `viewTransition`：它需要 data router，而项目使用 `BrowserRouter`。查询参数、hash 与设置浮层不触发动画；`prefers-reduced-motion` 下不运行。动画期间的 transform 会让内容区内 `position: fixed` 的元素暂时以内容区为参照；现有这类元素都是交互后才打开的菜单、抽屉和对话框，不会在页面进入的 320ms 内出现。
- **模型菜单（2026-11-10 追加）：** hypnos 反馈展开模型配置时列表盖住按钮。原因不是 z-index：模型用途（隐藏 select 叠在自定义外观上）和对话框模型选择都是原生 `<select>`，macOS 原生菜单会把当前选项对齐到控件上。改法：给这些 select 加 `data-menu`，在支持的浏览器上用 `appearance: base-select`。菜单成为锚定在按钮下方的卡片（空间不足时翻到上方），配圆角、阴影，并沿用 160ms ease-out 淡入。语义、键盘操作与读屏仍是原生 select。Safari 与 Firefox 暂不支持，保持原生菜单。

迁移与兼容：无数据、接口或配置变化。回退方式为 revert 本分支提交。

# 4. 对照实验与结果分析

比较对象：同一 Vite 开发服务与同一份 mock 数据（游客身份、3 个公共系列），改造前后各截图一次。页面：首页（含系列列表）、首页深色、助手、系列详情、文档、设置、首页 390px。另将 `--sans` 强制设为 Inter 截图，模拟非 Apple 设备。截图属于开发过程材料，未提交仓库。

| 检查 | 结果 |
| --- | --- |
| Inter 加载 | `document.fonts`：`Seekino Inter 100 900 loaded`；字体文件 200、`font/woff2` |
| 标签字号 | 替换后最小 0.72rem |
| 深色主按钮 | 欢迎卡片主按钮文字跟随 `--canvas`，深色下为黑字配薄荷色底，可读 |
| typecheck / test / build | 全部通过（630 项测试） |
| 页面进入动画 | 浏览器实测：从 `/watch` 点到 `/docs` 时，内容区上有一个运行中的动画；首次加载不触发 |
| 按下反馈 | 浏览器实测：按住顶栏 GitHub 按钮时 `transform` 为 `matrix(0.92, …)` |
| 模型菜单遮挡按钮 | 修复前：模型用途与对话框模型选择都是原生 `<select>`，macOS 原生菜单把当前选项盖在控件上，遮住按钮（系统菜单行为，与 z-index 无关）。修复后：Chrome 157 实测 `appearance: base-select` 生效、菜单打开；设置页菜单从按钮下方展开，按钮中心命中的仍是 select、下方命中 option；对话框底部的菜单向上展开，同样不遮挡按钮 |

# 5. 试点验证与方案取舍

样式改动可以整体回退，以截图评审替代试点。未在 Windows、Android 真机上验证；其渲染以强制 Inter 的截图近似。取舍：深色模式改为纯黑 canvas 后，Hero 与页面融为一体、不再有色带分隔，这与 Apple 深色页面一致，但失去了原来的绿色品牌氛围；品牌感改由 teal 强调色和 Kino 吉祥物承担。

# 6. 结果验收与闭环

| 指标 | 结果 |
| --- | --- |
| 字体一致性 | 已实现；Apple 上为 SF，强制 Inter 截图确认非 Apple 字形 |
| 视觉质感 | 待 hypnos 评审前后截图 |
| 可读性、字体体积 | 满足（0.72rem 下限；72.9 kB，仅拉丁字符） |
| 既有行为 | 2026-11-10：typecheck 通过；32 个文件 630 项前端测试通过；build 通过 |

剩余事项：Windows / Android 真机渲染未验证；任务、上传、资料库、播放页未单独截图，只依赖全局 token 变化；hypnos 评审后可能继续调整。
