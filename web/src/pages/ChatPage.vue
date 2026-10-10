<script setup lang="ts">
import { computed, h, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import { cleanSpeechText, getTtsConfig, playTtsStream, stopTtsStream } from '../api/ttsStream'
import {
  AddOutline,
  ArrowDownOutline,
  AttachOutline,
  ChevronBackOutline,
  CloseOutline,
  CodeSlashOutline,
  ConstructOutline,
  CopyOutline,
  CreateOutline,
  DesktopOutline,
  DocumentOutline,
  FolderOutline,
  EllipsisHorizontalOutline,
  LanguageOutline,
  CallOutline,
  MenuOutline,
  MicOutline,
  PaperPlaneOutline,
  RefreshOutline,
  SearchOutline,
  SparklesOutline,
  StopCircleOutline,
  TrashOutline,
  VolumeHighOutline,
  VolumeMuteOutline
} from '@vicons/ionicons5'
import {
  NAlert,
  NAvatar,
  NButton,
  NCard,
  NDrawer,
  NDrawerContent,
  NDropdown,
  NIcon,
  NInput,
  NModal,
  NPopconfirm,
  NSelect,
  NSpin,
  NSwitch,
  NTag,
  NTabs,
  NTabPane,
  NTooltip,
  useMessage
} from 'naive-ui'
import { useI18n } from 'vue-i18n'
import { storageKey, type SupportedLocale } from '../i18n'
import ExecutionProcess from '../components/ExecutionProcess.vue'
import MarkdownMessage from '../components/MarkdownMessage.vue'
import ToolCallCard from '../components/ToolCallCard.vue'

type TaskStatus = 'pending' | 'streaming' | 'completed' | 'cancelled' | 'failed' | 'waiting_permission'
type ToolStatus = 'running' | 'completed' | 'failed' | 'cancelled' | 'waiting_permission'

interface AttachmentRef {
  path?: string
  uri?: string
  name: string
  mimeType?: string
  kind?: 'file' | 'image'
  data?: string
  previewUrl?: string
}

interface AnalysisEntry {
  id: string
  text: string
}

interface PlanEntry {
  title: string
  status: string
}

interface ToolCallEntry {
  id: string
  name: string
  title?: string
  status: ToolStatus
  rawInput?: unknown
  output?: unknown
}

interface ExecutionProcess {
  startedAt: number
  completedAt?: number
  plan: PlanEntry[]
  analyses: AnalysisEntry[]
  toolCalls: ToolCallEntry[]
}

interface TextSegment { id: string; type: 'text'; text: string }
interface ThoughtSegment { id: string; type: 'thought'; text: string }
interface PlanSegment { id: string; type: 'plan' }
interface ToolSegment { id: string; type: 'tool'; toolId: string }
type AssistantSegment = TextSegment | ThoughtSegment | PlanSegment | ToolSegment

interface UserMessage {
  id: string
  role: 'user'
  text: string
  attachments: AttachmentRef[]
  createdAt: number
}

interface AssistantMessage {
  id: string
  role: 'assistant'
  finalText: string
  status: TaskStatus
  process: ExecutionProcess
  segments: AssistantSegment[]
  createdAt: number
}

type ChatMessage = UserMessage | AssistantMessage

interface Conversation {
  id: string
  agentSessionId?: string
  title: string
  updatedAt: number
  messages: ChatMessage[]
}

interface PermissionRequest {
  id: number | string
  toolName: string
  rawInput: unknown
}

// Legacy localStorage key, kept only for a one-time migration into MooFile.
const STORE_KEY = 'deepagents-acp-ui-sessions-v1'
const message = useMessage()
const { t, locale } = useI18n()
const markdown = new MarkdownIt()
const ws = ref<WebSocket | null>(null)
const connected = ref(false)
const initialized = ref(false)
const socketPromise = ref<Promise<void> | null>(null)
const initializedSessions = new Set<string>()
const restoringHistory = ref(false)
const requestId = ref(1)
const pendingRequests = new Map<number, { resolve: (value: any) => void; reject: (reason: Error) => void }>()
const conversations = ref<Conversation[]>([])
const activeConversationId = ref('')
const conversationSearch = ref('')
const filteredConversations = computed(() => {
  const keyword = conversationSearch.value.trim().toLowerCase()
  if (!keyword) return conversations.value
  return conversations.value.filter((conversation) =>
    conversation.title.toLowerCase().includes(keyword)
  )
})
const input = ref('')
const attachments = ref<AttachmentRef[]>([])
const previewImage = ref<string | null>(null)
const isUploading = ref(false)
const dragActive = ref(false)
const permissionRequest = ref<PermissionRequest | null>(null)
const sidebarVisible = ref(false)
const errorText = ref('')
const timeline = ref<HTMLElement | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const localeOptions = computed(() => [
  { label: '中文', value: 'zh' },
  { label: '日本語', value: 'ja' },
  { label: 'English', value: 'en' }
])
const actionLabels = computed(() => ({
  retry: locale.value === 'zh' ? '重新生成' : locale.value === 'ja' ? '再生成' : 'Regenerate',
  copyText: locale.value === 'zh' ? '复制纯文本' : locale.value === 'ja' ? 'テキストをコピー' : 'Copy text',
  copyMarkdown: locale.value === 'zh' ? '复制 Markdown' : locale.value === 'ja' ? 'Markdown をコピー' : 'Copy Markdown',
  speak: locale.value === 'zh' ? '朗读' : locale.value === 'ja' ? '読み上げ' : 'Read aloud',
  stopSpeak: locale.value === 'zh' ? '停止朗读' : locale.value === 'ja' ? '読み上げ停止' : 'Stop reading',
  ttsFailed: locale.value === 'zh' ? '语音合成失败，请检查网络或代理' : locale.value === 'ja' ? '音声合成に失敗しました。ネットワークを確認してください' : 'Speech synthesis failed, check network or proxy',
  startSpeechInput: locale.value === 'zh' ? '开始语音输入' : locale.value === 'ja' ? '音声入力を開始' : 'Start voice input',
  stopSpeechInput: locale.value === 'zh' ? '停止语音输入' : locale.value === 'ja' ? '音声入力を停止' : 'Stop voice input',
  speechUnsupported: locale.value === 'zh' ? '当前浏览器不支持语音识别' : locale.value === 'ja' ? 'このブラウザは音声認識をサポートしていません' : 'Speech recognition is not supported in this browser',
  speechDenied: locale.value === 'zh' ? '麦克风权限被拒绝，请检查浏览器设置' : locale.value === 'ja' ? 'マイク権限が拒否されました。ブラウザ設定を確認してください' : 'Microphone access denied, please check browser settings',
  selectVoice: locale.value === 'zh' ? '音色' : locale.value === 'ja' ? '音声' : 'Voice',
  speakReply: locale.value === 'zh' ? 'AI 回复完成后自动朗读' : locale.value === 'ja' ? 'AI応答を自動読み上げ' : 'Read AI replies aloud',
  openVoiceCall: locale.value === 'zh' ? '语音通话' : locale.value === 'ja' ? '音声通話' : 'Voice call',
  copied: locale.value === 'zh' ? '已复制' : locale.value === 'ja' ? 'コピーしました' : 'Copied',
  more: locale.value === 'zh' ? '更多操作' : locale.value === 'ja' ? 'その他の操作' : 'More actions',
  editMessage: locale.value === 'zh' ? '编辑消息' : locale.value === 'ja' ? 'メッセージを編集' : 'Edit message',
  editTitle: locale.value === 'zh' ? '编辑消息' : locale.value === 'ja' ? 'メッセージを編集' : 'Edit message',
  save: locale.value === 'zh' ? '保存' : locale.value === 'ja' ? '保存' : 'Save',
  cancel: locale.value === 'zh' ? '取消' : locale.value === 'ja' ? 'キャンセル' : 'Cancel',
  preview: locale.value === 'zh' ? '预览' : locale.value === 'ja' ? 'プレビュー' : 'Preview',
  deleteMessage: locale.value === 'zh' ? '删除该消息' : locale.value === 'ja' ? 'このメッセージを削除' : 'Delete message',
  skills: locale.value === 'zh' ? '技能' : locale.value === 'ja' ? 'スキル' : 'Skills',
  tools: locale.value === 'zh' ? '工具' : locale.value === 'ja' ? 'ツール' : 'Tools',
  builtinTools: locale.value === 'zh' ? '内置工具' : locale.value === 'ja' ? '内蔵ツール' : 'Built-in tools',
  context: locale.value === 'zh' ? '上下文' : locale.value === 'ja' ? 'コンテキスト' : 'Context',
  contextTooltip: locale.value === 'zh' ? '当前会话上下文用量（本地估算）' : locale.value === 'ja' ? '現在のセッションのコンテキスト使用量（ローカル推定）' : 'Current session context usage (local estimate)',
  comingSoon: locale.value === 'zh' ? '该功能即将支持' : locale.value === 'ja' ? 'この機能はまもなく対応予定です' : 'Coming soon',
  renameConversation: locale.value === 'zh' ? '重命名' : locale.value === 'ja' ? '名前を変更' : 'Rename',
  renameTitle: locale.value === 'zh' ? '重命名会话' : locale.value === 'ja' ? 'セッション名を変更' : 'Rename conversation',
  renamePlaceholder: locale.value === 'zh' ? '输入新名称' : locale.value === 'ja' ? '新しい名前を入力' : 'Enter a new name',
  settings: locale.value === 'zh' ? '设置' : locale.value === 'ja' ? '設定' : 'Settings',
  collapseSidebar: locale.value === 'zh' ? '收起侧边栏' : locale.value === 'ja' ? 'サイドバーを畳む' : 'Collapse sidebar',
  expandSidebar: locale.value === 'zh' ? '展开侧边栏' : locale.value === 'ja' ? 'サイドバーを展開' : 'Expand sidebar',
  panelComingSoon: locale.value === 'zh' ? '该模块即将支持，敬请期待' : locale.value === 'ja' ? 'このモジュールはまもなく対応予定です' : 'This module is coming soon',
  language: locale.value === 'zh' ? '语言' : locale.value === 'ja' ? '言語' : 'Language',
  closePanel: locale.value === 'zh' ? '关闭面板' : locale.value === 'ja' ? 'パネルを閉じる' : 'Close panel',
  scrollToBottom: locale.value === 'zh' ? '滚动到底部' : locale.value === 'ja' ? '一番下へスクロール' : 'Scroll to bottom',
  previewAttachment: locale.value === 'zh' ? '预览附件' : locale.value === 'ja' ? '添付をプレビュー' : 'Preview attachment',
  downloadAttachment: locale.value === 'zh' ? '下载附件' : locale.value === 'ja' ? '添付をダウンロード' : 'Download attachment',
  screenShare: locale.value === 'zh' ? '共享屏幕' : locale.value === 'ja' ? '画面共有' : 'Share screen',
  screenShareActive: locale.value === 'zh' ? '共享屏幕已开启，发送消息时自动截屏' : locale.value === 'ja' ? '画面共有をオン、送信時に自動でスクリーンショット' : 'Screen share on, screenshots on send',
  agent: locale.value === 'zh' ? 'Agent' : locale.value === 'ja' ? 'エージェント' : 'Agent',
  selectAgent: locale.value === 'zh' ? '选择 Agent' : locale.value === 'ja' ? 'エージェントを選択' : 'Select agent',
  agents: locale.value === 'zh' ? 'Agent 管理' : locale.value === 'ja' ? 'エージェント管理' : 'Agents',
  newSkill: locale.value === 'zh' ? '新建技能' : locale.value === 'ja' ? '新しいスキル' : 'New skill',
  newTool: locale.value === 'zh' ? '新建工具' : locale.value === 'ja' ? '新しいツール' : 'New tool',
  newAgent: locale.value === 'zh' ? '新建 Agent' : locale.value === 'ja' ? '新しいエージェント' : 'New agent',
  edit: locale.value === 'zh' ? '编辑' : locale.value === 'ja' ? '編集' : 'Edit',
  delete: locale.value === 'zh' ? '删除' : locale.value === 'ja' ? '削除' : 'Delete',
  confirmDelete: locale.value === 'zh' ? '确定删除？此操作不可恢复' : locale.value === 'ja' ? '削除しますか？元に戻せません' : 'Delete? This cannot be undone',
  enabled: locale.value === 'zh' ? '已启用' : locale.value === 'ja' ? '有効' : 'Enabled',
  disabled: locale.value === 'zh' ? '已停用' : locale.value === 'ja' ? '無効' : 'Disabled',
  linkedSkills: locale.value === 'zh' ? '关联技能' : locale.value === 'ja' ? '連携スキル' : 'Linked skills',
  linkedTools: locale.value === 'zh' ? '关联工具' : locale.value === 'ja' ? '連携ツール' : 'Linked tools',
  currentAgent: locale.value === 'zh' ? '当前使用' : locale.value === 'ja' ? '現在使用中' : 'In use',
  searchPlaceholder: locale.value === 'zh' ? '搜索…' : locale.value === 'ja' ? '検索…' : 'Search…',
  noMatch: locale.value === 'zh' ? '无匹配结果' : locale.value === 'ja' ? '一致する結果がありません' : 'No matches',
  mcpServers: locale.value === 'zh' ? 'MCP 服务' : locale.value === 'ja' ? 'MCP サーバー' : 'MCP Servers',
  emptyList: locale.value === 'zh' ? '暂无内容，可通过对话让 AI 生成' : locale.value === 'ja' ? 'まだありません。会話でAIに生成させることができます' : 'Empty. Ask the AI to create one via conversation'
}))

const currentConversation = computed(() =>
  conversations.value.find((conversation) => conversation.id === activeConversationId.value)
)
const currentMessages = computed(() => currentConversation.value?.messages ?? [])
// Large conversations render only a tail window so the full message list DOM
// stays small; the "load earlier" button widens the window step by step.
const MESSAGE_WINDOW = 60
const messageWindowSize = ref(MESSAGE_WINDOW)
const visibleMessages = computed(() => {
  const msgs = currentMessages.value
  if (msgs.length <= messageWindowSize.value) return msgs
  return msgs.slice(msgs.length - messageWindowSize.value)
})
const showLoadEarlier = computed(() => currentMessages.value.length > messageWindowSize.value)
function loadEarlierMessages() {
  const el = timeline.value
  const prevTop = el?.scrollTop ?? 0
  messageWindowSize.value += MESSAGE_WINDOW
  nextTick(() => {
    if (el) el.scrollTop = prevTop
  })
}
const lastAssistantId = computed(() => {
  const msgs = currentMessages.value
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'assistant') return msgs[i].id
  }
  return ''
})
// Running state only watches assistant status: during streaming (including the
// tool-call phase) the stop button must stay visible; finalText is not a
// reliable "done" signal because it becomes non-empty after the first chunk.
const isRunning = computed(() => currentMessages.value.some(
  (item) => item.role === 'assistant'
    && (item.status === 'pending' || item.status === 'streaming' || item.status === 'waiting_permission')
))
// Whether a single assistant message is still running (drives the tail loader)
function isAssistantRunning(item: AssistantMessage) {
  return item.role === 'assistant'
    && (item.status === 'pending' || item.status === 'streaming' || item.status === 'waiting_permission')
}
const currentTitle = computed(() => currentConversation.value?.title ?? t('newSessionTitle'))
const connectionLabel = computed(() => connected.value ? t('connected') : t('disconnected'))

