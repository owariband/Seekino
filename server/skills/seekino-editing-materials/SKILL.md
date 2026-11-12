---
name: seekino-editing-materials
description: Find source performances and editing references with Seekino MCP, review the original footage, prepare local layers, and save reusable editing techniques and feedback in the source series. Use for footage selection, style research, and updating a motion template after a cut.
---

# Editing with Seekino

把视频库中适合当前镜头的原片和出处取到本机剪辑工程。Seekino 提供素材、可回看的风格参考和共享经验；需要制作成片时，你在本机编写镜头、处理素材并渲染。

选片前，先说清这支片子要让谁看懂什么。产品演示可以从一次具体操作及其收益入手，叙事短片可以从人物想做什么、遇到什么、后来发生什么变化入手。先找到值得表达的内容，再判断哪些素材和手法能把它讲清楚。

## Choose footage

按剧本选人物表演时，先写清这一刻发生了什么、人物的神情与动作怎样变化。例如剧本需要“听见指责后愣住，再咬牙转头”，就围绕这些可观察的变化分别检索，比较多个候选。回看整段时，判断情绪起点、变化过程和收尾是否接得上剧情，也检查视线、朝向、姿态与相邻镜头的衔接；仅有同一种情绪标签还不足以判断。

1. 用 `list_series` 查看已连接的素材库。本机和 Cloud 连接可以同时使用。每个候选都记录所属连接、series、视频和原片毫秒区间；`evidence_ref` 回到签发它的连接解析。
2. 用 `retrieve` 找对白、动作或表情变化。搜索时结合人物名字的不同写法和可观察的动作。例如，为 MyGO 的爱音找反应镜头，可以搜索千早愛音、咬牙、举拳，再比较多个候选。需要查全已发布的出镜事件时，用 `browse_events`，采用结果中记录的人名，跟随 `next_cursor` 直到 null；省略人名筛选，可以看到未标注人物的事件。
3. 用 `read_evidence` 核对候选及其前后文。指定源视频的 `start_ms` 和 `end_ms` 后再次调用，起点包含、终点不包含；及时下载 `media.clip_url`，检查动作的准备、发生和收尾，再确定最终剪辑区间。Cloud 每次最多取 60 秒。表演依靠动作或原声成立时，保留它们；有意停住某个节拍时，可以使用静帧。

选中后，保留人物在这段时间内的连续动作、神情变化和自然停顿，再按剧本确定入点、出点。没有合适片段时，说明具体哪里不匹配，继续找候选或调整镜头安排。

## Borrow a visual technique

用 `find_edit_templates` 搜索画面中能看见的变化，例如“斜向擦除，分两次露出标题”。`techniques` 是已索引的原片观察。打开 `media.play_url`，逐帧检查变化，记下来源区间和具体值得借鉴的处理。`get_style_card(video_id)` 提供整片反复出现的风格特征及例证；用 `read_evidence` 打开例证，比较节奏、配色、构图和转场。空结果只表示这个库没有匹配的已发布观察，可以换一个 series 或本机连接的参考库继续找。

借鉴前，先看这个手法是否帮助观众理解当前内容。需要吸引注意时，可以利用内容里真实存在的预期差异；需要说明价值时，让观众看到具体动作、操作和结果。人物的目标与阻碍可以带出关心，前后状态的变化可以组织故事；技术解释、Logo 或抽象动画仍可按自身形式表达，不必硬套人物情绪和情节。

选择适合当前影片的处理，让借来的节奏、构图和运动与自己的变化保持可区分，并分别记录。实现、渲染、素材权利判断和创作决定留在剪辑机上。原片例证支持你借鉴的判断，不能替未观察到的效果或收益背书。

## Camera motion references and suggestions

在本机剪辑工程中编写镜头运动，根据镜头意图、节奏和素材判断怎么动。下面的做法供参考。

需要慢起、加速、落定的平移或推拉时，可以参考钟形速度。速度积分得到 S 形位移，Sigmoid 或合适的 easing 也能形成相近的运动。在镜头代码中调整时长、峰值、对称性和落点，让它们服务当前构图。镜头以停留结束时，检查运动是否顺利落定；硬切或刻意分步的运动可以采用不同节奏。

有助于表达当前镜头时，可以让拉伸、倾斜、拖影和模糊按瞬时速度与方向变化，强度结合画布和主体尺寸调整。检查快段，也检查到达时是否收得住。位移曲线控制时间分配，几何变化还需要相应的变换或滤镜。motion-showreel 和 video-shotcraft 提供了可复用或改写的运动辅助代码与示例；回看结果，再调整可读性和镜头衔接。

