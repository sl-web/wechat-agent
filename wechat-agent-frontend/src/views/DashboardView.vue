<script setup>
import { ref, onMounted, onActivated, computed, onUnmounted } from 'vue'
import { api } from '../api.js'
import { toast } from '../toast.js'
import {
  attachPipeline,
  isBusy,
  onPipelineDone,
  pipelineState,
  refreshStatus,
  runStage,
} from '../pipeline.js'

const data = ref(null)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.get('/dashboard')
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

// ---- 流水线执行与进度 ----
// 状态全部交给全局单例(../pipeline.js):抓取/分析/生成动辄几分钟,
// 而切换标签会卸载本组件 —— 状态放这里就会"切走再回来什么都没在跑"。
// 本组件只负责把全局状态渲染出来,不再自己持有定时器。
const running = computed(() => pipelineState.running)
const progress = computed(() => pipelineState.progress)
const lastResult = computed(() => pipelineState.result)
const busy = computed(() => isBusy())
const showPanel = computed({
  get: () => pipelineState.panelOpen,
  set: (v) => { pipelineState.panelOpen = v },
})

// 兼容按钮签名:触发阶段走全局入口(内含"已有任务在执行"的拦截)
function triggerStage(stage, label) {
  return runStage(stage)
}

// 任务真正结束后刷新看板数据(订阅在 onUnmounted 时取消)
let offDone = null

// ---- 执行结果的人话总结 ----
const summary = computed(() => {
  const p = progress.value
  const r = lastResult.value
  if (!p || (!p.running && p.ok === null && !r)) return null
  if (p.running) return null
  const res = r || p.result || {}
  if (res.ok === false || p.ok === false) {
    return {
      ok: false,
      title: '执行未成功',
      error: res.error || p.error || '未知原因',
      hint: res.hint || p.hint || '',
    }
  }
  const parts = []
  if (res.crawled !== undefined) {
    parts.push(`抓到 ${res.crawled} 条 → 新入库 ${res.fetched} 条,${res.duplicated} 条已在库(重复)`)
    if (res.fetched === 0) {
      parts.push('RSS 源暂无新文章;同一批内容重复抓取不会增加「今日抓取」计数,这是 URL 去重的正常行为')
    }
  }
  if (res.scored !== undefined) parts.push(`评分落库 ${res.scored} 条`)
  if (res.clusters !== undefined) parts.push(`聚类 ${res.clusters} 个主题`)
  if (res.article_id) parts.push(`生成文章 #${res.article_id}`)
  if (res.backfilled !== undefined) parts.push(`回流 ${res.backfilled} 条`)
  if (res.cleaned !== undefined) parts.push(`清理 ${Object.values(res.cleaned || {}).reduce((a, b) => a + b, 0)} 条`)
  return { ok: true, title: '执行成功', error: '', hint: parts.join(';') }
})

const failedSources = computed(() => (lastResult.value && lastResult.value.source_failed) || [])

// ---- 竖排指标菜单 ----
const metricMap = [
  { key: 'fetch_today', label: '今日抓取', num: 'fetch_count', type: 'topic' },
  { key: 'analyzed_today', label: '今日分析', num: 'analyzed_today', type: 'topic' },
  { key: 'generated_today', label: '今日生成', num: 'generated_today', type: 'article' },
  { key: 'pending_review', label: '待处理稿件', num: 'pending_review', type: 'article' },
  { key: 'generation_failed', label: '生成失败/熔断', num: 'blacklist_or_fail', type: 'article' },
  { key: 'published', label: '累计已发布', num: 'published_total', type: 'article' },
  { key: 'backfilled', label: '回流数据条数', num: 'backfilled_total', type: 'article' },
]
const curKey = ref('fetch_today')
const curMetric = computed(() => metricMap.find((m) => m.key === curKey.value) || metricMap[0])
function pick(key) { curKey.value = key; loadMetric() }

// 明细
const detail = ref(null)
const detailLoading = ref(false)
async function loadMetric() {
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await api.get('/dashboard/metric/' + curKey.value)
  } catch (e) {
    toast('加载明细失败: ' + e.message, 'err')
  } finally {
    detailLoading.value = false
  }
}

const statusLabel = { generated: '已生成', pushed: '已推送', pending_review: '待审核', edited: '已编辑', approved: '已通过', rejected: '已驳回', published: '已发布', backfilled: '已回流', generation_failed: '生成失败' }
const sourceLabel = { wechat: '微信', douyin: '抖音', rss: 'RSS' }
function cellVal(row, col) {
  let v = row[col.key]
  if (col.badge === 'status') return statusLabel[v] || v
  if (col.key === 'source') return sourceLabel[v] || v
  return v
}
function statusCls(s) {
  const map = {
    generated: 'blue', pushed: 'amber', pending_review: 'amber', edited: 'blue',
    approved: 'green', rejected: 'red', published: 'green', backfilled: 'green', generation_failed: 'red',
  }
  return map[s] || 'gray'
}
function isLink(col) { return col.link === true }
function isDate(col) {
  return ['created_at', 'published_at', 'stat_date', 'analyzed_at', 'first_analyzed_at'].includes(col.key)
}

