<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api.js'
import { toast } from '../toast.js'
import RangePicker from '../components/RangePicker.vue'
import {
  attachPipeline,
  isBusy,
  onPipelineDone,
  pipelineState,
  runStage,
} from '../pipeline.js'

const cfg = ref({})              // 全量配置(取分析参数)
const report = ref(null)         // 当前时间范围下的分析报告(实时聚类)
const reportMeta = ref('')
const range = ref('today')       // 时间范围:默认当天(信息每天累积,不筛选会越来越长)
const rangeHint = ref('')
const loading = ref(false)
const running = ref(false)

// 分析参数表单(与 config key 对应,加载后预填)
const normMethod = ref('percentile')
const decayHours = ref(24)
const keywordsCsv = ref('')
const relevanceEnabled = ref(true)
const sourceWeights = ref({ wechat: 0.6, douyin: 0.4, rss: 0.5 })
const topN = ref(20)
const dirty = ref(false)

// 传播画像:从账号自己的转发数据学出来的「什么标题会被转」(见后端 analyzer/growth.py)
const profile = ref(null)
const profBusy = ref(false)

function virBadge(v) {
  const x = v === null || v === undefined ? 0.5 : v
  if (x >= 0.6) return { cls: 'green', text: '易传播' }
  if (x <= 0.4) return { cls: 'gray', text: '难传播' }
  return { cls: 'gray', text: '中性' }
}
function liftCls(lift) { return lift > 1.05 ? 'green' : 'gray' }

async function loadProfile() {
  try {
    const r = await api.get('/analyze/growth-profile')
    profile.value = (r && r.profile) || null
  } catch (_) { /* 画像缺失不影响主流程 */ }
}

async function rebuildProfile() {
  profBusy.value = true
  toast('正在从公众号发表记录里学习转发规律…')
  try {
    const r = await api.post('/analyze/growth-profile', {}, 120000)
    profile.value = (r && r.profile) || null
    toast(`画像已重建:基于 ${profile.value?.samples || 0} 篇真实发表记录`)
    // 画像变了,热度排序也会变 —— 顺手刷新报告
    await loadReport()
  } catch (e) {
    toast('重建画像失败: ' + e.message, 'err')
  } finally {
    profBusy.value = false
  }
}

async function loadReport() {
  // 只刷新报告,不动下面的参数表单(切时间范围时不该清掉用户没保存的改动)
  try {
    const r = await api.get(`/analyze/report?range=${range.value}`)
    report.value = (r && r.report) || null
    rangeHint.value = r && r.live
      ? `按「${r.range_label || ''}」实时聚类 · 素材 ${r.source_count || 0} 条`
      : ''
    reportMeta.value = (r && r.empty_reason) || ''
  } catch (e) {
    toast('加载分析报告失败: ' + e.message, 'err')
  }
}

async function load() {
  loading.value = true
  try {
    cfg.value = await api.get('/config')
    const c = cfg.value
    normMethod.value = c.normalize_method || 'percentile'
    decayHours.value = c.decay_half_life_hours ?? 24
    keywordsCsv.value = (c.keywords || []).join(', ')
    relevanceEnabled.value = c.relevance_enabled !== false
    sourceWeights.value = { ...(c.source_weight || { wechat: 0.6, douyin: 0.4, rss: 0.5 }) }
    topN.value = c.top_n ?? 20
    await Promise.all([loadReport(), loadProfile()])
  } catch (e) {
    toast('加载失败: ' + e.message, 'err')
  } finally {
    loading.value = false
  }
}

async function saveParams() {
  try {
    await api.post('/config/normalize_method', { value: normMethod.value })
    await api.post('/config/decay_half_life_hours', { value: Number(decayHours.value) || 24 })
    await api.post('/config/keywords', { value: keywordsCsv.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean) })
    await api.post('/config/relevance_enabled', { value: relevanceEnabled.value })
    await api.post('/config/source_weight', { value: sourceWeights.value })
    await api.post('/config/top_n', { value: Number(topN.value) || 20 })
    toast('分析参数已保存')
    dirty.value = false
  } catch (e) { toast('保存失败: ' + e.message, 'err') }
}

function markDirty() { dirty.value = true }

async function rerunAnalyze() {
  running.value = true
  try {
    toast('[分析归纳] 已触发,请稍候…')
    const r = await api.post('/pipeline/analyze', undefined, 60000)
    if (r.ok) {
      toast(`分析完成:${r.clusters} 主题 · Top1:${r.top_topic?.topic || '无'} · 评分落库 ${r.scored} 条`)
      await load()
    } else {
      toast('分析未成功: ' + (r.reason || ''), 'err')
    }
  } catch (e) {
    toast('分析触发失败: ' + e.message, 'err')
  } finally {
    running.value = false
  }
}

// 选中主题 → 展示摘要与成员
const selected = ref(null)
function selectTopic(t) { selected.value = t }

