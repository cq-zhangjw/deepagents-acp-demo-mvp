/**
 * Knowledge base API (MooFile Base integration).
 *
 * The gateway proxies database listing (/api/knowledge/databases) and
 * per-agent enable/disable (toggle writes `.deepagents/agents/agents.json`
 * `knowledge[]`). The management page itself is opened in a new window at
 * MOOFILE_BASE_URL (config.baseUrl).
 */

export interface KnowledgeConfig {
  baseUrl: string
  topK: number
  threshold: number | null
  reachable: boolean
}

export interface KnowledgeDb {
  id: string
  name: string
  type: 'normal' | 'vector'
  recordCount: number
  documentCount: number
  enabled: boolean
}

export async function fetchKnowledgeConfig(): Promise<KnowledgeConfig> {
  const resp = await fetch('/api/knowledge/config', { cache: 'no-store' })
  if (!resp.ok) throw new Error('knowledge config fetch failed')
  return (await resp.json()) as KnowledgeConfig
}

export async function fetchKnowledgeDatabases(agent: string): Promise<KnowledgeDb[]> {
  const resp = await fetch(`/api/knowledge/databases?agent=${encodeURIComponent(agent || 'default')}`, {
    cache: 'no-store'
  })
  if (!resp.ok) throw new Error('knowledge databases fetch failed')
  const data = (await resp.json()) as KnowledgeDb[]
  return Array.isArray(data) ? data : []
}

export async function toggleKnowledgeDb(dbId: string, agent: string): Promise<void> {
  const resp = await fetch(
    `/api/knowledge/databases/${encodeURIComponent(dbId)}/toggle?agent=${encodeURIComponent(agent || 'default')}`,
    { method: 'POST' }
  )
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}))
    throw new Error(data.detail || data.error || `HTTP ${resp.status}`)
  }
}

export interface KnowledgeStartResult {
  ok: boolean
  already?: boolean
  started?: boolean
}

export async function startKnowledgeService(): Promise<KnowledgeStartResult> {
  const resp = await fetch('/api/knowledge/start', { method: 'POST' })
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}))
    throw new Error(data.detail || data.error || `HTTP ${resp.status}`)
  }
  return (await resp.json()) as KnowledgeStartResult
}