function fmt(n) { return n == null ? 0 : n }

function openInReview(row) {
  const aid = row && (row.article_id ?? row.id)
  if (!aid) return
  sessionStorage.setItem('articleBackTo', '#/dashboard')
  location.hash = '#/article/' + aid
}

// 任务链:环节状态图标
function chainIcon(node) {
  if (!node.last) return node.key === 'review' ? '·' : '·'
  return node.last.status === 'success' ? '✓' : '✕'
}
function chainCls(node) {
  if (!node.last) return 'gray'
  return node.last.status === 'success' ? 'green' : 'red'
}

onMounted(async () => {
  await load()
  await loadMetric()
  // 接管"可能已经在跑"的任务:从别的标签切回来也能看到进度
  await attachPipeline()
  offDone = onPipelineDone(async () => {
    await load()
    await loadMetric()
  })
})
// 注意:**不要**在这里停轮询 —— 轮询归全局所有,停了就看不到后台运行的任务了
onUnmounted(() => { if (offDone) offDone() })

// KeepAlive:切走再回来刷新数字(看板是只读的,刷新不会丢用户的东西);
// attachPipeline/onPipelineDone 只在 onMounted 挂一次,别在这里重复订阅。
let entered = false
onActivated(() => {
  if (entered) { load(); loadMetric() }
  entered = true
})
</script>