// ---------------- 人工选材 → 生成文章 ----------------
// 素材唯一键用 url:它既是热点的天然唯一键,老报告里的成员条目也可能没有 id。
const picked = ref([])          // [{key, id, url, title, source, topic}]
// 生成状态来自全局单例(../pipeline.js),不放在组件里 ——
// 否则切标签(组件卸载)就看不到正在跑的任务了。
const genRunning = computed(() => isBusy())          // 有任何任务在跑就不允许重复发起
const genActive = computed(() => {                    // 确实是"生成"在跑(用于按钮文案)
  const p = pipelineState.progress
  return !!(p && p.running && p.stage === 'generate')
})
// 只把生成阶段的快照交给进度面板,避免看板触发的抓取/分析进度混进这里
const genState = computed(() => {
  const p = pipelineState.progress
  return p && p.stage === 'generate' ? p : null
})
let offDone = null

function pickKey(m) { return (m && (m.url || ('id:' + m.id))) || '' }
function isPicked(m) { return picked.value.some((p) => p.key === pickKey(m)) }
function togglePick(m) {
  const key = pickKey(m)
  const i = picked.value.findIndex((p) => p.key === key)
  if (i >= 0) { picked.value.splice(i, 1); return }
  picked.value.push({
    key,
    id: m.id,
    url: m.url || '',
    title: m.title || '',
    source: m.source || '',
    topic: (selected.value && selected.value.topic) || '',
  })
}
const allPickedInTopic = computed(() => {
  const s = selected.value
  if (!s || !s.members || !s.members.length) return false
  return s.members.every((m) => isPicked(m))
})
function selectAllInTopic() {
  const s = selected.value
  if (!s || !s.members) return
  if (allPickedInTopic.value) {          // 已全选 → 取消本主题
    const keys = new Set(s.members.map(pickKey))
    picked.value = picked.value.filter((p) => !keys.has(p.key))
    return
  }
  for (const m of s.members) if (!isPicked(m)) togglePick(m)
}
function removePicked(key) {
  const i = picked.value.findIndex((p) => p.key === key)
  if (i >= 0) picked.value.splice(i, 1)
}
function clearPicked() { picked.value = [] }
function setMain(key) {                  // 把某条提为主选题(必须排第一)
  const i = picked.value.findIndex((p) => p.key === key)
  if (i > 0) { const [x] = picked.value.splice(i, 1); picked.value.unshift(x) }
}
const sameTopicPicked = computed(
  () => picked.value.length > 0 && picked.value.every((p) => p.topic === picked.value[0].topic)
)

// ---------------- 废弃素材(软删除,不物理删数据) ----------------
// 「选材勾选」与「废弃勾选」是两件事:直接用选材的勾选去删,会把"我要用这条"误读成"我要删这条"。
// 所以单独开一个「批量废弃模式」—— 同一个复选框在两种模式下含义不同,并有明显的模式提示。
const manageMode = ref(false)
const discardList = ref([])       // [{key, id, url, title}]
const discarding = ref(false)

function enterManage() { manageMode.value = true; discardList.value = [] }
function exitManage() { manageMode.value = false; discardList.value = [] }
function inDiscard(m) { return discardList.value.some((x) => x.key === pickKey(m)) }
function toggleDiscard(m) {
  const key = pickKey(m)
  const i = discardList.value.findIndex((x) => x.key === key)
  if (i >= 0) { discardList.value.splice(i, 1); return }
  discardList.value.push({ key, id: m.id, url: m.url || '', title: m.title || '' })
}
const allDiscardInTopic = computed(() => {
  const members = (selected.value && selected.value.members) || []
  return members.length > 0 && discardList.value.length === members.length
})
function selectAllForDiscard() {
  const members = (selected.value && selected.value.members) || []
  discardList.value = allDiscardInTopic.value ? []
    : members.map((m) => ({ key: pickKey(m), id: m.id, url: m.url || '', title: m.title || '' }))
}

/** 从本地报告视图摘掉已废弃的条目 */
function removeLocal(done) {
  const urls = new Set(done.map((d) => d.url).filter(Boolean))
  const ids = new Set(done.map((d) => String(d.id)))
  const topics = (report.value && report.value.topics) || []
  for (const t of topics) {
    if (!t.members) continue
    t.members = t.members.filter(
      (m) => !(m.url && urls.has(m.url)) && !(m.id && ids.has(String(m.id)))
    )
  }
}

async function discardMembers(items) {
  const urls = items.filter((x) => x.url).map((x) => x.url)
  const ids = items.filter((x) => !x.url && x.id).map((x) => x.id)
  if (!urls.length && !ids.length) { toast('这些条目没有链接或 id,无法定位到热点', 'err'); return }
  discarding.value = true
  try {
    const r = await api.post('/hot/bulk-delete', { urls, ids, reason: '分析报告人工废弃' })
    const skipped = r.skipped || []
    if (r.done_count) {
      // 报告是**上次分析的快照**,服务端不会因为软删就少人 —— 本地摘掉,
      // 否则用户会以为"点了没用"。重新分析后这些素材自然不再进聚类。
      removeLocal(r.done)
      const urls = new Set(r.done.map((d) => d.url))
      const ids = new Set(r.done.map((d) => String(d.id)))
      picked.value = picked.value.filter(
        (p) => !(p.url && urls.has(p.url)) && !(p.id && ids.has(String(p.id)))
      )
      exitManage()
    }
    const okMsg = `已废弃 ${r.done_count} 条素材(重新分析后不再参与聚类)`
    if (skipped.length) {
      toast(`${okMsg};跳过 ${skipped.length} 条 —— ${skipped[0].reason}`, 'err')
    } else if (r.done_count) {
      toast(okMsg)
    } else {
      toast('没有条目被废弃', 'err')
    }
  } catch (e) {
    toast('废弃失败: ' + e.message, 'err')
  } finally {
    discarding.value = false
  }
}

