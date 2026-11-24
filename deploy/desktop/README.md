# 在 macOS 上运行 Seekino 桌面后端

这里构建 PR 1 的原生验证宿主 `SeekinoBackend.app`。它在同一进程加载 CPython、SQLite 和 FFmpeg。PR 2 的原生窗口可直接调用 `server.desktop.runtime.open_library`。

## 构建并验证

在 Apple Silicon Mac 上安装 Xcode Command Line Tools 和 uv，从仓库根目录执行。构建器需要可随包移动的共享库版 CPython 3.12，项目验证使用 uv 的 3.12.16。输出路径须是新目录。先按媒体构建记录生成 wheel，然后使用下面的命令。[媒体源码和构建步骤](../../docs/report/server/desktop-backend/media-build.md)

```sh
uv run --locked --python 3.12.16 python deploy/desktop/build_host.py /tmp/SeekinoBackend.app --media-wheel "$stage"/wheels/av-*.whl
uv run --locked --python 3.12.16 python deploy/desktop/verify_host.py /tmp/SeekinoBackend.app
```

集成使用带构建清单的自建 wheel。开发时要对照 PyPI 的预编译版本，可显式选择 `--pypi-media`。构建环境可以安装服务器工具，应用依赖只来自 `desktop` 组。验证器在临时副本上回放固定模型响应并观察进程。该产物仅用于后端集成。[实测记录](../../docs/report/server/desktop-backend/embedded-host.md)

## 宿主怎样调用

```python
from pathlib import Path
from time import sleep
from server.desktop.runtime import open_library

root = Path.home() / "Library/Application Support/Seekino"
with open_library(root) as library:
    series = library.create_series("采访")
    upload = library.import_file(Path("访谈.mp4"), series_id=series.series_id,
                                 episode_order=1, idempotency_key="采访-第1集")
    # 命令行示例在这里等。原生窗口可用后台任务读取同一个进度。
    while (task := library.task(upload.video_parse_task_id).task).status != "COMPLETE":
        if task.status in {"PARSING_FAILED", "INDEXING_FAILED"}:
            raise RuntimeError(task.last_error)
        sleep(1)
    # 开启 keywords_only 可以离线查台词或画面文字。
    result = library.search("红旗", series_ids=[series.series_id], keywords_only=True)
    if result.hits:
        playback = library.open_result(result.hits[0].source_ref)
        # playback.path 是已复核的受管原片。start_ms/end_ms 是左闭右开的整数毫秒。
```

`open_library` 只接收目录。首次创建 `library.sqlite3`，同库的第二个宿主会收到占用错误。原片放在资料库的 `media/` 下。宿主应保持库打开以推进任务，退出时调用 `close()`。任务页可用 `set_paused`、`retry_parse`、`reparse`，按版本防止覆盖刚发生的变更。`list_series` 可列出已有系列，`delete_import` 会清理受管原片和派生索引。索引缺失时，`rebuild_index(task_id)` 使用已冻结的合同重建。

解析和描述搜索连接已配置的模型服务。开发者可把 SDK 模型配置放在资料库的 `models.yaml`，未提供时使用随包的 API 配置示例，密钥从模型配置指定的环境变量读取。数据库地址由宿主固定。服务端部署仍按 Docker 配置设置 MySQL、ES、鉴权和资源范围。[模型格式](../../deploy/cloud/models.example.yaml) · [云端部署](../cloud/README.md)

## 构建 Seekino.app 与 DMG

窗口在 `desktop/ui/`，macOS 与 Windows 均从 `desktop.main` 进入，使用同一套 Qt Quick 组件和进程内 Python 服务。Apple Silicon 上使用 Command Line Tools 或 Xcode、CPython 3.12、uv 和本页的自建媒体 wheel。目标系统从 macOS 15 起。随包的运行时按 `uv.lock` 装入，动态库和 Python 扩展位于 `Contents/Frameworks`，Python import 路径由包内相对软链接保留。

```sh
uv run --locked --python 3.12.16 python deploy/desktop/build_app.py /tmp/Seekino.app --media-wheel "$stage"/wheels/av-*.whl
uv run --locked --python 3.12.16 python deploy/desktop/verify_app.py /tmp/Seekino.app
python3 deploy/desktop/build_dmg.py /tmp/Seekino.app /tmp/Seekino-macos-arm64.dmg
```