<template>
  <div class="page">
    <div class="page-title">
      运营看板
      <button class="btn" @click="load(); loadMetric(); refreshStatus()" :disabled="loading">{{ loading ? '加载中…' : '刷新' }}</button>
    </div>

    <div v-if="error" class="empty">加载失败: {{ error }} (请确认后端已启动, dev 端口 8000)</div>

    <div v-else-if="!data" class="empty">加载中…</div>

    <div v-else class="dash-layout">
      <!-- 左侧竖排指标菜单 -->
      <aside class="side-menu">
        <div class="pipeline-box">
          <button class="btn primary run-all" :disabled="busy" @click="triggerStage('all', '一键流水线')">
            {{ running === 'all' ? '一键执行中…' : '▶ 一键流水线' }}
          </button>
          <div class="meta" style="margin-top:6px;">抓取→分析→生成,完成后人工审核</div>
          <div class="row wrap" style="margin-top:8px;">
            <button v-for="b in [{stage:'fetch',label:'仅抓取'},{stage:'analyze',label:'仅分析'},{stage:'generate',label:'仅生成'}]"
              :key="b.stage" class="btn sm" :disabled="busy" @click="triggerStage(b.stage, b.label)">
              {{ b.label }}{{ running === b.stage ? '…' : '' }}
            </button>
          </div>
          <button v-if="progress && !progress.running && progress.ok !== null" class="btn sm"
            style="margin-top:8px; width:100%;" @click="showPanel = !showPanel">
            {{ showPanel ? '收起执行结果' : '查看上次执行结果' }}
          </button>
        </div>

        <div class="menu-title">数据指标</div>
        <div v-for="m in metricMap" :key="m.key" class="menu-item" :class="{ active: curKey === m.key }" @click="pick(m.key)">
          <span class="label">{{ m.label }}</span>
          <span class="num">{{ fmt(data[m.num]) }}</span>
        </div>

        <div class="meta" style="padding: 10px 4px;">提示:点击右侧条目可跳文章处理窗口;最近运行/效果/滞留见「运行监控」Tab。</div>
      </aside>

      <!-- 右侧详情区 -->
      <section class="detail-area">
        <!-- 执行进度面板 -->
        <div v-if="showPanel && progress" class="card progress-card" :class="{ failed: summary && !summary.ok }">
          <div class="row between">
            <h3 style="margin:0;">
              执行进度 · {{ progress.label || STEP_LABEL[progress.stage] || '流水线' }}
              <span v-if="progress.running" class="badge blue" style="margin-left:8px;">执行中 {{ progress.elapsed }}s</span>
              <span v-else-if="progress.ok === true" class="badge green" style="margin-left:8px;">成功 · 耗时 {{ progress.elapsed }}s</span>
              <span v-else-if="progress.ok === false" class="badge red" style="margin-left:8px;">失败 · 耗时 {{ progress.elapsed }}s</span>
            </h3>
            <button class="btn sm" @click="showPanel = false">收起</button>
          </div>

          <!-- 阶段步骤 -->
          <div class="steps">
            <template v-for="(s, i) in progress.steps" :key="s.stage">
              <div class="step" :class="s.status">
                <span class="dot">
                  <template v-if="s.status === 'success'">✓</template>
                  <template v-else-if="s.status === 'failed'">✕</template>
                  <template v-else-if="s.status === 'running'">⟳</template>
                  <template v-else>○</template>
                </span>
                <div class="step-body">
                  <div class="step-name">{{ s.label }}</div>
                  <div class="step-detail">{{ s.detail || (s.status === 'pending' ? '等待中' : '') }}</div>
                </div>
              </div>
              <div v-if="i < progress.steps.length - 1" class="arrow">→</div>
            </template>
          </div>

          <!-- 结果 / 失败原因 -->
          <div v-if="summary" class="result" :class="summary.ok ? 'ok' : 'bad'">
            <strong>{{ summary.title }}</strong>
            <div v-if="summary.error" class="err-line">{{ summary.error }}</div>
            <div v-if="summary.hint" :class="summary.ok ? 'note-line' : 'hint-line'">
              {{ summary.ok ? '明细:' : '建议:' }}{{ summary.hint }}
            </div>
          </div>

          <!-- 逐源抓取明细 -->
          <div v-if="lastResult && lastResult.sources && lastResult.sources.length" class="sources">
            <div class="menu-title" style="padding-left:0;">信息源明细</div>
            <div class="row wrap">
              <span v-for="s in lastResult.sources" :key="s.name" class="src-chip" :class="s.ok ? 'ok' : 'bad'">
                {{ s.name }}:{{ s.ok ? s.count + ' 条' : '失败' }}<span class="src-cost">({{ s.elapsed }}s)</span>
              </span>
            </div>
            <div v-for="f in failedSources" :key="f.name" class="err-line" style="margin-top:6px;">
              ✕ {{ f.name }}:{{ f.error }}
            </div>
          </div>

          <!-- 实时日志 -->
          <div v-if="progress.logs && progress.logs.length" class="logs">
            <div v-for="(l, i) in progress.logs.slice(-6)" :key="i" class="log-line">
              <span class="log-t">{{ l.t.slice(11) }}</span>
              <span class="badge" :class="l.status === 'success' ? 'green' : (l.status === 'failed' ? 'red' : 'gray')">{{ l.label }}</span>
              <span class="log-d">{{ l.detail }}</span>
            </div>
          </div>
        </div>

        <!-- 任务链进度 -->
        <div class="card chain-card">
          <div class="row between">
            <h3 style="margin:0;">今日任务链进度<span class="meta" style="margin-left:8px;">{{ data.date }}</span></h3>
            <span class="meta">机器采编排 · 人工审发</span>
          </div>
          <div class="chain">
            <template v-for="(c, i) in data.chain || []" :key="c.key">
              <div class="chain-node">
                <div class="chain-top">
                  <span class="badge" :class="chainCls(c)">{{ chainIcon(c) }}</span>
                  <strong>{{ c.label }}</strong>
                </div>
                <div class="chain-count">{{ c.count }} <span class="unit">{{ c.unit }}</span></div>
                <div class="chain-last">
                  <template v-if="c.last">
                    {{ c.last.status === 'success' ? '最近成功' : '最近失败' }} {{ (c.last.finished_at || c.last.started_at || '').slice(5, 16) }}
                  </template>
                  <template v-else>待人工处理</template>
                </div>
              </div>
              <div v-if="i < (data.chain || []).length - 1" class="arrow">→</div>
            </template>
          </div>
        </div>

        <!-- 指标明细 -->
        <div class="card">
          <div class="row between">
            <h3 style="margin:0;">{{ curMetric.label }}<span v-if="detail" class="meta" style="margin-left:8px;">共 {{ detail.total }} 条</span></h3>
            <span class="meta">{{ detailLoading ? '加载中…' : '' }}</span>
          </div>
          <div v-if="detail && detail.note" class="filter-note">{{ detail.note }}</div>
          <div v-if="detail && detail.title && detail.title !== curMetric.label" class="meta" style="margin-top:2px;">{{ detail.title }}</div>
          <div v-if="detailLoading" class="empty">加载中…</div>
          <div v-else-if="!detail || !detail.rows.length" class="empty">暂无数据</div>
          <div v-else style="overflow:auto; max-height:52vh; margin-top:8px;">
            <table>
              <thead>
                <tr>
                  <th v-for="c in detail.cols" :key="c.key">{{ c.label }}</th>
                  <th v-if="curMetric.type === 'article'">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, i) in detail.rows" :key="i">
                  <td v-for="c in detail.cols" :key="c.key">
                    <a v-if="isLink(c) && row[c.key]" :href="row[c.key]" target="_blank">{{ row[c.key] }}</a>
                    <span v-else-if="c.badge === 'status'" class="badge" :class="statusCls(row[c.key])">{{ statusLabel[row[c.key]] || row[c.key] }}</span>
                    <span v-else-if="c.key === 'source'" class="badge blue">{{ sourceLabel[row[c.key]] || row[c.key] }}</span>
                    <span v-else-if="isDate(c)">{{ row[c.key] || '—' }}</span>
                    <span v-else>{{ cellVal(row, c) ?? '—' }}</span>
                  </td>
                  <td v-if="curMetric.type === 'article'"><button class="btn sm primary" @click="openInReview(row)">查看全文/处理</button></td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.dash-layout { display: flex; gap: 16px; align-items: flex-start; }
