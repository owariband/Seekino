---
name: seekino-editing-materials
description: Find source performances and editing references with Seekino MCP, review the original footage, prepare local layers, and save reusable editing techniques and feedback in the source series. Use for footage selection, style research, and updating a motion template after a cut.
---

# Editing with Seekino

Seekino 提供可追溯的原片、可回看的风格例证和共享经验，供你按当前任务取用。你可以自主规划制作过程，按需参考 motion-showreel 的代码动画和 video-shotcraft 的镜头配方，再用这里的素材、建议和例证补充或调整方案。代码、素材处理和渲染在本机工程完成。

下面各部分可独立取用，顺序和方法由你根据用户目标、素材与工程情况决定。构思时，可以从“这支片子要让谁看懂什么”出发：产品演示可以参考一次具体操作及其收益，叙事短片可以参考人物的目标、阻碍和处境变化。内容价值、反差、具体动作、可信证据、情绪和故事都可帮助判断如何表达，采用哪些取决于当前内容。

## Choose footage

剧情向剪辑可以参考人物一段时间内的神情与动作变化，判断是否符合剧本。例如剧本需要“听见指责后愣住，再咬牙转头”，可以围绕这些可观察的变化检索、比较候选。回看时，可结合情绪起点、变化过程、收尾，以及视线、朝向、姿态和相邻镜头判断；情绪标签本身不能说明整段表演是否匹配。这是一种可借鉴的选片方法，是否采用、保留多长、如何重组，由你根据当前方案判断。

需要素材时，可以参考这些入口：

1. `list_series` 可用于查看已连接的素材库。本机和 Cloud 连接可以同时使用。整理候选时，可记录所属连接、series、视频和原片毫秒区间，便于追溯；`evidence_ref` 由签发它的连接解析。
2. `retrieve` 可用于找对白、动作或表情变化，搜索时可结合人物名字的不同写法和可观察的动作。例如，为 MyGO 的爱音找反应镜头，可以搜索千早愛音、咬牙、举拳，再比较候选。需要查全已发布的出镜事件时，可用 `browse_events`，采用结果中记录的人名，跟随 `next_cursor` 直到 null；省略人名筛选，可以看到未标注人物的事件。
3. `read_evidence` 可用于核对候选及其前后文。取片时，同时指定源视频的 `start_ms` 和 `end_ms`，起点包含、终点不包含；返回的 `media.clip_url` 可供及时下载。Cloud 每次最多取 60 秒。可以回看动作的准备、发生和收尾来确定剪辑区间，也可以按节奏重组、直接使用原片或有意停住某一帧。是否保留原声和完整表演，由当前镜头需要决定。

选用人物表演时，可以保留连续动作、神情变化和自然停顿，为剧情提供更完整的反应过程。候选不合适时，也可以换片段、重组素材或调整镜头安排。

## Borrow a visual technique

需要借鉴视觉手法时，可以用 `find_edit_templates` 搜索画面中能看见的变化，例如“斜向擦除，分两次露出标题”。`techniques` 是已索引的原片观察，`media.play_url` 可用于逐帧回看，了解来源区间和具体处理。`get_style_card(video_id)` 提供整片反复出现的风格特征及例证；可用 `read_evidence` 打开例证，比较节奏、配色、构图和转场。空结果只表示这个库没有匹配的已发布观察，可以换一个 series 或本机连接的参考库继续找。

借鉴时，可以用“这个手法是否帮助观众理解当前内容”来判断是否适合。需要吸引注意时，可以利用内容里真实存在的预期差异；需要说明价值时，可以呈现具体动作、操作和结果。人物的目标与阻碍可以带出关心，前后状态的变化可以组织故事；技术解释、Logo 或抽象动画也可以按自身形式表达。

借来的节奏、构图和运动与自己的改动可以保持可区分、分别记录，方便回看和复用。实现、渲染、素材权利判断和创作决定由本机编辑 Agent 处理。原片例证支持对参考镜头的观察，不能证明未观察到的效果或收益。

## Camera motion references and suggestions

镜头运动可由你根据镜头意图、节奏和素材，在本机工程中自行编写。下面的做法供参考。

需要慢起、加速、落定的平移或推拉时，可以参考钟形速度。速度积分得到 S 形位移，Sigmoid 或合适的 easing 也能形成相近的运动。时长、峰值、对称性和落点可在镜头代码中按当前构图调整。镜头以停留结束时，可以回看运动是否顺利落定；硬切或刻意分步的运动也可以采用不同节奏。

