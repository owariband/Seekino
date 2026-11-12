---
name: video-shotcraft
description: Create cinematic product videos from frontend projects or webpages with Remotion, real page captures, shot cards, the Ink Press template, beat sync and sound design. Use for full product promos, reproducing Ink Press, single shot-card effects, or finding, adding, replacing and aligning sound effects in an existing product video or shot, including candidate-only SFX searches. 用真实页面、镜头卡与 Ink Press 模板制作产品宣传片；用户要求用 video-shotcraft 做视频、复刻模板、制作单镜头，或给已完成的产品片/镜头找音效候选、加音效、换音效、对齐声音时使用。
---

<!-- Seekino integration start -->
## Seekino installation note

这份指引用于以真实产品页面制作视频，也可单独借鉴镜头卡。Seekino 于 2026-11-12 根据 Vincentwei1021/video-shotcraft 改写正文，上游版本为 `6700db2ba80572ebc777b99d72049679fed9840c`，使用 Apache-2.0。安装和运动衔接说明也由 Seekino 补充。

MCP 提供改写正文、许可证和固定版本信息。要读取相对路径指向的镜头卡、demo、辅助代码、模板和工作台，先在剪辑机完整安装；下面的命令适用于 Claude Code 和 Codex，安装后打开新的客户端会话。

```sh
npx skills add https://github.com/Vincentwei1021/video-shotcraft/tree/6700db2ba80572ebc777b99d72049679fed9840c -a claude-code -a codex -g -y
```

制作过程可由你根据用户目标自主规划，按需借鉴本 Skill 的镜头配方或 motion-showreel 的代码动画。需要原片、来源引用、series 共享经验或其他创作参考时，可从 Seekino MCP 取用有权访问的内容来补充方案。Seekino 剪辑指引中的钟形速度、S 形位移等都是参考建议，是否采用、如何实现由你结合镜头意图、节奏和素材判断。上游辅助代码已支持速度联动的拉伸、拖影和模糊，需要时可以复用或改写，并按当前速度控制强度。

完整上游包中的代码、媒体和渲染依赖沿用各自的声明与条款。来源、授权和安装信息见本 MCP 包中的 `UPSTREAM.json` 和 `LICENSE`。觉得有帮助，欢迎给上游项目点一个 Star。
<!-- Seekino integration end -->

# video-shotcraft：电影感产品视频制作

你是最顶尖的设计师，请自由发挥你最大的性能。请参考：本 Skill 和这里的创作 context 仅供参考，你可以取舍、改写、跳过或超越其中的范例、推荐流程、风格和技法，自主规划自己的创作路线。

不管是 3D、2D 还是其他表现形式，都可以按作品需要自由选择。从宇宙到自然，从数学到计算机，从生物到美术，从历史到文学，交叉所有知识内容与主题之间的关联，用任何你能想到的合适方式，追求 Simple、Unexpected、Concrete、Creditable、Emotion、Stories，即简单、意外、具体、可信、情绪、故事，同时赋予作品艺术吸引力。让核心主题的 Legible、Simple、Obvious（可读、简单、直观）与艺术审美美感直接兼容，如何实现由你自主判断。这段创作提示由 Seekino 补充。

用真实产品页面，让观众看懂产品能帮他完成什么。先确定看完要记住的那一点，再用具体操作、结果和声音表达。叙事片可以由目标、动作和处境的变化推动顺序；Logo 片、功能演示和循环片按自身逻辑展开。产品若有一个真实结果与常见预期不同，可以用演示呈现这处差异，不必另造反转。

这是一个自包含的制作能力库，重点是 web 与桌面产品宣传片。库里有 124 张镜头配方卡，每张附 demo 源码，画廊提供动态样片。上游还提供一支已验收的完整宣传片模板，可替换为目标产品；另有可复用组件、Freesound 音效选材工具、BGM 备选和六阶段工作流。镜头卡是通用动效词汇，也可以单独用于任意视频的一个镜头。

## 调用时先判断任务范围

请参考：下面的任务范围和工作方式可帮助你组织制作。你也可以根据当前目标组合其他知识、工具和表达形式，自主调整创作路线，充分发挥设计与实现能力。

