# Seekino.app 安装候选实测

2026-11-23，在 Apple Silicon、macOS 26.5.1 上，用 CPython 3.12.16、Swift 6.3.3、PR #183 的 PyAV 19.0.2 和 FFmpeg 9.0.2 自建 wheel 验证 [PR #184](https://github.com/owariband/Seekino/pull/184)。App 声明最低 macOS 15，包内最高 Mach-O 部署目标来自 orjson 扩展。macOS 15 真机与 Intel 尚未测试。

| 操作 | 实测 |
| --- | --- |
| 建库与搜索 | 空 SQLite 库通过桌面调用入口导入两秒的带 AAC 音轨样片，V2 固定模型回放完成。关键词“红旗”和描述“挥动旗帜”均返回原片 [300, 1200) ms，回源文件与输入字节相同。固定回复在独立 Python 回放中提供，使用候选包的依赖。 |
| 原生播放 | 搬移并验证签名后的 App 在空 PATH 打开已索引的库，Swift 调用同一搜索入口和证据复核，首个画面与 PCM 样本均为 300 ms。播放时钟推进到 487 ms，暂停跳转稳定在 200 ms。App 观测为 0 次 fork 或 exec，观察器对照能捕获短命子进程。 |
| 原生画面 | 在本机 SwiftUI 窗口预填搜索后选择命中，显示片段时间、来源文件及连续播放后的红色测试帧。预填命令只加入临时视觉检查副本。按该进程 PID 查询，本机 TCP 监听和子进程均为 0。 |
| DMG | 本机 ad-hoc 签名包通过磁盘映像校验、只读挂载和复制后的严格签名检查。从复制出的 App 重开同一 SQLite 库，仍命中 [300, 1200) ms，首帧与音轨为 300 ms，暂停跳转为 200 ms。 |
| 回归 | SQLite、存储配置和空库 22 项通过。桌面 V2、VideoToolbox 媒体和上传恢复 11 项通过，两组共 33 项。 |

第一版播放在创建带自定义 IOSurface 字典的视频缓冲区时崩溃。调用栈落在 CoreVideo 的 `CVPixelBufferCreate`。改用默认缓冲区属性后，测试程序和实际窗口均成功输出画面。自动播放验证以静音运行，音轨使用解码出的 PCM 时间检查。

本地 DMG 为 89 MiB，SHA-256 为 `e172bb18b3a6f2e2cdedbc05f51905b44b4da8395d5e0b48a658d51c453397fd`。候选 App 含全部 Python、模型 SDK、SQLite 扩展和媒体库，安装时从 DMG 拖入 Applications。安装、签名与复跑命令见 [桌面构建文档](../../../../deploy/desktop/README.md)。

下一轮使用实际模型凭据和用户的短片及长片验证解析质量与响应，检查发布签名及公证后的干净机器启动、连续长片音画和跨版本升级。当前机器签名钥匙串没有有效的 Developer ID Application 身份，因此这份产物供本机验收。进度见 [工作台](../../../dev/workbench.html)。
