<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { NSelect } from 'naive-ui'
import {
  Mic, MicOff, Close, StopCircleOutline
} from '@vicons/ionicons5'

import { getTtsConfig, playTtsStream, stopTtsStream } from '../api/ttsStream'

const { t } = useI18n()

// ---------- 状态机 ----------
type CallState = 'idle' | 'listening' | 'thinking' | 'speaking'
const state = ref<CallState>('idle')
const replyText = ref('')      // AI 回复文本（字幕）
const liveText = ref('')       // 实时识别中间结果
const voices = ref<{ label: string; value: string }[]>([])
const selectedVoice = ref(localStorage.getItem('chat_voice_name') || 'zh')
const isListening = ref(false)

// 通过 opener 访问主窗口的语音桥接（同步输入框 / 发送 / 接收回复）
const bridge = (window as any).opener?.chatBridge
const hasBridge = !!bridge

const stateText = computed(() => {
  switch (state.value) {
    case 'listening': return t('voice.listening')
    case 'thinking': return t('voice.thinking')
    case 'speaking': return t('voice.speaking')
    default: return t('voice.idleHint')
  }
})

// ---------- STT（语音识别 → 实时同步主窗口输入框 → 停顿自动发送） ----------
let recognition: any = null
let accumulated = ''           // 当前句累积文本
let sendTimer: number | null = null

function speechLang(): string {
  const loc = (localStorage.getItem('chat_primary_language') || 'zh').toLowerCase()
  if (loc.startsWith('ja')) return 'ja-JP'
  if (loc.startsWith('en')) return 'en-US'
  return 'zh-CN'
}

function clearSendTimer() {
  if (sendTimer) { clearTimeout(sendTimer); sendTimer = null }
}

/** 识别到最终文本：追加累积 → 同步主窗口输入框 → 重置停顿计时 */
function onFinalSegment(segment: string) {
  accumulated = (accumulated + ' ' + segment).replace(/\s+/g, ' ').trim()
  if (accumulated) bridge?.updateInput?.(accumulated)
  clearSendTimer()
  sendTimer = window.setTimeout(() => autoSend(), 900)
}

/** 停顿结束：自动发送累积文本（无需手动点击发送） */
function autoSend() {
  clearSendTimer()
  const value = accumulated
  accumulated = ''
  if (!value.trim() || !hasBridge) return
  state.value = 'thinking'
  replyText.value = ''
  bridge.onReply?.((reply: string) => {
    replyText.value = reply
    state.value = 'speaking'
    void speak(reply)
  })
  bridge.sendMessage?.(value)
}

function startListening() {
  const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
  if (!SR) {
    liveText.value = t('voice.unsupported')
    return
  }
  if (recognition) stopListening()
  const rec = new SR()
  recognition = rec
  rec.continuous = true
  rec.interimResults = true
  rec.lang = speechLang()
  rec.onresult = (event: any) => {
    let final = ''
    let interim = ''
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const tr = event.results[i][0].transcript
      if (event.results[i].isFinal) final += tr
      else interim += tr
    }
    liveText.value = (accumulated ? accumulated + ' ' : '') + interim
    if (final) onFinalSegment(final)
  }
  rec.onerror = (event: any) => {
    if (event.error === 'not-allowed') {
      isListening.value = false
      liveText.value = t('voice.micDenied')
    }
  }
  rec.onend = () => {
    isListening.value = false
    recognition = null
    // 识别意外结束后仍有未发送文本 → 自动发送
    if (accumulated.trim()) autoSend()
    if (state.value === 'listening') state.value = 'idle'
  }
  rec.start()
  isListening.value = true
  state.value = 'listening'
}

function stopListening() {
  clearSendTimer()
  if (recognition) {
    try { recognition.stop() } catch { /* noop */ }
    recognition = null
  }
  isListening.value = false
  liveText.value = ''
  if (accumulated.trim()) autoSend()
  if (state.value === 'listening') state.value = 'idle'
}

function toggleListening() {
  if (isListening.value) stopListening()
  else startListening()
}

// ---------- TTS 播报 ----------
let replyAudio: HTMLAudioElement | null = null
let streamStop: (() => void) | null = null

function stopPlayback() {
  if (streamStop) { streamStop(); streamStop = null }
  if (replyAudio) { replyAudio.pause(); replyAudio = null }
}

async function speak(content: string) {
  stopPlayback()
  // 按后端 TTS_MODE 分流：file 直接走非流式；stream 流式优先（首包低延迟），失败回退非流式
  const cfg = await getTtsConfig()
  if (cfg.mode !== 'stream') {
    await playBase64Reply(content)
    return
  }
  const handle = await playTtsStream(content, selectedVoice.value, () => {
    streamStop = null
    state.value = 'idle'
    startListening()
  })
  if (handle) {
    streamStop = handle.stop
    return
  }
  // 回退：非流式 base64（保留当前可用版本）
  await playBase64Reply(content)
}