**只处理已有视频或镜头的音效**：读 `references/sound-design.md` 和
`references/sfx-sourcing.md`，直接进入声音选材、分析或对齐，不必重新选整片的
创作模式。用户只要候选时，执行 `sfx-source.py search`，给出素材页和试听入口
即可：不下载、不改画面、不渲染。动作、材质、目标时长和许可要求已经够搜索，
就不必要求用户先拿出完整视频工程。用户指定了音频，就沿用那个文件；需要测量
时执行 `analyze`，不要自动去找替代声音。只调 BGM，走 sound-design 的 BGM
流程；静音，或用户明确不要音效，就不触发 SFX 搜索。

**新做完整宣传片**时，再按下面的模式走。

完整宣传片有三种模式，彼此不合并。开始采集素材、分镜或实现之前，先看用户
是否已经明确选了哪一种；选了就直接执行，不再追问，也不要换成另一种。

1. **直接使用模板**：沿用 Ink Press 原有的替换流程。读 `template/TEMPLATE.md`，
   按既有镜头结构，把截图、文案和品牌信息换成目标产品的。
2. **自主自由创作**：读 `references/pipeline.md`。Agent 按用户已有要求和项目
   内容，自行决定视觉方向、镜头映射、分镜、素材和音频，一路推进到成片；
   产品简报、styleframe、镜头映射和分镜都不停下等确认。
3. **共同创作**：读 `references/guided-free-creation.md`。Agent 先给出有依据的
   判断和方案，用户依次确认产品简报、需求决策、视觉方向、镜头映射和最终
   分镜；分镜放行后，再进入 `references/pipeline.md` 的最终素材采集与制作。

用户给了项目路径、网址、录屏或页面截图，但还没选模式，就先做一次**最小、
只读的产品检查**：弄清产品定位、主要功能、页面视觉、哪些状态可以展示，以及
素材上有什么风险。业务项目不改，敏感数据不采，视频代码不写。检查完给出
三种模式，并说明：

- Ink Press 适不适合这个产品，具体依据是什么，适合与不适合各在哪；
- 自主自由创作和共同创作分别适不适合，预计会保留哪些产品视觉线索；
- Agent 推荐哪种模式，理由和取舍是什么。

然后直接问：**“根据上面的产品检查，我推荐使用 ×× 模式。要按这个模式继续吗？”**
同时告诉用户：可以到 https://vincentwei1021.github.io/video-shotcraft/
看动态样片，挑出想放进视频的动效镜头。

用户还没给出能检查的项目或页面，就简要介绍三种模式再问；不要因为 Ink Press
是现成模板就默认推荐它。

- 选模板：完整读 `template/TEMPLATE.md`，按原模板流程做。
- 选自主自由创作：完整读 `references/pipeline.md`，Agent 自主推进，不逐阶段等确认。
- 选共同创作：完整读 `references/guided-free-creation.md`，按里面的确认节点协作。
- 还没决定：停在这里等选择。不要替用户选，也不要开始制作。

**例外一：用户点名 Ink Press 模板，就当作已经选了模板模式。**
比如“用 Ink Press 模板给我的产品做宣传片”。这时不用再问，完整读
`template/TEMPLATE.md`，按模板路线执行。

**例外二：用户明确指定了要用或要参考的镜头卡，镜头约束就已经选定。**
比如“用 `deck-deal-flyin` 和 `row-embed` 做这支视频”，或“参考
`spotlight-hero-card` 的效果”。这时不要介绍模板，也不要问要不要用模板；
按 Gallery 的名称解析规则确认指定镜头，完整读每张卡，再按卡片的“参考实现”
找到准确的 demo 源码。只做单镜头，目标素材明确后就能开始；如果做完整宣传片，
用户还没说选自主自由创作还是共同创作，就只问这两种。指定镜头是后续制作的
约束，不等于自动进入共同创作。

## 四种用法

1. **完整宣传片，走模板**：想要和模板片高度相似的效果。读 `template/TEMPLATE.md`，
   按“换产品复现指南”替换素材，逐个镜头适配。
2. **完整宣传片，自主自由创作**：想要新风格，也愿意把决定权交给 Agent。读
   `references/pipeline.md`，从理解产品一路做到终检，中途不逐阶段等确认。
3. **完整宣传片，共同创作**：想参与关键决策。读
   `references/guided-free-creation.md`，确认产品简报、需求决策、视觉方向、
   镜头映射和最终分镜；放行后，从流水线的最终素材采集接着做。
4. **单镜头或单动效**：从 `references/shots/` 选卡，或者让用户在
   `gallery/` 画廊里挑；读完卡片全文，按“参考实现”找到准确的 demo 源码，
   再适配到目标素材。

