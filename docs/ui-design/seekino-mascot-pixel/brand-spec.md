# Seekino 吉祥物 · brand-spec

来源均为 Seekino 仓库现有资产（只读引用，未修改仓库）：

- 几何：`Seekino/assets/mascot/seekino-side.svg`（侧面）、`Seekino/assets/mascot/seekino-logo.svg`（3/4 主形象）
- 组件：`Seekino/web/src/shared/Mascot.tsx`（已有 idle / seek / puzzled / oops / swim / hop 情绪）
- 色板：`seekino-logo.svg` 注释 + `web/src/shared/mascot.module.css` + 全局 tokens

| 角色 | 色值 |
|---|---|
| body | `#74c5ba` |
| shade | `#349489` |
| belly / 近侧脚 | `#fcfaf1` |
| 远侧脚 | `#e6efe8` |
| ink（描边、眼、嘴） | `#0b3b33` |
| 舞台浅底 | `--canvas #f4f7f2` / `--surface-muted #edf3ed` / `--teal-wash #eaf5f0` |
| 舞台深底 | `--stage #0c1412` |

| 鳃（矢量版） | `#126b5d`（网页 token `--teal-deep`） |

鳃裂（2026-10-17 用户确认）：眼睛后面两道竖向短纹，前长后短，顺着身体弧度。
- 像素版用 shade `#349489` 色块，不加新颜色；只在侧面、跑步、3/4、正面、睡觉姿势上画，转身时只出现 1 帧的正中过渡姿势不画。
- 矢量版用 `#126b5d`、线宽 3.2（轮廓是 4），见 `assets/seekino-logo-gills.svg`。

气质：圆、钝、面无表情的「直线嘴」，鲸鲨不是凶鲨。禁区：不加牙齿、不加新颜色、不改直线嘴。

# 轻量分镜卡（循环 sprite，< 20s）

| 镜 | 画面 | 运动 | 验收帧 |
|---|---|---|---|
| 1 | 单只吉祥物 + 地面线 + 投影 | 4 帧步态：接触 → 经过（身体 +1px）→ 反向接触 → 经过；尾巴随步态摆动；每帧前进 1 像素，脚不打滑 | 任意一帧暂停都是完整侧面立绘 |