function migrateConversations(saved: unknown): Conversation[] {
  if (!Array.isArray(saved)) return []
  return saved.map((conversation) => ({
    ...conversation,
    messages: (conversation.messages ?? []).map((item: ChatMessage) => {
      if (item.role !== 'assistant') return item
      // History is never mid-run when restored: a stale streaming/pending
      // status saved during an interrupted run would otherwise keep the stop
      // button active forever after reload.
      if (item.status === 'pending' || item.status === 'streaming' || item.status === 'waiting_permission') {
        item = { ...item, status: 'completed' }
      }
      const hasNewSegments = Array.isArray(item.segments)
        && item.segments.some((segment) => segment.type === 'thought' || segment.type === 'tool' || segment.type === 'plan')
      if (hasNewSegments) return item
      // Legacy migration: rebuild the interleaved segments from plan/analysis/tool-calls/final text
      const segments: AssistantSegment[] = []
      if (item.process?.plan?.length) segments.push({ id: createId('plan'), type: 'plan' })
      ;(item.process?.analyses ?? []).forEach((analysis) => {
        segments.push({ id: createId('thought'), type: 'thought', text: analysis.text })
      })
      ;(item.process?.toolCalls ?? []).forEach((tool) => {
        segments.push({ id: createId('tool'), type: 'tool', toolId: tool.id })
      })
      if (item.finalText) segments.push({ id: createId('text'), type: 'text', text: item.finalText })
      return { ...item, segments }
    })
  }))
}

async function saveConversation(conversation: Conversation) {
  try {
    await fetch(`/api/history/${encodeURIComponent(conversation.id)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation }),
    })
  } catch {
    // server unavailable: keep the in-memory state, the next key event retries
  }
}

async function deleteConversationRemote(id: string) {
  try {
    await fetch(`/api/history/${encodeURIComponent(id)}`, { method: 'DELETE' })
  } catch {
    // server unavailable: nothing to do locally
  }
}

async function loadHistory() {
  let fromServer = false
  try {
    const res = await fetch('/api/history')
    if (res.ok) {
      const rows = await res.json()
      conversations.value = migrateConversations(rows)
      fromServer = true
    }
  } catch {
    // fall through to the legacy localStorage copy below
  }
  if (!fromServer) {
    // API unavailable (e.g. backend starting up): keep the previous behavior
    try {
      conversations.value = migrateConversations(JSON.parse(localStorage.getItem(STORE_KEY) ?? '[]'))
    } catch {
      conversations.value = []
    }
    return
  }
  // One-time migration: when MooFile is empty, seed it from the legacy
  // localStorage list, then drop the old key so it never re-imports.
  if (!conversations.value.length) {
    try {
      const legacy = JSON.parse(localStorage.getItem(STORE_KEY) ?? '[]')
      if (Array.isArray(legacy) && legacy.length) {
        conversations.value = migrateConversations(legacy)
        conversations.value.forEach((conversation) => void saveConversation(conversation))
      }
    } catch {
      // ignore malformed legacy data
    }
  }
  localStorage.removeItem(STORE_KEY)
  if (conversations.value.length) {
    activeConversationId.value = conversations.value[0]?.id ?? ''
  }
}

function createId(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

function createConversation() {
  const existingEmpty = conversations.value.find((conversation) => conversation.messages.length === 0)
  if (existingEmpty) {
    selectConversation(existingEmpty.id)
    conversationSearch.value = ''
    sidebarVisible.value = false
    nextTick(() => document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus())
    return
  }
  const conversation: Conversation = {
    id: `web-${Date.now()}`,
    title: t('newSessionTitle'),
    updatedAt: Date.now(),
    messages: []
  }
  conversations.value.unshift(conversation)
  activeConversationId.value = conversation.id
  conversationSearch.value = ''
  attachments.value = []
  input.value = ''
  errorText.value = ''
  sidebarVisible.value = false
  void saveConversation(conversation)
  nextTick(() => document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus())
}

/** Open the standalone voice-call window (#/voice hash route) */
function openVoiceCall() {
  const url = `${window.location.origin}${window.location.pathname}#/voice`
  window.open(url, 'voice-call', 'width=440,height=720,resizable=yes')
}

function selectConversation(id: string) {
  activeConversationId.value = id
  messageWindowSize.value = MESSAGE_WINDOW
  attachments.value = []
  sidebarVisible.value = false
  errorText.value = ''
  // Keep the in-memory state of a running conversation local so history cannot
  // roll the stream back
  const local = conversations.value.find((conversation) => conversation.id === id)
  const running = Boolean(local?.messages.some(
    (item) => item.role === 'assistant'
      && (item.status === 'pending' || item.status === 'streaming' || item.status === 'waiting_permission')
  ))
  if (!running) {
    conversationLoading.value += 1
    void (async () => {
      try {
        const res = await fetch(`/api/history/${encodeURIComponent(id)}`)
        if (res.ok) {
          const rows = migrateConversations([await res.json()])
          if (rows.length) {
            const index = conversations.value.findIndex((conversation) => conversation.id === id)
            if (index >= 0) {
              conversations.value[index] = rows[0]
            } else {
              conversations.value.unshift(rows[0])
            }
          }
        }
      } catch {
        // server unavailable: keep the local copy
      } finally {
        conversationLoading.value = Math.max(0, conversationLoading.value - 1)
        nextTick(scrollToBottom)
      }
    })()
  }
  nextTick(scrollToBottom)
}

function deleteConversation(id: string) {
  conversations.value = conversations.value.filter((conversation) => conversation.id !== id)
  if (activeConversationId.value === id) {
    activeConversationId.value = conversations.value[0]?.id ?? ''
  }
  void deleteConversationRemote(id)
}

const renameMenu = ref<{ id: string; x: number; y: number } | null>(null)
const renameTarget = ref<Conversation | null>(null)
const renameText = ref('')
function openRenameMenu(conversation: Conversation, event: MouseEvent) {
  renameMenu.value = { id: conversation.id, x: event.clientX, y: event.clientY }
}
function closeRenameMenu() {
  renameMenu.value = null
}
function openRenameModal() {
  const id = renameMenu.value?.id
  closeRenameMenu()
  const conversation = conversations.value.find((item) => item.id === id)
  if (!conversation) return
  renameTarget.value = conversation
  renameText.value = conversation.title
}
function saveRename() {
  const conversation = renameTarget.value
  if (conversation && renameText.value.trim()) {
    conversation.title = renameText.value.trim()
    conversation.updatedAt = Date.now()
    void saveConversation(conversation)
  }
  renameTarget.value = null
}

const contextUsage = computed(() => {
  const conversation = currentConversation.value
  if (!conversation || !conversation.messages.length || !contextSize.value) return null
  let chars = 0
  for (const item of conversation.messages) {
    if (item.role === 'user') {
      chars += item.text.length
    } else {
      chars += (item.finalText ?? '').length
      chars += (item.process?.toolCalls ?? []).reduce((sum, tool) => sum + (tool.input ?? '').length + (tool.output ?? '').length, 0)
    }
  }
  const tokens = Math.max(1, Math.round(chars / 1.7))
  const pct = Math.min(100, Math.round((tokens / contextSize.value) * 100))
  return { pct, tokens, total: contextSize.value }
})

const contextSize = ref(0)
async function loadContextSize() {
  try {
    const res = await fetch('/api/config')
    const data = await res.json()
    contextSize.value = Number(data.contentSize) || 0
  } catch {
    contextSize.value = 0
  }
}

const sidebarCollapsed = ref(false)
const rightPanelVisible = ref(false)
const rightPanelTab = ref<'skills' | 'tools' | 'agents' | 'settings'>('skills')
const rightPanelTitle = computed(() =>
  rightPanelTab.value === 'skills' ? actionLabels.value.skills
    : rightPanelTab.value === 'tools' ? actionLabels.value.tools
      : rightPanelTab.value === 'agents' ? actionLabels.value.agents
        : actionLabels.value.settings
)
function openRightPanel(tab: 'skills' | 'tools' | 'agents' | 'settings') {
  rightPanelTab.value = tab
  rightPanelVisible.value = true
  panelQuery.value = ''
}

// ---------- Manage panel (skills / tools / agents) ----------
const panelQuery = ref('')
const skillList = ref<any[]>([])
const toolList = ref<any[]>([])
const builtinTools = ref<any[]>([])
const agentList = ref<any[]>([])

/** Skills: filter by name/description, enabled first. */
const filteredSkills = computed(() => {
  const q = panelQuery.value.trim().toLowerCase()
  const list = skillList.value.filter((s) =>
    !q
      || s.name?.toLowerCase().includes(q)
      || (s.description || '').toLowerCase().includes(q)
  )
  return [...list].sort((a, b) => (b.enabled ? 1 : 0) - (a.enabled ? 1 : 0))
})

/** Built-in tools: filter by name. */
const filteredBuiltinTools = computed(() => {
  const q = panelQuery.value.trim().toLowerCase()
  if (!q) return builtinTools.value
  return builtinTools.value.filter((t) => (t.name || '').toLowerCase().includes(q))
})

/** MCP servers: filter by server/description/tool name (backend sorts enabled first). */
const filteredToolList = computed(() => {
  const q = panelQuery.value.trim().toLowerCase()
  if (!q) return toolList.value
  return toolList.value.filter((t) => {
    if ((t.name || '').toLowerCase().includes(q)) return true
    if ((t.description || '').toLowerCase().includes(q)) return true
    return (t.tools || []).some((x: any) => (x.name || '').toLowerCase().includes(q))
  })
})
const AGENT_STORAGE_KEY = 'currentAgentName'
const currentAgentName = ref(localStorage.getItem(AGENT_STORAGE_KEY) || '')
const agentDropdownOptions = computed(() =>
  agentList.value
    .filter((agent) => agent.enabled !== false)
    .map((agent) => ({
      key: agent.name,
      label: agent.name,
      render: () =>
        h('div', { class: 'agent-option' }, [
          h('div', { class: 'agent-option-name' }, agent.name),
          agent.description ? h('div', { class: 'agent-option-desc' }, agent.description) : null
        ])
    }))
)
function selectAgent(key: string) {
  currentAgentName.value = key
  localStorage.setItem(AGENT_STORAGE_KEY, key)
  fetchManageData() // tools panel state follows the current agent
  // Each agent maps to a separate ACP subprocess: drop the current connection
  // so the next send reconnects with the new agent name in the WS URL.
  if (ws.value) {
    ws.value.close()
    ws.value = null
  }
  socketPromise.value = null
  connected.value = false
  initialized.value = false
  initializedSessions.clear()
}

async function fetchManageData() {
  try {
    const [skills, agents, builtins] = await Promise.all([
      fetch('/api/manage/skills').then((r) => r.json()),
      fetch('/api/manage/agents').then((r) => r.json()),
      fetch('/api/manage/builtin-tools').then((r) => r.json())
    ])
    skillList.value = Array.isArray(skills) ? skills : []
    agentList.value = Array.isArray(agents) ? agents : []
    builtinTools.value = Array.isArray(builtins) ? builtins : []
    // fall back to the first available agent when the stored one is gone
    if (!agentList.value.some((a) => a.name === currentAgentName.value)) {
      const first = agentList.value.find((a) => a.enabled !== false)
      currentAgentName.value = first?.name || ''
    }
    // tool enablement is queried per agent (agents.json tools.mcp_tools)
    const agent = currentAgentName.value || 'default'
    const tools = await fetch(`/api/manage/tools?agent=${agent}`).then((r) => r.json())
    toolList.value = Array.isArray(tools) ? tools : []
  } catch {
    // keep the list empty when the backend is unavailable
  }
}

async function apiManage(path: string, method = 'GET', body?: unknown) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`)
  return data
}

async function toggleSkill(name: string) {
  try {
    await apiManage(`/api/manage/skills/${name}/toggle`, 'POST')
    await fetchManageData()
  } catch (e: any) {
    message.error(e.message)
  }
}
async function toggleTool(name: string) {
  try {
    await apiManage(`/api/manage/tools/${name}/toggle?agent=${currentAgentName.value || 'default'}`, 'POST')
    await fetchManageData()
  } catch (e: any) {
    message.error(e.message)
  }
}
/** Expand/collapse an MCP server group in the tools panel */
const expandedToolGroups = ref<string[]>([])
function toggleToolGroup(name: string) {
  const idx = expandedToolGroups.value.indexOf(name)
  if (idx >= 0) expandedToolGroups.value.splice(idx, 1)
  else expandedToolGroups.value.push(name)
}
/** Toggle a single MCP tool (add/remove in the current agent's mcp_tools) */
async function toggleToolItem(server: string, tool: string) {
  try {
    await apiManage(`/api/manage/tools/${server}/${tool}/toggle?agent=${currentAgentName.value || 'default'}`, 'POST')
    await fetchManageData()
  } catch (e: any) {
    message.error(e.message)
  }
}
/** Current agent's tool config (inner_tools + mcp_tools) */
const currentAgentTools = computed(() =>
  agentList.value.find((a) => a.name === currentAgentName.value)?.tools
)
/** Whether a built-in tool is enabled: inner_tools is an array containing it (default full array = all enabled) */
function innerToolEnabled(name: string): boolean {
  const inner = currentAgentTools.value?.inner_tools
  if (!Array.isArray(inner)) return true // fallback: legacy/null data means all enabled
  return inner.includes(name)
}
/** Clicking a built-in tool chip toggles the current agent's inner_tools */
async function toggleInnerTool(name: string) {
  if (!currentAgentName.value) return
  try {
    await apiManage(`/api/manage/agents/${currentAgentName.value}/inner-tools/${name}/toggle`, 'POST')
    await fetchManageData()
  } catch (e: any) {
    message.error(e.message)
  }
}
async function setAgentEnabled(agent: any) {
  try {
    await apiManage(`/api/manage/agents/${agent.name}`, 'PUT', {
      ...agent,
      enabled: !(agent.enabled !== false)
    })
    await fetchManageData()
  } catch (e: any) {
    message.error(e.message)
  }
}

// new skill modal
const skillModalVisible = ref(false)
const skillForm = ref({ name: '', description: '', content: '' })
async function submitSkill() {
  try {
    if (skillForm.value.name) {
      await apiManage(`/api/manage/skills/${skillForm.value.name}`, 'PUT', skillForm.value)
    } else {
      await apiManage('/api/manage/skills', 'POST', skillForm.value)
    }
    skillModalVisible.value = false
    skillForm.value = { name: '', description: '', content: '' }
    await fetchManageData()
  } catch (e: any) {
    message.error(e.message)
  }
}
function openSkillEdit(item?: any) {
  skillForm.value = item
    ? { name: item.name, description: item.description, content: '' }
    : { name: '', description: '', content: '' }
  if (item) {
    fetch(`/api/manage/skills/${item.name}`)
      .then((r) => r.json())
      .then((data) => { skillForm.value.content = data.content || '' })
      .catch(() => {})
  }
  skillModalVisible.value = true
}

// new tool modal
const toolModalVisible = ref(false)
const toolForm = ref({ name: '', description: '', command: '', args: '', cwd: '', env: '' })
async function submitTool() {
  try {
    const payload = {
      name: toolForm.value.name,
      description: toolForm.value.description,
      command: toolForm.value.command,
      args: toolForm.value.args.split('\n').map((s) => s.trim()).filter(Boolean),
      cwd: toolForm.value.cwd || undefined,
      env: Object.fromEntries(
        toolForm.value.env.split('\n').map((s) => s.trim()).filter(Boolean).map((line) => {
          const i = line.indexOf('=')
          return i > 0 ? [line.slice(0, i).trim(), line.slice(i + 1).trim()] : [line, '']
        })
      )
    }
    if (toolForm.value.name) {
      await apiManage(`/api/manage/tools/${toolForm.value.name}`, 'PUT', payload)
    } else {
      await apiManage('/api/manage/tools', 'POST', payload)
    }
    toolModalVisible.value = false
    toolForm.value = { name: '', description: '', command: '', args: '', cwd: '', env: '' }
    await fetchManageData()
  } catch (e: any) {
    message.error(e.message)
  }
}

// new agent modal
const agentModalVisible = ref(false)
const agentForm = ref({ name: '', description: '', skills: [] as string[], tools: [] as string[] })
const agentSkillOptions = computed(() =>
  skillList.value.filter((s) => s.enabled).map((s) => ({ label: s.name, value: s.name }))
)
const agentToolOptions = computed(() =>
  toolList.value.map((t) => ({ label: t.name, value: t.name }))
)
async function submitAgent() {
  try {
    if (agentForm.value.name) {
      await apiManage(`/api/manage/agents/${agentForm.value.name}`, 'PUT', agentForm.value)
    } else {
      await apiManage('/api/manage/agents', 'POST', agentForm.value)
    }
    agentModalVisible.value = false
    agentForm.value = { name: '', description: '', skills: [], tools: [] }
    await fetchManageData()
  } catch (e: any) {
    message.error(e.message)
  }
}
function openAgentEdit(item?: any) {
  agentForm.value = item
    ? {
        name: item.name,
        description: item.description || '',
        skills: item.skills || [],
        // tools new structure: extract the linked MCP server directory names
        tools: (item.tools?.mcp_tools || []).map((e: any) => e.server || e.serverInfo?.name).filter(Boolean)
      }
    : { name: '', description: '', skills: [], tools: [] }
  agentModalVisible.value = true
}

onMounted(() => {
  fetchManageData()
})

function notifyComingSoon(_feature: string) {
  message.info(actionLabels.value.comingSoon, { duration: 3000 })
}

function deleteMessage(item: { id: string }) {
  openMenuId.value = null
  const conversation = currentConversation.value
  if (!conversation) return
  const index = conversation.messages.findIndex((message) => message.id === item.id)
  if (index >= 0) conversation.messages.splice(index, 1)
  conversation.updatedAt = Date.now()
  void saveConversation(conversation)
}

const openMenuId = ref<string | null>(null)
function toggleMessageMenu(id: string) {
  openMenuId.value = openMenuId.value === id ? null : id
}
function closeMessageMenu() {
  openMenuId.value = null
}

// message editing (text only, does not re-run)
const editModalVisible = ref(false)
const editTargetId = ref<string | null>(null)
const editText = ref('')

function isMessageBusy(item: ChatMessage) {
  return item.role === 'assistant' &&
    (item.status === 'pending' || item.status === 'streaming' || item.status === 'waiting_permission')
}

function openEditMessage(item: ChatMessage) {
  editTargetId.value = item.id
  editText.value = item.role === 'user' ? item.text : (item.finalText ?? '')
  openMenuId.value = null
  editModalVisible.value = true
}

function saveEditMessage() {
  const conversation = currentConversation.value
  if (!conversation || !editTargetId.value) return
  const message = conversation.messages.find((entry) => entry.id === editTargetId.value)
  if (!message) return
  const text = editText.value
  if (message.role === 'user') {
    message.text = text
  } else {
    message.finalText = text
    const textSegment = message.segments.find((segment) => segment.type === 'text')
    if (textSegment) {
      textSegment.text = text
    } else {
      message.segments.push({ id: createId('seg'), type: 'text', text })
    }
  }
  conversation.updatedAt = Date.now()
  void saveConversation(conversation)
  editModalVisible.value = false
  editTargetId.value = null
}

function moveConversationToTop(conversation: Conversation) {
  conversations.value = [conversation, ...conversations.value.filter((item) => item.id !== conversation.id)]
}

function websocketUrl() {
  if (import.meta.env.VITE_ACP_WS_URL) return import.meta.env.VITE_ACP_WS_URL
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const agent = currentAgentName.value || 'default'
  return `${protocol}//${window.location.host}/acp-ws?agent=${encodeURIComponent(agent)}`
}

function connect() {
  if (connected.value && ws.value?.readyState === WebSocket.OPEN) return Promise.resolve()
  if (socketPromise.value) return socketPromise.value

  socketPromise.value = new Promise((resolve, reject) => {
    const socket = new WebSocket(websocketUrl())
    ws.value = socket
    socket.onopen = () => {
      connected.value = true
      socketPromise.value = null
      resolve()
    }
    socket.onerror = () => {
      socketPromise.value = null
      reject(new Error(t('connectionFailed')))
    }
    socket.onclose = () => {
      connected.value = false
      initialized.value = false
      initializedSessions.clear()
      ws.value = null
      pendingRequests.forEach(({ reject }) => reject(new Error(t('connectionClosed'))))
      pendingRequests.clear()
      // mark running assistant messages failed on abnormal close so the stop button reverts to send
      const active = activeAssistantMessage()
      if (
        active
        && active.status !== 'cancelled'
        && (active.status === 'pending' || active.status === 'streaming' || active.status === 'waiting_permission')
      ) {
        active.status = 'failed'
        active.process.completedAt = Date.now()
        appendTextSegment(active, `\n\n${t('connectionClosed')}`)
      }
    }
    socket.onmessage = (event) => handleAcpMessage(event.data)
  })
  return socketPromise.value
}

function callAcp(method: string, params: Record<string, unknown>) {
  return new Promise<any>((resolve, reject) => {
    const socket = ws.value
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      reject(new Error(t('connectionNotReady')))
      return
    }
    const id = requestId.value++
    pendingRequests.set(id, { resolve, reject })
    socket.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }))
  })
}