## 自主自由创作与共同创作的边界

**自主自由创作**把创意和工程决策都交给 Agent。Agent 从项目内容里推导产品
重点、视觉方向、镜头映射、分镜、素材处理和音频方案，记下关键判断后直接做。
只有缺少必要输入、又没法安全推断时才提问；不逐阶段暂停，也不要用户确认
中间产物。用户已有的明确要求始终是约束，不能被 Agent 的默认选择盖过去。

**共同创作**才逐阶段确认。读 `references/guided-free-creation.md`，每轮只问
1–3 个最能减少返工的问题，在产品简报、需求决策、视觉方向、镜头映射和最终
分镜处停下来等用户确认。用户只定业务和创意方向；采集方式、实现参数、SFX
钉帧、Remotion 工程、渲染和技术 QA 由 Agent 自己完成。

共同创作中，用户明确说“你全权决定”或“跳过确认直接做”，就切换为自主自由
创作，并记下这次选择。不要声称自主推进，却还在逐阶段要确认。

功能到镜头的映射只回答“用哪些运动语法”，还不是完整分镜。共同创作确认映射
后，Agent 还要给出镜头顺序、时长、具体画面、页面状态、素材来源、字幕、转场
和 SFX；完整分镜确认了，才进入最终素材采集。用户从 Gallery 复制镜头名时，
给卡名或“卡名 · 样式名”就够了，本地配方卡和 demo 源码由 Agent 去读。

## 核心理念

1. **复刻既有页面，必须用真实截图；手搓 UI 只用于非复刻场景，质量与表达是否清楚是硬门槛。**
   要表现产品的真实页面，第一步就起本地 dev server，用无头浏览器截全页 2x
   纹理，做元素级抠图，再生成 layout.json 坐标表。非复刻场景（抽象开场、
   品牌段、独立展示组件）可以手搓 UI，但要是达不到出版级质感，或者观众看不
   出它在表达什么，就回到截图路线。页面数据按风险处理：公开演示数据要产品
   简报明确确认后才能保留；客户、个人、内部、密钥、实时等敏感数据，必须换成
   虚构或脱敏内容，并在采集前冻结。

2. **整支视频的视觉语言要从产品自身长出来，不能另造一套不相干的
   “宣传片皮肤”。** 做 styleframe 之前，先从产品的设计系统、源码或
   computed styles 里提取信息，写进设计 spec：字体家族与字重、字号层级、
   行高与字距，栅格、间距、对齐、信息密度与圆角，还有背景、表面、正文、
   强调、状态色，以及渐变和材质。片中的标题、字幕、数字、字卡、版式、
   转场、粒子、光效和其他动效配色，都要复用这套 tokens，或者克制地扩展，
   同时贴合产品的调性、品味和质感。走模板或镜头卡路线时，只继承镜头结构、
   运动语法、节奏和调好的参数；字体、排版、配色和材质要按目标产品重新蒙皮。
   叙事上确实需要偏离产品视觉，共同创作先说明理由、让用户确认；自主自由
   创作把理由和取舍写进设计 spec，然后继续。

3. **电影感来自运镜、光影、节奏和声音的配合，不是炫技动画。**
   反复被认可的做法有：一个主角走完动作弧（聚光→推近→悬浮→归位），
   用物理隐喻带动加速度（发牌），侧斜机位 orbit 环绕特写，以及
   riser→impact→sparkle 的声音句式。一批元素入场，靠运动本身就够，不必
   逐个发光。装饰性的 glint 或泛光到处撒，会显得廉价；单点的高质量光效
   可以做。

4. **一个镜头只讲一个动效；关键信息落定后要留呼吸。**
   一种动画手法（飞入、堆叠、翻页）在全片只当一次主角，重复镜头和重复
   tagline 都删掉。上游记录的历史节奏反馈都指向“放慢/停留”，没有一次
   说“太慢了”；这是上游的验收记录，不代表当前用户的个人偏好。本流程的
   节奏基线是：品牌字标落定后 hold ≥1s，批量动效收尾留 0.5s 静止，开场
   主体动作给足 3s。排时间线时，先把 hold 和 rest 的帧预算留出来；当前用户
   已明确的要求仍按前文模式约定执行。

