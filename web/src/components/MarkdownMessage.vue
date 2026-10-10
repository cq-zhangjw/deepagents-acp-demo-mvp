<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js'
import 'highlight.js/styles/github.css'
import { useMessage } from 'naive-ui'
import { useI18n } from 'vue-i18n'

const props = defineProps<{ content: string; variant?: 'assistant' | 'user' }>()
const root = ref<HTMLElement | null>(null)
const message = useMessage()
const { t } = useI18n()
let mermaidSequence = 0

const rootClass = computed(() => ({
  'markdown-message': true,
  'markdown-message--user': props.variant === 'user'
}))

// 预加载 mermaid 并只初始化一次，避免每次渲染重新 import/initialize 的开销
let mermaidInstance: any | null = null
async function getMermaid() {
  if (!mermaidInstance) {
    const mermaid = (await import('mermaid')).default
    mermaid.initialize({
      startOnLoad: false,
      theme: 'default',
      securityLevel: 'strict',
      flowchart: { useMaxWidth: true, htmlLabels: false },
      sequence: { useMaxWidth: true }
    })
    mermaidInstance = mermaid
  }
  return mermaidInstance
}

// 渲染结果缓存：流式期间 DOM 会被反复重建，相同源码的图直接复用 SVG，避免重复渲染
const mermaidCache = new Map<string, string>()

// 开启 html 渲染（预览文本中的 html 元素），输出经 DOMPurify 白名单消毒防 XSS
const md = new MarkdownIt({ html: true, linkify: true })
const defaultLinkRender = md.renderer.rules.link_open ?? ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return defaultLinkRender(tokens, idx, options, env, self)
}
const safeHtml = computed(() => DOMPurify.sanitize(md.render(props.content)))

function languageOf(block: HTMLElement) {
  return Array.from(block.classList)
    .find((className) => className.startsWith('language-'))
    ?.replace('language-', '') ?? 'text'
}

async function copyText(text: string, button: HTMLButtonElement) {
  try {
    await navigator.clipboard.writeText(text)
    button.textContent = 'Copied'
    message.success(t('copied'))
  } catch {
    const textarea = document.createElement('textarea')
    textarea.value = text
    document.body.appendChild(textarea)
    textarea.select()
    document.execCommand('copy')
    textarea.remove()
    button.textContent = 'Copied'
    message.success(t('copied'))
  }
  window.setTimeout(() => { button.textContent = 'Copy' }, 1400)
}

function createCodeToolbar(source: string, language: string) {
  const toolbar = document.createElement('div')
  toolbar.className = 'code-toolbar'
  const label = document.createElement('span')
  label.textContent = language
  const copy = document.createElement('button')
  copy.type = 'button'
  copy.textContent = 'Copy'
  copy.addEventListener('click', () => copyText(source, copy))
  toolbar.append(label, copy)
  return toolbar
}

async function replaceMermaid(pre: HTMLElement, source: string) {
  const wrapper = document.createElement('section')
  wrapper.className = 'mermaid-block'
  const tabs = document.createElement('div')
  tabs.className = 'mermaid-tabs'
  const previewTab = document.createElement('button')
  previewTab.type = 'button'
  previewTab.textContent = 'Preview'
  previewTab.className = 'active'
  const sourceTab = document.createElement('button')
  sourceTab.type = 'button'
  sourceTab.textContent = 'Code'
  tabs.append(previewTab, sourceTab)

  const preview = document.createElement('div')
  preview.className = 'mermaid-preview'
  preview.textContent = 'Rendering diagram...'
  const sourcePanel = document.createElement('div')
  sourcePanel.className = 'mermaid-source'
  sourcePanel.hidden = true
  const sourcePre = document.createElement('pre')
  const sourceCode = document.createElement('code')
  sourceCode.className = 'language-mermaid'
  sourceCode.textContent = source
  sourcePre.appendChild(sourceCode)
  sourcePanel.append(createCodeToolbar(source, 'mermaid'), sourcePre)
  wrapper.append(tabs, preview, sourcePanel)
  pre.replaceWith(wrapper)

  previewTab.addEventListener('click', () => {
    previewTab.classList.add('active')
    sourceTab.classList.remove('active')
    preview.hidden = false
    sourcePanel.hidden = true
  })
  sourceTab.addEventListener('click', () => {
    sourceTab.classList.add('active')
    previewTab.classList.remove('active')
    preview.hidden = true
    sourcePanel.hidden = false
  })

  try {
    let svg = mermaidCache.get(source)
    if (!svg) {
      const mermaid = await getMermaid()
      const { svg: rendered, bindFunctions } = await mermaid.render(`mermaid-${Date.now()}-${mermaidSequence++}`, source)
      svg = rendered
      mermaidCache.set(source, svg)
      bindFunctions?.(preview)
    }
    preview.innerHTML = svg
  } catch {
    preview.textContent = 'Unable to render this Mermaid diagram.'
    sourceTab.click()
  }
}

