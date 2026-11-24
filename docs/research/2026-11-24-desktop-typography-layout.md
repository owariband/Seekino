# Seekino 桌面版排版调研

2026-11-24 · 以桌面版 0.2.9 为基线，面向 macOS 与 Windows 11 的简体中文工作区

**建议选 A：Mac 用 SF Pro 搭配苹方简体，Windows 用 Segoe UI Variable 搭配 Microsoft YaHei UI。** 中英文分别选择合适的字形，两端共享相同的阅读顺序。资料树里先找到影片，搜索结果里先读到命中的那句台词，回看时一眼能找到时间码。品牌辨识度继续交给 Seekino 字标、青绿色和原片画面。Web 本身也把 macOS 系统字体放在字体栈首位，Inter 排在其后。[Web 实际字体栈](https://github.com/owariband/Seekino/blob/f121debcc5b13c250bc0c15055c58351f8d3553e/web/src/styles/global.css#L75)

## 为什么现在的界面不协调

| 场景 | 0.2.9 的证据 | 对观感的影响与改法 |
| --- | --- | --- |
| 资料库首屏 | 默认窗口宽 1080，资料树约宽 205。有封面时，片名列约宽 334，字号却按整窗宽度的 5% 算到 **54**，加粗、字距 −1，最多两行。资料树名称为 13。 | 长中文片名一行约容六个全宽字，画面宽、文字列窄时尤其突兀。让片名跟随**文字列**的实际宽度。约 334 宽先试 26/34，有充足空间再试 32/40，封面画面仍可以大。[封面标题](https://github.com/owariband/Seekino/blob/f121debcc5b13c250bc0c15055c58351f8d3553e/desktop/ui/BrowserHero.qml#L101) · [页边距](https://github.com/owariband/Seekino/blob/f121debcc5b13c250bc0c15055c58351f8d3553e/desktop/ui/BrowserPane.qml#L20) |
| 搜索结果 | 常规命中正文为 15，时间、系列、文件名和“回看原片”均为 12；时间码、关键词和回看入口都用了青色。 | 第一眼需要找到命中的话。把台词提到首行，关键词用青色与字重强调；来源与时间合并为次行，平时用中性色。保留键盘焦点和清楚的回看入口，长文件名只在需要区分来源时单列。[结果卡](https://github.com/owariband/Seekino/blob/f121debcc5b13c250bc0c15055c58351f8d3553e/desktop/ui/DocumentRow.qml#L53) |
| 设置与资料树 | 设置已有 22 的标题、15 的组名、13 的字段名及 14 的输入字。字段内部间距 4，若干相邻组间只有 6–10。资料树行高 28，名称 13、计数 11，已有收起入口。 | 设置最值得调的是距离：标签与输入框保持靠近，“服务地址 / 鉴权 / 模型”之间留足空间。树保留紧凑行与折叠，长片名可适当增加行高；小窗优先把宽度让给台词、视频或表单。[模型设置](https://github.com/owariband/Seekino/blob/f121debcc5b13c250bc0c15055c58351f8d3553e/desktop/ui/Preferences.qml#L146) · [资料树](https://github.com/owariband/Seekino/blob/f121debcc5b13c250bc0c15055c58351f8d3553e/desktop/ui/TreeRow.qml#L13) |

上表的桌面尺寸为 QML 逻辑像素。“字号/行高”是建议的试排值；默认窗口和 334 宽的例子按当前布局公式计算。0.2.9 安装包的 24 个 QML 文件与 0.2.5 验收包逐字节相同，因此也对照了当时的 Mac 实拍和离屏搜索结果图。[既有内页验收记录](https://github.com/owariband/Seekino/blob/f121debcc5b13c250bc0c15055c58351f8d3553e/docs/report/desktop/native/2026-11-23-acceptance.md)

## 三套值得实际比较的组合

| 方案 | 中文与西文 | 适合的地方与代价 |
| --- | --- | --- |
| **A · 平台原生，首选** | Mac 简中用苹方 SC，西文用系统 SF；Win 11 简中用 Microsoft YaHei UI，西文用 Segoe UI Variable。 | 适合资料树、任务、设置、搜索、播放器。菜单、目录与正文容易连成同一个桌面应用。跨平台允许字形稍有不同，标题、正文、时间等角色保持一致。微软雅黑 UI 公布的字重为 Light、Regular、Bold，Windows 中文强调要按实际字体试排。[Apple 字体规范](https://developer.apple.com/design/human-interface-guidelines/typography) · [Apple 字体目录](https://developer.apple.com/fonts/system-fonts/?q=PingFang) · [Windows 字体规范](https://learn.microsoft.com/en-us/windows/apps/design/signature-experiences/typography) |
| **B · 跨平台统一** | 西文和数字用 Inter，简中用 Noto Sans CJK SC。思源黑体简体也可作为备选，样张 B 使用 Noto。 | 适合把两端的列表宽度、型号和时间码做得尽量一致。Inter 的等宽数字对时间列有用。发布要提供足够的中文字库、所需字重和相应许可，验收包体与任意导入片名的覆盖。现有桌面内置的 Inter 字标字库只适合画 Seekino 标志。[Inter](https://rsms.me/inter/) · [Noto CJK](https://github.com/notofonts/noto-cjk) · [思源黑体](https://github.com/adobe-fonts/source-han-sans) · [现有字标](https://github.com/owariband/Seekino/blob/f121debcc5b13c250bc0c15055c58351f8d3553e/desktop/ui/Brand.qml#L8) |
| **C · 片名用宋体，工作区用 A** | 只给系列封面上的中文片名用宋体，Mac 样张用 Songti SC。需要让两端片名一致时，可评估思源宋体 SC。摘要、时间码、侧栏和控件继续用 A。 | 最有片集目录或影展片单的气质，适合内容浏览。只放在足够大、背景相对平整的片名位置；密集小字、繁杂封面和很长的片名仍以易读为先。IBM Carbon 将工作区文字和更具表现力的编辑文字分开设计，这一做法可借鉴，具体宋体搭配是对 Seekino 的提案。[Apple 字体目录](https://developer.apple.com/fonts/system-fonts/?q=Songti) · [思源宋体](https://github.com/adobe-fonts/source-han-serif) · [Carbon 字体角色](https://www.carbondesignsystem.com/building-blocks/foundations/typography/overview) |

## 字号和布局怎么落到同一个窗口

Apple 的 macOS 内建样式中，正文为 13/16，一级标题 22/26，大标题 26/32，单位为 pt。Microsoft 的 Windows 正文为 14/20，副标题 20/28，标题 28/36，单位为有效像素 epx。这些是对照基准。下面是给 Seekino 的首轮试排值，按 QML 逻辑像素给出，实际高度须用对应平台字体复核。[Apple HIG](https://developer.apple.com/design/human-interface-guidelines/typography) · [Microsoft Learn](https://learn.microsoft.com/en-us/windows/apps/design/signature-experiences/typography) · [Qt 高 DPI 坐标](https://doc.qt.io/qt-6/highdpi.html)

| 文字角色 | Mac 首轮字号/行高 | Windows 首轮字号/行高 | 使用方式 |
| --- | ---: | ---: | --- |
| 封面片名 | 26/34，宽栏 32/40 | 28/36，宽栏 32/40 | 由片名列触发尺寸切换；两行以上优先调整版式 |
| 普通页标题 | 22/28 | 24/32 | 一页一个标题；工具栏避免再重复一遍大标题 |
| 命中台词、正文 | 15/23 | 15/23 | 默认常规体，搜索词局部强调；行间能容下汉字与标点 |
| 资料树、字段名 | 13/18 | 14/20 | 名称常规体，选中态优先用底色；表单组名才加重 |
| 时间码、出处 | 12/16 | 12/16 | 时间码启用等宽数字，出处保留比例字宽 |

**侧栏学 Typora 的用法。** Typora 把文件树放在内容一侧，Mac 可通过标题栏或菜单收起，Windows 也能从菜单或状态栏切换。Seekino 已经有可收起的树，可保留“系列 → 视频”的简短路径，把命中片段列表放进主区。Mac 保留标题栏切换入口和系统窗口控件，Windows 保留本平台的窗口控件及导航入口。两端共用字号角色和间距规则。[Typora 文件管理](https://support.typora.io/File-Management/) · [Apple 侧栏指南](https://developer.apple.com/design/human-interface-guidelines/sidebars)

**把空间留在需要断句的位置。** 建议先试主栏左右 24、窄窗 16，字段名到说明或输入框 4–8，组与组之间 16–24。搜索结果的台词、出处和输入区共用同一条文字左边线；播放器则把时间读数留在进度条旁，避免控制栏与命中证据争空间。4 像素节奏来自 Fluent 的常用间距体系，以上组合和具体数值是本次设计建议。[Fluent 2 布局](https://fluent2.microsoft.design/layout) · [Apple 布局指南](https://developer.apple.com/design/human-interface-guidelines/layout)

## 本机实测与验收边界

使用已安装 0.2.9 的 Qt 6.10.3、Cocoa 后端和 Basic 控件风格，单独运行无窗口排版探针。默认字体下，“海岸线以北”的字形运行使用系统简中字族，英文和数字使用系统西文字族。在 20 逻辑像素上，同一句中文以 400、500、600 渲染的三张小样逐像素相同，700 有区别。换成显式 PingFang SC 后，400、500、600 三种结果均有区别；英文 OpenAI 使用默认字族时三种也能区分。因此 A 方案中，重要中文标题值得显式选择适配的中文字体，并在 Qt 窗口核对。测量用的是 QTextLayout 与 QPainter，不能将结果推广为每台机器、每个汉字或所有 QML 控件的行为。

同一字体在 12 逻辑像素下，等长的两个时间码 `0:11.111` 与 `0:58.888` 默认宽 **41.72 / 52.53**，启用 OpenType `tnum` 后均为 **52.41**。这给播放器采用等宽数字一个具体理由；来源文件名和普通英文无需跟着换成等宽字体。Web 播放器已有对应设置。[Web 时间码](https://github.com/owariband/Seekino/blob/f121debcc5b13c250bc0c15055c58351f8d3553e/web/src/features/player/watch.module.css#L44) · [Qt Quick 字体特性](https://doc.qt.io/qt-6/qml-qtquick-text.html#font.features-prop)

如果进入改版，先在 1080×700 窗口比对资料库和搜索结果，再在 640×440 验证资料树收起、长中文片名换行、设置表单滚动。Windows 另用 125% 与 150% 显示缩放核对字体回退、裁切和时间码对齐。本次交付范围为调研和浏览器排版样张，Windows 真机字形与调整后的原生窗口仍待实测。