const DISCARD_TIP = '\n\n将从热点池移除(软删除,保留合规审计记录),重新分析后不再参与聚类。'
function discardOneMember(m) {
  if (!confirm(`确认废弃素材「${m.title}」?${DISCARD_TIP}`)) return
  discardMembers([{ key: pickKey(m), id: m.id, url: m.url || '', title: m.title }])
}
function discardPickedMembers() {
  if (!discardList.value.length) return
  if (!confirm(`确认废弃选中的 ${discardList.value.length} 条素材?${DISCARD_TIP}`)) return
  discardMembers([...discardList.value])
}

async function generateFromPicked() {
  if (!picked.value.length) { toast('请先在主题成员报道里勾选素材', 'err'); return }
  if (isBusy()) { toast('已有任务正在执行,请等它结束再发起', 'err'); return }
  const src = pickedSource()
  // 两步生成的第二步:人工选定的论点变成硬约束,论证姿态只影响这一篇
  const chosen = theses.value[chosenIdx.value]
  if (chosen) {
    src.thesis = chosen.thesis
    src.article_type = chosenType.value || chosen.type || ''
  }
  // 张力与论点是两个独立的硬约束:可以不选论点只选张力,
  // 也可以在选了论点之后**改选**另一条张力(改了就以改后的为准)。
  const tension = chosenTension.value
  if (tension) src.tension = tensionText(tension)
  const bits = []
  if (chosen) bits.push('选定论点')
  if (tension) bits.push('选定张力')
  toast(bits.length
    ? `已按${bits.join(' + ')}开始写作(真实模型通常 1~5 分钟)…`
    : `已按所选 ${picked.value.length} 条素材开始生成(真实模型通常 1~5 分钟)…`)
  // 走全局入口:状态与轮询都保存在 ../pipeline.js —— 切到别的标签再回来,
  // 进度条与"生成中…"仍在(以前状态放在本组件里,切走就没了)。
  await runStage('generate', { body: { source: src } })
}

// ---------------- 两步生成:先出候选论点,人工选定后再写 ----------------
// 为什么要拆两步:模型同时"想观点"和"写文章"时,会被写作格式的压力带向稳妥的综述 ——
// 注意力花在把段落写满上,而不是想清楚要说什么。先让它单独给判断,再由人选一个,
// 写第二步时论点就成了**硬约束**而不是可选项。
const TYPES = [
  { v: 'industry_judgment', t: '行业判断型' },
  { v: 'critical', t: '批判质疑型' },
  { v: 'teardown', t: '深度拆解型' },
  { v: 'forecast', t: '趋势预测型' },
]
const theses = ref([])
const thesesBusy = ref(false)
const thesesMeta = ref(null)     // {recommended_type, type_reason, rule_suggestion}
const chosenIdx = ref(-1)
const chosenType = ref('')
// 张力:这组素材里的冲突。论点是对张力的回答 —— 选论点会自动带上它的张力,
// 也允许单独改选(改了就以改后的为准)。
const tensions = ref([])
const tensionIdx = ref(-1)
const TENSION_MARKS = ['①', '②', '③', '④', '⑤']

function typeLabel(v) { return (TYPES.find((t) => t.v === v) || {}).t || v || '—' }
function typeCls(v) {
  return { industry_judgment: 'blue', critical: 'red', teardown: 'amber', forecast: 'green' }[v] || 'gray'
}

/** 张力 id → 显示序号(①②③…);找不到返回空串 */
function tensionMark(id) {
  const i = tensions.value.findIndex((t) => t.id === id)
  return i >= 0 ? (TENSION_MARKS[i] || `#${i + 1}`) : ''
}
const chosenTension = computed(
  () => (tensionIdx.value >= 0 ? tensions.value[tensionIdx.value] : null)
)

/** 发给后端的张力文本:一句话 + 双方/反差/依据,信息给全比只给一句话写得更贴 */
function tensionText(t) {
  if (!t) return ''
  const extra = []
  if (t.sides) extra.push(`冲突双方:${t.sides}`)
  if (t.contrast) extra.push(`声称 vs 实际:${t.contrast}`)
  if (t.evidence) extra.push(`依据:${t.evidence}`)
  return extra.length ? `${t.tension}\n(${extra.join(';')})` : t.tension
}

function pickThesis(i) {
  chosenIdx.value = i
  // 选中论点时,跟着把它的张力也选上 —— 论点就是从那条张力里长出来的
  const t = theses.value[i]
  if (t && t.tension_id) {
    const ti = tensions.value.findIndex((x) => x.id === t.tension_id)
    if (ti >= 0) tensionIdx.value = ti
  }
}

