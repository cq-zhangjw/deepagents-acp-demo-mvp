<template>
  <NModal
    :show="show"
    preset="card"
    :closable="true"
    :mask-closable="true"
    class="kb-manage-modal"
    :style="{ width: '85vw', maxWidth: '1600px' }"
    :title="title"
    @update:show="onUpdateShow"
    @after-leave="loaded = false"
  >
    <div class="kb-manage-body">
      <div v-if="!loaded" class="kb-manage-loading">
        <NSpin size="small" />
        <span>{{ loadingText }}</span>
      </div>
      <iframe
        v-if="src"
        :src="src"
        class="kb-manage-frame"
        :title="title"
        @load="loaded = true"
      />
    </div>
  </NModal>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { NModal, NSpin } from 'naive-ui'

defineProps<{ show: boolean; src: string; title?: string; loadingText?: string }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const loaded = ref(false)

function onUpdateShow(show: boolean) {
  if (!show) emit('close')
}
</script>

<style scoped>
/* Keep the modal visually distinct as a dialog: solid border + shadow so the
   background chat page stays clearly visible around the iframe. */
.kb-manage-modal {
  border: 1px solid rgba(127, 127, 127, 0.25);
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.28);
}

.kb-manage-body {
  position: relative;
  height: calc(85vh - 110px);
  min-height: 380px;
  box-sizing: border-box;
}

.kb-manage-frame {
  width: 100%;
  height: 100%;
  border: 0;
  border-radius: 6px;
  background: #fff;
  display: block;
}

.kb-manage-loading {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: #888;
  font-size: 13px;
}
</style>
