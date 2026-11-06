# 管理工作台：revision 4 同步、闭环地图与浏览器验收

日期：2026-11-03。需求 PM-02，分支 `feat/management-workbench`，[PR #32](https://github.com/owariband/Seekino/pull/32)，目标 `dev`。设计见 [主 TRD](../../../../design/03-product-reassessment/management-workbench-trd.md)；本记录是交付快照，最新状态以 [管理工作台](../../../../dev/workbench.html) 为准。

## 数据迁移

[build_data.py](build_data.py) 从提交 `940ed941`（原样同步的 Kimi 工作区 revision 4）读取数据，按 TRD §3.1 规则生成 revision 5，写入 `docs/dev/workbench.html` 的 `#workbench-data`。可重复执行，输出一致。

| 核对项 | 结果 |
| --- | --- |
| revision 4 卡片保留 | 76 / 76 |
| 新增卡片 | 13：MOD-LOOP、LOOP-01 至 LOOP-09、C25、C28、PM-02 |
| 状态变化 | 7 张带“待验收”标签的 DONE 卡迁入 VERIFYING：B07、B20、C21、C23、PR-18、PR-24、PR-25；其余状态不变 |
| 其他字段变化 | PR-24、PR-30、PR-31 增加 P0、里程碑与截止日，PR-30/PR-31 关联门槛；COST-01 设为 P1；PM-01 标为已合并 dev；MOD-QUALITY、PM-01、PR-24、PR-30、PR-31、COST-01 的关联增加新卡。正文、来源、链接未改 |
| 里程碑 P0 任务 | 14 张，截止日 14 / 14；除 PR-24（课程 QA 本身）外都关联门槛或阻塞关系 |
| 验收门槛 | 17 条：通过 3、未通过 4、待测 10；17 条均有证据链接或待测说明 |
| 状态一致性警告 | 37：DONE 仍带待核对、待验收、Draft 等标签 37，P0 缺截止日 0 |
| 状态总数 | DONE 66、TODO 11、VERIFYING 8（含本需求 PM-02）、PROGRESSING 4 |

门槛状态引用课程 QA（PR #24，提交 `23c88814`）11-03 的报告与 GPT v11 费用审计，未重跑业务验收，也没有模型调用。

37 张警告卡大多来自 revision 4 的批量状态调整，例如 C22 的正文与 11-03 QA 结论仍是 TODO，但卡片为 DONE。本次按 TRD 只标警告，不改判。

## 浏览器验收

[browser-check.cjs](browser-check.cjs)：Chrome，1440×900 与 375×812，离线上下文，36 项全部通过（视觉重做后为 38 项，见下文），结果见 [results.json](results.json)。

覆盖：默认概览；门槛通过数与图例；未完成 P0、READY 课程、已知费用（未知调用不计 0）、状态矛盾指标与数据一致；八个闭环阶段及“边看边问”未通过、“整理学习要点”不在本里程碑；紧急事项按截止日分组并显示上游阻塞；门槛与卡片详情、关联跳转；看板四列、VERIFYING 列、类型与里程碑筛选、PR 链接；门槛表与状态筛选；移动端视图切换与无横向溢出；旧浏览器草稿不能覆盖仓库数据；无编辑、保存、导入或拖动入口；无 JS 错误与网络请求。

截图：[概览](overview.png) · [看板](board.png) · [验收门槛](gates.png) · [详情](detail.png) · [移动端](mobile.png)

## 视觉重做

2026-11-03 晚，PR #32 合入 dev 后，按用户要求在 dev 上废弃原青绿配色，重做为暖色纸面风格：米白背景与浅灰侧栏，标题、关键数字和阶段编号用衬线字体，正文用系统无衬线；只用一种陶土色做链接、当前项和 P0 标记；卡片和表格用细分隔线、轻阴影；页面固定浅色（`color-scheme: only light`）。门槛和状态颜色仍然配图标与文字，不只靠颜色区分。

`#workbench-data` 与重做前逐字一致，三个视图、筛选、详情与跳转逻辑不变。[browser-check.cjs](browser-check.cjs) 沿用原 36 项，新增“页面只用浅色”“标题用衬线字体”2 项，共 38 项全部通过；截图和 [results.json](results.json) 已按新页面更新。

未验证：长期管理效率、真实使用中的查找耗时；课程进度与费用为 11-03 快照，不会自动刷新。2026-11-02 版的 [看板验收记录](../kanban/README.md) 与其脚本针对旧文件名，保留为历史证据。