保留原片观察和实测轨迹，作为参考镜头的证据。保存可复用模板与反馈时，另行记录自己的创作改动。

## Consult local production Skills

需要代码驱动的图形、文字、界面动画或受支持的人物分层合成时，读 `skill://motion-showreel/SKILL.md`。完整本机包包含风格参考、starter、时间线和逐帧渲染脚本。它的适用范围、先预览再导出流程，以及用户明确免除预览的条件，都按该指引用于当前任务。

需要镜头配方、Remotion 合成和速度联动的运动处理时，读 `skill://video-shotcraft/SKILL.md`。完整本机包包含对应的镜头卡、demo 和渲染辅助代码。

两份入口都提供固定版本的本机安装方法。按影片需要查阅制作参考，在本机工程里编写运动。Seekino 这份指引负责选片、来源核对、本机分层和 series 共享经验；模板写入遵循所属 series 的权限。

## Prepare a cutout when the shot calls for it

剧情需要换背景或合成时，可以把选定人物在这段区间内的连续表演抠出来，保留神情、转头、手势和身体动作。在本机准备透明人物视频，或与原片逐帧对应的蒙版序列。保持统一画布，或记录裁剪偏移，保住人物本身的运动；人物层、蒙版和保留的原声按同一时间轴对齐，并记录原片区间与工程时间的对应关系。

连续回看整段，检查是否始终保留同一个人物，脸部、头发、手部和遮挡是否处理完整，有无闪烁、边缘丢失或漂移。合成后再对照剧本检查神情变化、动作节奏与前后镜头；移动、缩放或变速后也要复看。当前随附脚本只处理单帧，连续人物分离需在本机另行编写或调用相应工具，并检查整段结果，不能用单帧验收代替。

制作定格或图形插入时，随附的 `scripts/split_frame.py` 在 CPU 上处理，并保留原画布。按需在本机 Python 3.12 环境安装依赖（`numpy`、`pillow`、`onnxruntime`），下载一次[固定版本的 anime-seg ONNX 权重](https://huggingface.co/skytnt/anime-seg/resolve/a0a563c41338cbe0d23dfb4bfc3e243c518e5768/isnetis.onnx)。脚本会校验文件的 SHA-256。

```sh
python scripts/split_frame.py frame.png --weights isnetis.onnx --output layers
```

把 `foreground.png` 和 `background-hole.png` 放在同一个原点。`mask.png` 是软蒙版，查看 `red-preview.png` 时检查头发、轮廓是否丢失，是否混入多余背景。背景层在人物原位置留有透明孔洞；移动人物时，可能还需要单独的干净背景或本机补画。

## Reuse and update a template

* 模板检索结果在 `templates` 列表里，用 `find_edit_templates` 获取。设为 `include_parse_techniques=false` 时，只查你能查看的 series 共享模板。用 `get_edit_template` 读取具体版本；模板没有默认版本时，用 `list_edit_template_versions` 查看并确定一版，在当前剪辑中固定该版本。
* 依据该版本的入口、文件、运行环境、素材槽位和参数，在本机重建效果，按目标帧率和画布尺寸检查渲染帧。可访问的来源快照保留作者当时看到的内容，无权访问的来源细节会隐藏；来源已经变化时，另行检查当前原片。
* 用 `save_edit_template_version` 保存可复用效果。新模板指定 series_id，带上合同、可编辑文本文件、入口、运行环境和本连接签发的证据引用。所有能查看该 series 的人都能读取保存的版本与反馈。修改已有效果时，提供模板和父版本，用 `revision_rationale` 说明改了什么。把观众原话、自己的判断、实际改动、已验证和未验证项分别记录。
* 后续反馈或客户端报告的渲染检查用 `append_edit_template_note` 追加。确实需要将某个已验证版本设为默认时，再用 `set_default_edit_template_version`；先读取模板当前修订号，并记录理由。同一次写入重试时，沿用同一个 `request_id`。

每个模板归属一个 series。读取、搜索和版本历史都遵循该 series 的可见性，作者和游客身份不改变这条规则。换一枚有效游客凭据仍能读同一公开 series 的模板。写入沿用已有的 series 编辑权限；Cloud 编辑者从账号菜单取得可写 MCP 凭据，本机 series 沿用本机访问规则。
