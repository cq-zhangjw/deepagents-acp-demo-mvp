---
name: remotion-best-practices
description: Remotion 最佳实践 - 使用 React 创建视频
metadata:
  tags: remotion, video, react, animation, composition, 视频制作, 动画
---

## 何时使用

当您处理 Remotion 代码时,请使用此技能来获取特定领域的知识和最佳实践。

## 如何使用

阅读各个规则文件以获取详细说明和代码示例:

### 核心概念

- [rules/animations.md](rules/animations.md) - Remotion 的基础动画技巧
- [rules/compositions.md](rules/compositions.md) - 定义组合、静态图像、文件夹、默认属性和动态元数据
- [rules/timing.md](rules/timing.md) - Remotion 中的插值曲线 - 线性、缓动、弹簧动画
- [rules/sequencing.md](rules/sequencing.md) - Remotion 的序列模式 - 延迟、修剪、限制项目持续时间

### 媒体处理

- [rules/images.md](rules/images.md) - 使用 Img 组件在 Remotion 中嵌入图像
- [rules/videos.md](rules/videos.md) - 在 Remotion 中嵌入视频 - 修剪、音量、速度、循环、音调
- [rules/audio.md](rules/audio.md) - 在 Remotion 中使用音频和声音 - 导入、修剪、音量、速度、音调
- [rules/gifs.md](rules/gifs.md) - 显示与 Remotion 时间轴同步的 GIF
- [rules/assets.md](rules/assets.md) - 将图像、视频、音频和字体导入 Remotion

### 高级功能

- [rules/3d.md](rules/3d.md) - 使用 Three.js 和 React Three Fiber 在 Remotion 中创建 3D 内容
- [rules/display-captions.md](rules/display-captions.md) - 在 Remotion 中显示字幕,支持 TikTok 风格的分页和单词高亮
- [rules/import-srt-captions.md](rules/import-srt-captions.md) - 使用 @remotion/captions 将 .srt 字幕文件导入 Remotion
- [rules/transcribe-captions.md](rules/transcribe-captions.md) - 转录音频以在 Remotion 中生成字幕
- [rules/lottie.md](rules/lottie.md) - 在 Remotion 中嵌入 Lottie 动画
- [rules/charts.md](rules/charts.md) - Remotion 的图表和数据可视化模式
- [rules/maps.md](rules/maps.md) - 使用 Mapbox 添加地图并进行动画处理

### 文本和排版

- [rules/fonts.md](rules/fonts.md) - 在 Remotion 中加载 Google 字体和本地字体
- [rules/text-animations.md](rules/text-animations.md) - Remotion 的排版和文本动画模式
- [rules/measuring-text.md](rules/measuring-text.md) - 测量文本尺寸、使文本适应容器以及检查溢出
- [rules/measuring-dom-nodes.md](rules/measuring-dom-nodes.md) - 在 Remotion 中测量 DOM 元素尺寸

### 视频处理工具

- [rules/extract-frames.md](rules/extract-frames.md) - 使用 Mediabunny 在特定时间戳从视频中提取帧
- [rules/get-video-duration.md](rules/get-video-duration.md) - 使用 Mediabunny 获取视频文件的时长(秒)
- [rules/get-video-dimensions.md](rules/get-video-dimensions.md) - 使用 Mediabunny 获取视频文件的宽度和高度
- [rules/get-audio-duration.md](rules/get-audio-duration.md) - 使用 Mediabunny 获取音频文件的时长(秒)
- [rules/can-decode.md](rules/can-decode.md) - 使用 Mediabunny 检查浏览器是否可以解码视频

### 配置和优化

- [rules/parameters.md](rules/parameters.md) - 通过添加 Zod 模式使视频可参数化
- [rules/calculate-metadata.md](rules/calculate-metadata.md) - 动态设置组合持续时间、尺寸和属性
- [rules/tailwind.md](rules/tailwind.md) - 在 Remotion 中使用 TailwindCSS
- [rules/transparent-videos.md](rules/transparent-videos.md) - 渲染带有透明度的视频
- [rules/trimming.md](rules/trimming.md) - Remotion 的修剪模式 - 剪切动画的开头或结尾
- [rules/transitions.md](rules/transitions.md) - Remotion 的场景转场模式

## 重要提示

- 所有动画必须由 `useCurrentFrame()` 钩子驱动
- 禁止使用 CSS 过渡或动画 - 它们无法正确渲染
- 禁止使用 Tailwind 动画类名 - 它们无法正确渲染
- 使用秒为单位编写动画,并乘以 `useVideoConfig()` 中的 `fps` 值
