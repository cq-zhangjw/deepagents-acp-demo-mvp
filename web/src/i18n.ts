import { createI18n } from 'vue-i18n'

export type SupportedLocale = 'zh' | 'ja' | 'en'

const storageKey = 'acp-ui-locale'
const messages = {
  zh: {
    brandSubline: 'DeepAgents 工作台', language: '语言', newConversation: '新建会话', recentConversations: '最近会话', connected: '已连接', disconnected: '未连接', taskRunning: '任务执行中', taskFailed: '任务执行失败', openConversations: '打开会话列表', moreActions: '更多会话操作', startTask: '开始一个任务', startTaskHint: '描述目标，或附加图片与文件，让 Agent 帮你分步完成。', you: '你', assistant: 'DeepAgents', processing: '正在处理任务', waitingPermission: '等待你的权限确认', viewProcess: '查看执行过程', steps: '步', seconds: '秒', analysisStep: '分析步骤', parameters: '参数', output: '返回值', upload: '上传附件', removeAttachment: '移除附件', inputPlaceholder: '输入任务，Enter 发送，Shift + Enter 换行', send: '发送任务', cancel: '取消任务', permissionTitle: 'Agent 请求权限', permissionPrefix: '即将执行：', reject: '拒绝', approve: '允许', approveAlways: '始终允许', deleteConversation: '删除会话', newSessionTitle: '新建会话', attachmentTask: '附件任务', taskComplete: '任务已完成。', uploadFailed: '文件上传失败。', connectionFailed: '无法连接 ACP 服务，请确认后端已启动。', connectionClosed: 'ACP 连接已断开。', connectionNotReady: 'ACP 连接尚未建立。', requestFailed: 'ACP 请求失败。', invalidSession: 'Agent 未返回有效会话 ID。', copied: '已复制', searchConversations: '搜索会话', noMatchingConversations: '未找到匹配的会话', emptyConversations: '暂无会话', deleteConfirmTitle: '删除会话？', deleteConfirmBody: '该会话的历史记录将被移除，且无法恢复。', deleteConfirmOk: '删除', deleteConfirmCancel: '取消', status: { pending: '处理中', streaming: '生成中', completed: '已完成', cancelled: '已取消', failed: '失败', running: '运行中', waiting_permission: '等待授权' }
  },
  ja: {
    brandSubline: 'DeepAgents ワークスペース', language: '言語', newConversation: '新しい会話', recentConversations: '最近の会話', connected: '接続済み', disconnected: '未接続', taskRunning: 'タスク実行中', taskFailed: 'タスク実行失敗', openConversations: '会話一覧を開く', moreActions: 'その他の操作', startTask: 'タスクを始める', startTaskHint: '目的を入力するか、画像やファイルを添付してください。', you: 'あなた', assistant: 'DeepAgents', processing: 'タスクを処理中', waitingPermission: '権限の確認待ち', viewProcess: '実行プロセスを表示', steps: 'ステップ', seconds: '秒', analysisStep: '分析ステップ', parameters: 'パラメーター', output: '出力', upload: '添付をアップロード', removeAttachment: '添付を削除', inputPlaceholder: 'タスクを入力。Enter で送信、Shift + Enter で改行', send: '送信', cancel: 'タスクを中止', permissionTitle: 'Agent が権限を要求しています', permissionPrefix: '実行内容：', reject: '拒否', approve: '許可', approveAlways: '常に許可', deleteConversation: '会話を削除', newSessionTitle: '新しい会話', attachmentTask: '添付タスク', taskComplete: 'タスクが完了しました。', uploadFailed: 'ファイルをアップロードできませんでした。', connectionFailed: 'ACP サービスに接続できません。', connectionClosed: 'ACP 接続が切断されました。', connectionNotReady: 'ACP 接続が確立されていません。', requestFailed: 'ACP リクエストに失敗しました。', invalidSession: 'Agent から有効なセッション ID が返されませんでした。', copied: 'コピーしました', searchConversations: '会話を検索', noMatchingConversations: '一致する会話がありません', emptyConversations: '会話はありません', deleteConfirmTitle: '会話を削除しますか？', deleteConfirmBody: 'この会話の履歴は削除され、復元できません。', deleteConfirmOk: '削除', deleteConfirmCancel: 'キャンセル', status: { pending: '処理中', streaming: '生成中', completed: '完了', cancelled: 'キャンセル済み', failed: '失敗', running: '実行中', waiting_permission: '権限待ち' }
  },
  en: {
    brandSubline: 'DeepAgents Workspace', language: 'Language', newConversation: 'New conversation', recentConversations: 'Recent conversations', connected: 'Connected', disconnected: 'Disconnected', taskRunning: 'Task running', taskFailed: 'Task failed', openConversations: 'Open conversations', moreActions: 'More actions', startTask: 'Start a task', startTaskHint: 'Describe a goal or attach an image or file for the Agent.', you: 'You', assistant: 'DeepAgents', processing: 'Processing task', waitingPermission: 'Waiting for your approval', viewProcess: 'View execution process', steps: 'steps', seconds: 'seconds', analysisStep: 'Analysis step', parameters: 'Parameters', output: 'Output', upload: 'Upload attachment', removeAttachment: 'Remove attachment', inputPlaceholder: 'Enter a task. Enter to send, Shift + Enter for a new line', send: 'Send task', cancel: 'Cancel task', permissionTitle: 'Agent requests permission', permissionPrefix: 'About to run:', reject: 'Reject', approve: 'Allow', approveAlways: 'Always allow', deleteConversation: 'Delete conversation', newSessionTitle: 'New conversation', attachmentTask: 'Attachment task', taskComplete: 'Task completed.', uploadFailed: 'File upload failed.', connectionFailed: 'Unable to connect to the ACP service.', connectionClosed: 'ACP connection closed.', connectionNotReady: 'ACP connection is not ready.', requestFailed: 'ACP request failed.', invalidSession: 'The Agent did not return a valid session ID.', copied: 'Copied', searchConversations: 'Search conversations', noMatchingConversations: 'No matching conversations', emptyConversations: 'No conversations yet', deleteConfirmTitle: 'Delete this conversation?', deleteConfirmBody: 'Its history will be removed and cannot be restored.', deleteConfirmOk: 'Delete', deleteConfirmCancel: 'Cancel', status: { pending: 'Processing', streaming: 'Generating', completed: 'Completed', cancelled: 'Cancelled', failed: 'Failed', running: 'Running', waiting_permission: 'Waiting for approval' }
  }
}

function resolveLocale(): SupportedLocale {
  const saved = localStorage.getItem(storageKey)
  if (saved === 'zh' || saved === 'ja' || saved === 'en') return saved
  const browser = navigator.language.toLowerCase()
  if (browser.startsWith('ja')) return 'ja'
  if (browser.startsWith('zh')) return 'zh'
  return 'en'
}

const i18n = createI18n({
  legacy: false,
  locale: resolveLocale(),
  fallbackLocale: 'en',
  messages
})

export { storageKey }
export default i18n