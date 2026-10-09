/**
 * 流式 TTS 播放（Audio8 /tts_stream）。
 *
 * 后端返回 16-bit PCM 字节流（单声道 44.1kHz LE），这里用 Web Audio API 边收边排队播放，
 * 首块到达即可出声，显著降低首包延迟。失败时返回 null，调用方应回退到非流式 base64 版本。
 */

let activeCtx: AudioContext | null = null

export function stopTtsStream(): void {
  if (activeCtx) {
    const ctx = activeCtx
    activeCtx = null
    void ctx.close().catch(() => { /* noop */ })
  }
}

export async function playTtsStream(
  text: string,
  voice: string,
  onEnd?: () => void
): Promise<{ stop: () => void } | null> {
  stopTtsStream()
  if (!text.trim()) return null
  try {
    const resp = await fetch('/api/chat_voice/tts_stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, voice })
    })
    if (!resp.ok || !resp.body) return null
    const Ctor = window.AudioContext || (window as any).webkitAudioContext
    if (!Ctor) return null
    const ctx = new Ctor({ sampleRate: 44100 })
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
          const float = new Float32Array(value.byteLength / 2)
          const dv = new DataView(value.buffer, value.byteOffset, value.byteLength)
          for (let i = 0; i < float.length; i++) float[i] = dv.getInt16(i * 2, true) / 32768
          const buf = ctx.createBuffer(1, float.length, 44100)
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
