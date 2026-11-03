"""Build the management workbench data (schemaVersion 2) from kanban revision 4.

Usage, from the repository root:
    python3 docs/report/docs/dev/workbench/build_data.py

Reads revision 4 exactly as synced in commit 940ed941, applies the migration rules of
docs/design/03-product-reassessment/management-workbench-trd.md §3.1 and writes the result
into the #workbench-data node of docs/dev/workbench.html. Re-running gives the same output.
"""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
PAGE = ROOT / "docs/dev/workbench.html"
REV4_COMMIT = "940ed94138c002891dd8ab5e7b24e8c4f248a4ef"
NODE = re.compile(r'(<script id="workbench-data" type="application/json">)(.*?)(</script>)', re.S)

GH = "https://github.com/owariband/Seekino"
QA = f"{GH}/blob/23c888144a2d1885c8d7647d9dc68604abe275f3/"
DEV = f"{GH}/blob/823db7371a36335426fd51a1e2385ffddb7a2ffb/"
PR31_TRD = f"{GH}/blob/fix/realtime-asr-evidence/docs/design/03-product-reassessment/realtime-asr-evidence-trd.md"
CAPTION = QA + "docs/report/server/course-qa-deepseek-caption-20261103.md"
JOINT = QA + "docs/report/server/course-qa-deepseek-20261103.md"
ASR_GAP = QA + "docs/report/server/course-qa-realtime-asr-gap-20261103.md"
FIRST = QA + "docs/report/server/course-qa-20261101.md"
QA_TRD = QA + "docs/design/03-product-reassessment/course-qa-trd.md"
COST = DEV + "docs/report/gateway/deepseek-flash-chunk-audit-20261103.md"
HANDOFF = DEV + "docs/report/server/pr18-scope-handoff-20261103.md"
MS = "M-20261105"
TODAY = "2026-11-03"


def pr(number):
    return {"label": f"PR #{number}", "url": f"{GH}/pull/{number}", "kind": "pr"}


def link(label, url, kind="evidence"):
    return {"label": label, "url": url, "kind": kind}


def new_card(id, title, category, *, type="task", status="todo", priority="P0", due=None,
             description, next, verification, related=(), blocked_by=(), gates=(), links=(),
             tags=(), branch="", milestone=MS):
    card = {
        "id": id, "title": title, "category": category, "type": type, "status": status,
        "priority": priority, "description": description, "next": next,
        "verification": verification, "updatedAt": TODAY, "tags": list(tags),
        "related": list(related), "branch": branch, "links": list(links),
        "source": {"ref": "2026-11-03 闭环最短路径评审", "path": "conversation"},
    }
    if milestone:
        card["milestone"] = milestone
    if due:
        card["due"] = due
    if blocked_by:
        card["blockedBy"] = list(blocked_by)
    if gates:
        card["gates"] = list(gates)
    return card


def gate(id, title, status, note, links, checked=None):
    return {"id": id, "milestone": MS, "title": title, "status": status, "note": note,
            "checkedAt": checked, "links": links}


