# Seekino 像素吉祥物 · A 终端团子

只依赖 React 的像素鲸鲨组件，五种状态共用同一套帧数据。展示页和 MP4 短片也是从同一份引擎生成的。

| 状态 | 用在哪 | 动作 |
|---|---|---|
| `wander` 游荡 | 空闲页面、首页角落 | 走到一端，转身正对屏幕，左右看，再走回去 |
| `look` 停下看屏幕 | 等用户输入 | 原地转过来，左右看一眼，眨眼，小跳 |
| `loading` 加载中 | 检索、生成回答 | 原地走，地面往后滚，头顶三个点依次亮起 |
| `run` 跑步 | 长任务赶进度 | 身体前倾，两步一腾空，扬起尘土，到头急刹转身 |
| `sleep` 睡觉 | 空状态、暂停、夜间 | 趴下闭眼，背部起伏呼吸，头顶飘 Z |

切换状态时会从当前姿势接着动：躺着会先爬起来，侧身会先转过来，冲刺中停下会急刹。系统开启「减少动态效果」时只显示一帧。标签页隐藏时暂停。

```tsx
import { PixelMascot } from "./PixelMascot";

<PixelMascot state="loading" width={96} scale={3} title="正在检索" />
```

- `width`：场景宽度，单位是像素格（最少 38），鲸鲨在这个宽度里来回走。
- `scale`：每个像素格占多少 CSS 像素。
- `ground`：是否画地面虚线和脚下阴影，默认画。
- 颜色可以用 CSS 变量覆盖：`--pixel-mascot-body`、`-shade`、`-belly`、`-ink`、`-ground`、`-shadow`。

要放进 Seekino 的 `web/src/shared/`，复制 `src/engine.ts` 和 `src/PixelMascot.tsx` 两个文件就行，测试在 `src/*.test.ts(x)`。

## 命令

```bash
npm test                          # 26 个测试：姿势、步态不打滑、边界、状态衔接、组件渲染
npm run typecheck
node scripts/build-gallery.mjs    # 生成 ../Seekino Pixel A · 动作合集.html
node scripts/shoot-gallery.mjs    # 截图并检查控制台报错（需 Chromium）
node scripts/reel.mjs             # 生成 ../out/seekino-pixel-reel.mp4（需 Chromium、ffmpeg）
node scripts/contact.mjs <out.png> <state> [width] [from] [count] [cols]   # 逐帧拼图检查
```

Chromium 借用的是 `Seekino/web/node_modules` 里的 Playwright。

## 短片

`../out/seekino-pixel-reel.mp4`：1920×1080，30fps，18.8 秒，带音频。分镜和音频 cue 见 `../导演稿.md`。

- 画面：直接把引擎帧放大 20 倍逐帧写进 ffmpeg，没有浏览器录屏，所以每次生成结果一样。
- 音效：8-bit 方波、三角波和噪声现场合成，时间点取自引擎帧上的 `cue` 字段，和画面逐帧对齐。
- 配乐：`bgm-tech.mp3`（huashu-design 自带），低通 4kHz；音效高通 800Hz，两轨 `normalize=0` 混合。
- 字标：Seekino 网页同款 Inter 750（`assets/fonts`，SIL OFL）。