.side-menu { width: 220px; flex-shrink: 0; background: #fff; border: 1px solid var(--border); border-radius: var(--radius); padding: 12px; box-shadow: var(--shadow); position: sticky; top: 70px; }
.pipeline-box { border-bottom: 1px dashed var(--border); padding-bottom: 12px; margin-bottom: 8px; }
.run-all { width: 100%; font-size: 14px; padding: 9px 10px; }
.menu-title { font-size: 12px; color: var(--text-sub); padding: 8px 6px 4px; }
.menu-item { display: flex; justify-content: space-between; align-items: center; padding: 10px 10px; border-radius: 8px; cursor: pointer; margin-bottom: 2px; }
.menu-item:hover { background: var(--primary-light); }
.menu-item.active { background: var(--primary); color: #fff; }
.menu-item .num { font-weight: 600; font-size: 16px; }
.menu-item.active .num { color: #fff; }
.detail-area { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 14px; }

/* 进度面板 */
.progress-card { border-left: 4px solid var(--primary); }
.progress-card.failed { border-left-color: #d9534f; }
.steps { display: flex; align-items: stretch; gap: 6px; flex-wrap: wrap; margin-top: 12px; }
.step { display: flex; gap: 8px; align-items: flex-start; padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; background: #fafaf8; min-width: 150px; }
.step .dot { font-weight: 700; font-size: 14px; color: var(--text-sub); }
.step.running { border-color: var(--primary); background: var(--primary-light); }
.step.running .dot { color: var(--primary); }
.step.success { border-color: #2e7d32; background: #eef7ee; }
.step.success .dot { color: #2e7d32; }
.step.failed { border-color: #d9534f; background: #fdeeee; }
.step.failed .dot { color: #d9534f; }
.step-name { font-size: 13px; font-weight: 600; }
.step-detail { font-size: 11px; color: var(--text-sub); margin-top: 2px; max-width: 230px; }
.arrow { align-self: center; color: var(--text-sub); }
.result { margin-top: 12px; padding: 10px 12px; border-radius: 8px; font-size: 13px; }
.result.ok { background: #eef7ee; border: 1px solid #2e7d32; }
.result.bad { background: #fdeeee; border: 1px solid #d9534f; }
.err-line { color: #b3261e; font-size: 12px; margin-top: 4px; word-break: break-all; }
.hint-line { color: #7a5c00; font-size: 12px; margin-top: 4px; }
.note-line { color: #1b5e20; font-size: 12px; margin-top: 4px; }
.filter-note {
  margin-top: 6px; font-size: 12px; color: var(--primary);
  background: var(--primary-light); border-radius: 6px; padding: 5px 10px; display: inline-block;
}
.sources { margin-top: 12px; }
.src-chip { display: inline-block; font-size: 11px; padding: 3px 8px; border-radius: 12px; margin: 2px 6px 2px 0; border: 1px solid var(--border); }
.src-chip.ok { background: #eef7ee; border-color: #2e7d32; color: #1b5e20; }
.src-chip.bad { background: #fdeeee; border-color: #d9534f; color: #b3261e; }
.src-cost { color: var(--text-sub); margin-left: 3px; }
.logs { margin-top: 12px; border-top: 1px dashed var(--border); padding-top: 8px; }
.log-line { display: flex; gap: 8px; align-items: center; font-size: 12px; padding: 2px 0; }
.log-t { color: var(--text-sub); font-variant-numeric: tabular-nums; }
.log-d { color: var(--text-sub); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 任务链 */
.chain { display: flex; align-items: stretch; gap: 6px; flex-wrap: wrap; margin-top: 12px; }
.chain-node { flex: 1 1 150px; border: 1px solid var(--border); border-radius: 8px; padding: 10px; background: #fcfbf8; }
.chain-top { display: flex; align-items: center; gap: 6px; font-size: 13px; }
.chain-count { font-size: 20px; font-weight: 700; margin: 6px 0 2px; }
.chain-count .unit { font-size: 12px; font-weight: 400; color: var(--text-sub); }
.chain-last { font-size: 11px; color: var(--text-sub); }

@media (max-width: 860px) { .dash-layout { flex-direction: column; } .side-menu { width: 100%; position: static; } }
</style>
