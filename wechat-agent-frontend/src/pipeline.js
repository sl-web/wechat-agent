// 流水线任务状态(全局单例)
//
// 为什么必须放在组件外面:
// 抓取/分析/生成动辄几分钟,而切换标签会**卸载视图组件**。状态与轮询定时器一旦写在
// 组件里,切走再切回来就变成"什么都没在跑"(任务其实还在后端跑着),用户只能干等。
// 这里用模块级 reactive 对象 + 模块级定时器,让状态与轮询都脱离组件生命周期 ——
// 切标签、甚至刷新页面后重新挂载,都能接着看到正在跑的任务。
//
// 唯一真源是后端 `/api/pipeline/status`;本模块只做"前端怎么展示与轮询"。
import { reactive } from 'vue'

import { api } from './api.js'

export const STEP_LABEL = {
  fetch: '抓取热点', analyze: '分析归纳', generate: '生成文章',
  push: '推送草稿', backfill: '效果回流', clean: '知识库清理', all: '一键流水线',
}
// 一键流水线 = 依次跑这几个阶段
export const STAGE_CHAIN = { all: ['fetch', 'analyze', 'generate'] }

export const pipelineState = reactive({
  running: '',        // 本前端发起的阶段('' = 本端没发起)
  progress: null,     // /pipeline/status 快照
  result: null,       // 最近一次 POST 的返回(比快照多带细节,如 source_failed)
  panelOpen: false,
  netError: '',       // 仅网络层错误(请求没发出去/连不上)
})

let timer = null
let seenRunning = false        // 防止"刚触发、后端还没 begin"被误判成已完成
let notifiedKey = ''
const listeners = new Set()

/** 任务真正结束时通知一次(视图据此刷新列表)。返回取消订阅函数。 */
export function onPipelineDone(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function notify() {
  const p = pipelineState.progress || {}
  const key = `${p.run_id || ''}|${p.finished_at || ''}|${p.ok}`
  if (key === notifiedKey) return       // 去重:轮询与 finally 可能都会走到这里
  notifiedKey = key
  for (const fn of listeners) {
    try { fn(p) } catch (_) { /* 单个订阅者出错不影响别人 */ }
  }
}

/** 后端快照到达前先渲染骨架,让用户马上有反馈 */
export function skeleton(stage) {
  const chain = STAGE_CHAIN[stage] || [stage]
  return {
    running: true, stage, label: STEP_LABEL[stage] || stage, elapsed: 0,
    steps: chain.map((s) => ({ stage: s, label: STEP_LABEL[s] || s, status: 'pending', detail: '' })),
    logs: [], ok: null, error: null, hint: null, result: null,
  }
}

/** 是否有任务在跑(本端发起的,或后端正在跑的) */
export function isBusy() {
  const p = pipelineState.progress
  return !!pipelineState.running || !!(p && p.running)
}

export async function refreshStatus() {
  try {
    pipelineState.progress = await api.get('/pipeline/status')
  } catch (_) { /* 轮询失败不打扰用户 */ }
  return pipelineState.progress
}

function tick() {
  refreshStatus().then(() => {
    const p = pipelineState.progress
    if (!p) return
    if (p.running) { seenRunning = true; return }
    if (!seenRunning) return            // 还没真正开始过,别急着判完成
    stopPolling()
    pipelineState.running = ''
    notify()
  })
}

export function startPolling() {
  if (timer) return
  timer = setInterval(tick, 1000)
}

export function stopPolling() {
  if (timer) { clearInterval(timer); timer = null }
}

/**
 * 视图挂载时调用:接管"已经在跑"的任务。
 * 这样从别的标签切回来(组件重新挂载)时,进度条与轮询都还在。
 */
export async function attachPipeline() {
  await refreshStatus()
  const p = pipelineState.progress
  if (p && p.running) {
    seenRunning = true
    pipelineState.panelOpen = true
    startPolling()
  }
}

/**
 * 触发一个阶段 —— **所有**发起入口都走这里,保证同一时刻只有一个任务、
 * 且状态与会话无关地保存在全局。
 */
export async function runStage(stage, { body = undefined } = {}) {
  if (isBusy()) {
    return { ok: false, error: '已有任务正在执行,请等它结束再发起新的任务' }
  }
  seenRunning = false
  pipelineState.running = stage
  pipelineState.netError = ''
  pipelineState.result = null
  pipelineState.panelOpen = true
  pipelineState.progress = skeleton(stage)
  startPolling()
  try {
    // 不设前端超时:执行期间由进度面板展示进展(真实 LLM 生成可能超过 2 分钟)
    const r = await api.post('/pipeline/' + stage, body, 0)
    pipelineState.result = r
    return r
  } catch (e) {
    pipelineState.netError = e.message
    pipelineState.result = {
      ok: false, error: e.message,
      hint: '请求本身失败(网络中断或后端未启动),可稍后重试。',
    }
    return pipelineState.result
  } finally {
    pipelineState.running = ''
    await refreshStatus()
    const p = pipelineState.progress
    if (!p || !p.running) { stopPolling(); notify() }
  }
}