function pickedSource() {
  const first = picked.value[0]
  if (!first) return null
  return {
    cluster_topic: first.topic || '',
    topic_urls: picked.value.map((p) => p.url).filter(Boolean),
    topic_ids: picked.value.map((p) => p.id).filter((x) => x != null),
    // 建议角度/摘要只在「全部选自同一主题」时才有意义
    angle: sameTopicPicked.value ? (selected.value?.suggested_angle || '') : '',
    summary: sameTopicPicked.value ? (selected.value?.summary || '') : '',
  }
}

async function planTheses() {
  const src = pickedSource()
  if (!src) { toast('请先在主题成员报道里勾选素材', 'err'); return }
  thesesBusy.value = true
  chosenIdx.value = -1
  tensionIdx.value = -1
  try {
    const r = await api.post('/pipeline/theses', { source: src, n: 4 }, 300000)
    if (!r || r.ok === false) {
      toast('生成候选论点失败:' + ((r && r.error) || '未知原因'), 'err')
      return
    }
    theses.value = r.theses || []
    tensions.value = r.tensions || []          // 张力是增强项,可能为空
    thesesMeta.value = r
    // ⚠️ 默认用**控制面板配置的论证姿态**,不是模型推荐值 ——
    // 推荐值只是建议;以前直接拿它当默认,用户设的"行业判断型"会被模型偏好的
    // "批判质疑型"悄悄顶掉,并且作为 override 传给生成阶段,配置形同虚设(实测踩过)。
    chosenType.value = (cfg.value && cfg.value.article_type) || r.recommended_type || ''
    if (!theses.value.length) { toast('没有拿到可用的候选论点,可重试一次', 'err'); return }
    const nT = tensions.value.length
    toast(nT
      ? `已提取 ${nT} 条张力、${theses.value.length} 条候选论点,挑一个再写`
      : `已生成 ${theses.value.length} 条候选论点(这次没提取到张力)`)
  } catch (e) {
    toast('生成候选论点失败:' + e.message, 'err')
  } finally {
    thesesBusy.value = false
  }
}

onMounted(async () => {
  await load()
  // 接管可能已经在跑的生成任务:从别的标签切回来时进度还在
  await attachPipeline()
  offDone = onPipelineDone((p) => {
    // 完成提示统一由 App.vue 弹(全局只弹一次);这里只负责把数据刷新出来
    if (!p) return
    if (p.stage === 'generate' && p.ok) load()
    if (p.stage === 'analyze' && p.ok) loadReport()   // 分析跑完,报告要跟着变
  })
})
onUnmounted(() => { if (offDone) offDone() })

// ⚠️ 本页**故意不做 onActivated 自动刷新**(KeepAlive 之后切走再回来,视图原样保留):
//    `load()` 会用服务端配置覆盖这一页的参数表单(normalize_method / keywords / top_n …),
//    用户没保存的改动会被冲掉;报告里的素材勾选也可能被换掉。
//    数据要更新有两条更好的路:上面的 onPipelineDone(任务跑完自动刷)、以及页面上的刷新按钮。
</script>

