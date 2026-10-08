import { createI18n } from 'vue-i18n'
import zh from './locales/zh'
import ja from './locales/ja'
import en from './locales/en'

export type SupportedLocale = 'zh' | 'ja' | 'en'

const storageKey = 'acp-ui-locale'
const messages = { zh, ja, en }

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
