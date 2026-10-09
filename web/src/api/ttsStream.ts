/**
 * 流式 TTS 播放。
 *
 * 后端返回 16-bit PCM 字节流（单声道，引擎采样率 genie=32k / edge+sapi=24k），
 * 这里用 Web Audio API 边收边排队播放：PCM 先线性重采样到 AudioContext 的设备
 * 采样率（不强行指定 32k 运行，避免 Windows 声卡驱动低质量重采样导致的嘶嘶声），
 * 首块到达即可出声。失败时返回 null，调用方应回退到非流式 base64 版本。
 */

let activeCtx: AudioContext | null = null

export interface TtsConfig {
  mode: 'stream' | 'file'
  chunk_frames: number
  sample_rate: number
}

let cachedConfig: TtsConfig | null = null

/** 读取后端播报模式配置（TTS_MODE / TTS_STREAM_CHUNK_FRAMES），带进程内缓存 */
export async function getTtsConfig(): Promise<TtsConfig> {
  if (cachedConfig) return cachedConfig
  try {
    const resp = await fetch('/api/chat_voice/config', { cache: 'no-store' })
    if (!resp.ok) throw new Error('config fetch failed')
    cachedConfig = (await resp.json()) as TtsConfig
  } catch {
    cachedConfig = { mode: 'file', chunk_frames: 48, sample_rate: 44100 }
  }
  return cachedConfig
}

export function stopTtsStream(): void {
  if (activeCtx) {
    const ctx = activeCtx
    activeCtx = null
    void ctx.close().catch(() => { /* noop */ })
  }
}

/** 线性插值重采样单声道 Float32 PCM 到目标采样率（srcRate === dstRate 时原样返回）。 */
export function resampleFloat32(src: Float32Array, srcRate: number, dstRate: number): Float32Array {
  if (!src.length || srcRate === dstRate || dstRate <= 0) return src
  const n = Math.max(1, Math.round((src.length * dstRate) / srcRate))
  const out = new Float32Array(n)
  const step = (src.length - 1) / (n - 1)
  for (let i = 0; i < n; i++) {
    const pos = i * step
    const i0 = Math.floor(pos)
    const i1 = Math.min(i0 + 1, src.length - 1)
    const frac = pos - i0
    out[i] = src[i0] * (1 - frac) + src[i1] * frac
  }
  return out
}

/** 朗读前的文本清理：去掉 markdown 标记、emoji、颜文字，只保留可朗读的正文。 */
export function cleanSpeechText(text: string): string {
  if (!text) return ''
  let s = text
  // 代码块整体跳过（朗读正文，不读代码）
  s = s.replace(/```[\s\S]*?```/g, ' ')
  s = s.replace(/~~~[\s\S]*?~~~/g, ' ')
  s = s.replace(/`[^`]*`/g, ' ')
  // 图片/链接：保留可见文本
  s = s.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
  s = s.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
  // 裸 URL 与邮箱
  s = s.replace(/https?:\/\/\S+/g, ' ')
  s = s.replace(/[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g, ' ')
  // markdown 块级符号
  s = s.replace(/^#{1,6}\s*/gm, '')
  s = s.replace(/^\s*[-*+]\s+/gm, '')
  s = s.replace(/^\s*\d+\.\s+/gm, '')
  s = s.replace(/^\s*>\s+/gm, '')
  s = s.replace(/^\s*\|/gm, ' ')
  s = s.replace(/\|\s*$/gm, ' ')
  s = s.replace(/^\s*([-*_])\1{2,}\s*$/gm, ' ')
  // 颜文字（须在行内强调之前处理，否则 T_T 这类会被 _x_ 规则拆散）
  s = s.replace(/[（(][^（）()\u4e00-\u9fff，。！？；：、""''「」【】a-zA-Z0-9]*[)）]/g, ' ')
  s = s.replace(/(^|\s)(orz|QAQ|T_T|TAT|TTATT|X_X|o_O|O_O|>_<|\^_\^|u_u|0_0)(?=\s|$)/gi, '$1')
  // 行内强调
  s = s.replace(/\*\*([^*]+)\*\*/g, '$1')
  s = s.replace(/__([^_]+)__/g, '$1')
  s = s.replace(/\*([^*]+)\*/g, '$1')
  s = s.replace(/_([^_]+)_/g, '$1')
  s = s.replace(/~~([^~]+)~~/g, '$1')
  s = s.replace(/(^|\s)[#>*~|`\-+]+/g, '$1')
  // emoji（Unicode 范围 + 变体选择符 + ZWJ + 肤色调）
  s = s.replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE00}-\u{FE0F}\u{200D}\u{20E3}\u{1F3FB}-\u{1F3FF}\u{2190}-\u{21FF}\u{2B05}-\u{2B07}\u{2934}-\u{2935}\u{25A0}-\u{25FF}\u{2B00}-\u{2BFF}]/gu, ' ')
  // 压缩空白
  s = s.replace(/\s+/g, ' ').trim()
  return s
}

export async function playTtsStream(
  text: string,
  voice: string,
  onEnd?: () => void
): Promise<{ stop: () => void } | null> {
  stopTtsStream()
  if (!text.trim()) return null
  const controller = new AbortController()
  try {
    const resp = await fetch('/api/chat_voice/tts_stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, voice }),
      signal: controller.signal
    })
    if (!resp.ok || !resp.body) return null
    const Ctor = window.AudioContext || (window as any).webkitAudioContext
    if (!Ctor) return null
    const cfg = await getTtsConfig()
    // 用默认（设备）采样率创建 context；PCM 在前端重采样到 ctx.sampleRate，
    // 避免 AudioContext 运行在 32k/24k 时交给 Windows 驱动低质量重采样产生嘶嘶声
    const ctx = new Ctor()
    activeCtx = ctx
    if (ctx.state === 'suspended') await ctx.resume()

    const reader = resp.body.getReader()
    let nextTime = ctx.currentTime + 0.08
    let finished = false

    const pump = async () => {
      try {
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          if (!value || !value.byteLength) continue
          let float = new Float32Array(value.byteLength / 2)
          const dv = new DataView(value.buffer, value.byteOffset, value.byteLength)
          for (let i = 0; i < float.length; i++) float[i] = dv.getInt16(i * 2, true) / 32768
          float = resampleFloat32(float, cfg.sample_rate, ctx.sampleRate)
          if (!float.length) continue
          const buf = ctx.createBuffer(1, float.length, ctx.sampleRate)
          buf.copyToChannel(float, 0)
          const src = ctx.createBufferSource()
          src.buffer = buf
          src.connect(ctx.destination)
          const startAt = Math.max(ctx.currentTime + 0.01, nextTime)
          src.start(startAt)
          nextTime = startAt + buf.duration + 0.004
        }
      } catch { /* 流中断：播放提前结束 */ }
      finished = true
      const remainMs = Math.max(80, (nextTime - ctx.currentTime) * 1000 + 150)
      window.setTimeout(() => {
        if (activeCtx === ctx) {
          activeCtx = null
          void ctx.close()
        }
        onEnd?.()
      }, remainMs)
    }
    void pump()
    return {
      stop: () => {
        // 先中断 fetch：后端 StreamingResponse 会因此终止合成，不再白白占用 CPU
        controller.abort()
        if (activeCtx === ctx) {
          activeCtx = null
          void ctx.close().catch(() => { /* noop */ })
        }
      }
    }
  } catch {
    return null
  }
}
