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
