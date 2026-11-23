# Apple Silicon 媒体库构建记录

桌面宿主使用 PyAV 19.0.2 调用 FFmpeg 9.0.2。两份源码的 SHA-256 都固定在仓库里。FFmpeg 的摘要是 `8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e`，PyAV 的摘要来自 `uv.lock`。FFmpeg 9.0.2 对应 libavcodec / libavformat 63、libavutil 61、libavdevice 63、libavfilter 12、libswscale 10 和 libswresample 7。[FFmpeg 发布与签名说明](https://ffmpeg.org/download.html#releases) · [PyAV 源码安装](https://pyav.basswood.io/docs/stable/overview/installation.html#bring-your-own-ffmpeg)

## 重建

在 Apple Silicon Mac 上使用 Xcode Command Line Tools、仓库指定的 Python 3.12 和 `uv`。以下命令从仓库根目录执行。所有依赖和产物都写入新建的临时目录。

```sh
stage=$(mktemp -d)
uv venv --python 3.12 "$stage/build-env"
uv pip install --python "$stage/build-env/bin/python" cython==3.3.1 setuptools==84.2.1 wheel==0.48.0 delocate==0.13.0
curl --fail --location --output "$stage/ffmpeg-9.0.2.tar.xz" https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz
curl --fail --location --output "$stage/av-19.0.2.tar.gz" https://files.pythonhosted.org/packages/23/01/76b6bf563fe907597cf58f10d10168e93fe588b6db9f2658db87e87efa7e/av-19.0.2.tar.gz
"$stage/build-env/bin/python" deploy/desktop/build_media.py --sources "$stage" --wheel-dir "$stage/wheels"
```

构建脚本先检查归档的 SHA-256，再编译 FFmpeg 的共享库。配置显式开启 VideoToolbox，并使用 macOS 随附的 zlib 和 bzip2 处理压缩媒体数据。构建关闭外部库自动探测、GPL、version3、nonfree、网络协议和命令行程序。PyAV 只用这次构建的头文件和库编译。随后 delocate 把动态库复制进 wheel，修正加载路径，脚本再逐个核对扩展和动态库的 Mach-O 架构及依赖。[构建脚本](../../../../deploy/desktop/build_media.py)

wheel 中的 `av/.dylibs/` 保存 FFmpeg 动态库。`av-19.0.2.dist-info/seekino-media.json` 保存两份源码的地址、摘要和构建选项，`licenses/FFmpeg/` 保存 FFmpeg 的 `LICENSE.md` 和 `COPYING.LGPLv2.1`。发布包的签名、公证、源码提供方式和最终分发义务由发布环节核对。[FFmpeg 项目许可说明](https://ffmpeg.org/legal.html)

## 本机验收

2026-11-23，在 Apple Silicon 开发机上用 macOS 26.5.1、Apple clang 21.0.0、macOS SDK 26.5 和 CPython 3.12.16 编译。FFmpeg 官网下载超时后取了 MacPorts 镜像的同版归档，按 FFmpeg 官方公布的 RSA 公钥指纹 `FCF986EA15E6E293A5644F10B4322F04D67658D8` 核对了归档的分离签名。PyAV 源码归档的 SHA-256 与锁文件相同。

产物 `av-19.0.2-cp312-abi3-macosx_11_0_arm64.whl` 共 11,710,447 字节，本次产物的 SHA-256 为 `c6c00e76740c716e82f909d6f511834ab63b015b1f2e4f822561db2fb18dcb04`。对 50 个 PyAV 扩展和 7 个 FFmpeg 动态库分别执行了架构和加载路径检查。库内依赖均能从 wheel 解析，外部依赖指向 macOS 框架或系统库。重新加载 `libavcodec` 后，读出的构建配置和 H.264 VideoToolbox 编码器与预期一致。wheel 的部署目标为 macOS 11，实际运行环境为 26.5.1。

把 wheel 安装到新的临时目录，退出编译目录，将 `PATH` 设为空路径，直接调用 `PyAVMedia`。样片的视频轨为 H.264、128×72、24 fps、2500 ms，AAC 音轨延伸到 3000 ms；文件 SHA-256 为 `1d0b888956fcb083747cf5522b38a32adefb27c425e878679632a4c948676f30`。

| 从业务代码发起的调用 | 回读结果 |
| --- | --- |
| 读视频时长 | 2500 ms，与视频轨一致 |
| 在 1333 ms 取帧 | JPEG 能解码，尺寸 128×72 |
| 提取 1000–2500 ms 的 WAV | 16 kHz，解码得到 24,000 个采样 |
| 用 VideoToolbox 编码 500–2500 ms 的 MP4 | 2000 ms，H.264 视频可解码 48 帧，保留 AAC 音轨 |

开发沙箱里打开 VideoToolbox 硬编码会话失败。同一台 Mac 上获得硬件访问后，这一组调用通过。这里只记录当前硬件上的运行结果；干净机器安装、签名和公证留到桌面交付验收。宿主如何装入这份自建 wheel 见[桌面宿主构建说明](../../../../deploy/desktop/README.md)。

同一份自建 wheel 还跑了媒体适配器和 Server 媒体服务测试。通用环境用 FFmpeg 自带的 MPEG-4 编码器测试时间和音轨，本机另开 VideoToolbox 用例，用产品默认 `PyAVMedia()` 编码并断言输出 H.264。普通 Mac 进程共通过 54 项，沙箱通过 52 项、跳过两项需要硬件编码的检查。[媒体适配器测试](../../../../tests/server/test_embedded_media.py) · [Server 媒体服务测试](../../../../tests/server/test_video_parse_media.py)

在上面的目录中构建 wheel 后，可在项目测试环境中把它放到导入路径首位并运行这组用例。测试数据使用本机 FFmpeg CLI 生成，正式媒体处理调用 PyAV。

```sh
uv pip install --python .venv/bin/python --no-deps --target "$stage/installed-media" "$stage"/wheels/av-*.whl
SEEKINO_TEST_VIDEOTOOLBOX=1 PYTHONPATH="$stage/installed-media:." .venv/bin/python -m pytest -q tests/server/test_embedded_media.py tests/server/test_video_parse_media.py
```