function respondAcp(id: number | string, result: Record<string, unknown>) {
  ws.value?.send(JSON.stringify({ jsonrpc: '2.0', id, result }))
}

async function initialize() {
  if (initialized.value) return
  await callAcp('initialize', {
    protocolVersion: 2,
    clientCapabilities: {},
    clientInfo: { name: 'deepagents-acp-ui', version: '1.0.0' }
  })
  initialized.value = true
}

function formatAcpError(error: any): string {
  const message = error?.message ?? t('requestFailed')
  const data = error?.data
  if (!data) return message
  if (typeof data === 'string') return `${message}: ${data}`
  if (data.details != null) return `${message}: ${data.details}`
  const rest = Object.entries(data)
    .filter(([key, value]) => value != null && key !== 'message')
    .map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`)
    .join(', ')
  return rest ? `${message}: ${rest}` : message
}

function handleAcpMessage(raw: string) {
  let payload: any
  try {
    payload = JSON.parse(raw)
  } catch {
    return
  }
  if (payload.id !== undefined && payload.method) {
    if (payload.method === 'session/request_permission') {
      const toolCall = payload.params?.toolCall ?? {}
      permissionRequest.value = {
        id: payload.id,
        toolName: toolCall.title ?? toolCall.toolName ?? toolCall.toolCallId ?? '工具调用',
        rawInput: toolCall.rawInput ?? {}
      }
      const active = activeAssistantMessage()
      if (active) active.status = 'waiting_permission'
    }
    return
  }
  if (payload.id !== undefined && pendingRequests.has(payload.id)) {
    const request = pendingRequests.get(payload.id)!
    pendingRequests.delete(payload.id)
    if (payload.error) request.reject(new Error(formatAcpError(payload.error)))
    else request.resolve(payload.result)
    return
  }
  if (payload.method === 'session/update') handleSessionUpdate(payload.params?.update ?? {})
}

function activeAssistantMessage() {
  const items = currentConversation.value?.messages ?? []
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (items[index].role === 'assistant') return items[index] as AssistantMessage
  }
  return undefined
}

function appendTextSegment(assistant: AssistantMessage, text: string) {
  if (!text) return
  assistant.finalText += text
  const last = assistant.segments.at(-1)
  if (last?.type === 'text') last.text += text
  else assistant.segments.push({ id: createId('text'), type: 'text', text })
}

function appendThoughtSegment(assistant: AssistantMessage, text: string) {
  if (!text) return
  const last = assistant.segments.at(-1)
  if (last?.type === 'thought') last.text += text
  else assistant.segments.push({ id: createId('thought'), type: 'thought', text })
}

function ensurePlanSegment(assistant: AssistantMessage) {
  if (!assistant.segments.some((segment) => segment.type === 'plan')) {
    assistant.segments.push({ id: createId('plan'), type: 'plan' })
  }
}

function ensureToolSegment(assistant: AssistantMessage, toolId: string) {
  if (!assistant.segments.some((segment) => segment.type === 'tool' && segment.toolId === toolId)) {
    assistant.segments.push({ id: createId('tool'), type: 'tool', toolId })
  }
}

function findTool(process: ExecutionProcess, toolId: string) {
  return process.toolCalls.find((tool) => tool.id === toolId)
}

function segmentTool(assistant: AssistantMessage, segment: ToolSegment): ToolCallEntry {
  return findTool(assistant.process, segment.toolId)
    ?? { id: segment.toolId, name: 'Run', status: 'running' }
}

function textFromContent(content: any): string {
  if (!content) return ''
  if (typeof content === 'string') return content
  if (Array.isArray(content)) return content.map(textFromContent).filter(Boolean).join('\n')
  if (typeof content.text === 'string') return content.text
  if (content.content) return textFromContent(content.content)
  return ''
}

// ACP tool_call events only carry a title (e.g. "Read `path`" / "Execute: cmd" / "glob"),
// with no toolName. Infer the badge name (built-in short name) from title/kind; '' means the Run placeholder.
const BUILTIN_TOOL_NAMES = new Set(['read_file', 'write_file', 'edit_file', 'glob', 'grep', 'ls', 'delete', 'task', 'execute'])
function inferToolName(update: any): string {
  const title = String(update.title ?? '').trim()
  const kind = String(update.kind ?? '')
  const m = title.match(/^(Read|Write|Edit|Execute)\s*[:\s]\s*(.*)$/s)
  if (m) return m[1] === 'Execute' ? 'Run' : m[1]
  if (BUILTIN_TOOL_NAMES.has(title)) return title
  if (kind === 'read') return 'Read'
  if (kind === 'edit') return 'Edit'
  if (kind === 'search') return 'Search'
  if (kind === 'execute') return 'Run'
  return ''
}
// body after the badge: strip "Read/Write/Edit/Execute:" prefixes; empty when identical to the badge
function toolDisplayTitle(update: any): string {
  const title = String(update.title ?? '').trim()
  const badge = inferToolName(update)
  const m = title.match(/^(Read|Write|Edit|Execute)\s*[:\s]\s*(.*)$/s)
  let rest = m ? m[2].trim() : title
  if (rest && (rest === badge || BUILTIN_TOOL_NAMES.has(rest))) rest = ''
  return rest
}

function normalizeToolStatus(status?: string): ToolStatus {
  if (status === 'failed' || status === 'error') return 'failed'
  if (status === 'cancelled') return 'cancelled'
  if (status === 'waiting_permission') return 'waiting_permission'
  if (status === 'completed' || status === 'success') return 'completed'
  return 'running'
}

function handleSessionUpdate(update: any) {
  if (restoringHistory.value) return
  if (cancelRequested) return
  const assistant = activeAssistantMessage()
  if (!assistant) return
  // A cancelled run must ignore any late streaming events, otherwise the
  // assistant status flips back to streaming and the stop button gets stuck.
  // Same guard applies once the message is completed/failed: no late event may
  // resurrect the running state (stop button) after the run is over.
  if (assistant.status === 'cancelled' || assistant.status === 'completed' || assistant.status === 'failed') return
  const process = assistant.process
  switch (update.sessionUpdate) {
    case 'agent_message_chunk':
      assistant.status = 'streaming'
      appendTextSegment(assistant, textFromContent(update.content))
      break
    case 'agent_thought_chunk': {
      const text = textFromContent(update.content)
      if (text) {
        assistant.status = 'streaming'
        appendThoughtSegment(assistant, text)
      }
      break
    }
    case 'plan': {
      process.plan = (update.entries ?? []).map((entry: any) => ({
        title: entry.title ?? entry.task ?? entry.content ?? '执行步骤',
        status: entry.status ?? 'pending'
      }))
      if (process.plan.length) ensurePlanSegment(assistant)
      break
    }
    case 'tool_call':
    case 'tool_call_start': {
      const id = update.toolCallId ?? createId('tool')
      assistant.status = 'streaming' // keep running during tool calls so the stop button stays
      if (!process.toolCalls.some((tool) => tool.id === id)) {
        process.toolCalls.push({
          id,
          name: inferToolName(update) || 'Run',
          title: toolDisplayTitle(update),
          status: 'running',
          rawInput: update.rawInput
        })
      }
      ensureToolSegment(assistant, id)
      break
    }
    case 'tool_call_update': {
      const id = update.toolCallId ?? createId('tool')
      let tool = process.toolCalls.find((item) => item.id === id)
      if (!tool) {
        tool = { id, name: inferToolName(update) || 'Run', status: 'running' }
        process.toolCalls.push(tool)
      }
      ensureToolSegment(assistant, id)
      if (update.title) tool.title = toolDisplayTitle(update)
      tool.rawInput = update.rawInput ?? tool.rawInput
      tool.output = textFromContent(update.content) || update.content || tool.output
      tool.status = normalizeToolStatus(update.status)
      break
    }
  }
  if (!scrollPausedByUser.value) nextTick(scrollToBottom)
}

async function ensureAgentSession(conversation: Conversation, isNewConversation: boolean) {
  if (initializedSessions.has(conversation.id)) return
  if (isNewConversation) {
    const response = await callAcp('session/new', { sessionId: conversation.id, cwd: '.', mcpServers: [] })
    if (!response?.sessionId) throw new Error(t('invalidSession'))
    conversation.agentSessionId = response.sessionId
  } else {
    const agentSessionId = conversation.agentSessionId ?? conversation.id
    restoringHistory.value = true
    try {
      await callAcp('session/load', { sessionId: agentSessionId, cwd: '.', mcpServers: [] })
    } finally {
      restoringHistory.value = false
    }
  }
  initializedSessions.add(conversation.id)
}

// localStorage only mirrors the display state; the authoritative agent context
// lives in the SQLite checkpoint. On load, validate every history session
// against the backend and drop local ones whose checkpoint is missing.
async function validateStoredSessions() {
  if (!conversations.value.length) return
  try {
    await connect()
    await initialize()
  } catch {
    // keep local sessions when the backend is offline; retry on next validation
    return
  }
  const invalid: string[] = []
  for (const conversation of conversations.value) {
    if (!conversation.messages.length) continue
    const agentSessionId = conversation.agentSessionId ?? conversation.id
    restoringHistory.value = true
    try {
      await callAcp('session/load', { sessionId: agentSessionId, cwd: '.', mcpServers: [] })
      initializedSessions.add(conversation.id)
    } catch {
      invalid.push(conversation.id)
    } finally {
      restoringHistory.value = false
    }
  }
  if (invalid.length) {
    conversations.value = conversations.value.filter((conversation) => !invalid.includes(conversation.id))
    if (activeConversationId.value && invalid.includes(activeConversationId.value)) {
      activeConversationId.value = conversations.value[0]?.id ?? ''
    }
    invalid.forEach((id) => void deleteConversationRemote(id))
    // create a fresh empty session when all are invalid so the input stays usable
    if (!conversations.value.length) createConversation()
  }
}

async function sendAgentPrompt(
  conversation: Conversation,
  text: string,
  resources: AttachmentRef[],
  assistantMessage: AssistantMessage,
  isNewConversation: boolean
) {
  // Reset the cancel flag on every prompt path (submit & retry), otherwise a
  // previous cancel would skip the completed status and leave the stop button stuck.
  cancelRequested = false
  try {
    await connect()
    await initialize()
    await ensureAgentSession(conversation, isNewConversation)
    const fileAttachmentContext = resources
      .filter((resource) => resource.kind !== 'image')
      .map((resource) => `附件虚拟路径：${resource.path ?? resource.uri}\n请直接使用本地文件工具读取该路径；不要转换为 Windows 路径或 HTTP 地址。`)
      .join('\n\n')
    const promptText = [text, fileAttachmentContext].filter(Boolean).join('\n\n')
    const content: Array<Record<string, unknown>> = []
    if (promptText || !resources.some((resource) => resource.kind === 'image')) {
      content.push({ type: 'text', text: promptText })
    }
    resources
      .filter((resource) => resource.kind === 'image' && resource.data)
      .forEach((resource) => content.push({
        type: 'image',
        data: resource.data,
        mimeType: resource.mimeType ?? 'image/png'
      }))
    cancelRequested = false // reset the cancel flag for a new run
    await callAcp('session/prompt', { sessionId: conversation.agentSessionId ?? conversation.id, prompt: content })
    if (assistantMessage.status !== 'cancelled' && assistantMessage.status !== 'failed' && !cancelRequested) {
      assistantMessage.status = 'completed'
      assistantMessage.process.completedAt = Date.now()
      if (!assistantMessage.finalText) appendTextSegment(assistantMessage, t('taskComplete'))
      speakReplyIfEnabled(assistantMessage.finalText ?? '')
    }
  } catch (error) {
    if (assistantMessage.status !== 'cancelled') {
      assistantMessage.status = 'failed'
      assistantMessage.process.completedAt = Date.now()
      const message = error instanceof Error ? error.message : t('taskFailed')
      errorText.value = message
      // Show the raw error message as assistant content instead of only a status label
      appendTextSegment(assistantMessage, `\n\n${message}`)
    }
  } finally {
    conversation.updatedAt = Date.now()
    void saveConversation(conversation)
    nextTick(scrollToBottom)
  }
}

async function submitPrompt() {
  const conversation = currentConversation.value
  const text = input.value.trim()
  if (!conversation || (!text && !attachments.value.length && !screenShareActive.value) || isRunning.value || isUploading.value) return
  // capture the screen as an attachment before sending when share-screen is on
  if (screenShareActive.value) await captureScreenAttachment()

  const isNewConversation = conversation.messages.length === 0
  errorText.value = ''
  const resources = [...attachments.value]
  const userMessage: UserMessage = { id: createId('user'), role: 'user', text, attachments: resources, createdAt: Date.now() }
  const assistantMessage: AssistantMessage = {
    id: createId('assistant'),
    role: 'assistant',
    finalText: '',
    status: 'pending',
    process: { startedAt: Date.now(), plan: [], analyses: [], toolCalls: [] },
    segments: [],
    createdAt: Date.now()
  }
  conversation.messages.push(userMessage, assistantMessage)
  conversation.title = conversation.title === t('newSessionTitle') ? (text || resources[0]?.name || t('attachmentTask')).slice(0, 24) : conversation.title
  conversation.updatedAt = Date.now()
  moveConversationToTop(conversation)
  input.value = ''
  attachments.value = []
  void saveConversation(conversation)
  await nextTick(scrollToBottom)
  await sendAgentPrompt(conversation, text, resources, assistantMessage, isNewConversation)
}

function assistantSourceMessage(assistant: AssistantMessage) {
  const conversation = currentConversation.value
  const index = conversation?.messages.findIndex((item) => item.id === assistant.id) ?? -1
  const source = index > 0 ? conversation?.messages[index - 1] : undefined
  return source?.role === 'user' ? source : undefined
}

async function retryAssistant(assistant: AssistantMessage) {
  const conversation = currentConversation.value
  const source = assistantSourceMessage(assistant)
  if (!conversation || !source || isRunning.value) return

  errorText.value = ''
  assistant.finalText = ''
  assistant.status = 'pending'
  assistant.process = { startedAt: Date.now(), plan: [], analyses: [], toolCalls: [] }
  assistant.segments = []
  conversation.updatedAt = Date.now()
  void saveConversation(conversation)
  await nextTick(scrollToBottom)
  await sendAgentPrompt(conversation, source.text, source.attachments, assistant, !conversation.agentSessionId)
}

async function copyText(value: string) {
  try {
    await navigator.clipboard.writeText(value)
    message.success(actionLabels.value.copied, { duration: 3000 })
  } catch {
    message.error(t('requestFailed'), { duration: 3000 })
  }
}

function copyAssistantText(assistant: AssistantMessage, asMarkdown: boolean) {
  const value = asMarkdown
    ? assistant.finalText
    : new DOMParser().parseFromString(markdown.render(assistant.finalText), 'text/html').body.textContent?.trim() ?? ''
  void copyText(value)
}

function copyUserText(item: { text?: string }, asMarkdown: boolean) {
  const raw = item.text ?? ''
  const value = asMarkdown
    ? raw
    : new DOMParser().parseFromString(markdown.render(raw), 'text/html').body.textContent?.trim() ?? ''
  void copyText(value)
}

// ---------- TTS synthesis and voice bridging ----------
let speechAudio: HTMLAudioElement | null = null
let speechStreamStop: (() => void) | null = null
let cancelRequested = false
const speakingMessageId = ref<string | null>(null)
const selectedVoice = ref(localStorage.getItem('chat_voice_name') || 'zh')
const speakReplyEnabled = ref(localStorage.getItem('chat_speak_reply') === '1')

let cachedVoiceNames: string[] | null = null
/** Return a voice name valid for the active engine; stale localStorage values
 *  (e.g. 'zh') fall back to '', letting the backend pick a default by language. */
async function resolveVoiceParam(): Promise<string> {
  const v = selectedVoice.value
  if (!v) return ''
  if (cachedVoiceNames === null) {
    try {
      const list = await fetch('/api/chat_voice/voices').then((r) => r.json())
      cachedVoiceNames = list.map((x: any) => x.value)
    } catch {
      cachedVoiceNames = []
    }
  }
  return cachedVoiceNames.includes(v) ? v : ''
}

function messagePlainText(item: { role: string; text?: string; finalText?: string }): string {
  return item.role === 'user' ? (item.text ?? '') : (item.finalText ?? '')
}

/** Stop the current playback (streaming or not) */
function stopSpeech() {
  if (speechStreamStop) {
    speechStreamStop()
    speechStreamStop = null
  }
  if (speechAudio) {
    speechAudio.pause()
    speechAudio = null
  }
}

/** Synthesize and play text (returns success); used for message read-aloud and
 *  AI reply broadcast. Routes by backend TTS_MODE: file -> non-stream base64,
async function speakText(text: string): Promise<boolean> {
  text = cleanSpeechText(text)
  if (!text.trim()) return false
  stopSpeech()
  const voice = await resolveVoiceParam()
  const cfg = await getTtsConfig()
  if (cfg.mode !== 'stream') {
    return await playBase64Speech(text, voice)
  }
  // streaming first
  const handle = await playTtsStream(text, voice, () => {
    speechStreamStop = null
    speakingMessageId.value = null
  })
  if (handle) {
    speechStreamStop = handle.stop
    return true
  }
  // fallback: non-stream base64 (kept working version)
  return await playBase64Speech(text, voice)
}

async function playBase64Speech(text: string, voice: string): Promise<boolean> {
  try {
    const resp = await fetch('/api/chat_voice/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, voice })
    })
    const data = await resp.json()
    if (!data.audio) return false
    const audio = new Audio('data:audio/wav;base64,' + data.audio)
    speechAudio = audio
    audio.onended = () => {
      speechAudio = null
      speakingMessageId.value = null
    }
    audio.onerror = () => {
      speechAudio = null
      speakingMessageId.value = null
    }
    audio.play().catch(() => {
      speechAudio = null
    })
    return true
  } catch {
    return false
  }
}

// ---------- voice-call window bridge (the #/voice window calls via opener) ----------
let replyListener: ((text: string) => void) | null = null

/** After an AI reply: notify the voice window first, else read aloud in the main window per the toggle */
function speakReplyIfEnabled(text: string) {
  if (!text.trim()) return
  if (replyListener) {
    const cb = replyListener
    replyListener = null
    cb(text)
    return
  }
  if (!speakReplyEnabled.value) return
  void speakText(text)
}

/** Exposed to the voice window via window.opener.chatBridge */
function setupChatBridge() {
  const bridge = {
    /** Voice window: live-sync recognized speech into the main input */
    updateInput: (text: string) => {
      input.value = text
    },
    /** Voice window: send the final text through the main session (normal ACP flow) */
    sendMessage: (text: string) => {
      const value = text.trim()
      if (!value || isRunning.value) return
      input.value = value
      void submitPrompt()
    },
    /** Voice window: register an AI-reply-done callback (once per send) */
    onReply: (cb: (text: string) => void) => {
      replyListener = cb
    }
  }
  ;(window as any).chatBridge = bridge
}

async function speakMessage(item: { id: string; role: string; text?: string; finalText?: string }) {
  if (speakingMessageId.value === item.id) {
    stopSpeech()
    speakingMessageId.value = null
    return
  }
  const text = messagePlainText(item)
  if (!text.trim()) return
  speakingMessageId.value = item.id
  const ok = await speakText(text)
  if (!ok) {
    speakingMessageId.value = null
    message.error(actionLabels.value.ttsFailed, { duration: 3000 })
  }
}

// ---------- voice input (STT -> text box) ----------
function toggleSpeakReply() {
  speakReplyEnabled.value = !speakReplyEnabled.value
  localStorage.setItem('chat_speak_reply', speakReplyEnabled.value ? '1' : '0')
}

async function cancelTask() {
  const conversation = currentConversation.value
  const assistant = activeAssistantMessage()
  if (!conversation || !assistant) return
  cancelRequested = true
  // set cancelled immediately so a prompt Promise resolving before session/cancel cannot mark done and read a partial reply
  assistant.status = 'cancelled'
  assistant.process.completedAt = Date.now()
  assistant.process.toolCalls.forEach((tool) => {
    if (tool.status === 'running' || tool.status === 'waiting_permission') tool.status = 'cancelled'
  })
  stopSpeech() // also stop any read-aloud (including in-flight TTS playback)
  // Fire-and-forget the cancel request, then close the socket immediately.
  // Awaiting the ACP response would block here while the agent subprocess is
  // busy generating (it cannot answer until the current step finishes), so the
  // socket would never close and the bridge would never terminate the process.
  // Closing the socket makes app.py's bridge kill the whole agent process tree.
  try {
    const socket = ws.value
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ jsonrpc: '2.0', id: requestId.value++, method: 'session/cancel', params: { sessionId: conversation.agentSessionId ?? conversation.id } }))
    }
  } catch {
    // Socket already closing; the bridge terminates the subprocess on WS close anyway.
  } finally {
    ws.value?.close()
    void saveConversation(conversation)
  }
}

function respondPermission(optionId: string) {
  if (!permissionRequest.value) return
  respondAcp(permissionRequest.value.id, { outcome: { outcome: 'selected', optionId } })
  const assistant = activeAssistantMessage()
  if (assistant) assistant.status = 'pending'
  permissionRequest.value = null
}

function fileToDataUrl(file: File) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error(t('uploadFailed')))
    reader.onload = () => typeof reader.result === 'string' ? resolve(reader.result) : reject(new Error(t('uploadFailed')))
    reader.readAsDataURL(file)
  })
}

async function addFiles(files: Iterable<File>) {
  const selectedFiles = Array.from(files)
  if (!selectedFiles.length) return
  isUploading.value = true
  try {
    for (const file of selectedFiles) {
      if (file.type.startsWith('image/')) {
        const previewUrl = await fileToDataUrl(file)
        attachments.value.push({
          name: file.name || `pasted-image-${Date.now()}.png`,
          mimeType: file.type,
          kind: 'image',
          data: previewUrl.slice(previewUrl.indexOf(',') + 1),
          previewUrl
        })
        continue
      }
      const form = new FormData()
      form.append('file', file)
      const response = await fetch('/upload', { method: 'POST', body: form })
      if (!response.ok) throw new Error(t('uploadFailed'))
      attachments.value.push({ ...(await response.json()), kind: 'file' })
    }
  } catch (error) {
    message.error(error instanceof Error ? error.message : t('uploadFailed'), { duration: 3000 })
  } finally {
    isUploading.value = false
  }
}

async function uploadFile(event: Event) {
  const inputElement = event.target as HTMLInputElement
  await addFiles(inputElement.files ?? [])
  inputElement.value = ''
}

function onComposerPaste(event: ClipboardEvent) {
  const files = Array.from(event.clipboardData?.items ?? [])
    .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
    .map((item) => item.getAsFile())
    .filter((file): file is File => Boolean(file))
  if (!files.length) return
  event.preventDefault()
  void addFiles(files)
}

function dropFiles(event: DragEvent) {
  dragActive.value = false
  void addFiles(event.dataTransfer?.files ?? [])
}

function removeAttachment(index: number) {
  attachments.value.splice(index, 1)
}

// ---------- share screen (auto-capture as attachment on send) ----------
const screenShareActive = ref(false)
let screenStream: MediaStream | null = null
let screenVideo: HTMLVideoElement | null = null

async function toggleScreenShare() {
  if (screenShareActive.value) {
    stopScreenShare()
    return
  }
  try {
    const stream = await (navigator.mediaDevices as any).getDisplayMedia({ video: { frameRate: 5 } })
    screenStream = stream
    const video = document.createElement('video')
    video.srcObject = stream
    video.muted = true
    video.playsInline = true
    await video.play()
    screenVideo = video
    screenShareActive.value = true
    stream.getVideoTracks()[0]?.addEventListener('ended', () => stopScreenShare())
  } catch {
    // user cancelled the picker; keep it off
  }
}

function stopScreenShare() {
  if (screenStream) {
    screenStream.getTracks().forEach((track) => track.stop())
    screenStream = null
  }
  screenVideo = null
  screenShareActive.value = false
}

/** Capture one frame of the screen and queue it as an image attachment */
async function captureScreenAttachment() {
  if (!screenShareActive.value || !screenVideo) return
  try {
    const canvas = document.createElement('canvas')
    canvas.width = screenVideo.videoWidth || 1920
    canvas.height = screenVideo.videoHeight || 1080
    canvas.getContext('2d')?.drawImage(screenVideo, 0, 0, canvas.width, canvas.height)
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'))
    if (!blob) return
    const file = new File([blob], `screen_${Date.now()}.png`, { type: 'image/png' })
    const previewUrl = await fileToDataUrl(file)
    attachments.value.push({
      name: file.name,
      mimeType: file.type,
      kind: 'image',
      data: previewUrl.slice(previewUrl.indexOf(',') + 1),
      previewUrl
    })
  } catch {
    // ignore capture failures
  }
}

function statusText(status: TaskStatus | ToolStatus) {
  return t(`status.${status}`)
}

function formatTime(timestamp: number) {
  const date = new Date(timestamp)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

function duration(process: ExecutionProcess) {
  const end = process.completedAt ?? Date.now()
  return Math.max(0, Math.round((end - process.startedAt) / 1000))
}

const showScrollBottom = ref(false)
// user scroll pauses auto-follow: mouse/touch scroll during streaming pauses it;
// follow resumes after 2s idle or on the scroll-to-bottom button
const scrollPausedByUser = ref(false)
let scrollPauseTimer: number | null = null
function pauseAutoScrollByUser() {
  if (!isRunning.value) return
  scrollPausedByUser.value = true
  if (scrollPauseTimer) window.clearTimeout(scrollPauseTimer)
  scrollPauseTimer = window.setTimeout(() => {
    scrollPausedByUser.value = false
    scrollPauseTimer = null
    if (isRunning.value) nextTick(() => scrollToBottom(false))
  }, 2000)
}
function onTimelineScroll() {
  const el = timeline.value
  if (!el) return
  showScrollBottom.value = el.scrollTop + el.clientHeight < el.scrollHeight - 8
}

function scrollToBottom(smooth = false) {
  scrollPausedByUser.value = false
  if (scrollPauseTimer) {
    window.clearTimeout(scrollPauseTimer)
    scrollPauseTimer = null
  }
  const el = timeline.value
  if (!el) return
  if (smooth) {
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  } else {
    // use instant positioning for stream follow-scroll to avoid jitter from smooth animation + incremental content
    const prev = el.style.scrollBehavior
    el.style.scrollBehavior = 'auto'
    el.scrollTop = el.scrollHeight
    el.style.scrollBehavior = prev
  }
  showScrollBottom.value = false
}

function openAttachment(attachment: AttachmentRef) {
  if (attachment.kind === 'image') {
    // Pasted/dragged images are stored as inline data URLs; show them in an
    // in-page overlay instead of window.open (which blocks data: URLs).
    const url = attachment.previewUrl
      ?? (attachment.data ? `data:${attachment.mimeType ?? 'image/png'};base64,${attachment.data}` : undefined)
      ?? attachment.path
      ?? attachment.uri
    if (url) previewImage.value = url
    return
  }
  // Uploaded files: resolve the virtual path against the current origin so the
  // download anchor always points at a real, absolute URL.
  const url = attachment.path ?? attachment.uri ?? attachment.previewUrl
  if (!url) return
  const absolute = new URL(url, window.location.origin).href
  const anchor = document.createElement('a')
  anchor.href = absolute
  anchor.download = attachment.name || ''
  anchor.target = '_blank'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

// ---------- quick actions (@ files / slash skills) ----------
const quickAction = ref<'file' | 'skill' | null>(null)
const quickItems = ref<any[]>([])
const quickLoading = ref(false)
// overlay loading: initial load (initialLoading) and conversation switch
// (conversationLoading, counter-based so rapid switches hide only after the last)
const initialLoading = ref(true)
const conversationLoading = ref(0)
const quickIndex = ref(0)
let quickTimer: number | undefined

function closeQuickAction() {
  quickAction.value = null
  quickItems.value = []
  quickIndex.value = 0
}

async function loadQuickItems(action: 'file' | 'skill', q: string) {
  quickLoading.value = true
  try {
    if (action === 'file') {
      const res = await fetch(`/api/manage/files?q=${encodeURIComponent(q)}`)
      const data = await res.json()
      quickItems.value = Array.isArray(data) ? data : []
    } else {
      const res = await fetch('/api/manage/skills')
      const data = await res.json()
      const list = Array.isArray(data) ? data : []
      quickItems.value = q
        ? list.filter((s: any) => s.name.toLowerCase().includes(q.toLowerCase()))
        : list
    }
    quickIndex.value = 0
  } catch {
    quickItems.value = []
  } finally {
    quickLoading.value = false
  }
}

function selectQuickItem(item: any) {
  if (quickAction.value === 'file') {
    // @ + backticked absolute path + space
    input.value = '@' + '`' + (item.path ?? item.absolute_path ?? item.name) + '`' + ' '
  } else if (quickAction.value === 'skill') {
    // / + backticked skill name + space
    input.value = '/' + '`' + item.name + '`' + ' '
  }
  closeQuickAction()
}

watch(input, (value) => {
  if (value.length >= 1) {
    const ch = value[0]
    if (value.length === 1 && (ch === '@' || ch === '/')) {
      quickAction.value = ch === '@' ? 'file' : 'skill'
      quickItems.value = []
      quickLoading.value = true
      clearTimeout(quickTimer)
      quickTimer = window.setTimeout(() => void loadQuickItems(quickAction.value!, ''), 120)
      return
    }
    if (quickAction.value && (ch === '@' || ch === '/')) {
      const q = value.slice(1)
      clearTimeout(quickTimer)
      quickTimer = window.setTimeout(() => void loadQuickItems(quickAction.value!, q), 120)
      return
    }
  }
  closeQuickAction()
})

function onComposerBlur() {
  closeQuickAction()
}

function submitOnEnter(event: KeyboardEvent) {
  if (!event.shiftKey) {
    event.preventDefault()
    if (quickAction.value) {
      if (quickItems.value.length) selectQuickItem(quickItems.value[quickIndex.value] ?? quickItems.value[0])
      else closeQuickAction()
      return
    }
    submitPrompt()
  }
}

watch(locale, (value) => localStorage.setItem(storageKey, value as SupportedLocale))
onMounted(async () => {
  void loadContextSize()
  setupChatBridge()
  try {
    await loadHistory()
    if (!conversations.value.length) createConversation()
    const defaultEmpties = conversations.value.filter((conversation) => conversation.messages.length === 0 && conversation.title === t('newSessionTitle'))
    if (defaultEmpties.length > 1) {
      const keepId = defaultEmpties[0].id
      conversations.value = conversations.value.filter((conversation) => conversation.id === keepId || conversation.messages.length > 0 || conversation.title !== t('newSessionTitle'))
      defaultEmpties.slice(1).forEach((conversation) => void deleteConversationRemote(conversation.id))
    }
    void validateStoredSessions()
  } finally {
    initialLoading.value = false
  }
  window.addEventListener('click', closeMessageMenu)
  window.addEventListener('click', closeRenameMenu)
})
watch(() => currentMessages.value.length, () => {
  nextTick(() => {
    const el = timeline.value
    if (el) onTimelineScroll()
  })
})
onBeforeUnmount(() => {
  window.removeEventListener('click', closeMessageMenu)
  window.removeEventListener('click', closeRenameMenu)
  if (scrollPauseTimer) window.clearTimeout(scrollPauseTimer)
  stopScreenShare()
  stopSpeech()
  ws.value?.close()
})
</script>

<template>
  <main class="chat-app" :class="{ 'chat-app--collapsed': sidebarCollapsed }">
    <aside class="sidebar" :class="{ 'sidebar--mobile-open': sidebarVisible }">
      <div class="brand-row">
        <div class="brand-mark"><NIcon :component="CodeSlashOutline" /></div>
        <div>
          <strong>ACP client base</strong>
          <span>{{ t('brandSubline') }}</span>
        </div>
        <NButton class="sidebar-collapse-btn" quaternary circle size="tiny" :aria-label="actionLabels.collapseSidebar" @click="sidebarCollapsed = true">
          <template #icon><NIcon :component="ChevronBackOutline" /></template>
        </NButton>
      </div>

      <NButton class="new-conversation" type="primary" block @click="createConversation">
        <template #icon><NIcon :component="AddOutline" /></template>
        {{ t('newConversation') }}
      </NButton>

      <div class="conversation-search">
        <NInput v-model:value="conversationSearch" size="small" clearable :placeholder="t('searchConversations')">
          <template #prefix><NIcon :component="SearchOutline" /></template>
        </NInput>
      </div>
      <div class="conversation-label">{{ t('recentConversations') }}</div>
      <nav class="conversation-list" :aria-label="t('recentConversations')">
        <div v-if="!filteredConversations.length" class="conversation-empty">{{ conversationSearch.trim() ? t('noMatchingConversations') : t('emptyConversations') }}</div>
        <button
          v-for="conversation in filteredConversations"
          :key="conversation.id"
          class="conversation-item"
          :class="{ active: conversation.id === activeConversationId }"
          @click="selectConversation(conversation.id)"
          @contextmenu.prevent="openRenameMenu(conversation, $event)"
        >
          <span class="conversation-title">{{ conversation.title }}</span>
          <span class="conversation-time">{{ formatTime(conversation.updatedAt) }}</span>
          <NPopconfirm
            :positive-text="t('deleteConfirmOk')"
            :negative-text="t('deleteConfirmCancel')"
            :positive-button-props="{ type: 'error' }"
            @positive-click="deleteConversation(conversation.id)"
          >
            <template #trigger>
              <span class="conversation-delete" role="button" tabindex="0">
                <NIcon :component="TrashOutline" />
              </span>
            </template>
            <strong>{{ t('deleteConfirmTitle') }}</strong>
            <span class="delete-confirm-body">{{ t('deleteConfirmBody') }}</span>
          </NPopconfirm>
        </button>
      </nav>

      <div class="sidebar-footer">
        <span class="connection-dot" :class="{ connected }" />
        {{ connectionLabel }}
      </div>
    </aside>

    <div
      v-if="renameMenu"
      class="rename-menu"
      :style="{ left: renameMenu.x + 'px', top: renameMenu.y + 'px' }"
      @click.stop
    >
      <button type="button" class="message-menu-item" @click="openRenameModal">
        <NIcon :component="CreateOutline" />{{ actionLabels.renameConversation }}
      </button>
    </div>

    <section class="workspace">
      <header class="workspace-header">
        <div class="header-title">
          <NTooltip>
            <template #trigger>
              <NButton class="mobile-menu" quaternary circle :aria-label="t('openConversations')" @click="sidebarVisible = true">
                <template #icon><NIcon :component="MenuOutline" /></template>
              </NButton>
            </template>
            {{ t('openConversations') }}
          </NTooltip>
          <NTooltip v-if="sidebarCollapsed">
            <template #trigger>
              <NButton class="sidebar-expand-btn" quaternary circle :aria-label="actionLabels.expandSidebar" @click="sidebarCollapsed = false">
                <template #icon><NIcon :component="MenuOutline" /></template>
              </NButton>
            </template>
            {{ actionLabels.expandSidebar }}
          </NTooltip>
          <div>
            <h1>{{ currentTitle }}</h1>
            <span>{{ isRunning ? t('taskRunning') : connectionLabel }}</span>
          </div>
        </div>
        <div class="header-actions">
          <NIcon :component="LanguageOutline" class="language-icon" />
          <NSelect v-model:value="locale" class="locale-select" size="small" :options="localeOptions" :aria-label="t('language')" />
          <NButton quaternary circle :title="actionLabels.openVoiceCall" @click="openVoiceCall">
            <template #icon><NIcon :component="CallOutline" /></template>
          </NButton>
          <NTooltip>
            <template #trigger>
              <NButton quaternary circle :type="speakReplyEnabled ? 'primary' : 'default'" :aria-label="actionLabels.speakReply" @click="toggleSpeakReply">
                <template #icon><NIcon :component="speakReplyEnabled ? VolumeHighOutline : VolumeMuteOutline" /></template>
              </NButton>
            </template>
            {{ actionLabels.speakReply }}
          </NTooltip>
          <NTooltip>
            <template #trigger>
              <NButton quaternary circle :type="screenShareActive ? 'primary' : 'default'" :aria-label="actionLabels.screenShare" @click="toggleScreenShare">
                <template #icon><NIcon :component="DesktopOutline" /></template>
              </NButton>
            </template>
            {{ screenShareActive ? actionLabels.screenShareActive : actionLabels.screenShare }}
          </NTooltip>
          <NDropdown :options="[{ label: t('newConversation'), key: 'new' }]" @select="createConversation">
            <NButton quaternary circle :aria-label="t('moreActions')">
              <template #icon><NIcon :component="EllipsisHorizontalOutline" /></template>
            </NButton>
          </NDropdown>
        </div>
      </header>

      <div class="timeline-wrap">
        <section ref="timeline" class="timeline" aria-live="polite" @scroll="onTimelineScroll" @wheel.passive="pauseAutoScrollByUser" @touchmove.passive="pauseAutoScrollByUser">
          <div v-if="!currentMessages.length" class="welcome">
          <div class="welcome-symbol"><NIcon :component="CodeSlashOutline" /></div>
          <h2>{{ t('startTask') }}</h2>
          <p>{{ t('startTaskHint') }}</p>
        </div>

        <div v-else class="message-column">
          <div v-if="showLoadEarlier" class="load-earlier" role="button" tabindex="0" @click="loadEarlierMessages">{{ t('loadEarlier') }}</div>
          <article v-for="item in visibleMessages" :key="item.id" class="message" :class="`message--${item.role}`">
            <NAvatar round :size="30" :color="item.role === 'user' ? '#2563eb' : '#1d2733'">
              {{ item.role === 'user' ? '我' : 'AI' }}
            </NAvatar>
            <div class="message-body">
              <div class="message-meta">
                <strong>{{ item.role === 'user' ? t('you') : t('assistant') }}</strong>
                <span>{{ formatTime(item.createdAt) }}</span>
              </div>
              <div v-if="item.role === 'user'" class="message-bubble user-bubble">
                <MarkdownMessage v-if="item.text" class="user-content" :content="item.text" />
                <div v-if="item.attachments.length" class="attachment-list">
                  <span v-for="attachment in item.attachments" :key="attachment.path ?? attachment.uri ?? attachment.name" class="attachment-chip" :class="{ 'attachment-chip--image': attachment.kind === 'image' }" role="button" :aria-label="attachment.kind === 'image' ? t('previewAttachment') : t('downloadAttachment')" @click="openAttachment(attachment)">
                    <img v-if="attachment.kind === 'image' && attachment.previewUrl" :src="attachment.previewUrl" :alt="attachment.name" class="attachment-preview" />
                    <NIcon :component="AttachOutline" /><span class="attachment-name">{{ attachment.name }}</span>
                  </span>
                </div>
              </div>
              <div v-if="item.role === 'user'" class="message-actions message-actions--user">
                <NTooltip>
                  <template #trigger>
                    <NButton quaternary circle size="tiny" :aria-label="actionLabels.copyText" @click="copyUserText(item, false)">
                      <template #icon><NIcon :component="CopyOutline" /></template>
                    </NButton>
                  </template>
                  {{ actionLabels.copyText }}
                </NTooltip>
                <NTooltip>
                  <template #trigger>
                    <NButton quaternary circle size="tiny" :aria-label="actionLabels.copyMarkdown" @click="copyUserText(item, true)">
                      <template #icon><NIcon :component="CodeSlashOutline" /></template>
                    </NButton>
                  </template>
                  {{ actionLabels.copyMarkdown }}
                </NTooltip>
                <NTooltip>
                  <template #trigger>
                    <NButton quaternary circle size="tiny" :aria-label="actionLabels.speak" @click="speakMessage(item)">
                      <template #icon><NIcon :component="speakingMessageId === item.id ? StopCircleOutline : VolumeHighOutline" /></template>
                    </NButton>
                  </template>
                  {{ speakingMessageId === item.id ? actionLabels.stopSpeak : actionLabels.speak }}
                </NTooltip>
                <NTooltip>
                  <template #trigger>
                    <NButton class="hidden-action" quaternary circle size="tiny" :aria-label="actionLabels.editMessage" @click="openEditMessage(item)">
                      <template #icon><NIcon :component="CreateOutline" /></template>
                    </NButton>
                  </template>
                  {{ actionLabels.editMessage }}
                </NTooltip>
                <div class="message-menu-wrap hidden-action" @click.stop>
                  <NTooltip>
                    <template #trigger>
                      <NButton quaternary circle size="tiny" :aria-label="actionLabels.more" @click.stop="toggleMessageMenu(item.id)">
                        <template #icon><NIcon :component="EllipsisHorizontalOutline" /></template>
                      </NButton>
                    </template>
                    {{ actionLabels.more }}
                  </NTooltip>
                  <div v-if="openMenuId === item.id" class="message-menu">
                    <button type="button" class="message-menu-item" @click="deleteMessage(item)">
                      <NIcon :component="TrashOutline" />{{ actionLabels.deleteMessage }}
                    </button>
                  </div>
                </div>
              </div>
              <template v-else>
                <template v-for="segment in item.segments" :key="segment.id">
                  <MarkdownMessage v-if="segment.type === 'text' || segment.type === 'thought'" class="assistant-content" :content="segment.text" />
                  <ExecutionProcess v-else-if="segment.type === 'plan'" :process="item.process" />
                  <ToolCallCard v-else-if="segment.type === 'tool'" :tool="segmentTool(item, segment)" />
                </template>
                <span v-if="isAssistantRunning(item)" class="msg-loader" aria-label="loading" />
                <div v-if="!isRunning && item.status !== 'pending'" class="message-actions">
                  <NTooltip v-if="item.id === lastAssistantId">
                    <template #trigger>
                      <NButton quaternary circle size="tiny" :aria-label="actionLabels.retry" @click="retryAssistant(item)">
                        <template #icon><NIcon :component="RefreshOutline" /></template>
                      </NButton>
                    </template>
                    {{ actionLabels.retry }}
                  </NTooltip>
                  <NTooltip>
                    <template #trigger>
                      <NButton quaternary circle size="tiny" :aria-label="actionLabels.copyText" @click="copyAssistantText(item, false)">
                        <template #icon><NIcon :component="CopyOutline" /></template>
                      </NButton>
                    </template>
                    {{ actionLabels.copyText }}
                  </NTooltip>
                  <NTooltip>
                    <template #trigger>
                      <NButton quaternary circle size="tiny" :aria-label="actionLabels.copyMarkdown" @click="copyAssistantText(item, true)">
                        <template #icon><NIcon :component="CodeSlashOutline" /></template>
                      </NButton>
                    </template>
                    {{ actionLabels.copyMarkdown }}
                  </NTooltip>
                  <NTooltip>
                    <template #trigger>
                      <NButton quaternary circle size="tiny" :aria-label="actionLabels.speak" @click="speakMessage(item)">
                        <template #icon><NIcon :component="speakingMessageId === item.id ? StopCircleOutline : VolumeHighOutline" /></template>
                      </NButton>
                    </template>
                    {{ speakingMessageId === item.id ? actionLabels.stopSpeak : actionLabels.speak }}
                  </NTooltip>
                  <NButton class="hidden-action" quaternary circle size="tiny" :aria-label="actionLabels.editMessage" :disabled="isMessageBusy(item)" @click="openEditMessage(item)">
                    <template #icon><NIcon :component="CreateOutline" /></template>
                  </NButton>
                  <div class="message-menu-wrap hidden-action" @click.stop>
                    <NButton quaternary circle size="tiny" :aria-label="actionLabels.more" @click.stop="toggleMessageMenu(item.id)">
                      <template #icon><NIcon :component="EllipsisHorizontalOutline" /></template>
                    </NButton>
                    <div v-if="openMenuId === item.id" class="message-menu">
                      <button type="button" class="message-menu-item" @click="deleteMessage(item)">
                        <NIcon :component="TrashOutline" />{{ actionLabels.deleteMessage }}
                      </button>
                    </div>
                  </div>
                </div>
              </template>
            </div>
          </article>
        </div>
      </section>
      <NButton v-if="showScrollBottom" class="scroll-bottom-btn" circle type="primary" size="small" :aria-label="t('scrollToBottom')" @click.capture="scrollToBottom(true)">
        <template #icon><NIcon :component="ArrowDownOutline" /></template>
      </NButton>
      </div>

      <div class="composer-wrap">
        <div class="composer-column">
          <NAlert v-if="errorText" type="error" closable class="task-error" @close="errorText = ''">
            {{ errorText }}
          </NAlert>
          <div v-if="attachments.length" class="pending-attachments">
            <span v-for="(attachment, index) in attachments" :key="attachment.path ?? attachment.uri ?? attachment.name" class="attachment-chip" :class="{ 'attachment-chip--image': attachment.kind === 'image' }">
              <img v-if="attachment.kind === 'image' && attachment.previewUrl" :src="attachment.previewUrl" :alt="attachment.name" class="attachment-preview" />
              <NIcon :component="AttachOutline" /><span class="attachment-name">{{ attachment.name }}</span>
              <button aria-label="移除附件" @click="removeAttachment(index)">×</button>
            </span>
          </div>
          <div class="composer" :class="{ 'composer--dragging': dragActive }" @dragenter.prevent="dragActive = true" @dragover.prevent="dragActive = true" @dragleave.prevent="dragActive = false" @drop.prevent="dropFiles" @paste="onComposerPaste">
            <input ref="fileInput" class="hidden-input" type="file" multiple @change="uploadFile" />
            <NInput v-model:value="input" class="composer-input" type="textarea" :autosize="{ minRows: 2, maxRows: 6 }" :placeholder="t('inputPlaceholder')" @keydown.enter.exact="submitOnEnter" @blur="onComposerBlur" />
            <div v-if="quickAction" class="quick-action-panel" @mousedown.prevent>
              <div v-if="quickLoading" class="quick-action-empty">{{ t('loading') }}</div>
              <div v-else-if="!quickItems.length" class="quick-action-empty">{{ t('noResults') }}</div>
              <div
                v-for="(item, index) in quickItems"
                :key="item.path ?? item.name"
                class="quick-action-item"
                :class="{ 'quick-action-item--active': index === quickIndex }"
                @mousedown.prevent="selectQuickItem(item)"
              >
                <NIcon :component="quickAction === 'file' ? (item.type === 'dir' ? FolderOutline : DocumentOutline) : SparklesOutline" />
                <span class="quick-action-name">{{ quickAction === 'file' ? item.name : item.name }}</span>
                <span class="quick-action-desc">{{ quickAction === 'skill' ? item.description : item.path }}</span>
              </div>
            </div>
            <div class="composer-toolbar">
              <div class="composer-toolbar-left">
                <NTooltip>
                  <template #trigger>
                    <NButton quaternary circle size="small" :loading="isUploading" :aria-label="t('upload')" @click="fileInput?.click()">
                      <template #icon><NIcon :component="AttachOutline" /></template>
                    </NButton>
                  </template>
                  {{ t('upload') }}
                </NTooltip>
                <NDropdown :options="agentDropdownOptions" trigger="click" @select="selectAgent">
                  <NButton quaternary size="small" class="toolbar-pill agent-pill" :aria-label="actionLabels.selectAgent">
                    <template #icon><NIcon :component="SparklesOutline" /></template>{{ currentAgentName || actionLabels.agent }}
                  </NButton>
                </NDropdown>
                <NButton quaternary size="small" class="toolbar-pill" :aria-label="actionLabels.skills" @click="openRightPanel('skills')">
                  <template #icon><NIcon :component="CodeSlashOutline" /></template>{{ actionLabels.skills }}
                </NButton>
                <NButton quaternary size="small" class="toolbar-pill" :aria-label="actionLabels.tools" @click="openRightPanel('tools')">
                  <template #icon><NIcon :component="ConstructOutline" /></template>{{ actionLabels.tools }}
                </NButton>
              </div>
              <div class="composer-toolbar-right">
                <span v-if="contextUsage" class="composer-context" :title="`${contextUsage.tokens} / ${contextUsage.total} tokens`">{{ actionLabels.context }} {{ contextUsage.pct }}%</span>
                <NTooltip v-if="isRunning">
                  <template #trigger>
                    <NButton circle type="error" size="small" :aria-label="t('cancel')" @click="cancelTask">
                      <template #icon><NIcon :component="StopCircleOutline" /></template>
                    </NButton>
                  </template>
                  {{ t('cancel') }}
                </NTooltip>
                <NTooltip v-else>
                  <template #trigger>
                    <NButton circle type="primary" size="small" :aria-label="t('send')" :disabled="(!input.trim() && !attachments.length) || isUploading" @click="submitPrompt">
                      <template #icon><NIcon :component="PaperPlaneOutline" /></template>
                    </NButton>
                  </template>
                  {{ t('send') }}
                </NTooltip>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <div v-if="previewImage" class="image-preview-overlay" @click="previewImage = null">
      <img :src="previewImage" :alt="actionLabels.previewAttachment" @click.stop />
    </div>

    <div v-if="initialLoading || conversationLoading > 0" class="chat-loading-overlay" aria-live="polite">
      <div class="chat-loading-box">
        <NSpin size="large" />
        <span>{{ t('loading') }}</span>
      </div>
    </div>

    <NDrawer v-model:show="sidebarVisible" placement="left" :width="280">
      <NDrawerContent :title="t('recentConversations')">
        <div class="drawer-list">
          <NButton type="primary" block @click="createConversation"><template #icon><NIcon :component="AddOutline" /></template>{{ t('newConversation') }}</NButton>
          <NInput v-model:value="conversationSearch" size="small" clearable :placeholder="t('searchConversations')">
            <template #prefix><NIcon :component="SearchOutline" /></template>
          </NInput>
          <NButton v-for="conversation in filteredConversations" :key="conversation.id" text class="drawer-item" @click="selectConversation(conversation.id)">
            {{ conversation.title }}
          </NButton>
        </div>
      </NDrawerContent>
    </NDrawer>

    <aside class="right-panel" :class="{ 'right-panel--open': rightPanelVisible }">
      <div class="right-panel-header">
        <strong>{{ rightPanelTitle }}</strong>
        <NButton quaternary circle size="tiny" :aria-label="actionLabels.closePanel" @click.capture="rightPanelVisible = false">
          <template #icon><NIcon :component="CloseOutline" /></template>
        </NButton>
      </div>
      <div class="right-panel-body">
        <div v-if="rightPanelTab === 'skills' || rightPanelTab === 'tools'" class="panel-search">
          <NInput v-model:value="panelQuery" size="small" clearable :placeholder="actionLabels.searchPlaceholder">
            <template #prefix><NIcon :component="SearchOutline" /></template>
          </NInput>
        </div>
        <!-- 技能管理 -->
        <div v-if="rightPanelTab === 'skills'" class="panel-section">
          <div v-if="!filteredSkills.length" class="panel-placeholder"><p>{{ panelQuery ? actionLabels.noMatch : actionLabels.emptyList }}</p></div>
          <div v-for="skill in filteredSkills" :key="skill.name" class="manage-card" :class="{ 'manage-card--disabled': !skill.enabled }">
            <div class="manage-card-head">
              <strong class="manage-card-title">{{ skill.name }}</strong>
              <NSwitch size="small" :value="skill.enabled" @update:value="toggleSkill(skill.name)" />
            </div>
            <p v-if="skill.description" class="manage-card-desc">{{ skill.description }}</p>
            <div class="manage-card-actions">
              <NButton quaternary size="tiny" @click="openSkillEdit(skill)">{{ actionLabels.edit }}</NButton>
            </div>
          </div>
        </div>
        <!-- 工具（MCP）管理 -->
        <div v-else-if="rightPanelTab === 'tools'" class="panel-section">
          <template v-if="filteredBuiltinTools.length">
            <div class="panel-group-head">
              <span class="panel-group-label">{{ actionLabels.builtinTools }}</span>
              <span class="panel-group-count">{{ filteredBuiltinTools.length }}</span>
            </div>
            <div class="builtin-tool-list">
              <span
                v-for="tool in filteredBuiltinTools"
                :key="tool.name"
                class="builtin-tool-chip"
                :class="{ 'builtin-tool-chip--active': innerToolEnabled(tool.name) }"
                :title="tool.description"
                role="button"
                @click="toggleInnerTool(tool.name)"
              >
                <code>{{ tool.name }}</code>
              </span>
            </div>
          </template>
          <template v-if="filteredToolList.length">
            <div class="panel-group-head panel-group-head--mcp">
              <span class="panel-group-label">{{ actionLabels.mcpServers }}</span>
              <span class="panel-group-count">{{ filteredToolList.length }}</span>
            </div>
            <div v-for="tool in filteredToolList" :key="tool.name" class="manage-card" :class="{ 'manage-card--disabled': !tool.enabled }">
              <div class="manage-card-head manage-card-head--group" @click="toggleToolGroup(tool.name)">
                <span class="tool-group-caret">{{ expandedToolGroups.includes(tool.name) ? '▾' : '▸' }}</span>
                <span class="manage-card-title">{{ tool.name }}</span>
                <span v-if="tool.tools?.length" class="tool-count">{{ tool.tools.length }}</span>
                <NSwitch size="small" :value="tool.enabled" @update:value="toggleTool(tool.name)" @click.stop />
              </div>
              <p v-if="tool.description" class="manage-card-desc">{{ tool.description }}</p>
              <code v-if="tool.command" class="manage-card-cmd">{{ tool.command }}</code>
              <div v-if="expandedToolGroups.includes(tool.name)" class="tool-item-list">
                <div v-for="t in tool.tools || []" :key="t.name" class="tool-item" :class="{ 'tool-item--disabled': !t.enabled }">
                  <div class="tool-item-head">
                    <code>{{ t.name }}</code>
                    <NSwitch size="small" :value="t.enabled" :disabled="!tool.enabled" @update:value="toggleToolItem(tool.name, t.name)" />
                  </div>
                  <p v-if="t.description" class="tool-item-desc">{{ t.description }}</p>
                </div>
                <div v-if="!(tool.tools || []).length" class="panel-placeholder"><p>{{ actionLabels.emptyList }}</p></div>
              </div>
            </div>
          </template>
          <div v-if="!filteredToolList.length && !filteredBuiltinTools.length" class="panel-placeholder">
            <p>{{ panelQuery ? actionLabels.noMatch : actionLabels.emptyList }}</p>
          </div>
        </div>
        <!-- Agent 管理 -->
        <div v-else-if="rightPanelTab === 'agents'" class="panel-section">
          <NButton size="small" @click="openAgentEdit()"><template #icon><NIcon :component="AddOutline" /></template>{{ actionLabels.newAgent }}</NButton>
          <div v-if="!agentList.length" class="panel-placeholder"><p>{{ actionLabels.emptyList }}</p></div>
          <div v-for="agent in agentList" :key="agent.name" class="manage-card" :class="{ 'manage-card--disabled': !agent.enabled }">
            <div class="manage-card-head">
              <strong>{{ agent.name }}</strong>
              <span v-if="agent.name === currentAgentName" class="agent-current">{{ actionLabels.currentAgent }}</span>
              <NSwitch size="small" :value="agent.enabled !== false" @update:value="setAgentEnabled(agent)" />
            </div>
            <p v-if="agent.description" class="manage-card-desc">{{ agent.description }}</p>
            <div v-if="(agent.skills || []).length" class="manage-card-chips">
              <span class="manage-chip-label">{{ actionLabels.linkedSkills }}</span>
              <NTag v-for="s in agent.skills" :key="s" size="small" type="info">{{ s }}</NTag>
            </div>
            <div v-if="(agent.tools || []).length" class="manage-card-chips">
              <span class="manage-chip-label">{{ actionLabels.linkedTools }}</span>
              <NTag v-for="t in agent.tools" :key="t" size="small" type="success">{{ t }}</NTag>
            </div>
            <div class="manage-card-actions">
              <NButton quaternary size="tiny" @click="openAgentEdit(agent)">{{ actionLabels.edit }}</NButton>
            </div>
          </div>
        </div>
        <!-- 设置（占位） -->
        <div v-else class="panel-placeholder">
          <p>{{ actionLabels.panelComingSoon }}</p>
        </div>
      </div>
    </aside>

    <!-- 技能新建/编辑弹窗 -->
    <NModal :show="skillModalVisible" :mask-closable="false" @after-leave="skillModalVisible = false">
      <NCard :title="skillForm.name ? actionLabels.edit : actionLabels.newSkill" :bordered="false" class="manage-modal manage-modal--wide" role="dialog">
        <div class="manage-form">
          <NInput v-if="!skillForm.name" v-model:value="skillForm.name" size="small" :placeholder="'name'" />
          <NInput v-model:value="skillForm.description" size="small" :placeholder="'description'" />
          <div class="skill-edit-split">
            <div class="skill-edit-col">
              <label class="skill-edit-label">SKILL.md (markdown)</label>
              <NInput v-model:value="skillForm.content" type="textarea" :autosize="{ minRows: 8, maxRows: 20 }" :placeholder="'SKILL.md content (markdown)'" class="manage-form-code skill-edit-editor" />
            </div>
            <div class="skill-edit-col skill-edit-preview">
              <label class="skill-edit-label">Preview</label>
              <div class="skill-edit-preview-body">
                <MarkdownMessage v-if="skillForm.content" :content="skillForm.content" />
                <p v-else class="skill-edit-preview-empty">No content</p>
              </div>
            </div>
          </div>
        </div>
        <template #footer>
          <div class="manage-form-actions">
            <NButton @click="skillModalVisible = false">{{ t('cancel') }}</NButton>
            <NButton type="primary" @click="submitSkill">OK</NButton>
          </div>
        </template>
      </NCard>
    </NModal>

    <!-- 工具新建/编辑弹窗 -->
    <NModal :show="toolModalVisible" :mask-closable="false" @after-leave="toolModalVisible = false">
      <NCard :title="toolForm.name ? actionLabels.edit : actionLabels.newTool" :bordered="false" class="manage-modal" role="dialog">
        <div class="manage-form">
          <NInput v-if="!toolForm.name" v-model:value="toolForm.name" size="small" :placeholder="'name'" />
          <NInput v-model:value="toolForm.description" size="small" :placeholder="'description'" />
          <NInput v-model:value="toolForm.command" size="small" :placeholder="'command (e.g. npx)'" />
          <NInput v-model:value="toolForm.args" size="small" :placeholder="'args (one per line)'" />
          <NInput v-model:value="toolForm.cwd" size="small" :placeholder="'cwd (optional)'" />
          <NInput v-model:value="toolForm.env" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" :placeholder="'env (KEY=VALUE, one per line)'" class="manage-form-code" />
        </div>
        <template #footer>
          <div class="manage-form-actions">
            <NButton @click="toolModalVisible = false">{{ t('cancel') }}</NButton>
            <NButton type="primary" @click="submitTool">OK</NButton>
          </div>
        </template>
      </NCard>
    </NModal>

    <!-- Agent 新建/编辑弹窗 -->
    <NModal :show="agentModalVisible" :mask-closable="false" @after-leave="agentModalVisible = false">
      <NCard :title="agentForm.name ? actionLabels.edit : actionLabels.newAgent" :bordered="false" class="manage-modal" role="dialog">
        <div class="manage-form">
          <NInput v-if="!agentForm.name" v-model:value="agentForm.name" size="small" :placeholder="'name'" />
          <NInput v-model:value="agentForm.description" size="small" :placeholder="'description'" />
          <NSelect v-model:value="agentForm.skills" multiple size="small" :options="agentSkillOptions" :placeholder="actionLabels.linkedSkills" />
          <NSelect v-model:value="agentForm.tools" multiple size="small" :options="agentToolOptions" :placeholder="actionLabels.linkedTools" />
        </div>
        <template #footer>
          <div class="manage-form-actions">
            <NButton @click="agentModalVisible = false">{{ t('cancel') }}</NButton>
            <NButton type="primary" @click="submitAgent">OK</NButton>
          </div>
        </template>
      </NCard>
    </NModal>

    <NModal :show="Boolean(permissionRequest)" :mask-closable="false">
      <NCard :title="t('permissionTitle')" :bordered="false" class="permission-card" role="dialog">
        <p>{{ t('permissionPrefix') }} <strong>{{ permissionRequest?.toolName }}</strong></p>
        <pre class="permission-code">{{ JSON.stringify(permissionRequest?.rawInput ?? {}, null, 2) }}</pre>
        <template #footer>
          <div class="permission-actions">
            <NButton @click="respondPermission('reject')">{{ t('reject') }}</NButton>
            <NButton type="primary" @click="respondPermission('approve')">{{ t('approve') }}</NButton>
            <NButton type="warning" @click="respondPermission('approve_always')">{{ t('approveAlways') }}</NButton>
          </div>
        </template>
      </NCard>
    </NModal>
    <NModal :show="editModalVisible" :mask-closable="false" @after-leave="editTargetId = null">
      <NCard :title="actionLabels.editTitle" :bordered="false" class="edit-card" role="dialog">
        <div class="edit-body">
          <NInput
            v-model:value="editText"
            type="textarea"
            :autosize="{ minRows: 10, maxRows: 30 }"
            :placeholder="'Markdown'"
            class="edit-input"
          />
          <div class="edit-preview">
            <div class="edit-preview-label">{{ actionLabels.preview }}</div>
            <div class="edit-preview-body">
              <MarkdownMessage :content="editText" />
            </div>
          </div>
        </div>
        <template #footer>
          <div class="permission-actions">
            <NButton @click="editModalVisible = false">{{ actionLabels.cancel }}</NButton>
            <NButton type="primary" @click="saveEditMessage">{{ actionLabels.save }}</NButton>
          </div>
        </template>
      </NCard>
    </NModal>
    <NModal :show="Boolean(renameTarget)" :mask-closable="false" @after-leave="renameTarget = null">
      <NCard :title="actionLabels.renameTitle" :bordered="false" class="rename-card" role="dialog">
        <NInput
          v-model:value="renameText"
          type="text"
          :placeholder="actionLabels.renamePlaceholder"
          @keydown.enter.exact="saveRename"
        />
        <template #footer>
          <div class="permission-actions">
            <NButton @click="renameTarget = null">{{ actionLabels.cancel }}</NButton>
            <NButton type="primary" @click="saveRename">{{ actionLabels.save }}</NButton>
          </div>
        </template>
      </NCard>
    </NModal>
  </main>
</template>

<style scoped>
.chat-app { --canvas:#f7f8fa; --surface:#fff; --muted:#f1f4f8; --border:#e4e8ee; --text:#1d2733; --subtle:#6b7785; height:100dvh; display:flex; overflow:hidden; background:var(--canvas); color:var(--text); }
.sidebar { width:264px; flex:0 0 264px; display:flex; flex-direction:column; padding:22px 14px 16px; background:var(--surface); border-right:1px solid var(--border); }
.brand-row { display:flex; gap:10px; align-items:center; padding:0 10px 23px; } .brand-row strong { display:block; font-size:15px; } .brand-row span { display:block; margin-top:2px; color:var(--subtle); font-size:11px; } .sidebar-collapse-btn { margin-left:auto; } .sidebar-expand-btn { display:none; } .chat-app--collapsed .sidebar { width:0; flex:0 0 0; padding:0; overflow:hidden; border-right:0; } .mobile-menu { display:none; }
.brand-mark,.welcome-symbol { display:grid; place-items:center; width:32px; height:32px; color:#fff; background:#1d2733; border-radius:8px; font-size:18px; }
.new-conversation { justify-content:flex-start; margin-bottom:16px; } .conversation-search { padding:0 2px 8px; } .conversation-label { padding:0 10px 8px; color:var(--subtle); font-size:12px; } .conversation-empty { padding:14px 10px; color:var(--subtle); font-size:12px; text-align:center; } .delete-confirm-body { display:block; margin-top:2px; color:var(--subtle); font-size:12px; }
.conversation-list { display:flex; flex:1; flex-direction:column; gap:3px; overflow:auto; } .conversation-item { position:relative; min-height:54px; padding:9px 28px 9px 10px; overflow:hidden; color:var(--text); text-align:left; background:transparent; border:0; border-radius:7px; cursor:pointer; } .conversation-item:hover { background:var(--muted); } .conversation-item.active { background:#eaf1ff; color:#174cb9; }
.conversation-title,.conversation-time { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .conversation-title { font-size:13px; } .conversation-time { margin-top:4px; color:var(--subtle); font-size:11px; } .conversation-delete { position:absolute; right:8px; top:18px; display:none; color:var(--subtle); } .conversation-item:hover .conversation-delete { display:block; }
.sidebar-footer { display:flex; gap:7px; align-items:center; padding:10px; color:var(--subtle); font-size:12px; } .connection-dot { width:7px; height:7px; border-radius:50%; background:#aab4c0; } .connection-dot.connected { background:#16805b; }
.workspace { min-width:0; flex:1; display:flex; flex-direction:column; } .workspace-header { height:64px; flex:0 0 64px; display:flex; align-items:center; justify-content:space-between; padding:0 28px; background:rgba(255,255,255,.72); border-bottom:1px solid var(--border); } .header-title,.header-actions { display:flex; align-items:center; gap:8px; } .header-title h1 { max-width:440px; margin:0; overflow:hidden; font-size:15px; font-weight:650; text-overflow:ellipsis; white-space:nowrap; } .header-title span { display:block; margin-top:3px; color:var(--subtle); font-size:11px; } .mobile-menu { display:none; } .language-icon { color:var(--subtle); font-size:17px; } .locale-select { width:102px; }
.timeline-wrap { position:relative; flex:1; min-height:0; display:flex; flex-direction:column; } .timeline { flex:1; overflow:auto; scroll-behavior:smooth; } .scroll-bottom-btn { position:absolute; right:max(28px, calc((100% - 840px) / 2 + 28px)); bottom:28px; z-index:5; box-shadow:0 2px 10px rgba(29,39,51,.2); } .message-column,.composer-column { width:min(840px, calc(100% - 64px)); margin:0 auto; } .message-column { padding:32px 0 48px; } .welcome { display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:100%; padding:48px 24px 150px; text-align:center; } .welcome-symbol { width:44px; height:44px; margin-bottom:16px; font-size:23px; } .welcome h2 { margin:0 0 8px; font-size:20px; } .welcome p { max-width:360px; margin:0; color:var(--subtle); font-size:14px; line-height:1.7; }
.message { display:flex; gap:10px; margin-bottom:24px; } .message--user { flex-direction:row-reverse; } .message-body { min-width:0; max-width:calc(100% - 42px); } .message--user .message-body { display:flex; flex-direction:column; align-items:flex-end; width:fit-content; max-width:72%; } .message-meta { display:flex; gap:8px; align-items:center; margin-bottom:6px; color:var(--subtle); font-size:12px; } .message-meta strong { color:var(--text); font-size:13px; } .message-bubble { border-radius:8px; } .user-bubble { max-width:100%; padding:11px 14px; background:#eaf1ff; } .user-bubble p { margin:0; white-space:pre-wrap; word-break:break-word; }
.assistant-content { margin-bottom:12px; } .message-status { display:inline-block; margin-top:10px; color:#b76a00; font-size:12px; }
.message-actions { display:flex; gap:2px; align-items:center; margin-top:7px; } .message-actions--user { justify-content:flex-end; } .message-menu-wrap { position:relative; display:inline-flex; } .message-menu { position:absolute; right:0; bottom:calc(100% + 6px); z-index:60; display:flex; flex-direction:column; min-width:130px; padding:4px; background:#fff; border:1px solid #e4e8ee; border-radius:8px; box-shadow:0 4px 16px rgba(29,39,51,.12); } .message-menu-item { display:flex; gap:6px; align-items:center; padding:6px 10px; color:#1d2733; background:transparent; border:0; border-radius:6px; cursor:pointer; font-size:13px; } .message-menu-item:hover { background:#f1f4f8; }
.attachment-list,.pending-attachments { display:flex; flex-wrap:wrap; gap:6px; margin-top:9px; } .attachment-chip { display:inline-flex; gap:5px; align-items:center; max-width:220px; padding:4px 8px; overflow:hidden; color:#34527e; background:#fff; border:1px solid #cbd8ed; border-radius:6px; font-size:12px; cursor:pointer; } .attachment-chip .attachment-name { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .attachment-chip button { flex-shrink:0; } .attachment-chip--image { max-width:260px; } .attachment-preview { width:34px; height:34px; flex:0 0 34px; object-fit:cover; border:1px solid #d8e1ef; border-radius:4px; } .attachment-chip button { padding:0; color:inherit; background:none; border:0; cursor:pointer; font-size:15px; }
.process-collapse { margin-top:14px; background:var(--surface); border:1px solid var(--border); border-radius:8px; } .process-heading,.tool-heading { display:flex; gap:7px; align-items:center; min-width:0; } .process-heading { color:#405166; font-size:13px; } .process-heading small { color:var(--subtle); font-size:11px; } .plan-list { display:flex; flex-direction:column; gap:7px; padding:2px 0 10px; } .plan-item { display:flex; gap:8px; align-items:center; color:#405166; font-size:13px; } .plan-index { display:grid; place-items:center; width:19px; height:19px; color:#2563eb; background:#eaf1ff; border-radius:50%; font-size:11px; } .plan-item :deep(.n-tag) { margin-left:auto; } .detail-collapse { margin-top:8px; border:1px solid var(--border); border-radius:6px; } .analysis-text { padding:8px 0; color:#405166; line-height:1.65; white-space:pre-wrap; } .tool-heading span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .tool-heading :deep(.n-tag) { margin-left:auto; } .message-status { margin-top:10px; }
.composer-wrap { flex:0 0 auto; padding:12px 0 20px; background:linear-gradient(0deg, var(--canvas) 82%, rgba(247,248,250,0)); } .task-error { margin-bottom:10px; } .pending-attachments { margin:0 0 8px; } .composer { position:relative; display:flex; flex-direction:column; gap:2px; padding:10px 12px 8px; background:var(--surface); border:1px solid var(--border); border-radius:20px; box-shadow:0 4px 14px rgba(29,39,51,.05); } .composer:focus-within { border-color:var(--border); box-shadow:0 4px 14px rgba(29,39,51,.05); } .composer--dragging { border-color:#2563eb; background:#f5f9ff; box-shadow:0 0 0 3px rgba(37,99,235,.16); } .composer-input :deep(.n-input) { --n-border: transparent; --n-box-shadow: none; --n-box-shadow-focus: none; --n-box-shadow-hover: none; --n-color: transparent; --n-color-focus: transparent; background:transparent !important; box-shadow:none !important; } .composer-input :deep(.n-input__border), .composer-input :deep(.n-input__state-border) { border:0 !important; box-shadow:none !important; display:none; } .composer-input :deep(textarea) { padding-top:8px; padding-bottom:4px; } .composer-toolbar { display:flex; align-items:center; justify-content:space-between; margin-top:2px; } .composer-toolbar-left { display:flex; align-items:center; gap:2px; } .composer-toolbar-right { display:flex; align-items:center; gap:6px; } .toolbar-pill { border-radius:10px; padding:0 10px; } .composer-context { font-size:12px; color:var(--subtle); white-space:nowrap; } .hidden-input { display:none; } .rename-menu { position:fixed; z-index:70; display:flex; flex-direction:column; min-width:130px; padding:4px; background:#fff; border:1px solid #e4e8ee; border-radius:8px; box-shadow:0 4px 16px rgba(29,39,51,.12); }
.permission-card { width:min(520px, calc(100vw - 32px)); } .permission-code { max-height:260px; margin:0; padding:10px; overflow:auto; background:#f1f4f8; border:1px solid #e4e8ee; border-radius:6px; font:12px/1.55 "Cascadia Code",Consolas,monospace; } .permission-actions { display:flex; justify-content:flex-end; gap:8px; }
.hidden-action { display: none; }
.agent-pill { max-width:130px; overflow:hidden; text-overflow:ellipsis; }
.agent-option { padding:2px 0; } .agent-option-name { font-weight:600; } .agent-option-desc { margin-top:2px; color:#999; font-size:12px; line-height:1.4; }
.panel-placeholder { display:flex; flex-direction:column; align-items:center; gap:10px; padding:44px 10px; color:var(--subtle); font-size:13px; text-align:center; } .panel-placeholder .n-icon { font-size:22px; } .panel-section { display:flex; flex-direction:column; gap:6px; padding:4px 2px 14px; } .panel-section label { color:var(--subtle); font-size:12px; }
.right-panel { display:none; flex:0 0 368px; width:368px; flex-direction:column; background:var(--surface); border-left:1px solid var(--border); } .right-panel--open { display:flex; } .right-panel-header { display:flex; align-items:center; justify-content:space-between; height:48px; padding:0 10px 0 16px; border-bottom:1px solid var(--border); } .right-panel-header strong { font-size:14px; } .right-panel-body { flex:1; overflow-y:auto; padding:12px; display:flex; flex-direction:column; gap:10px; }
.panel-search { position:sticky; top:0; z-index:2; background:var(--surface); padding-bottom:6px; }
.panel-section { display:flex; flex-direction:column; gap:10px; }
.panel-section > .n-button { align-self:flex-start; }
.panel-group-head { display:flex; align-items:center; gap:6px; margin-top:2px; }
.panel-group-head--mcp { margin-top:6px; padding-top:8px; border-top:1px dashed var(--border); }
.panel-group-label { font-size:12px; font-weight:600; color:var(--text); }
.panel-group-count { font-size:10px; color:var(--subtle); background:var(--canvas); border:1px solid var(--border); border-radius:99px; padding:0 6px; line-height:16px; }
.manage-card { border:1px solid var(--border); border-radius:10px; padding:10px 12px; background:var(--canvas); display:flex; flex-direction:column; gap:6px; transition:box-shadow .15s ease; }
.manage-card:hover { box-shadow:0 2px 8px rgba(29,39,51,.06); }
.manage-card--disabled { opacity:.55; }
.manage-card-head { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.manage-card-title { font-size:13px; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.manage-card-desc { font-size:12px; color:var(--subtle); margin:0; line-height:1.5; word-break:break-word; }
.manage-card-cmd { display:block; font-size:11px; color:var(--subtle); background:var(--surface); border-radius:6px; padding:4px 8px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.manage-card-chips { display:flex; align-items:center; flex-wrap:wrap; gap:4px; }
.manage-chip-label { font-size:11px; color:var(--subtle); margin-right:2px; }
.manage-card-actions { display:flex; justify-content:flex-end; gap:2px; }
.manage-card-head--group { cursor:pointer; user-select:none; }
.tool-group-caret { font-size:12px; color:var(--subtle); width:14px; text-align:center; }
.tool-count { font-size:10px; color:var(--subtle); background:var(--surface); border-radius:99px; padding:0 6px; line-height:16px; }
.tool-item-list { display:flex; flex-direction:column; gap:6px; border-left:2px solid var(--border); margin:2px 0 2px 10px; padding:2px 0 2px 10px; }
.tool-item { padding:6px 8px; border-radius:8px; background:var(--surface); display:flex; flex-direction:column; gap:4px; }
.tool-item--disabled { opacity:.55; }
.tool-item-head { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.tool-item-head code { font-size:12px; color:var(--primary, inherit); }
.tool-item-desc { font-size:11px; color:var(--subtle); margin:0; line-height:1.5; word-break:break-word; max-height:3em; overflow:hidden; }
.agent-current { font-size:10px; color:#2563eb; border:1px solid rgba(37,99,235,.4); border-radius:99px; padding:1px 7px; }
.manage-modal { width:min(520px, calc(100vw - 32px)); }
.manage-form { display:flex; flex-direction:column; gap:10px; }
.manage-form-code :deep(textarea) { font-family:"Cascadia Code",Consolas,monospace; font-size:12px; line-height:1.6; }
.manage-form-actions { display:flex; justify-content:flex-end; gap:8px; }
.agent-option-name { font-size:13px; }
.agent-option-desc { font-size:11px; color:var(--subtle); margin-top:2px; max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.quick-action-panel { position:absolute; bottom:calc(100% + 8px); left:0; right:0; z-index:30; max-height:220px; overflow-y:auto; padding:4px; background:var(--surface); border:1px solid var(--border); border-radius:12px; box-shadow:0 8px 24px rgba(29,39,51,.12); display:flex; flex-direction:column; gap:2px; }
.quick-action-item { display:flex; align-items:center; gap:8px; padding:7px 10px; border-radius:8px; font-size:13px; cursor:pointer; color:var(--text); }
.quick-action-item:hover, .quick-action-item--active { background:rgba(37,99,235,.08); }
.quick-action-name { flex:0 0 auto; max-width:45%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; }
.quick-action-desc { color:var(--subtle); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.quick-action-empty { padding:12px; color:var(--subtle); font-size:12px; text-align:center; }
.builtin-tool-list { display:flex; flex-wrap:wrap; gap:6px; }
.builtin-tool-chip { padding:3px 8px; border:1px solid var(--border); border-radius:8px; background:rgba(37,99,235,.05); cursor:pointer; transition:all .15s ease; }
.builtin-tool-chip:hover { border-color:rgba(37,99,235,.6); }
.builtin-tool-chip code { font:12px/1.4 "Cascadia Code",Consolas,monospace; color:var(--text); }
.builtin-tool-chip--active { background:rgba(37,99,235,.14); border-color:rgba(37,99,235,.55); }
.builtin-tool-chip--active code { color:#2563eb; font-weight:600; }
.builtin-tool-chip:not(.builtin-tool-chip--active) { opacity:.62; }
.skill-edit-split { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:10px; }
.skill-edit-col { display:flex; flex-direction:column; gap:6px; min-height:0; }
.skill-edit-label { color:var(--subtle); font-size:12px; }
.skill-edit-editor :deep(textarea) { font:12px/1.5 "Cascadia Code",Consolas,monospace !important; }
.skill-edit-preview-body { flex:1; min-height:280px; max-height:420px; overflow-y:auto; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:var(--canvas); }
.skill-edit-preview-empty { color:var(--subtle); font-size:12px; }
.manage-modal--wide { width:min(860px, calc(100vw - 32px)); }
.edit-card { width:70vw; } .rename-card { width:min(400px, calc(100vw - 32px)); } .edit-body { display:flex; gap:16px; align-items:stretch; } .edit-input { flex:1 1 50%; min-width:0; } .edit-input :deep(textarea) { font-family:"Cascadia Code",Consolas,monospace; font-size:13px; line-height:1.6; } .edit-preview { flex:1 1 50%; min-width:0; display:flex; flex-direction:column; } .edit-preview-label { margin-bottom:6px; color:var(--subtle); font-size:12px; } .edit-preview-body { flex:1; padding:12px 14px; background:var(--canvas); border:1px solid var(--border); border-radius:8px; overflow:auto; } .edit-preview-body :deep(p) { margin:0 0 8px; } .edit-preview-body :deep(p:last-child) { margin-bottom:0; }.drawer-list { display:flex; flex-direction:column; gap:8px; } .drawer-item { justify-content:flex-start; padding:10px; }
@media (min-width: 901px) { .chat-app--collapsed .sidebar-expand-btn { display:inline-flex; } }
@media (max-width: 900px) { .sidebar { display:none; } .mobile-menu { display:inline-flex; } .right-panel { display:none; } .workspace-header { padding:0 14px; } .message-column,.composer-column { width:calc(100% - 32px); } .message-column { padding-top:22px; } .message--user .message-body { max-width:86%; } }
/* animated loading dots under a running assistant message */
.msg-loader {
  --color-1: #9aa3b2;
  --color-2: #9aa3b233;
  --size: 0.4px;
  width: calc(16 * var(--size));
  height: calc(16 * var(--size));
  border-radius: 50%;
  background-color: var(--color-1);
  box-shadow:
    calc(32 * var(--size)) 0 var(--color-1),
    calc(-32 * var(--size)) 0 var(--color-1);
  position: relative;
  margin: 8px 0 2px 15px;
  display: block;
  animation: msgflash 0.5s ease-out infinite alternate;
}
@keyframes msgflash {
  0% {
    background-color: var(--color-2);
    box-shadow:
      calc(32 * var(--size)) 0 var(--color-2),
      calc(-32 * var(--size)) 0 var(--color-1);
  }
  50% {
    background-color: var(--color-1);
    box-shadow:
      calc(32 * var(--size)) 0 var(--color-2),
      calc(-32 * var(--size)) 0 var(--color-2);
  }
  100% {
    background-color: var(--color-2);
    box-shadow:
      calc(32 * var(--size)) 0 var(--color-1),
      calc(-32 * var(--size)) 0 var(--color-2);
  }
}
.image-preview-overlay {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.82);
  cursor: zoom-out;
}
.image-preview-overlay img {
  max-width: 92vw;
  max-height: 92vh;
  border-radius: 8px;
  box-shadow: 0 8px 40px rgba(0, 0, 0, 0.6);
  cursor: default;
}
.chat-loading-overlay {
  position: fixed;
  inset: 0;
  z-index: 1600;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(247, 248, 250, 0.6);
}
.chat-loading-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  padding: 22px 34px;
  border-radius: 14px;
  background: var(--surface);
  box-shadow: 0 6px 30px rgba(29, 39, 51, 0.14);
  border: 1px solid var(--border);
  color: var(--subtle);
  font-size: 13px;
}
.load-earlier {
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 14px auto;
  padding: 8px 18px;
  width: fit-content;
  border-radius: 20px;
  background: var(--muted);
  border: 1px solid var(--border);
  color: var(--subtle);
  font-size: 12px;
  cursor: pointer;
  user-select: none;
  transition: background 0.2s, color 0.2s;
}
.load-earlier:hover {
  background: var(--border);
  color: var(--text);
}
</style>