async function playBase64Reply(content: string) {
  try {
    const resp = await fetch('/api/chat_voice/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: content, voice: selectedVoice.value })
    })
    const data = await resp.json()
    if (!data.audio) {
      state.value = 'idle'
      startListening()
      return
    }
    const audio = new Audio('data:audio/wav;base64,' + data.audio)
    replyAudio = audio
    audio.onended = () => {
      replyAudio = null
      state.value = 'idle'
      startListening()
    }
    audio.onerror = () => {
      replyAudio = null
      state.value = 'idle'
    }
    audio.play().catch(() => {
      replyAudio = null
      state.value = 'idle'
    })
  } catch {
    state.value = 'idle'
  }
}

function hangUp() {
  stopListening()
  stopPlayback()
  state.value = 'idle'
  replyText.value = ''
  liveText.value = ''
}

function closeWindow() {
  hangUp()
  window.close()
}

// ---------- 生命周期 ----------
onMounted(async () => {
  document.title = t('voice.windowTitle')
  try {
    const resp = await fetch('/api/chat_voice/voices')
    voices.value = await resp.json()
    const saved = localStorage.getItem('chat_voice_name')
    if (saved && voices.value.some((v) => v.value === saved)) selectedVoice.value = saved
  } catch { /* noop */ }
  if (hasBridge) startListening()
})

onUnmounted(() => {
  clearSendTimer()
  stopListening()
  stopPlayback()
})
</script>

<template>
  <div class="voice-call">
    <!-- 顶部栏 -->
    <div class="top-bar">
      <div class="call-status">
        <span class="status-dot" :class="{ active: state !== 'idle' }"></span>
        <span>{{ state !== 'idle' ? t('voice.connected') : t('voice.disconnected') }}</span>
      </div>
      <div class="top-actions">
        <NSelect
          v-model:value="selectedVoice"
          class="voice-select"
          size="small"
          :options="voices.map((v) => ({ label: v.label, value: v.value }))"
          :placeholder="t('voice.selectVoice')"
          @update:value="(val) => localStorage.setItem('chat_voice_name', String(val))"
        />
        <button class="icon-btn hang-up" :title="t('voice.hangUp')" @click="hangUp">
          <StopCircleOutline />
        </button>
        <button class="icon-btn" :title="t('voice.close')" @click="closeWindow">
          <Close />
        </button>
      </div>
    </div>

    <!-- 中央状态 -->
    <div class="center-area">
      <button class="mic-btn" :class="{ active: isListening }" @click="toggleListening" :title="isListening ? t('voice.stopListen') : t('voice.startListen')">
        <Mic v-if="isListening" />
        <MicOff v-else />
      </button>
      <div class="state-text">{{ stateText }}</div>
      <div v-if="liveText" class="live-text">{{ liveText }}</div>
      <div v-if="replyText" class="reply-text">{{ replyText }}</div>
      <div v-if="!hasBridge" class="no-bridge">{{ t('voice.noBridge') }}</div>
    </div>
  </div>
</template>

<style scoped>
.voice-call {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: #0f1115;
  color: #e5e7eb;
  font-family: inherit;
}

.top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 14px;
  background: #171a21;
  border-bottom: 1px solid #262b36;
}
.call-status {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #9ca3af;
  white-space: nowrap;
}
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #6b7280;
}
.status-dot.active {
  background: #22c55e;
  box-shadow: 0 0 8px #22c55e;
}
.top-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.voice-select {
  width: 150px;
}
.icon-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: #9ca3af;
  cursor: pointer;
  flex: 0 0 auto;
}
.icon-btn:hover {
  background: #262b36;
  color: #e5e7eb;
}
.icon-btn svg {
  width: 17px;
  height: 17px;
}
.icon-btn.hang-up {
  color: #ef4444;
}
.icon-btn.hang-up:hover {
  background: rgba(239, 68, 68, 0.15);
}

.center-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  padding: 0 20px;
  overflow-y: auto;
}
.mic-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 88px;
  height: 88px;
  border-radius: 50%;
  border: 2px solid #334155;
  background: radial-gradient(circle at 35% 35%, #2b3a55, #1a2333);
  color: #cbd5e1;
  cursor: pointer;
}
.mic-btn.active {
  border-color: #22c55e;
  box-shadow: 0 0 24px rgba(34, 197, 94, 0.4);
}
.mic-btn svg {
  width: 34px;
  height: 34px;
}
.state-text {
  font-size: 14px;
  color: #9ca3af;
}
.live-text {
  max-width: 100%;
  padding: 8px 12px;
  background: #1a2333;
  border-radius: 8px;
  font-size: 13px;
  color: #e5e7eb;
  word-break: break-word;
}
.reply-text {
  max-width: 100%;
  padding: 8px 12px;
  background: #1e293b;
  border-radius: 8px;
  font-size: 13px;
  color: #93c5fd;
  word-break: break-word;
}
.no-bridge {
  color: #f59e0b;
  font-size: 13px;
}
</style>