有助于表达当前镜头时，可以让拉伸、倾斜、拖影和模糊按瞬时速度与方向变化，强度结合画布和主体尺寸调整。快段和到达时的表现都可作为回看重点。位移曲线控制时间分配，几何变化还需要相应的变换或滤镜。motion-showreel 和 video-shotcraft 提供了可复用或改写的运动辅助代码与示例，也可结合回看结果调整可读性和镜头衔接。

记录参考时，可以保留原片观察和实测轨迹，另行记录自己的创作改动，便于以后理解和复用。

## Consult local production Skills

需要代码驱动的图形、文字、界面动画或受支持的人物分层合成时，可参考 `skill://motion-showreel/SKILL.md`。完整本机包包含风格参考、starter、时间线和逐帧渲染脚本；原生指引说明了它的适用范围、先预览再导出流程及用户明确免除预览的条件，供你选用时了解。

需要镜头配方、Remotion 合成和速度联动的运动处理时，可参考 `skill://video-shotcraft/SKILL.md`。完整本机包包含对应的镜头卡、demo 和渲染辅助代码。

两份入口都提供固定版本的本机安装方法，可以分别参考，也可以按任务组合使用。制作过程由你自主规划，Seekino 在需要时补充选片、来源、分层建议和 series 共享经验；模板写入遵循所属 series 的权限。

## Prepare a cutout when the shot calls for it

连续人物分层是一种可参考的合成方法。如果它有助于当前剧情或画面，可以把选定人物在一段时间内的神情、转头、手势和身体动作分离出来。采用这种方法时，可在本机准备透明人物视频或与原片逐帧对应的蒙版序列，保留统一画布或裁剪偏移，让人物层、蒙版和保留的原声对齐，并记录原片区间与工程时间的对应关系。是否分层、怎样使用这段表演，由当前方案决定。

回看时，可关注整段是否保留同一个人物，脸部、头发、手部和遮挡是否完整，有无闪烁、边缘丢失或漂移；也可对照剧本观察合成或变速后的神情、节奏与前后镜头。当前随附脚本只处理单帧。若采用连续人物分离，需要在本机另行编写或调用相应工具；单帧检查只反映当帧，无法说明整段的稳定性。

制作定格或图形插入时，随附的 `scripts/split_frame.py` 在 CPU 上处理，并保留原画布。按需在本机 Python 3.12 环境安装依赖（`numpy`、`pillow`、`onnxruntime`），下载一次[固定版本的 anime-seg ONNX 权重](https://huggingface.co/skytnt/anime-seg/resolve/a0a563c41338cbe0d23dfb4bfc3e243c518e5768/isnetis.onnx)。脚本会校验文件的 SHA-256。

```sh
python scripts/split_frame.py frame.png --weights isnetis.onnx --output layers
```

把 `foreground.png` 和 `background-hole.png` 放在同一个原点。`mask.png` 是软蒙版，查看 `red-preview.png` 时检查头发、轮廓是否丢失，是否混入多余背景。背景层在人物原位置留有透明孔洞；移动人物时，可能还需要单独的干净背景或本机补画。

## Reuse and update a template

需要复用效果或积累经验时，可以参考以下用法：

* 模板检索结果在 `templates` 列表里，可用 `find_edit_templates` 获取。设为 `include_parse_techniques=false` 时，只查你能查看的 series 共享模板。`get_edit_template` 可读取具体版本；模板没有默认版本时，可用 `list_edit_template_versions` 查看并选用一版，在当前剪辑中固定该版本有助于重现。
* 选用某个版本时，可依据其入口、文件、运行环境、素材槽位和参数在本机重建效果，并按目标帧率和画布尺寸回看。可访问的来源快照保留作者当时看到的内容，无权访问的来源细节会隐藏；来源已经变化时，可以另行检查当前原片。
* 有值得积累的效果时，可用 `save_edit_template_version` 保存。新模板指定 series_id，带上合同、可编辑文本文件、入口、运行环境和本连接签发的证据引用。所有能查看该 series 的人都能读取保存的版本与反馈。修改已有效果时，提供模板和父版本，用 `revision_rationale` 说明改了什么。观众原话、自己的判断、实际改动、已验证和未验证项可以分别记录，便于复用。
* 后续反馈或客户端报告的渲染检查可用 `append_edit_template_note` 追加。需要将某个已验证版本设为默认时，可用 `set_default_edit_template_version`，传入模板当前修订号并记录理由。同一次写入重试时，沿用同一个 `request_id`。

每个模板归属一个 series。读取、搜索和版本历史都遵循该 series 的可见性，作者和游客身份不改变这条规则。换一枚有效游客凭据仍能读同一公开 series 的模板。写入沿用已有的 series 编辑权限；Cloud 编辑者从账号菜单取得可写 MCP 凭据，本机 series 沿用本机访问规则。
