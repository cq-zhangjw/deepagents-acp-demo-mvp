<script setup lang="ts">
import { computed } from 'vue'
import { CodeSlashOutline, ChevronDownOutline } from '@vicons/ionicons5'
import { NCode, NCollapse, NCollapseItem, NIcon, NTabPane, NTag, NTabs } from 'naive-ui'
import { useI18n } from 'vue-i18n'

type ToolStatus = 'running' | 'completed' | 'failed' | 'cancelled' | 'waiting_permission'

interface PlanEntry { title: string; status: string }
interface AnalysisEntry { id: string; text: string }
interface ToolCallEntry { id: string; name: string; title?: string; status: ToolStatus; rawInput?: unknown; output?: unknown }

const props = defineProps<{
  process: { startedAt: number; completedAt?: number; plan: PlanEntry[]; analyses: AnalysisEntry[]; toolCalls: ToolCallEntry[] }
}>()

const { t } = useI18n()
const stepCount = computed(() => props.process.plan.length + props.process.analyses.length + props.process.toolCalls.length)
const duration = computed(() => Math.max(0, Math.round(((props.process.completedAt ?? Date.now()) - props.process.startedAt) / 1000)))

function jsonValue(value: unknown) {
  if (typeof value === 'string') return value
  try { return JSON.stringify(value ?? {}, null, 2) } catch { return String(value ?? '') }
}

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
  <NCollapse class="process-collapse">
    <NCollapseItem name="process">
      <template #header>
        <div class="process-heading">
          <NIcon :component="ChevronDownOutline" />
          <span>{{ t('viewProcess') }}</span>
          <small>{{ stepCount }} {{ t('steps') }} · {{ duration }} {{ t('seconds') }}</small>
        </div>
      </template>

      <div v-if="process.plan.length" class="plan-list">
        <div v-for="(step, index) in process.plan" :key="`${step.title}-${index}`" class="plan-item">
          <span class="plan-index">{{ index + 1 }}</span>
          <span>{{ step.title }}</span>
          <NTag size="small" :type="step.status === 'completed' ? 'success' : 'info'">{{ statusText(step.status) }}</NTag>
        </div>
      </div>

      <NCollapse v-if="process.analyses.length" class="detail-collapse">
        <NCollapseItem v-for="(analysis, index) in process.analyses" :key="analysis.id" :name="analysis.id">
          <template #header>{{ t('analysisStep') }} {{ index + 1 }}</template>
          <div class="analysis-text">{{ analysis.text }}</div>
        </NCollapseItem>
      </NCollapse>

      <NCollapse v-if="process.toolCalls.length" class="detail-collapse">
        <NCollapseItem v-for="tool in process.toolCalls" :key="tool.id" :name="tool.id">
          <template #header>
            <div class="tool-heading">
              <NIcon :component="CodeSlashOutline" />
              <span>{{ tool.title || tool.name }}</span>
              <NTag size="small" :type="tagType(tool.status)">{{ statusText(tool.status) }}</NTag>
            </div>
          </template>
          <NTabs type="line" size="small">
            <NTabPane name="input" :tab="t('parameters')"><NCode :code="jsonValue(tool.rawInput)" language="json" word-wrap /></NTabPane>
            <NTabPane name="output" :tab="t('output')"><NCode :code="jsonValue(tool.output)" language="json" word-wrap /></NTabPane>
          </NTabs>
        </NCollapseItem>
      </NCollapse>
    </NCollapseItem>
  </NCollapse>
</template>

<style scoped>
.process-collapse { margin-top:14px; background:#fff; border:1px solid #e4e8ee; border-radius:8px; } .process-heading,.tool-heading { display:flex; gap:7px; align-items:center; min-width:0; } .process-heading { color:#405166; font-size:13px; } .process-heading small { color:#6b7785; font-size:11px; } .plan-list { display:flex; flex-direction:column; gap:7px; padding:2px 0 10px; } .plan-item { display:flex; gap:8px; align-items:center; color:#405166; font-size:13px; } .plan-index { display:grid; place-items:center; width:19px; height:19px; color:#2563eb; background:#eaf1ff; border-radius:50%; font-size:11px; } .plan-item :deep(.n-tag) { margin-left:auto; } .detail-collapse { margin-top:8px; border:1px solid #e4e8ee; border-radius:6px; } .analysis-text { padding:8px 0; color:#405166; line-height:1.65; white-space:pre-wrap; } .tool-heading span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .tool-heading :deep(.n-tag) { margin-left:auto; }
</style>