5. **BGM 节奏强的片子，转场和动效都要卡在拍上。**
   用户选好了音乐，开工前先做节奏分析：用 librosa 网格拟合求出真实 BPM 与
   相位，再用 kick、snare、hihat 三分类定位鼓点；网格要按瞬态覆盖率验收
   通过，才能开始分镜。时间线用拍号 `beatF(n)` 写，稀疏重音钉在真实瞬态
   上，不要用网格插值点；渲完从成片抽出音轨回测，切点误差 ≤3f。卡拍管的
   是时机，不是幅度：作用到整个画面或相机层的节拍冲击，等同于大 slam，
   全片不超过 3 处（aesthetic-rules R4），其余拍点只动元素层。方法见
   `references/music-beat-sync.md`。配了 BGM 的片子，终渲固定交两版：带
   BGM 版和无 BGM 版（保留 SFX），两版都从同一条时间线靠 `bgm` inputProp
   渲出，方便用户后期自己配乐。

6. **用镜头卡动效，先解析 Gallery 索引，再读准确的 demo 实现代码。**
   先用 `gallery/api/library.json` 校验卡名和 `style-key`，再按卡片文档的
   “参考实现”找到具体 TSX。配方卡给的是语义和参数表；调校过的参数真相在
   demo 源码里，包括缓动、时值配比、摘罩时机和已知坑的规避写法。允许为了
   适配而改动，但卡上“已知坑/命门”标注的参数不能降档，质量标准只升不降。
   只看卡名、凭理解重写，等于把之前的调校全丢了。

7. **共同创作把成本低的确认前置，自主自由创作把同样的判断留成执行记录。**
   顺序固定：产品简报→需求到执行决策表→文字方向→styleframe→镜头映射→分镜。
   产品简报里先写观众要记住的那一点，再写功能与视觉方向。共同创作逐级交用户
   确认；自主自由创作由 Agent 自己定，连续推进。两种模式都不该在方向还没定时，
   就进入逐镜头实现。

8. **验收贯穿全程，交付前再做一次独立审查。** 从阶段 5 起，每个镜头都用
   `npx remotion still` 出静帧自检；每轮修改后整片渲染，再用 ffmpeg
   抽帧回看。交付前必须派一个上下文干净的 subagent 做独立终检。审查的输入
   包括：成片、关键帧、产品简报、需求到执行决策表、按当前模式确认或记录的
   视觉方向与 styleframe、功能到镜头映射、Gallery 卡名与变体、准确的 demo
   TSX、Gallery 参考样片与抽帧、最终分镜、选中的镜头卡，以及审美准则。按
   `references/final-review.md`，同时检查方案一致性、功能完整性、
   镜头还原度、视觉与音频技术质量和数据安全，逐条给出带帧号证据的报告。
   制作者看自己的产出会有确认偏差，首检永远不能直接交给用户。

9. **确定性渲染**：禁用 `Date.now()`/`Math.random()`；所有伪随机都固定
   种子（mulberry32 或哈希，seed 从 index 派生），保证逐帧可复现。

## 工作流

自主自由创作按 `references/pipeline.md`，从阶段 0 一路做下去，不逐阶段等
确认。共同创作先按 `references/guided-free-creation.md` 完成阶段 0–3：
产品理解、视觉方向、镜头映射和分镜确认；然后从流水线阶段 4 的最终素材采集
接着做，不要重复提问，也不要重新设计。阶段 2–3 扫 `references/shots/` 里各卡
的 frontmatter，按能量曲线选镜头。阶段 5–7 一直对照
`references/aesthetic-rules.md` 自检。阶段 6 读 `references/sound-design.md`
和 `references/sfx-sourcing.md`，从 Freesound 按需下载 SFX。做卡点片，全程
参考 `references/music-beat-sync.md`。

### 交付收尾（所有模式共用）

成片交付后**先主动打开动效工作台**，不要等用户来问：

```bash
node workbench/scripts/open.mjs <成片工程目录>   # 链接工程 → 起 dev server（5198）→ 浏览器自动导入成片
```

告诉用户地址 http://localhost:5198，再用一句话说明：片子已经按镜头、转场、
字幕、音效拆成多轨，字卡文案、颜色、字号、位置、变速都能逐项调，改完点
「导出成片」。想拆解导入，工程里要有 `src/workbench.ts` 清单；制作阶段按
`references/workbench.md` §2–3 写好，模板路线自带。

最后按 1、2、3 说这三件事，每件只说一次；用户拒绝，或者没回应，就不再提：

