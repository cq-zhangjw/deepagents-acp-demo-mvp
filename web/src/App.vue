<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { NConfigProvider, NMessageProvider, type GlobalThemeOverrides } from 'naive-ui'
import { useI18n } from 'vue-i18n'

const { locale } = useI18n()

// 统一使用中文字体栈显示中/日/英界面（中日文字形覆盖更好，英文也完全可读）
const fontFamily = '"PingFang SC", "Microsoft YaHei", "Noto Sans SC", system-ui, sans-serif'

const themeOverrides = computed<GlobalThemeOverrides>(() => ({
  common: {
    primaryColor: '#2563eb',
    primaryColorHover: '#1d4ed8',
    primaryColorPressed: '#1e40af',
    borderRadius: '8px',
    fontFamily
  }
}))

function applyBodyFont() {
  document.body.style.fontFamily = fontFamily
}

watch(locale, applyBodyFont)
onMounted(applyBodyFont)
</script>

<template>
  <NConfigProvider :theme-overrides="themeOverrides">
    <NMessageProvider>
      <RouterView />
    </NMessageProvider>
  </NConfigProvider>
</template>
