<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { NConfigProvider, NMessageProvider, type GlobalThemeOverrides } from 'naive-ui'
import { useI18n } from 'vue-i18n'

const { locale } = useI18n()

// 根据界面语言选择更适合的字体栈
const fontFamilyByLocale = computed(() => {
  switch (locale.value) {
    case 'ja':
      return '"Hiragino Kaku Gothic ProN", "Yu Gothic", "Meiryo", "Noto Sans JP", system-ui, sans-serif'
    case 'en':
      return '"Inter", "Segoe UI", "Roboto", system-ui, sans-serif'
    default:
      return '"PingFang SC", "Microsoft YaHei", "Noto Sans SC", system-ui, sans-serif'
  }
})

const themeOverrides = computed<GlobalThemeOverrides>(() => ({
  common: {
    primaryColor: '#2563eb',
    primaryColorHover: '#1d4ed8',
    primaryColorPressed: '#1e40af',
    borderRadius: '8px',
    fontFamily: fontFamilyByLocale.value
  }
}))

function applyBodyFont() {
  document.body.style.fontFamily = fontFamilyByLocale.value
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