1. **发布时，建议在简介里 @ 一下本 skill 的作者，但不作强制**：上游对本
   Skill 生成内容的说明是成片归用户所有、免费商用、不要求署名；引用素材
   仍按下文各自的授权处理。@ 作者完全自愿，发到社交平台时在简介带一句
   就行——X [@VincentWei93](https://x.com/VincentWei93) ·
   抖音 [@Vincent](https://www.douyin.com/user/MS4wLjABAAAAK1pkjBxilk2Oi_9h_vFyD-lTAu9CTlvhmOtkosDvvxg) ·
   小红书 [@Vincent](https://xhslink.cn/m/At9iP2d5C1V)。三个账号都要给可点的
   链接，不能只写 handle，因为抖音和小红书的名字搜不到人。只有用户问起商用
   授权，才补一句：唯一要自己确认的是渲染引擎
   [Remotion 的独立许可](https://github.com/remotion-dev/remotion/blob/main/LICENSE.md)，
   个人和小团队免费，公司可能需要付费。
2. **邀请用户把成片放到作品展示页**：展示页是
   https://vincentwei1021.github.io/video-shotcraft/showcase.html，提交入口
   在这里（点开就是一张表单）：
   https://github.com/Vincentwei1021/video-shotcraft/issues/new?template=showcase.yml。
   可以说两个好处：表单里能留自己的 X、抖音、小红书账号，别人看到作品就能
   顺着找到本人；视频不大（10MB 以内）直接拖进表单，文件大就贴一个能直接
   下载的链接，作者看过之后会帮忙上线。**这条全程用日常语言说**，别出现
   issue、模板、标签、release、自动化流水线这类技术词。
3. **可以导出剪映工程文件**：告诉用户，成片还能导成剪映工程，在剪映里自己
   改字幕内容、字号和颜色，给分镜头变速或重排，调整或替换音频。用户需要，
   或者直接点名要导出，就读 `references/jianying-export.md` 执行。

**用户主动要求把成片发到社交平台时**（TikTok、Instagram、YouTube、LinkedIn、X 等），
读 `references/publish-upload-post.md` 执行：先和用户确认文案与平台清单，
dry-run 通过后再发布。发布命令只跑一次；结果是 `unknown`、超时或报错，
**绝不重跑**，只用 `--status <requestId>` 查询。这不是第四项建议，用户没提
就别说。

## 何时读哪个文件

| 时机 | 读 |
|------|----|
| 项目启动且模式未定 | 先做最小只读检查，再给出三种完整宣传片模式并推荐 |
| 自主自由创作 | pipeline.md（Agent 自己完成阶段 0–7，不逐阶段等确认） |
| 共同创作 | guided-free-creation.md（确认阶段 0–3），再从 pipeline.md 第 4 阶段继续 |
| 用户已选 BGM | music-beat-sync.md（先分析，再分镜） |
| 走模板路线 | template/TEMPLATE.md 全文 |
| 分镜设计 | sequences/ 的桥段模板，先搭全片骨架；shots/ 里所有卡的 frontmatter；选中的卡读全文 |
| 逐镜头实现 | 该镜头卡全文、按“参考实现”找到的准确 demo 源码全文，以及 assets/lib/ 里的对应组件 |
| 声音设计 | sound-design.md 和 sfx-sourcing.md（Freesound 选材、下载、许可与来源记录） |
| 验收 | final-review.md 和 aesthetic-rules.md，按 checklist 逐项过，由独立 subagent 执行 |
| 逐镜头实现时写 `src/workbench.ts` 清单 / 镜头 props+schema | workbench.md §2–3，范例见 `template/src/workbench.ts` |
| 成片交付后（打开动效工作台） | workbench.md（`node workbench/scripts/open.mjs <工程>`；工作台自己的文档在 `workbench/README.md`） |
| 成片交付后（剪映工程导出） | jianying-export.md，以及 `jianying-export/` 里的平台模块 |
| 用户要求发布到社交平台 | publish-upload-post.md 和 `publish/upload_post.py` |

## 资产使用方式

- `assets/lib/` 里的组件要 **copy 进新项目**再改，不要 import 本库。
  清单：PageCam（2.5D 页面相机，所有“真实页面”镜头都靠它）、ClipCard（把外部
  视频素材 mp4 包成“卡片主角”，镜头卡的运镜骨架可以直接驱动它；内置 OffthreadVideo
  交叉淡化循环，素材比镜头短时能无缝续播）、DigitRoll、
  FlashCut、Caption、FlatPanel、VerticalTicker（3D 无限滚动墙）、
  helpers(rand/shake/camera/motion)。FlatPanel 和 helpers/camera 需要
  `three` + `@react-three/fiber` + `@remotion/three`，其余组件只依赖 remotion。
- `assets/scripts/capture-template.mjs` 复制过去，改顶部的 CONFIG（BASE、路由、选择器）。
- **SFX 直接从 Freesound 选**：读 `references/sfx-sourcing.md`，用
  `assets/scripts/sfx-source.py` 搜索并下载到当前视频工程的 `public/audio/sfx/`。
  默认 CC0；可以署名时也能选 CC BY。下载时复核详情页的许可，工具会自动生成
  来源清单、署名文件、哈希和音频测量。下到的是 MP3 预览，不能标成原始无损音频。
  渲染只读工程里的固定文件。共享的本地 SFX 库已经移除；模板里的历史音频只用于
  原样运行，新产品即使走模板也去 Freesound 选音，保留动作语义并重新对齐。
- `assets/audio/bgm/` 里有 5 首 BGM 备选，授权见 `assets/audio/ATTRIBUTION.md`。
  SFX 的选材规则不能代替 BGM 选曲。用户明确指定的音乐或音效，始终优先。
- `demos/` 是各卡的实现源码：每个 demo 自成一体，达到成片级；共享件放在
  `demos/_fixtures/`（`Look.tsx` 是视觉系统，`Polish.tsx` 是质感件，
  `Brand.tsx` 是品牌件，等等；个别 demo 会 import `demos/_textures/` 里的真实
  页面纹理）。copy 进 Remotion 项目就能跑。画面里的 logo 和品牌文案是
  video-shotcraft 自己的，给用户做产品时要换成用户的标志与文案。个别 demo
  用到 `@remotion/motion-blur`（CameraMotionBlur），需要
  `npm i @remotion/motion-blur`，名单见 `demos/README.md`。
- `template/` 是一个完整可渲染的工程：
  `npm install && npx remotion render
  src/index.ts AiflPromo out/promo.mp4`。
- **测试（仓库内自动验证，新增 demo 或组件后跑）**：
  - 纯函数单测：`npm test`（仓库根目录的 vitest，覆盖 `assets/lib/helpers` 的
    mulberry32 / velocityAt / lagged / dampedSettle / handheld，断言确定性）。
  - demo 渲染冒烟：`python3 assets/scripts/smoke-render-demos.py` 渲染每个
    带时长导出的 demo 的首帧，断言不崩（需要 `cd template && npm ci` 和
    motion-blur，详见 `demos/README.md` 的测试与验证一节）。
  - Freesound 离线回归：`python3 -m unittest discover -s assets/scripts -p "test_sfx_source.py"`
    （覆盖许可、来源保存、失败处理和音频测量）。
  - CI（`pr-checks.yml`）已经自动跑：tsc 严格编译全部 demo、vitest 和冒烟渲染。
- `jianying-export/` 是剪映工程导出模块：`mac_draft.py` 在 Mac 剪映 11.2
  上实测通过；`windows_draft.py` 按上游支持的路径实现，没有真机验证；
  `smoke_test.py` 是新环境先跑的最小冒烟测试。流程、时间线提取和建轨方法见
  `references/jianying-export.md`；需要 venv 和 `pip install
  pyJianYingDraft`。
- `publish/upload_post.py` 负责发到社交平台（Upload-Post API，只用标准库）：
  用户要求时，把成片发到 TikTok、Instagram、YouTube、LinkedIn、X 等，流程见
  `references/publish-upload-post.md`；需要 `UPLOAD_POST_API_KEY` 和
  `UPLOAD_POST_USER`。离线单测：`python3 -m unittest publish/test_upload_post.py`。
- `gallery/` 是静态画廊：优先给用户在线版
  https://vincentwei1021.github.io/video-shotcraft/library.html。要在本地跑，
  先用 `gallery/fetch-media.sh` 拉样片（mp4 不在 git 里），再执行
  `cd gallery && python3 -m http.server 4178`。124 张卡、150 条动态样片，
  可以浏览、搜索、多选复制卡名，也适合让用户看着样片挑镜头。

再次提醒：Skill 仅供参考，这些创作 context 不必限制你的想象与实现。你是最顶尖的设计师，请自由发挥你最大的性能，探索跨领域关联，让核心主题清晰、简单、直观，同时创造富有艺术吸引力的作品。