GATES = [
    gate("DP01-GPT", "《Let's build GPT》任务、内容与索引版本一致并可检索", "untested",
         "v11 任务 93d60de9 在 11-03 12:26 快照为理解 117/117、Refine 106/115、索引 96/117，Consumer 已停止，尚未 READY。",
         [link("持久成果核对", HANDOFF), link("费用与逐块审计", COST)], "2026-11-03"),
    gate("DP01-MG", "micrograd（反向传播）一集解析至 READY", "untested",
         "示例问题“懂导数和矩阵，想弄懂反向传播”需要这一集；10-30 曾在开发库暂停，当前进度未采集。",
         [link("QA 课程准备用例", QA_TRD + "#qa-preparation")]),
    gate("SE01", "找到“树叶新闻”示例片段并可回看", "untested", "等待 GPT 课程 READY；Q03 的局部时间引用不算召回通过。",
         [link("课程检索用例", QA_TRD + "#qa-retrieval")]),
    gate("SE02", "中文问“相同输入为何回答不同”，定位英文讲解", "untested", "等待 GPT 课程 READY。",
         [link("课程检索用例", QA_TRD + "#qa-retrieval")]),
    gate("SE04", "限定开头三分钟找不存在的内容时明确说没找到", "untested", "等待 GPT 课程 READY；原片负例标注待补。",
         [link("课程检索用例", QA_TRD + "#qa-retrieval")]),
    gate("LP01", "单课：按已有基础给出两三段有来源的观看顺序", "untested", "等待 GPT 课程 READY 与参考片段标注。",
         [link("学习路径用例", QA_TRD + "#qa-learning-path")]),
    gate("LP02", "跨课：从导数和矩阵到反向传播的最短观看路线", "untested", "需要 micrograd 与 GPT 两集 READY，并完成独立标注。",
         [link("学习路径用例", QA_TRD + "#qa-learning-path")]),
    gate("PB01", "点击回答中的时间链接，跳到原片并播放", "pass",
         "11-03 复测：Q03 来源 01:25 定位到 85.4 秒并播放，未新增模型请求；结构化视频卡未通过，另见 Q01/Q03。",
         [link("captions 修复后 QA", CAPTION)], "2026-11-03"),
    gate("PB02", "两门课程间切换来源回看，上下文跟随", "untested", "需要两门 READY 课程。",
         [link("来源回看用例", QA_TRD + "#qa-playback")]),
    gate("RT01", "新会话问当前画面在演示什么，两句话回答", "untested", "尚未执行。",
         [link("实时问答用例", QA_TRD + "#qa-realtime")]),
    gate("RT07", "整课失败但已有 ASR 时，回答“老师在讲什么”", "fail",
         "42:53 用户实测：工具漏读已有 2,476 条 ASR，把父任务失败误报为语音转写失败（C28）。",
         [link("复现报告", ASR_GAP)], "2026-11-03"),
    gate("Q01", "暂停位置的解释与课程目标关系，简短并给依据", "fail",
         "看帧成功，但回答偏长、暴露内部术语（C25），第一组另有 2 次视频卡失败（C22）。",
         [link("captions 修复后 QA", CAPTION)], "2026-11-03"),
    gate("Q02", "按用户基础用三句话继续解释", "fail", "两组都在三点之外追加来源段，未满足三句话（C25）。",
         [link("captions 修复后 QA", CAPTION)], "2026-11-03"),
    gate("Q03", "给出原片依据并区分原话与补充解释", "fail",
         "能区分原话与解释，但视频卡失败后误称没有可播放片段（C22）。",
         [link("captions 修复后 QA", CAPTION)], "2026-11-03"),
    gate("RE01", "刷新后会话、消息与完成状态恢复", "pass", "两组刷新恢复通过，任务和基础 Chunk 前后相同。",
         [link("captions 修复后 QA", CAPTION)], "2026-11-03"),
    gate("RE05", "未 READY 课程明确提示，不伪造检索结果", "pass", "HTTP 返回 409 SERIES_NOT_READY；页面反馈未单独核对。",
         [link("联合运行 QA", JOINT)], "2026-11-03"),
    gate("ON01", "外部用户通过发布地址走完找课→回看→追问→刷新", "untested",
         "范围取决于 LOOP-01：若周四只做本机演示，本条移出本里程碑。",
         [link("公开入口用例", QA_TRD + "#qa-release")]),
]

STAGES = [
    {"id": "S1", "title": "课程库就绪", "value": "至少 GPT 与 micrograd 两集 READY，否则后面的找课与路径都无从验收。",
     "gates": ["DP01-GPT", "DP01-MG"], "cards": ["LOOP-02", "LOOP-03", "LOOP-04", "PR-30"]},
    {"id": "S2", "title": "说出目标，找到讲解", "value": "用户提出学习目标，系统在课程库中找到实际讲解及时间范围。",
     "gates": ["SE01", "SE02", "SE04"], "cards": ["LOOP-02", "LOOP-07"]},
    {"id": "S3", "title": "组织观看顺序", "value": "按已有基础说明先看哪段、每段解决什么、必须先补什么。",
     "gates": ["LP01", "LP02"], "cards": ["LOOP-04", "LOOP-07"]},
    {"id": "S4", "title": "回看核对", "value": "点击来源跳到原片对应位置，能播放核对。",
     "gates": ["PB01", "PB02"], "cards": ["LOOP-04"]},
    {"id": "S5", "title": "边看边问", "value": "暂停时问当前讲解，连续追问能接住上下文，并回到原片核对。",
     "gates": ["RT01", "RT07", "Q01", "Q02", "Q03"], "cards": ["LOOP-05", "LOOP-06", "C28", "C25", "PR-31"]},
    {"id": "S6", "title": "会话可靠", "value": "刷新、未就绪课程等情况下，已有成果不丢、反馈准确。",
     "gates": ["RE01", "RE05"], "cards": []},
    {"id": "S7", "title": "对外入口", "value": "外部用户不依赖开发机即可完成同一路径。",
     "gates": ["ON01"], "cards": ["LOOP-01", "LOOP-09"]},
    {"id": "S8", "title": "整理学习要点", "value": "整理本次学习的要点、来源与未解决问题。", "outOfScope": True,
     "gates": [], "cards": [], "note": "P1，不进入周四里程碑；产品承诺中保留，验证通过前不对外宣称。"},
]