async function enhanceMarkdown() {
  await nextTick()
  const element = root.value
  if (!element) return
  const blocks = Array.from(element.querySelectorAll<HTMLElement>('pre code'))
  for (const block of blocks) {
    if (block.dataset.enhanced) continue
    block.dataset.enhanced = 'true'
    const pre = block.closest('pre')
    if (!pre) continue
    const source = block.textContent ?? ''
    const language = languageOf(block)
    if (language === 'mermaid') {
      void replaceMermaid(pre, source)
      continue
    }
    hljs.highlightElement(block)
    pre.prepend(createCodeToolbar(source, language))
  }
}

onMounted(enhanceMarkdown)
watch(safeHtml, enhanceMarkdown, { flush: 'post' })
</script>

<template>
  <div ref="root" :class="rootClass" v-html="safeHtml"></div>
</template>

<style scoped>
.markdown-message { color: #1d2733; font-size: 15px; line-height: 1.75; overflow-wrap: anywhere; }
.markdown-message :deep(p) { margin: 0 0 12px; } .markdown-message :deep(p:last-child) { margin-bottom: 0; }
.markdown-message :deep(h1),.markdown-message :deep(h2),.markdown-message :deep(h3) { margin: 20px 0 10px; line-height: 1.35; } .markdown-message :deep(h1) { font-size: 20px; } .markdown-message :deep(h2) { font-size: 17px; } .markdown-message :deep(h3) { font-size: 15px; }
.markdown-message :deep(ul),.markdown-message :deep(ol) { padding-left: 22px; margin: 8px 0 12px; } .markdown-message :deep(table) { display:block; max-width:100%; overflow:auto; border-collapse:collapse; margin:12px 0; } .markdown-message :deep(th),.markdown-message :deep(td) { padding:7px 10px; border:1px solid #e4e8ee; text-align:left; } .markdown-message :deep(th) { background:#f1f4f8; }
.markdown-message :deep(pre) { position:relative; margin:12px 0; padding:38px 12px 12px; overflow:auto; background:#f1f4f8; border:1px solid #e4e8ee; border-radius:7px; } .markdown-message :deep(pre code) { padding:0; color:#1d2733; background:transparent; font-family:"Cascadia Code",Consolas,monospace; font-size:13px; line-height:1.55; }
.markdown-message :deep(code:not(pre code)) { padding:2px 6px; color:#174cb9; background:#e9eef6; border:1px solid #dbe4f0; border-radius:5px; font-family:"Cascadia Code",Consolas,monospace; font-size:0.9em; }
.markdown-message--user :deep(code:not(pre code)) { color:#1d4ed8; background:#fff; border-color:#b9c9e8; box-shadow:0 1px 2px rgba(29,39,51,.08); }
.markdown-message :deep(.code-toolbar) { position:absolute; top:0; right:0; left:0; display:flex; justify-content:space-between; align-items:center; height:30px; padding:0 9px; color:#6b7785; background:#e9edf2; border-bottom:1px solid #dce2ea; font:11px "Cascadia Code",Consolas,monospace; } .markdown-message :deep(.code-toolbar button) { padding:2px 7px; color:#405166; background:#fff; border:1px solid #d3dae4; border-radius:4px; cursor:pointer; font:inherit; }
.markdown-message :deep(.mermaid-block) { margin:12px 0; overflow:hidden; background:#fff; border:1px solid #e4e8ee; border-radius:7px; } .markdown-message :deep(.mermaid-tabs) { display:flex; gap:2px; padding:6px 7px; background:#f1f4f8; border-bottom:1px solid #e4e8ee; } .markdown-message :deep(.mermaid-tabs button) { padding:4px 9px; color:#6b7785; background:transparent; border:0; border-radius:4px; cursor:pointer; font-size:12px; } .markdown-message :deep(.mermaid-tabs button.active) { color:#174cb9; background:#fff; box-shadow:0 1px 2px rgba(29,39,51,.08); } .markdown-message :deep(.mermaid-preview) { min-height:120px; padding:18px; overflow:auto; text-align:center; } .markdown-message :deep(.mermaid-preview svg) { max-width:100%; height:auto; } .markdown-message :deep(.mermaid-source) { position:relative; } .markdown-message :deep(.mermaid-source pre) { margin:0; border:0; border-radius:0; }
</style>