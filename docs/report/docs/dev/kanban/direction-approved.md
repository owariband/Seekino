# 单版设计范围与依据

日期：2026-11-02。

用户原话：“帮我通过 huashu-design（但是只用设计一个版本）设计一个 kanban 用于 unified management about the module/requirement/task”。前序讨论已经明确 category、TODO / PROGRESSING / DONE 三列、卡片承载需求/模块/任务，以及关联 PR/TRD、使用单个 HTML。

按这个明确范围实施一个设计版本；不增加第二个变体或三方向选择流程。[huashu-design 原始技能](https://github.com/alchaincyf/huashu-design/blob/master/SKILL.md) 用于品牌依据、内容驱动布局和浏览器验证。读取版本为 3.1.0，SKILL blob SHA 为 `23f09262546164b63c04fbca36b04d77a9fc1618`。技能未安装到用户的全局目录。

| Form 问题 | 本次回答 |
|---|---|
| 叙事角色 | 日常工作看板；让使用者找到一个工作项，理解状态并打开证据 |
| 观众距离 | 笔记本桌面为主，兼顾手机查看与状态编辑 |
| 视觉温度 | 安静、清晰，沿用 Seekino 灰绿与青绿 |
| 容量 | 每分类三列，每列先显示三张；其余展开，分类可折叠，支持搜索与类型过滤 |
| 内容独有的结构 | 分类表达工作归属，三列表达推进状态，卡片类型表达不同粒度，资料链接表达可追溯性 |

Form 来自项目工作的“归属、状态、粒度、证据”四个实际维度。视觉系统见 [品牌依据](brand-spec.md)，具体交付与未验证边界见 [验收记录](README.md)。这份记录保存实施依据，不冒充用户已经验收最终页面。