OUT_OF_SCOPE = [
    {"title": "Jev 双维路由（PR #31 交付 B）", "card": "PR-31",
     "why": "截图失败的根因是漏读 ASR，不是路由；Jev 一次回答两个问题尚未实测。周四前保持 query_hook 关闭。"},
    {"title": "视频类型自动识别与 thinking=low A/B", "card": "COST-01",
     "why": "周四前直接按任务配置 0.5 fps；自动分类与推理档位对照放到之后。"},
    {"title": "七集全量解析", "card": None, "why": "预算不允许；本里程碑只要求 GPT 与 micrograd 两集。"},
    {"title": "本地 OCR 补代码文字", "card": None,
     "why": "现有 OCR 是对同一批帧再跑一次视觉模型，不便宜；本地 OCR 是新组件，周四后与 0.5 fps 一起对照。"},
    {"title": "学习总结、持久用户画像", "card": None, "why": "学习总结为 P1；用户画像暂不实现。"},
    {"title": "FTT 分布统计、断网与进程崩溃恢复（RE04/RE06）", "card": None, "why": "不阻断周四放行，保留在课程 QA 中后续执行。"},
]

NEW_CARDS = [
    new_card("MOD-LOOP", "课程学习闭环最短路径", "quality", type="module", status="progressing",
             description="产品承诺：告诉 Seekino 学习目标和已有基础，它从课程库中组织一条有来源、可回看的学习路径，并在学习过程中回答问题。\n\n本模块把这条承诺拆成八个阶段：课程库就绪 → 说出目标找到讲解 → 组织观看顺序 → 回看核对 → 边看边问 → 会话可靠 → 对外入口 →（P1）整理学习要点。每个阶段由验收门槛判定，阶段前的阻塞工作列为 LOOP 任务。\n\n2026-11-03 判断：观看追问已有局部证据，产品承诺的前半段（找讲解、排顺序）没有任何门槛通过，原因是还没有一门课 READY；示例问题“反向传播”需要 micrograd 一集。",
             next="按截止日推进 LOOP-01 至 LOOP-09；每完成一项即更新卡片与门槛。",
             verification="模块完成以周四门槛全部通过为准（ON01 视 LOOP-01 决定是否计入）。门槛结果引用课程 QA 报告；工作台展示不等于验收通过。周四前 query_hook 保持关闭，QA 一直在关闭状态下测试。",
             related=["LOOP-01", "LOOP-02", "LOOP-03", "LOOP-04", "LOOP-05", "LOOP-06", "LOOP-07", "LOOP-08", "LOOP-09",
                      "C25", "C28", "PR-24", "PR-30", "PR-31", "COST-01", "MOD-PARSE", "MOD-CHAT", "MOD-SEARCH"],
             links=[link("课程 QA TRD", QA_TRD, "trd"), pr(24)], tags=["周四里程碑"], due="2026-11-05"),
    new_card("LOOP-01", "确定周四“上线”范围：公开地址还是本机演示", "platform", due="2026-11-03",
             description="公开部署要求媒体 Range、真实模型与存储配置、数据归属都在外部环境可用；本机 WhisperX 与 rewriter 会成为部署依赖。本机演示则只需在 dev HEAD 上验收。",
             next="由用户决定；决定后更新 ON01 是否计入门槛，以及 LOOP-09 的范围。",
             verification="决定记录在本卡与 MOD-LOOP；未决定前 ON01 保持待测。",
             related=["MOD-LOOP", "LOOP-09"], gates=["ON01"], tags=["待用户决定"]),
    new_card("LOOP-02", "合并 #29/#30，GPT v11 完成 Refine 与索引并 READY", "parse", status="progressing", due="2026-11-04",
             description="PR #29 修复 DeepSeek 严格 Schema，PR #30 修复长解析任务查询与 Consumer 恢复。v11 任务 93d60de9 在 11-03 12:26 为理解 117/117、Refine 106/115、索引 96/117；任务查询触发 MySQL 1038，Consumer 已停止，剩余 Refine 与索引不会自动推进。",
             next="评审并合并 #29、#30；在 dev 上恢复 v11 剩余 Refine 与索引，确认 READY 后用真实查询核对可检索。",
             verification="以任务 READY 且 SE01 真实查询返回对应版本内容为准；PR 合并本身不算完成。",
             related=["PR-30", "MOD-LOOP", "B07"], gates=["DP01-GPT", "SE01", "SE02", "SE04", "LP01"],
             links=[pr(29), pr(30), link("持久成果核对", HANDOFF)]),
    new_card("LOOP-03", "0.5 fps 与 3 fps 五分钟代码画面对照", "parse", due="2026-11-03",
             description="v11 每次 Understanding 通常带 198 张图，已知费用 ¥31.26 中 Understanding 占 ¥25.2。上传与重新解析的 parseConfig 已支持按任务设定 vision.fps（0.2–5.0）并写入任务快照，无需改代码。",
             next="截取约 5 分钟含代码画面的片段，分别以 3 fps 与 0.5 fps 解析，记录图片数、输入 token、费用、耗时，并核对代码与变量识别。",
             verification="估算单集约 ¥10–14，未实测；代码细节在 3 fps 下已有 wei→weigh、v2.py→bigram.py 错误，0.5 fps 需单独核对。实验费用预计约 ¥2。",
             related=["COST-01", "LOOP-04", "MOD-LOOP"], links=[link("费用与逐块审计", COST)], tags=["成本"]),
    new_card("LOOP-04", "micrograd 以 0.5 fps 解析至 READY", "parse", due="2026-11-04",
             description="反向传播由 Zero to Hero 第 1 集 micrograd 系统讲解，GPT 一集使用 PyTorch autograd，不覆盖示例问题。片长约 2 小时 25 分，解析耗时是最大的排期风险。",
             next="LOOP-03 结果可接受后立即上传或重新解析，parseConfig 设 vision.fps=0.5；记录费用与进度。",
             verification="以任务 READY 且 LP02 所需片段可检索为准。若周三晚仍未 READY，退回只验 GPT 单课的 LP01，并如实说明跨课路径未验证。",
             related=["LOOP-03", "COST-01", "MOD-LOOP"], blocked_by=["LOOP-03"], gates=["DP01-MG", "LP02", "PB02"], tags=["排期风险"]),
    new_card("LOOP-05", "实时问答读取已有 ASR（PR #31 交付 A）", "chat", due="2026-11-04",
             description="实时工具在父任务 PARSING_FAILED 时只返回缺证，没有读取同一媒体已校验的整片 ASR。交付 A：冻结播放点，默认读取前 3 分钟已有 ASR，缺失时才局部补转写。Codex worktree 中已有约 700 行未提交草稿。",
             next="用户放行 PR #31 的交付 A；实现后复验 RT07，新增 Whisper 调用应为 0。交付 B（Jev 双维路由）延后。",
             verification="RT07 在原失败任务上回答正确并可回看，新增 ASR 为 0；不能用已补齐 Chunk 的任务替代。",
             related=["PR-31", "C28", "MOD-LOOP"], gates=["RT07", "RT01"], links=[pr(31), link("TRD", PR31_TRD, "trd")]),
    new_card("LOOP-06", "实时证据不提交视频卡，失败表述准确，回答长度受控", "chat", due="2026-11-04",
             description="submit_video_item 不论有无可检索范围都会注册；实时证据不属于可信检索候选，提交必然失败，模型随后误称没有可播放片段。Q01/Q02 回答超出“简短”“三句话”要求，并暴露 PARSING_FAILED 等内部术语。",
             next="只在课程库范围可用时暴露视频卡工具，实时证据统一用时间码链接交付；收紧回答长度与用户可见术语。随后复测 Q01–Q03。",
             verification="Q01–Q03 不再出现不可成功的视频卡提交，Q02 总共不超过三句话，时间链接仍可回看；不以更少证据换取更短回答。",
             related=["C22", "C25", "MOD-LOOP"], gates=["Q01", "Q02", "Q03"], links=[link("captions 修复后 QA", CAPTION)]),
    new_card("LOOP-07", "标注 SE01/SE02/SE04 与 LP01/LP02 的参考片段", "quality", due="2026-11-04",
             description="检索与学习路径用例尚无独立核准的参考片段，不能只拿系统输出自证。LP02 需要 micrograd 中讲链式法则、手算梯度的段落，以及 GPT 集中的训练代码。",
             next="人工回看原片，固定每题可接受的片段范围与负例；参考答案只供评审，不发送给模型。",
             verification="每题有原片时间范围和判定说明；SE04 负例需确认前三分钟确实没有目标内容。",
             related=["PR-24", "LOOP-04", "MOD-LOOP"], gates=["SE01", "SE02", "SE04", "LP01", "LP02"],
             links=[link("课程检索用例", QA_TRD + "#qa-retrieval"), link("学习路径用例", QA_TRD + "#qa-learning-path")]),
    new_card("LOOP-08", "在 dev HEAD 上跑周四最小门槛", "quality", due="2026-11-05",
             description="QA 迄今运行在 PR #18 + dev + PR #29 的联合副本上，不是要上线的版本。周四需要在合并后的 dev HEAD 上重跑本里程碑全部门槛。",
             next="上游卡片完成后，固定运行提交、模型配置与课程版本，按门槛顺序执行并回填结果与证据。",
             verification="门槛全部通过才放行；失败、待测分别保留，不以平均分抵消阻断项。",
             related=["PR-24", "MOD-LOOP"], blocked_by=["LOOP-02", "LOOP-04", "LOOP-05", "LOOP-06", "LOOP-07"],
             gates=["RT01", "RT07", "Q01", "Q02", "Q03", "SE01", "SE02", "SE04", "LP01", "LP02", "PB01", "PB02", "RE01", "RE05"],
             links=[pr(24)]),
    new_card("LOOP-09", "按 LOOP-01 的决定完成对外入口验收", "platform", due="2026-11-05",
             description="若决定公开访问：部署真实配置，验证页面、API、媒体 Range 与模型可用，执行 ON01。若只做本机演示：本卡关闭，ON01 移出门槛。",
             next="等待 LOOP-01。",
             verification="ON01 在干净浏览器通过；部署成功不等于学习路径通过。",
             related=["LOOP-01", "MOD-LOOP"], blocked_by=["LOOP-01", "LOOP-08"], gates=["ON01"], tags=["视决定"]),
    new_card("C25", "观看解释超出简短回答要求，并向用户暴露内部取证术语", "chat", due="2026-11-04",
             description="Q01 要求简短、Q02 要求三句话，DeepSeek Flash / low 两轮均超出；Q01 夹带 partial、Chunk、visual_summary 等术语，Q02 在三点外追加来源段。完整分析见 QA 分支 problems C25。",
             next="由 LOOP-06 承接修复。",
             verification="Q02 完整回复最多三句话，Q01 用用户能理解的表达说明已知事实与限制；精简不能删掉来源。",
             related=["LOOP-06", "PR-24", "MOD-CHAT"], gates=["Q01", "Q02"],
             links=[link("问题记录", QA + "docs/dev/problems.md#L722", "source"), link("captions 修复后 QA", CAPTION)]),
    new_card("C28", "观看工具漏读已有整片 ASR，整课失败被误报成无法核对当前讲解", "chat", due="2026-11-04",
             description="42:53.152 问“现在老师在讲什么？”，实时工具 4.034 秒后只返回画面和三个缺失 Chunk；同一任务已有 2,476 条通过校验的 ASR，最近三分钟有 62 条。完整分析见 QA 分支 problems C28。",
             next="由 LOOP-05（PR #31 交付 A）承接修复。",
             verification="失败 Parse 不妨碍读取有效 ASR；本位置基于已有转写回答，新增 Whisper 调用为 0。",
             related=["LOOP-05", "PR-31", "MOD-CHAT"], gates=["RT07"],
             links=[link("问题记录", QA + "docs/dev/problems.md#L748", "source"), link("复现报告", ASR_GAP)]),
    new_card("PM-02", "管理工作台与课程闭环地图", "quality", type="requirement", status="verifying", priority="P1",
             description="看板升级为管理工作台：概览（里程碑、指标、闭环地图、紧急事项）、看板（四列状态）与验收门槛；同步 Kimi 工作区 revision 4，文件改名为 docs/dev/workbench.html。",
             next="已通过 36 项浏览器检查并随 PR #32 合入 dev；收集周四里程碑期间的使用反馈后再判断是否完成。",
             verification="以 TRD §4.1 的实验计划为准；工作台展示不代表任何业务门槛通过。",
             related=["PM-01", "MOD-QUALITY", "MOD-LOOP"], branch="feat/management-workbench",
             links=[pr(32), link("TRD", f"{GH}/blob/feat/management-workbench/docs/design/03-product-reassessment/management-workbench-trd.md", "trd")],
             milestone=None),
]