<template>
  <div class="page">
    <div class="page-title">
      分析报告
      <button class="btn" @click="load" :disabled="loading">{{ loading ? '加载中…' : '刷新' }}</button>
    </div>

    <div class="grid grid-2">
      <!-- 左:分析结果 -->
      <div class="card">
        <div class="row between">
          <h3 style="margin:0;">分析结果</h3>
          <button class="btn primary sm" :disabled="running" @click="rerunAnalyze">
            {{ running ? '分析中…' : '重新分析' }}
          </button>
        </div>
        <div style="margin:8px 0;">
          <RangePicker v-model="range" @update:modelValue="loadReport" />
        </div>
        <div class="meta" style="margin:0 0 8px;">{{ rangeHint }}</div>
        <div v-if="reportMeta" class="meta warn-text" style="margin:0 0 8px;">{{ reportMeta }}</div>

        <div v-if="!report" class="empty">
          该时间范围内没有可展示的报告 —— 换个范围,或先点右上「重新分析」
        </div>

        <template v-else>
          <div class="note" v-if="report.total_topics !== undefined">
            共分析 {{ report.total_topics }} 条热点 → {{ report.clusters }} 个主题
          </div>

          <div v-if="report.topics && report.topics.length" class="mt8">
            <div v-for="(t, i) in report.topics" :key="i" class="topic-card clickable"
              :class="{ sel: selected && selected.topic === t.topic }" @click="selectTopic(t)">
              <div class="row between">
                <strong style="font-size:14px;">#{{ i + 1 }} {{ t.topic }}</strong>
                <span class="row" style="gap:6px;">
                  <!-- 传播潜力:来自账号自己的转发数据。热度高不等于会被转发,两个维度一起看 -->
                  <span v-if="t.virality !== undefined" class="badge" :class="virBadge(t.virality).cls"
                        :title="`传播潜力 ${t.virality}(0.5 = 与账号历史平均持平);来自你自己的转发数据画像`">
                    传播 {{ virBadge(t.virality).text }}
                  </span>
                  <!-- 分析阶段就给出该信息适合写哪种文章,让用户据此选择 -->
                  <span v-if="t.suggested_type" class="badge" :class="typeCls(t.suggested_type.type)"
                        :title="t.suggested_type.reason">{{ t.suggested_type.label }}</span>
                  <span class="badge blue">{{ t.hot_count }} 条</span>
                </span>
              </div>
              <div class="meta" style="margin-top:4px;">{{ t.suggested_angle }}</div>
              <div class="meta" style="margin-top:2px; color:var(--primary);">点击查看分析摘要 ›</div>
            </div>
          </div>
        </template>
      </div>

      <!-- 右栏 -->
      <div style="display:flex; flex-direction:column; gap:16px;">
        <!-- ⓪ 传播画像:把回流数据接回选题(涨粉的关键是"被转发",而热度不反映这一点) -->
        <div class="card">
          <div class="row between">
            <h3 style="margin:0;">传播画像</h3>
            <button class="btn sm" :disabled="profBusy" @click="rebuildProfile">
              {{ profBusy ? '学习中…' : '重建画像' }}
            </button>
          </div>
          <div v-if="!profile || !profile.ok" class="meta" style="margin-top:6px;">
            还没有画像。点「重建画像」会读取你公众号的发表记录,
            从真实转发数据里学「什么样的标题更容易被转发」——然后给选题加一个传播潜力分。
          </div>
          <template v-else>
            <div class="meta" style="margin-top:6px;">
              基于 {{ profile.samples }} 篇真实发表记录 · 平均转发率
              {{ ((profile.baseline_rate || 0) * 100).toFixed(1) }}%
              <span v-if="profile.built_at"> · {{ profile.built_at }}</span>
            </div>
            <div class="row wrap" style="gap:6px; margin-top:8px;">
              <span v-for="f in profile.forms" :key="f.key" class="badge" :class="liftCls(f.lift)"
                    :title="`${f.n} 篇命中;lift ${f.lift} = 这类标题的平均转发率 ÷ 全站平均`">
                {{ f.label }} ×{{ f.lift }}
              </span>
            </div>
            <div v-if="profile.terms && profile.terms.length" class="meta" style="margin-top:8px;">
              高频词:{{ profile.terms.map((t) => `${t.term} ×${t.lift}`).join(' · ') }}
            </div>
            <div class="note" style="margin-top:10px; font-size:12px;">{{ profile.limits }}</div>
          </template>
        </div>

        <!-- ① 人工选材 → 两步生成 -->
        <div class="card">
          <div class="row between">
            <h3 style="margin:0;">生成文章</h3>
            <span class="badge" :class="picked.length ? 'blue' : 'gray'">已选 {{ picked.length }} 条</span>
          </div>
          <div class="meta" style="margin:8px 0;">
            第 1 步:在左侧主题的「成员报道」里勾选素材(<strong>第 1 条作为主选题</strong>)。
            第 2 步:让 AI 给几个候选论点,<strong>你挑一个</strong>,再按它写 ——
            先有判断再有文章,比一路写下来更像分析。
          </div>

          <div v-if="!picked.length" class="empty">尚未勾选素材 —— 点开左侧主题,在成员报道前打勾</div>
          <div v-else>
            <div v-for="(p, i) in picked" :key="p.key" class="picked-row">
              <span class="idx" :class="{ main: i === 0 }">{{ i === 0 ? '主' : i + 1 }}</span>
              <span class="grow" style="min-width:0; word-break:break-all;">
                <span class="badge gray" style="margin-right:4px;">{{ p.source === 'rss' ? 'RSS' : p.source }}</span>{{ p.title }}
                <span v-if="!sameTopicPicked" class="meta">· 来自「{{ p.topic }}」</span>
              </span>
              <button v-if="i > 0" class="btn sm" @click="setMain(p.key)">设为主选题</button>
              <button class="btn sm" @click="removePicked(p.key)">移除</button>
            </div>
          </div>

          <div class="row wrap" style="margin-top:10px;">
            <button class="btn primary" :disabled="!picked.length || thesesBusy" @click="planTheses">
              {{ thesesBusy ? 'AI 思考中…(约 1 分钟)' : (theses.length ? '重新生成候选论点' : '① 生成候选论点') }}
            </button>
            <button class="btn" :disabled="!picked.length || genRunning" @click="generateFromPicked">
              跳过,直接写文章
            </button>
            <button class="btn" :disabled="!picked.length || genRunning" @click="clearPicked">清空</button>
          </div>

          <!-- AI 推荐写法 vs 规则建议:两者不一致时说明这组素材取向偏模糊 -->
          <div v-if="thesesMeta" class="rec-box">
            <div class="row wrap">
              <strong style="font-size:13px;">
                模型推荐写法:{{ thesesMeta.recommended_label || typeLabel(thesesMeta.recommended_type) }}
              </strong>
              <span v-if="cfg.article_type" class="meta">
                · 控制面板设定:{{ typeLabel(cfg.article_type) }}(写作时默认按它)
              </span>
              <span v-if="thesesMeta.rule_suggestion" class="meta">
                · 规则建议:{{ thesesMeta.rule_suggestion.label }}
                <span v-if="thesesMeta.rule_suggestion.label !== thesesMeta.recommended_label">
                  (两者不一致 —— 这组素材的取向本身就偏模糊,值得你自己拍板)
                </span>
              </span>
            </div>
            <div v-if="thesesMeta.type_reason" class="meta" style="margin-top:2px;">
              理由:{{ thesesMeta.type_reason }}
            </div>
          </div>

          <!-- 素材里的张力:先让人看见"冲突在哪",再决定围绕哪条写 -->
          <div v-if="tensions.length" class="tension-box">
            <div class="row between wrap">
              <strong style="font-size:13px;">素材里的张力({{ tensions.length }} 条)</strong>
              <span class="meta">点一条选定 —— 全文围绕这个冲突展开;再点一次取消</span>
            </div>
            <div v-for="(t, i) in tensions" :key="t.id" class="tension-card"
                 :class="{ sel: tensionIdx === i }"
                 @click="tensionIdx = tensionIdx === i ? -1 : i">
              <div class="row" style="align-items:flex-start; gap:8px;">
                <span class="idx" :class="{ main: tensionIdx === i }">{{ TENSION_MARKS[i] || (i + 1) }}</span>
                <span class="grow" style="min-width:0; font-size:13px; line-height:1.7; word-break:break-word;">
                  {{ t.tension }}
                </span>
              </div>
              <div class="row wrap meta" style="margin-top:4px; gap:10px;">
                <span v-if="t.sides">双方:{{ t.sides }}</span>
                <span v-if="t.contrast">反差:{{ t.contrast }}</span>
                <span v-if="t.evidence">依据:{{ t.evidence }}</span>
              </div>
            </div>
          </div>

          <!-- 候选论点:每条 = 判断 + 支撑事实 + 角度 + 钩子 -->
          <div v-for="(t, i) in theses" :key="i" class="thesis-card" :class="{ sel: chosenIdx === i }"
               @click="pickThesis(i)">
            <div class="row" style="align-items:flex-start; gap:8px;">
              <span class="idx" :class="{ main: chosenIdx === i }">{{ i + 1 }}</span>
              <span class="grow" style="min-width:0; font-size:13px; line-height:1.7; word-break:break-word;">
                {{ t.thesis }}
              </span>
              <span v-if="tensionMark(t.tension_id)" class="badge gray">
                张力{{ tensionMark(t.tension_id) }}
              </span>
              <span v-if="thesesMeta && t.type === thesesMeta.recommended_type" class="badge green">推荐</span>
            </div>
            <div v-if="t.support" class="meta" style="margin-top:4px;">依据:{{ t.support }}</div>
            <div class="row wrap meta" style="margin-top:4px; gap:10px;">
              <span v-if="t.angle">角度:{{ t.angle }}</span>
              <!-- 姿态由配置统一决定,所以只在"模型为这一条提了例外"时才显示 -->
              <span v-if="t.type && t.type !== ((cfg && cfg.article_type) || '')">
                本条建议:{{ typeLabel(t.type) }}
              </span>
              <span v-if="t.hook">开头钩子:{{ t.hook }}</span>
            </div>
          </div>

          <div v-if="chosenIdx >= 0 || chosenTension" class="step-two">
            <div class="step-head">② 确认要写什么,然后写文章</div>
            <div class="row wrap">
              <span class="meta" style="min-width:60px;">论证姿态</span>
              <select v-model="chosenType" style="max-width:230px;">
                <option v-for="o in TYPES" :key="o.v" :value="o.v">{{ o.t }}</option>
              </select>
              <span class="meta">默认取控制面板的设定;只在这一次生效,不改全局配置</span>
              <span v-if="thesesMeta && thesesMeta.recommended_type && thesesMeta.recommended_type !== chosenType"
                    class="meta warn-text">
                (模型认为素材更适合:{{ typeLabel(thesesMeta.recommended_type) }})
              </span>
            </div>
            <div v-if="chosenIdx >= 0" class="note" style="margin-top:8px;">
              <span class="badge blue">论点</span>{{ theses[chosenIdx].thesis }}
            </div>
            <div v-if="chosenTension" class="note" style="margin-top:6px;">
              <span class="badge amber">张力{{ TENSION_MARKS[tensionIdx] || (tensionIdx + 1) }}</span>
              {{ chosenTension.tension }}
              <button class="btn sm" style="margin-left:6px;" @click="tensionIdx = -1">不约束张力</button>
            </div>
            <div v-else-if="tensions.length" class="meta" style="margin-top:6px;">
              未选张力 —— 文章不会被要求围绕某个特定冲突展开
            </div>
            <div class="row wrap" style="margin-top:8px;">
              <button class="btn primary" :disabled="genRunning" @click="generateFromPicked">
                {{ genActive ? '生成中…' : '按以上设定写文章' }}
              </button>
            </div>
          </div>

          <!-- 生成进度 -->
          <div v-if="genState" class="gen-progress">
            <div class="row between">
              <strong style="font-size:13px;">
                {{ genState.running ? '生成进度' : (genState.ok ? '生成完成' : '生成失败') }}
                <span class="meta" style="margin-left:6px;">{{ genState.elapsed || 0 }}s</span>
              </strong>
              <span v-if="genState.ok === true" class="badge green">成功</span>
              <span v-else-if="genState.ok === false && !genState.running" class="badge red">失败</span>
            </div>
            <div v-if="genState.steps && genState.steps.length" class="steps-mini">
              <span v-for="s in genState.steps" :key="s.key" class="step-chip" :class="s.status">
                {{ s.status === 'success' ? '✓' : s.status === 'failed' ? '✕' : s.status === 'running' ? '⟳' : '○' }} {{ s.label }}
              </span>
            </div>
            <div v-if="genState.message" class="meta" style="margin-top:4px;">{{ genState.message }}</div>
            <div v-if="genState.error" class="err-line">{{ genState.error }}</div>
            <div v-if="genState.hint" class="hint-line">建议:{{ genState.hint }}</div>
            <div v-if="genState.result && genState.result.article_id" class="note-line">
              已生成文章 #{{ genState.result.article_id }}
              <a :href="'#/article/' + genState.result.article_id">打开处理窗口 ›</a>
            </div>
          </div>
        </div>

        <!-- ② 选中主题的摘要详情 -->
        <div class="card">
          <h3 style="margin:0;">
            主题摘要
            <span v-if="selected" class="badge blue" style="margin-left:6px;">{{ selected.topic }}</span>
          </h3>
          <div v-if="!selected" class="empty">← 点击左侧主题查看聚类摘要与成员报道</div>
          <template v-else>
            <div class="note" style="white-space:pre-wrap; margin:8px 0 10px;">{{ selected.summary || '暂无摘要' }}</div>
            <div class="meta" style="margin-bottom:6px;">
              建议角度:{{ selected.suggested_angle }}
            </div>
            <div v-if="selected.members && selected.members.length" class="mt8">
              <div class="row between wrap" style="margin-bottom:4px;">
                <span class="meta" style="font-weight:600;">
                  成员报道({{ selected.members.length }} 条)
                  <span v-if="manageMode" class="badge amber" style="margin-left:6px;">
                    批量废弃模式 · 已选 {{ discardList.length }}
                  </span>
                </span>
                <div class="row wrap">
                  <template v-if="manageMode">
                    <button class="btn sm" :disabled="discarding" @click="selectAllForDiscard">
                      {{ allDiscardInTopic ? '取消全选' : '全选' }}
                    </button>
                    <button class="btn sm danger" :disabled="!discardList.length || discarding"
                      @click="discardPickedMembers">
                      {{ discarding ? '处理中…' : '确认废弃' }}
                    </button>
                    <button class="btn sm" :disabled="discarding" @click="exitManage">取消</button>
                  </template>
                  <template v-else>
                    <button class="btn sm" @click="selectAllInTopic">
                      {{ allPickedInTopic ? '取消本主题' : '全选本主题' }}
                    </button>
                    <button class="btn sm" @click="enterManage">批量废弃</button>
                  </template>
                </div>
              </div>
              <div v-if="manageMode" class="note" style="margin-bottom:6px;">
                勾选要废弃的素材 —— 与「选材」是两回事,这里勾的会被移出热点池。
              </div>
              <div v-for="(m, j) in selected.members" :key="j" class="member-row"
                   :class="{ picked: isPicked(m), doomed: inDiscard(m) }">
                <div class="row between">
                  <input v-if="manageMode" type="checkbox" :checked="inDiscard(m)" @change="toggleDiscard(m)"
                         style="margin-right:8px; flex:0 0 auto;" title="勾选后将从热点池废弃" />
                  <input v-else type="checkbox" :checked="isPicked(m)" @change="togglePick(m)"
                         style="margin-right:8px; flex:0 0 auto;"
                         title="勾选后可作为生成文章的素材" />
                  <span style="flex:1; min-width:0;">
                    <span v-if="m.source === 'rss'" class="badge gray" style="margin-right:6px;">RSS</span>
                    <span v-else class="badge gray" style="margin-right:6px;">{{ m.source }}</span>
                    <a v-if="m.url" :href="m.url" target="_blank" style="word-break:break-all;">{{ m.title }}</a>
                    <span v-else>{{ m.title }}</span>
                  </span>
                  <span class="meta" style="white-space:nowrap;">热度 {{ m.heat ?? '—' }}</span>
                  <button v-if="!manageMode" class="btn sm danger" :disabled="discarding"
                    :title="'从热点池废弃(软删除,可追溯)'" @click="discardOneMember(m)">废弃</button>
                </div>
                <div v-if="m.summary" class="meta" style="margin-top:2px;">{{ m.summary }}</div>
              </div>
            </div>
            <div v-else class="empty">该主题暂无成员条目</div>
          </template>
        </div>

        <!-- 分析标准编辑 -->
        <div class="card">
          <h3>分析标准(评分与聚类参数)</h3>
        <label>领域关键词(逗号分隔)</label>
        <input type="text" v-model="keywordsCsv" @input="markDirty" />

        <label>归一化方式</label>
        <select v-model="normMethod" @change="markDirty">
          <option value="percentile">排名百分位(推荐:消除跨平台量纲)</option>
          <option value="log">对数归一化(log10)</option>
          <option value="minmax">Min-Max</option>
        </select>

        <label>时效衰减半衰期(小时)</label>
        <input type="number" v-model="decayHours" @input="markDirty" min="1" />

        <label>关键词相关度加权</label>
        <select v-model="relevanceEnabled" @change="markDirty">
          <option :value="true">开启(命中关键词越多热度越高)</option>
          <option :value="false">关闭</option>
        </select>

        <label>各来源权重(跨平台公平性)</label>
        <div class="row" style="gap:8px;">
          <input type="number" v-model="sourceWeights.wechat" step="0.05" min="0" @input="markDirty" placeholder="微信" />
          <input type="number" v-model="sourceWeights.douyin" step="0.05" min="0" @input="markDirty" placeholder="抖音" />
          <input type="number" v-model="sourceWeights.rss" step="0.05" min="0" @input="markDirty" placeholder="RSS" />
        </div>
        <div class="meta" style="margin-top:4px;">顺序:微信 / 抖音 / RSS(未启用来源权重无影响)</div>

        <label>参与聚类热点上限 top_n</label>
        <input type="number" v-model="topN" @input="markDirty" min="5" />

        <div class="mt16">
          <button class="btn success" :disabled="!dirty" @click="saveParams">保存分析标准</button>
        </div>
        </div>   <!-- card:分析标准结束 -->
      </div>     <!-- 右栏 flex 容器结束 -->
    </div>       <!-- grid 结束 -->
  </div>
