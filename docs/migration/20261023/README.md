# 2026-10-23 设备迁移代码快照

`dev` 保存原 main 工作目录在本次盘点时的源码、测试、配置模板、设计文档及实验脚本。
这是迁移保存点；没有将四份实验代码自动合并成一套业务实现，也没有合并 PR #5。
原 main 服务工作区和各实验目录保留原状。

## 实验代码入口

| 分支 | 快照 commit | 原始基准 |
|---|---|---|
| `migration/ep04-continuous-turn-20261021` | `3d9eb75d651a2cf44f39fbbf446b54f89e833206` | `322db8e` |
| `migration/ep04-continuous-turn-20261021-v23` | `6ff0a7329b62ce1e57e547c5d317c3a2c6fa39fb` | `322db8e` |
| `migration/ep04-offscreen-20261022` | `707bdcecee4d2542b1aeee844d49fb2cf1b4fef8` | `322db8e` |
| `migration/ep04-segmentation-20261020` | `963e53bae4d5b3a0c3b4e6d14cebada142471e12` | `215aadf` |

- continuous-turn 保存初版连续话轮提示及音频复核工具整合。
- continuous-turn-v23 保存后续混合 cue、完整局部上下文提示。
- offscreen 保存 v25 画外发声提示对应代码，实验运行时为 1 秒预填规则。
- segmentation 保存独立开发线，包含 pyannote 尝试和后来严格大于 500ms 的预填规则。
- 四个分支均保留对应的 `agent/video_parse_v2/design/voiceprint-trd.md`。
- PR #5 的已提交来源为 `215aadf1ec2e39086109fde804622a057f5e156a`：
  https://github.com/owariband/Seekino/pull/5 。该提交仍是 segmentation 快照的父提交。

## 新电脑取得代码

Windows 建议在 WSL2 的 Linux 文件系统中克隆，以保留 Bash、权限和符号链接语义：

```bash
git clone --branch dev https://github.com/owariband/Seekino.git Seekino
cd Seekino
git branch -r
git worktree add --detach ../Seekino-ep04-v25 origin/migration/ep04-offscreen-20261022
```

普通 clone 会取得这些远端分支；不要使用 `--single-branch`，否则需要另行 fetch 实验分支。
恢复实验应检出对应快照；不要把其全部文件直接覆盖到 dev 或 main。

`dev` 中当前 `start.sh` 固定寻找 main 工作区，不能用它证明 dev 代码已启动。
恢复 dev 时先按照部署说明准备数据库、ES、私有配置、模型和媒体，再从当前目录直接调用：

```bash
uv run --env-file .env.local python scripts/local_services.py start
```

这个命令仍不负责启动本地 Whisper 和查询改写模型。环境依赖及验收边界见
[部署说明](../../../deploy/README.md#local-assets)。

## 这次 Git 保存的范围

- 保留已跟踪文件的当前内容、删除操作，以及未被忽略的新增源码和测试。
- dev 另保存原先被忽略的设计文档、MDV 文档、矢量图、实验脚本和依赖说明。
- 文档保持原路径；仍保留宽泛忽略规则，已纳入 Git 的文件后续修改可以正常提交。
- 不保存 `.venv`、`node_modules` 的本机依赖链接；原来被 Git 跟踪的 Logo 相对链接保留。
- `manifest.json` 列出分支、基准、变更文件、纳入的设计资料及排除的原始记录。

## 仍需私下备份的内容

`.env.local`、实际 `main-model-config.yaml`、手机测试配置、私钥/凭据、数据库备份、
业务媒体、模型权重、自训练成果，以及需保留的原始验收与调用记录不包含在这些提交中。
原始 chat-logs、deep-trace 等记录未公开上传。
本机 `outputs/migration/20261023/` 的三个 ZIP 只覆盖部分资料，不是完整设备备份。

## 验证范围

本轮按保存现有状态处理，没有修改生产逻辑或重新运行真实模型验收。
提交前逐文件核对内容摘要和文件模式，并检查已知凭据及常见密钥格式；
提交后检查 Git 对象连通性，推送后核对远端 commit 并重新 clone 检查。
历史单测和 EP04 验收结论以各工作区原记录为准，不能据此宣称 dev 的全部功能已经联合验收。
