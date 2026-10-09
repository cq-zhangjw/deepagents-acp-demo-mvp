<script setup lang="ts">
import { computed } from 'vue'
import { NCollapse, NCollapseItem, NTag, NTabs, NTabPane } from 'naive-ui'
import { useI18n } from 'vue-i18n'
import MarkdownMessage from './MarkdownMessage.vue'

type ToolStatus = 'running' | 'completed' | 'failed' | 'cancelled' | 'waiting_permission'

interface ToolCallEntry {
  id: string
  name: string
  title?: string
  status: ToolStatus
  rawInput?: unknown
  output?: unknown
}

const props = defineProps<{ tool: ToolCallEntry }>()
const { t } = useI18n()

function jsonValue(value: unknown) {
  if (typeof value === 'string') return value
  try { return JSON.stringify(value ?? {}, null, 2) } catch { return String(value ?? '') }
}

// 参数理论上始终是 JSON 对象/字符串 → 用 ```json 代码块 + markdown 渲染（带语法高亮与复制按钮）
const paramsMarkdown = computed(() => `\`\`\`json\n${jsonValue(props.tool.rawInput)}\n\`\`\``)

// 返回值可能携带 markdown（如 execute 的 Command/Output/Status 结构）→ markdown 渲染；
// 若为对象则回退为 JSON 代码块
const outputMarkdown = computed(() => {
  const raw = props.tool.output
  if (typeof raw === 'string') return raw
  return `\`\`\`json\n${jsonValue(raw)}\n\`\`\``
})

function tagType(status: ToolStatus) {
  if (status === 'completed') return 'success'
  if (status === 'failed') return 'error'
  if (status === 'waiting_permission') return 'warning'
  return 'info'
}

function statusText(status: string) {
  return t(`status.${status}`)
}
</script>

<template>
  <NCollapse class="tool-call-collapse">
    <NCollapseItem name="tool">
      <template #header>
        <div class="tool-heading">
          <code class="tool-badge">{{ tool.name || 'Run' }}</code>
          <span v-if="tool.title" class="tool-name" :title="tool.title">{{ tool.title }}</span>
          <NTag size="small" :type="tagType(tool.status)">{{ statusText(tool.status) }}</NTag>
        </div>
      </template>
      <NTabs type="line" size="small" class="tool-tabs">
        <NTabPane name="input" :tab="t('parameters')">
          <div class="tool-pane tool-pane--params"><MarkdownMessage :content="paramsMarkdown" /></div>
        </NTabPane>
        <NTabPane name="output" :tab="t('output')">
          <div class="tool-pane tool-pane--output"><MarkdownMessage :content="outputMarkdown" /></div>
        </NTabPane>
      </NTabs>
    </NCollapseItem>
  </NCollapse>
</template>

<style scoped>
.tool-call-collapse { margin-top:10px; background:#fff; border:1px solid #e4e8ee; border-radius:10px; overflow:hidden; }
.tool-call-collapse :deep(.n-collapse-item__header) { padding:12px 16px; }
.tool-call-collapse :deep(.n-collapse-item:first-child > .n-collapse-item__header) { padding-top:12px; }
.tool-call-collapse :deep(.n-collapse-item__content-inner) { padding:0 16px 14px; }
.tool-heading { display:flex; gap:8px; align-items:center; min-width:0; }
.tool-badge { flex:none; max-width:45%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font:12px/1.5 "Cascadia Code",Consolas,monospace; color:#2563eb; background:rgba(37,99,235,.08); border:1px solid rgba(37,99,235,.25); border-radius:6px; padding:0 8px; }
.tool-name { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:13px; color:var(--text); }
.tool-heading :deep(.n-tag) { flex:none; }
.tool-tabs :deep(.n-tabs-nav) { padding:0; }
.tool-tabs :deep(.n-tabs-tab) { padding:5px 12px 7px; }
.tool-pane { padding:12px 0 4px; white-space:pre-wrap; overflow-wrap:anywhere; }
.tool-pane :deep(pre), .tool-pane :deep(code) { white-space:pre; }
.tool-pane--params :deep(.markdown-message) { font-size:13px; }
.tool-pane--output :deep(.markdown-message) { font-size:13px; }
</style>