</template>

<style scoped>
.topic-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 8px;
  background: #fcfbf8;
}
.topic-card.clickable { cursor: pointer; transition: border-color .12s; }
.topic-card.clickable:hover { border-color: var(--primary); }
.topic-card.sel { border-color: var(--primary); background: var(--primary-light); }
.member-row {
  border-bottom: 1px dashed var(--border);
  padding: 6px 0;
}
.member-row:last-child { border-bottom: none; }
.member-row.picked { background: var(--primary-light); border-radius: 6px; padding: 6px; }
/* 待废弃:用警示色与「选材」的蓝色区分开,避免两种勾选状态看混 */
.member-row.doomed { background: var(--danger-light); border-radius: 6px; padding: 6px; }

.picked-row {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 0; border-bottom: 1px dashed var(--border);
  font-size: 13px;
}
.picked-row:last-child { border-bottom: none; }
.picked-row .idx {
  flex: 0 0 auto; width: 22px; height: 22px; line-height: 22px;
  text-align: center; border-radius: 50%;
  background: #e5e3dc; color: #888; font-size: 11px;
}
.picked-row .idx.main { background: var(--primary); color: #fff; }

.gen-progress { margin-top: 12px; border-top: 1px solid var(--border); padding-top: 10px; }
.steps-mini { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.step-chip {
  font-size: 12px; padding: 2px 8px; border-radius: 10px;
  background: #eeece6; color: #777;
}
.step-chip.success { background: #e6f4ea; color: #1b5e20; }
.step-chip.failed { background: #fdecea; color: #b3261e; }
.step-chip.running { background: #e8f0fe; color: #185fa5; }

.err-line { color: #b3261e; font-size: 12px; margin-top: 4px; word-break: break-all; }
.hint-line { color: #7a5c00; font-size: 12px; margin-top: 4px; }
.note-line { color: #1b5e20; font-size: 12px; margin-top: 4px; }

/* 两步生成 */
.rec-box {
  margin-top: 10px; padding: 8px 10px; border-radius: 6px;
  background: var(--primary-light); border-left: 3px solid var(--primary);
}
/* 张力:用警示色,跟主色的「论点」明显区分,避免两种卡片看混 */
.tension-box {
  margin-top: 12px; padding: 10px 11px; border-radius: 8px;
  background: var(--warn-light); border-left: 3px solid var(--warn);
}
.tension-card {
  border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 11px; margin-top: 8px; cursor: pointer;
  background: #fff; transition: border-color .12s;
}
.tension-card:hover { border-color: var(--warn); }
.tension-card.sel { border-color: var(--warn); background: var(--warn-light); }
.tension-card .idx {
  flex: 0 0 auto; width: 22px; height: 22px; line-height: 22px;
  text-align: center; border-radius: 50%;
  background: #e5e3dc; color: #888; font-size: 11px;
}
.tension-card .idx.main { background: var(--warn); color: #fff; }
.thesis-card {
  border: 1px solid var(--border); border-radius: 8px;
  padding: 9px 11px; margin-top: 8px; cursor: pointer;
  background: #fcfbf8; transition: border-color .12s;
}
.thesis-card:hover { border-color: var(--primary); }
.thesis-card.sel { border-color: var(--primary); background: var(--primary-light); }
.thesis-card .idx {
  flex: 0 0 auto; width: 22px; height: 22px; line-height: 22px;
  text-align: center; border-radius: 50%;
  background: #e5e3dc; color: #888; font-size: 11px;
}
.thesis-card .idx.main { background: var(--primary); color: #fff; }
.step-two {
  margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--border);
}
.step-head {
  font-size: 12px; font-weight: 600; color: var(--text-sub);
  letter-spacing: .5px; margin-bottom: 6px;
}
</style>
