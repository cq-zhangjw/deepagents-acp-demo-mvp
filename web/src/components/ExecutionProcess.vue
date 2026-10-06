<script setup lang="ts">
import { computed } from 'vue'
import { ChevronDownOutline } from '@vicons/ionicons5'
import { NCollapse, NCollapseItem, NIcon, NTag } from 'naive-ui'
import { useI18n } from 'vue-i18n'

interface PlanEntry { title: string; status: string }
interface ToolCallEntry { id: string; name: string; title?: string; status: string; rawInput?: unknown; output?: unknown }

const props = defineProps<{
  process: { startedAt: number; completedAt?: number; plan: PlanEntry[]; toolCalls: ToolCallEntry[] }
}>()

const { t } = useI18n()
const stepCount = computed(() => props.process.plan.length + props.process.toolCalls.length)
const duration = computed(() => Math.max(0, Math.round(((props.process.completedAt ?? Date.now()) - props.process.startedAt) / 1000)))

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
    </NCollapseItem>
  </NCollapse>
</template>

<style scoped>
.process-collapse { margin-top:14px; background:#fff; border:1px solid #e4e8ee; border-radius:10px; overflow:hidden; }
.process-collapse :deep(.n-collapse-item__header) { padding:12px 16px; }
.process-collapse :deep(.n-collapse-item:first-child > .n-collapse-item__header) { padding-top:12px; }
.process-collapse :deep(.n-collapse-item__content-inner) { padding:0 16px 12px; }
.process-heading { display:flex; gap:8px; align-items:center; min-width:0; color:#405166; font-size:13px; }
.process-heading small { color:#6b7785; font-size:11px; }
.plan-list { display:flex; flex-direction:column; gap:8px; padding:6px 0 8px; }
.plan-item { display:flex; gap:8px; align-items:center; color:#405166; font-size:13px; }
.plan-index { display:grid; place-items:center; width:19px; height:19px; color:#2563eb; background:#eaf1ff; border-radius:50%; font-size:11px; }
.plan-item :deep(.n-tag) { margin-left:auto; }
</style>