# Cards that sit on the milestone's critical path keep their content; only management fields are added.
MANAGED = {
    "PR-30": {"priority": "P0", "milestone": MS, "due": "2026-11-04", "gates": ["DP01-GPT"]},
    "PR-31": {"priority": "P0", "milestone": MS, "due": "2026-11-04", "gates": ["RT07"]},
    "PR-24": {"priority": "P0", "milestone": MS, "due": "2026-11-05"},
    "COST-01": {"priority": "P1"},
}


def main():
    rev4_page = subprocess.run(["git", "show", f"{REV4_COMMIT}:docs/dev/kanban.html"], cwd=ROOT,
                               check=True, capture_output=True, text=True).stdout
    rev4 = json.loads(re.search(r'<script id="kanban-data" type="application/json">(.*?)</script>', rev4_page, re.S).group(1))
    assert rev4["revision"] == 4 and len(rev4["cards"]) == 76
    cards = []
    for card in rev4["cards"]:
        card = dict(card)
        # "Verifying" now has its own status; the card's own tag already said so.
        if card["status"] == "done" and "待验收" in card["tags"]:
            card["status"] = "verifying"
        card.update(MANAGED.get(card["id"], {}))
        if card["id"] == "PM-01":
            card["tags"] = ["已合并 dev"]
            card["next"] = "已由 PR #27 合入 dev（823db737）；后续演进见 PM-02 管理工作台。"
            card["related"] = card["related"] + ["PM-02"]
        if card["id"] == "MOD-QUALITY":
            card["related"] = card["related"] + ["PM-02", "MOD-LOOP"]
        if card["id"] in ("PR-30", "PR-31", "PR-24", "COST-01"):
            card["related"] = card["related"] + ["MOD-LOOP"]
        cards.append(card)
    cards.extend(NEW_CARDS)
    data = {
        "schemaVersion": 2, "project": "Seekino",
        "updatedAt": "2026-11-03T21:30:00+08:00", "sourceCheckedAt": "2026-11-03T21:07:00+08:00",
        "sourceRef": "823db737", "revision": 5,
        "categories": rev4["categories"],
        "milestones": [{
            "id": MS, "title": "课程学习闭环验收", "due": "2026-11-05", "module": "MOD-LOOP",
            "promise": "告诉 Seekino 你的学习目标和已有基础，它会从你的课程库中组织一条有来源、可回看的学习路径，并在学习过程中回答问题。",
            "promiseNote": "这是待用真实课程验证的产品主线；门槛全部通过前，不作为已实现能力介绍。",
            "stages": STAGES,
            "courses": [
                {"name": "Let's build GPT", "ready": False,
                 "progress": "v11 任务 93d60de9：理解 117/117、Refine 106/115、索引 96/117，Consumer 已停止（2026-11-03 12:26 快照）"},
                {"name": "micrograd（反向传播）", "ready": False,
                 "progress": "10-30 曾在开发库暂停；当前进度未采集，计划以 0.5 fps 重新解析（LOOP-04）"},
            ],
            "cost": {"knownCny": 31.26, "unknownCalls": 69, "capCny": None, "scope": "GPT v11 单集已知用量"},
            "outOfScope": OUT_OF_SCOPE,
        }],
        "gates": GATES,
        "cards": cards,
    }
    text = json.dumps(data, ensure_ascii=False, indent=1).replace("<", "\\u003c")
    page = PAGE.read_text(encoding="utf-8")
    assert NODE.search(page), "workbench data node not found"
    PAGE.write_text(NODE.sub(lambda m: m.group(1) + text + m.group(3), page, count=1), encoding="utf-8")
    print(f"revision {data['revision']}: {len(cards)} cards, {len(GATES)} gates")


if __name__ == "__main__":
    main()