双击 DMG，将 Seekino 拖进 Applications 后打开。用 `⌘O` 选择视频，或直接拖入窗口。用 `⌘,` 打开设置，在「模型服务 → 添加连接」填写名称，在同一表单选择 Ark 或 OpenAI 兼容协议并填写凭据。OpenAI 兼容连接可自填服务地址，可保存多个同类型连接。在「模型用途」分别给视频理解、语音转写、向量检索与观看问答选连接和模型，点「应用」。向量维度应与服务实际返回的维度一致。观看问答也可以沿用视频理解。

从工具栏打开解析任务，可以查看进度、暂停或重试。`⌘K` 查找台词或画面，点击结果从对应时间回看原片。`⌘⇧L` 收起或展开系列资料树。设置 → 外观可切换中文与 English，选择保存在本机。F1 或帮助菜单打开原生使用文档窗口。替换程序时资料库仍在 `~/Library/Application Support/Seekino/`。程序内保存的模型凭据由 macOS 钥匙串持有。

首页可横向浏览系列。点资料树的箭头展开剧集，点系列名称打开选集。普通播放退出后，回到系列页可从「继续观看」接着看。搜索结果显示原片画面、来源和时间段，点「回看原片」定位到命中处。[内页验收](../../docs/report/desktop/native/2026-11-23-acceptance.md#024-内页验收)

播放时点 Kino，打开陪看面板里的「观看设置」。选择「右侧栏」给视频和问答各留一块空间，选择「悬浮窗」可按住标题栏移动整个对话。可以指定鼠标移开几秒后淡化，或关闭自动淡化。窗口宽度不超过 800 点时，对话显示在播放器下方。

在 macOS 活动监视器中按 Seekino 过滤也会显示系统为该应用提供的服务。使用系统文件选择框时可以看到 Open and Save Panel Service 和 QuickLookUIService，系统自动填充也可单列一行。Python、SQLite 与媒体解码库加载在 Seekino 主进程内；衡量整个操作的内存占用时，需同时核对归属于应用的系统服务。 [Apple 文件选择框说明](https://developer.apple.com/documentation/appkit/nsopenpanel)

默认命令生成供本机验收的 ad-hoc 签名包。正式分发时，构建 App 增加 `--identity`，构建 DMG 增加同一 `--identity` 与 `--notary-profile`。以签名公证成功的实际 DMG 做干净机器安装验收。媒体 wheel 保留 FFmpeg 许可和构建清单，随正式发布提供该清单对应的 FFmpeg 源码。 [Apple 分发说明](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution) · [媒体构建记录](../../docs/report/server/desktop-backend/media-build.md)


## Windows x64 桌面验收包

解压 ZIP 后打开 `Seekino/Seekino.exe`，保留同目录的运行库。资料库存放在 `%LOCALAPPDATA%/Seekino`，模型密钥保存在 Windows 凭据管理器。文件选择和系统窗口控件由 Windows 提供。`Ctrl+O` 导入、`Ctrl+K` 搜索；设置 → 外观可切换中文与 English，F1 打开使用文档。

用 uv 下载 x64 Windows CPython 3.12.16，构建机安装项目 Python 依赖、Pillow、pefile 和 Zig。构建器按锁文件校验 Windows wheel，并链接 Windows GUI 入口。在有独立 RC 编译器的环境可传 `--rc` 指定。

```sh
uv python install --no-bin --no-registry --install-dir /tmp/seekino-python cpython-3.12.16-windows-x86_64-none
python deploy/desktop/build_windows.py /tmp/Seekino-windows-x64.zip --python-runtime /tmp/seekino-python/cpython-3.12.16-windows-x86_64-none
```

macOS 候选使用项目自建的 FFmpeg 与 VideoToolbox。Windows 验收包使用锁定的 PyAV Windows wheel 和其中的 libx264 编码器。Windows 真机安装、音频设备、凭据存取及正式分发所需的媒体构建和签名需单独验收。两个窗口共用资料树、搜索与回放逻辑，每个平台的数据库保存在本机。
