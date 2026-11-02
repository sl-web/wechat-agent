<script setup>
import { ref, onMounted, onActivated, onDeactivated, onUnmounted, computed } from 'vue'
import { api } from '../api.js'
import { toast } from '../toast.js'

const props = defineProps({ articleId: { type: Number, default: 0 } })

const current = ref(null)
const loading = ref(false)
const editMode = ref(false)
const draftUrl = ref('')
const rejectReason = ref('')
const editForm = ref({ title: '', lead: '', thesis: '', body: '', quote: '', cta: '' })

// —— 标题候选 ——
// 打开率决定阅读量,而转发的前提是被打开。模型默认只给一个"稳妥标题",
// 这里一次给 3 个**形态不同**的候选让人挑,并标上传播潜力分(来自账号自己的转发数据)。
const titleCands = ref([])
const titleBusy = ref(false)

async function suggestTitles() {
  titleBusy.value = true
  try {
    const r = await api.post('/pipeline/titles', { article_id: props.articleId, n: 3 }, 180000)
    titleCands.value = (r && r.titles) || []
    if (!titleCands.value.length) toast('没有拿到可用标题,可重试一次', 'err')
  } catch (e) {
    toast('生成候选标题失败: ' + e.message, 'err')
  } finally {
    titleBusy.value = false
  }
}

async function useTitle(t) {
  try {
    // 只提交 title,后端只更新传入的字段(不会动正文)
    await api.post('/review/edit/' + props.articleId, { title: t.title })
    toast('标题已更新 —— 状态回到「已编辑」')
    titleCands.value = []
    await load()
  } catch (e) {
    toast('更新标题失败: ' + e.message, 'err')
  }
}

function virBadge(v) {
  const x = v === null || v === undefined ? 0.5 : v
  if (x >= 0.6) return { cls: 'green', text: '易传播' }
  if (x <= 0.4) return { cls: 'gray', text: '难传播' }
  return { cls: 'gray', text: '中性' }
}

// —— 发布时段建议 ——
// 阅读集中在推送后的前几小时,所以"什么时候发"同样影响打开率。
const pubWindow = ref(null)
async function loadWindow() {
  try {
    pubWindow.value = await api.get('/pusher/publish-window')
  } catch (_) { /* 时段建议缺失不影响主流程 */ }
}

const STATUS_META = [
  { key: 'generated', label: '已生成', cls: 'blue' },
  { key: 'pushed', label: '已推送', cls: 'amber' },
  { key: 'pending_review', label: '待审核', cls: 'amber' },
  { key: 'edited', label: '已编辑', cls: 'blue' },
  { key: 'approved', label: '已通过', cls: 'green' },
  { key: 'rejected', label: '已驳回', cls: 'red' },
  { key: 'published', label: '已发布', cls: 'green' },
  { key: 'backfilled', label: '已回流', cls: 'green' },
  { key: 'generation_failed', label: '生成失败', cls: 'red' },
]
const labelOf = (s) => (STATUS_META.find((m) => m.key === s) || {}).label || s
const clsOf = (s) => (STATUS_META.find((m) => m.key === s) || {}).cls || 'gray'

const can = {
  approve: (s) => ['generated', 'pending_review', 'edited', 'rejected'].includes(s),
  push: (s) => ['generated', 'approved', 'pushed', 'pending_review', 'edited', 'rejected'].includes(s),
  publish: (s) => ['approved', 'pushed'].includes(s),
  backfill: (s) => ['published', 'backfilled'].includes(s),
  editable: (s) => !['published', 'backfilled', 'generation_failed'].includes(s),
  clean: (s) => !['published', 'backfilled'].includes(s),
}

async function load() {
  loading.value = true
  try {
    current.value = await api.get('/review/articles/' + props.articleId)
    const a = current.value
    editForm.value = {
      title: a.title || '', lead: a.lead || '', thesis: a.thesis || '',
      body: a.body || '', quote: a.quote || '', cta: a.cta || '',
    }
  } catch (e) {
    toast('加载失败: ' + e.message, 'err')
  } finally {
    loading.value = false
  }
}

async function afterAction() { await load(); toast('操作成功,列表已同步(返回后刷新)') }
async function approve() { try { await api.post('/review/approve/' + props.articleId); afterAction() } catch (e) { toast(e.message, 'err') } }
async function pushDraft() {
  if (!confirm('推送草稿:未配置公众号自动化时标记待人工处理,继续?')) return
  try { await api.post('/review/push/' + props.articleId, { draft_url: draftUrl.value }); afterAction() } catch (e) { toast(e.message, 'err') }
}
async function publishConfirm() {
  try { await api.post('/review/publish/' + props.articleId, { draft_url: draftUrl.value }); afterAction() } catch (e) { toast(e.message, 'err') }
}
async function backfillOne() {
  try {
    const r = await api.post('/review/backfill/' + props.articleId)
    const s = r.stats || {}
    toast(`回流完成:阅读${s.read_count ?? 0} 赞${s.like_count ?? 0} 转${s.share_count ?? 0}`)
  } catch (e) { toast(e.message, 'err') }
}
async function rejectOne() {
  if (!rejectReason.value.trim()) { toast('请填写驳回原因', 'err'); return }
  try { await api.post('/review/reject/' + props.articleId, { reason: rejectReason.value }); afterAction() } catch (e) { toast(e.message, 'err') }
}
async function saveEdit() {
  try {
    await api.post('/review/edit/' + props.articleId, editForm.value)
    toast('已保存编辑,状态回待审核')
    editMode.value = false
    await load()
  } catch (e) { toast(e.message, 'err') }
}
async function cleanOne() {
  if (!confirm('确定清理(删除)这篇未发布稿件?')) return
  try {
    await api.post('/review/clean/' + props.articleId, { reason: 'page_clean' })
    toast('已清理删除')
    goBack()
  } catch (e) { toast(e.message, 'err') }
}

// 返回来源 tab
const backTo = ref('#/review')
function goBack() {
  const b = sessionStorage.getItem('articleBackTo')
  if (b) { sessionStorage.removeItem('articleBackTo'); location.hash = b } else location.hash = backTo.value
}

function parseNotes(s) {
  if (!s) return []
  try { const arr = JSON.parse(s); return Array.isArray(arr) ? arr : [s] } catch { return [s] }
}

// ---- 生成参考信息来源 ----
const showMaterial = ref(false)
const refs = computed(() => {
  const raw = current.value && current.value.source_refs
  if (!raw) return null
  try { return JSON.parse(raw) } catch { return null }
})
const sourceLabel = { wechat: '微信', douyin: '抖音', rss: 'RSS' }
function srcName(s) { return sourceLabel[s] || s || '—' }
const refCount = computed(() => {
  if (!refs.value) return 0
  return (refs.value.main_topic ? 1 : 0) + (refs.value.related_topics || []).length
})

// ---- 公众号草稿推送(Playwright 自动化)----
const pusher = ref(null)          // /pusher/status 快照
const planB = ref(null)           // 方案 B 导出结果
let pollTimer = null

// ---- 封面(模板渲染,可推送前预览)----
// 加 ?t= 破缓存:重新生成后文件名不变,不加这个参数浏览器会一直显示旧图
const coverBusy = ref(false)
const coverOk = ref(false)
const coverBust = ref(Date.now())
const coverUrl = computed(
  () => `/api/pusher/cover/${props.articleId}?t=${coverBust.value}`
)
async function checkCover() {
  try {
    const res = await fetch(`/api/pusher/cover/${props.articleId}`)
    coverOk.value = res.ok
  } catch { coverOk.value = false }
}
async function doGenCover() {
  coverBusy.value = true
  try {
    const r = await api.post('/pusher/cover/' + props.articleId, {}, 120000)
    if (!r.ok) {
      toast('封面生成失败:' + (r.error || '未知原因'), 'err')
      return
    }
    coverBust.value = Date.now()
    coverOk.value = true
    toast('封面已生成,推送时会自动使用')
  } catch (e) {
    toast('封面生成失败:' + e.message, 'err')
  } finally {
    coverBusy.value = false
  }
}

const session = computed(() => (pusher.value && pusher.value.session) || {})
const loginState = computed(() => (pusher.value && pusher.value.login) || {})
const pushState = computed(() => (pusher.value && pusher.value.push) || {})
const pushRunning = computed(() => !!pushState.value.running)
const loginRunning = computed(() => !!loginState.value.running)

// 推送状态是**全局单例**(同一时刻只能有一个 Playwright 浏览器在跑),
// 所以必须判断它属于哪篇文章 —— 否则打开任何文章都会看到「上一篇文章」的推送结果。
const pushArticleId = computed(() => pushState.value.article_id)
const pushForThis = computed(
  () => pushArticleId.value != null && Number(pushArticleId.value) === Number(props.articleId)
)
// 正在推别的文章:要提示用户,而不是把别人的进度伪装成本篇的
const pushForOther = computed(() => !!pushState.value.running && !pushForThis.value)
// 本篇自己的草稿链接(落库在 articles.draft_url)
const draftLink = computed(() => {
  const u = (current.value && current.value.draft_url) || ''
  return /^https?:/.test(u) ? u : ''
})

const pusherBadge = computed(() => {
  if (loginRunning.value) return { cls: 'blue', text: '等待扫码' }
  if (pushRunning.value) {
    return pushForThis.value
      ? { cls: 'blue', text: '推送中' }
      : { cls: 'amber', text: '其他文章推送中' }
  }
  if (!session.value.session_exists) return { cls: 'gray', text: '未登录' }
  if (session.value.expired) return { cls: 'amber', text: '会话可能已过期' }
  return { cls: 'green', text: '已登录' }
})
const canPush = computed(() => ['generated', 'approved', 'pushed', 'edited'].includes(current.value && current.value.status))

async function loadPusher() {
  try { pusher.value = await api.get('/pusher/status') } catch (_) { /* 轮询失败静默 */ }
}
function startPoll() {
  stopPoll()
  pollTimer = setInterval(async () => {
    await loadPusher()
    if (!pushRunning.value && !loginRunning.value && pushState.value.finished_at) {
      stopPoll()
      await load()
    }
  }, 1500)
}
function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null } }

async function doLogin() {
  // 已有会话时点「重新扫码」必须强制:否则浏览器带着旧 cookie 自动登录,
  // 二维码根本不会出现 —— 用户只看到窗口一闪而过。
  const force = !!session.value.session_exists
  const r = await api.post('/pusher/login', { force }, 15000)
  if (!r.ok) { toast(r.error + (r.hint ? ' | ' + r.hint : ''), 'err'); return }
  toast(r.message)
  startPoll()
}

async function doLogout() {
  if (!confirm('确认退出公众号登录?\n\n将清除本地保存的会话,下次推送需要重新扫码登录。' +
               '(不影响已推送的草稿)')) return
  if (loginRunning.value) { toast('请等当前登录流程结束再退出', 'err'); return }
  try {
    const r = await api.post('/pusher/logout', {}, 15000)
    if (r && r.ok === false) { toast(r.error || '退出失败', 'err'); return }
    toast((r && r.message) || '已退出登录,本地会话已清除')
    await loadPusher()
  } catch (e) {
    toast('退出失败: ' + e.message, 'err')
  }
}

async function doPush() {
  const r = await api.post('/pusher/push/' + props.articleId, {}, 15000)
  if (!r.ok) { toast(r.error + (r.hint ? ' | ' + r.hint : ''), 'err'); return }
  toast(r.message)
  startPoll()
}

async function loadPlanB() {
  try {
    const r = await api.get('/pusher/plan-b/' + props.articleId)
    if (!r.ok) { toast(r.error, 'err'); return }
    planB.value = r
  } catch (e) { toast(e.message, 'err') }
}

async function copyRich() {
  if (!planB.value) return
  const { html, markdown } = planB.value
  try {
    await navigator.clipboard.write([new ClipboardItem({
      'text/html': new Blob([html], { type: 'text/html' }),
      'text/plain': new Blob([markdown], { type: 'text/plain' }),
    })])
    toast('富文本已复制,直接粘贴到公众号编辑器即可保留排版')
  } catch (e) {
    toast('复制富文本失败(可手动选中预览区复制): ' + e.message, 'err')
  }
}

async function copyMarkdown() {
  try {
    await navigator.clipboard.writeText(planB.value.markdown)
    toast('Markdown 已复制')
  } catch (e) { toast('复制失败: ' + e.message, 'err') }
}

onMounted(async () => {
  await load()
  await loadPusher()
  loadWindow()
  checkCover()
  // 打开页面时推送若已在运行,自动跟随到结束 ——
  // 否则面板会一直停在"打开那一刻"的快照,看起来像推送卡住了。
  if (pushRunning.value) startPoll()
})
onUnmounted(stopPoll)

// ⚠️ KeepAlive 之后,切到别的标签**不会**卸载本组件,onUnmounted 也就不会触发 ——
// 轮询必须在这里停,否则每开过一个文章页就多留一个 1.5s 的定时器在后台跑。
onDeactivated(stopPoll)
onActivated(async () => {
  await load()          // 回到本页补一次最新数据(状态可能已经被别处改了)
  await loadPusher()
  if (pushRunning.value) startPoll()   // 推送还在跑就继续跟随
})
</script>

<template>
  <div class="page">
    <div class="row between" style="margin-bottom: 14px;">
      <div class="row">
        <button class="btn" @click="goBack">← 返回</button>
        <h2 style="margin:0 0 0 10px; font-size:16px;">
          文章处理 #{{ props.articleId }}
          <span v-if="current" class="badge" :class="clsOf(current.status)" style="margin-left:8px;">{{ labelOf(current.status) }}</span>
        </h2>
      </div>
      <button class="btn" @click="load" :disabled="loading">刷新</button>
    </div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="!current" class="empty">未找到该文章</div>

    <div v-else>
      <!-- 元信息 -->
      <div class="card">
        <h3 style="margin:0 0 8px 0;">{{ current.title }}</h3>
        <div class="row wrap" style="gap:8px; margin-bottom:8px;">
          <button class="btn sm" :disabled="titleBusy" @click="suggestTitles">
            {{ titleBusy ? 'AI 想标题中…' : '换个更易传播的标题' }}
          </button>
          <span class="meta">一次给 3 个不同形态的候选,并标注传播潜力</span>
        </div>

        <div v-if="titleCands.length" class="title-cands">
          <div v-for="(t, i) in titleCands" :key="i" class="title-cand">
            <div class="row between" style="align-items:flex-start; gap:8px;">
              <span class="grow" style="font-size:13px; line-height:1.6;">{{ t.title }}</span>
              <span class="row" style="gap:6px; white-space:nowrap;">
                <span v-if="t.form" class="badge blue">{{ t.form }}</span>
                <span class="badge" :class="virBadge(t.virality).cls"
                      :title="`传播潜力 ${t.virality}(0.5 = 与账号历史平均持平)`">
                  {{ virBadge(t.virality).text }}
                </span>
                <button class="btn sm primary" @click="useTitle(t)">用这个</button>
              </span>
            </div>
            <div v-if="t.reason" class="meta" style="margin-top:3px;">{{ t.reason }}</div>
          </div>
        </div>

        <div class="row wrap meta">
          <span>状态: <strong>{{ labelOf(current.status) }}</strong></span>
          <span v-if="current.topic_title">· 来源热点: {{ current.topic_title }}</span>
          <span v-if="current.published_at">· 发布时间: {{ current.published_at }}</span>
        </div>
        <div v-if="current.draft_url && current.draft_url !== 'pending_manual_push'" class="meta mt8">
          草稿链接: <a :href="current.draft_url" target="_blank">{{ current.draft_url }}</a>
        </div>
        <div v-if="current.reject_reason" class="note">驳回原因: {{ current.reject_reason }}</div>
        <div v-for="(n, i) in parseNotes(current.fact_notes)" :key="i" class="note">【待核实】{{ n }}</div>
      </div>

      <!-- 生成参考信息来源 -->
      <div class="card refs-card">
        <div class="row between">
          <h3 style="margin:0;">参考信息来源<span v-if="refs" class="meta" style="margin-left:8px;">共 {{ refCount }} 条素材</span></h3>
          <span v-if="refs && refs.generated_at" class="meta">生成于 {{ refs.generated_at }}</span>
        </div>

        <div v-if="!refs" class="empty">该文章生成于溯源功能上线前,未记录参考信息。</div>

        <template v-else>
          <!-- 分析归纳背景 -->
          <div class="refs-block">
            <div class="refs-title">① 分析归纳背景</div>
            <template v-if="refs.analysis && refs.analysis.cluster_topic">
              <div class="row wrap meta">
                <span>所属主题:<strong>{{ refs.analysis.cluster_topic }}</strong></span>
                <span>· 该主题聚合 {{ refs.analysis.cluster_count || 0 }} 条报道</span>
                <span v-if="refs.analysis.run_id">· 分析批次 {{ refs.analysis.run_id }}</span>
              </div>
              <div v-if="refs.analysis.suggested_angle" class="meta" style="margin-top:4px;">建议角度:{{ refs.analysis.suggested_angle }}</div>
              <div v-if="refs.analysis.cluster_summary" class="refs-summary">{{ refs.analysis.cluster_summary }}</div>
            </template>
            <div v-else class="meta">本次为「直接生成」,未经分析归纳环节,素材仅为主选题 1 条。走「一键流水线」可带上分析报告与同主题报道。</div>
          </div>

          <!-- 主选题 -->
          <div v-if="refs.main_topic" class="refs-block">
            <div class="refs-title">② 主选题</div>
            <div>
              <span class="badge blue">{{ srcName(refs.main_topic.source) }}</span>
              <a v-if="refs.main_topic.url" :href="refs.main_topic.url" target="_blank" style="margin-left:6px;">{{ refs.main_topic.title }}</a>
              <span v-else style="margin-left:6px;">{{ refs.main_topic.title }}</span>
              <span v-if="refs.main_topic.id" class="meta" style="margin-left:6px;">热点 #{{ refs.main_topic.id }}</span>
            </div>
            <div v-if="refs.main_topic.summary" class="refs-summary">{{ refs.main_topic.summary }}</div>
          </div>

          <!-- 相关报道 -->
          <div v-if="refs.related_topics && refs.related_topics.length" class="refs-block">
            <div class="refs-title">③ 同主题相关报道({{ refs.related_topics.length }} 条,作为交叉印证)</div>
            <div v-for="(r, i) in refs.related_topics" :key="i" class="related-item">
              <span class="badge gray">{{ srcName(r.source) }}</span>
              <a v-if="r.url" :href="r.url" target="_blank" style="margin-left:6px;">{{ r.title }}</a>
              <span v-else style="margin-left:6px;">{{ r.title }}</span>
              <div v-if="r.summary" class="meta" style="margin-top:2px;">{{ r.summary }}</div>
            </div>
          </div>

          <!-- 生成参数 -->
          <div class="refs-block">
            <div class="refs-title">④ 生成参数</div>
            <div class="row wrap meta">
              <span v-if="refs.template">模板:{{ refs.template.template_id }} / 结构 {{ refs.template.structure_id }}</span>
              <span v-if="refs.run_id">· 生成批次 {{ refs.run_id }}</span>
            </div>
          </div>

          <!-- 实际素材 -->
          <div class="refs-block">
            <button class="btn sm" @click="showMaterial = !showMaterial">
              {{ showMaterial ? '收起实际喂给模型的素材' : '查看实际喂给模型的素材' }}
            </button>
            <pre v-if="showMaterial" class="material">{{ refs.material || '(无)' }}</pre>
          </div>
        </template>
      </div>

      <!-- 内容 -->
      <div class="card">
        <template v-if="!editMode">
          <div class="meta">导语</div>
          <div class="article-body" style="background:transparent; border:0; padding:4px 0;">{{ current.lead || '—' }}</div>
          <template v-if="current.thesis">
            <div class="meta" style="margin-top:14px;">核心论点</div>
            <div class="thesis-box">{{ current.thesis }}</div>
          </template>
          <div class="meta" style="margin-top:14px;">正文</div>
          <div v-if="current.body_html" class="article-body rendered" v-html="current.body_html"></div>
          <div v-else class="article-body">{{ current.body }}</div>
          <div class="meta" style="margin-top:14px;">金句</div>
          <div class="article-body" style="background:transparent; border:0; padding:4px 0;">{{ current.quote || '—' }}</div>
          <div class="meta" style="margin-top:14px;">结尾引导</div>
          <div class="article-body" style="background:transparent; border:0; padding:4px 0;">{{ current.cta || '—' }}</div>
        </template>
        <template v-else>
          <label>标题</label><input type="text" v-model="editForm.title" />
          <label>导语</label><textarea v-model="editForm.lead" rows="3"></textarea>
          <label>核心论点(论点驱动结构必填,应是可被反驳的判断)</label>
          <textarea v-model="editForm.thesis" rows="2"></textarea>
          <label>正文</label><textarea v-model="editForm.body" rows="20"></textarea>
          <label>金句</label><input type="text" v-model="editForm.quote" />
          <label>结尾</label><textarea v-model="editForm.cta" rows="3"></textarea>
        </template>
      </div>

      <!-- 公众号草稿推送 -->
      <div class="card push-card">
        <div class="row between">
          <h3 style="margin:0;">公众号草稿推送</h3>
          <span class="badge" :class="pusherBadge.cls">{{ pusherBadge.text }}</span>
        </div>

        <div class="row wrap meta" style="margin-top:6px;">
          <span>会话:{{ session.session_exists ? '本地已保存' : '未登录' }}</span>
          <span v-if="session.last_verified_at">· 上次验证 {{ session.last_verified_at }}</span>
          <span>· 今日额度 {{ session.pushed_today || 0 }}/{{ session.daily_limit || 5 }}</span>
          <span v-if="session.author">· 作者 {{ session.author }}</span>
          <span :class="session.cover_auto || session.cover_ready ? '' : 'warn-text'">
            · 封面:{{ session.cover_auto
              ? ('自动生成' + (session.cover_style ? '(' + session.cover_style + ')' : ''))
              : (session.cover_ready ? '默认封面图' : '未配置(推送后需手动添加)') }}
          </span>
        </div>

        <!-- 发布时段建议:阅读集中在推送后前几小时,所以"什么时候发"同样影响打开率 -->
        <div v-if="pubWindow" class="meta" style="margin-top:6px;"
             :class="pubWindow.in_window ? 'ok-text' : 'warn-text'">
          发布时段:{{ pubWindow.hint }}
          <span v-if="pubWindow.windows && pubWindow.windows.length">
            (推荐时段 {{ pubWindow.windows.join(' / ') }})
          </span>
        </div>

        <div class="row wrap" style="margin-top:10px;">
          <button class="btn" @click="doLogin" :disabled="loginRunning || pushRunning">
            {{ loginRunning ? '等待扫码…' : (session.session_exists ? '重新扫码登录' : '扫码登录') }}
          </button>
          <button v-if="session.session_exists" class="btn" @click="doLogout"
            :disabled="loginRunning || pushRunning" title="清除本地会话,下次推送需重新扫码">
            退出登录
          </button>
          <button class="btn primary" @click="doPush" :disabled="pushRunning || loginRunning || !canPush">
            {{ pushRunning ? '推送中…' : '推送到草稿箱(自动)' }}
          </button>
          <button class="btn" @click="loadPlanB">方案 B:导出可粘贴正文</button>
        </div>

        <!-- 封面:推送前就能看到效果,不用先推一次再回后台找 -->
        <div class="cover-box">
          <div class="cover-frame">
            <img v-if="coverOk" :src="coverUrl" alt="封面预览" />
            <div v-else class="cover-empty">封面未生成</div>
          </div>
          <div class="cover-side">
            <button class="btn sm" @click="doGenCover" :disabled="coverBusy">
              {{ coverBusy ? '生成中…' : (coverOk ? '重新生成封面' : '生成封面') }}
            </button>
            <div class="meta" style="line-height:1.7;">
              按标题用模板渲染(2.35:1 首图)。<br />
              风格在「控制面板 → 正文排版」里选。<br />
              开启「封面自动生成」后,推送会自动用它。
            </div>
          </div>
        </div>
        <div v-if="!canPush" class="meta" style="margin-top:6px;">
          当前状态「{{ labelOf(current.status) }}」不可推送(仅已生成/已通过/已推送/已编辑可推送)。
        </div>

        <!-- 正在推送**其他**文章:与本篇无关,只作提示,不展示别人的进度/结果 -->
        <div v-if="pushForOther" class="push-progress">
          <div class="row between">
            <strong style="font-size:13px;">
              正在推送其他文章{{ pushArticleId ? '(#' + pushArticleId + ')' : '' }}
            </strong>
            <span class="badge blue">进行中</span>
          </div>
          <div class="meta" style="margin-top:4px;">
            浏览器不能并发操作,等这次推送结束后再推本篇。
          </div>
        </div>

        <!-- 扫码登录 或 **本篇**的推送进度/结果 -->
        <div v-else-if="loginRunning || pushForThis" class="push-progress">
          <div class="row between">
            <strong style="font-size:13px;">
              {{ pushRunning ? '推送进度' : (loginRunning ? '扫码登录' : '本篇推送结果') }}
              <span class="meta" style="margin-left:6px;">{{ (pushRunning ? pushState : loginState).elapsed || 0 }}s</span>
            </strong>
            <span v-if="pushForThis && pushState.ok === true" class="badge green">成功</span>
            <span v-else-if="pushForThis && pushState.ok === false && !pushRunning" class="badge red">失败</span>
          </div>

          <template v-if="pushForThis && pushState.steps.length">
            <div class="steps">
              <template v-for="(s, i) in pushState.steps" :key="s.key">
                <div class="step" :class="s.status">
                  <span class="dot">
                    <template v-if="s.status === 'success'">✓</template>
                    <template v-else-if="s.status === 'failed'">✕</template>
                    <template v-else-if="s.status === 'running'">⟳</template>
                    <template v-else-if="s.status === 'skipped'">–</template>
                    <template v-else>○</template>
                  </span>
                  <div>
                    <div class="step-name">{{ s.label }}</div>
                    <div class="step-detail">{{ s.detail || '' }}</div>
                  </div>
                </div>
                <div v-if="i < pushState.steps.length - 1" class="arrow">→</div>
              </template>
            </div>
          </template>
          <div v-else-if="loginRunning" class="meta">{{ loginState.message }}</div>

          <template v-if="pushForThis">
            <div v-if="pushState.error" class="err-line">{{ pushState.error }}</div>
            <div v-if="pushState.hint" class="hint-line">建议:{{ pushState.hint }}</div>
            <div v-if="pushState.result && pushState.result.draft_url" class="note-line">
              草稿已保存:<a :href="pushState.result.draft_url" target="_blank">{{ pushState.result.draft_url }}</a>
            </div>
            <div v-for="(w, i) in (pushState.result && pushState.result.warnings) || []" :key="i" class="hint-line">
              · {{ w }}
            </div>
          </template>
        </div>

        <!-- 本篇已推送:直接给出落库的草稿链接(与全局推送状态无关) -->
        <div v-if="draftLink" class="note-line" style="margin-top:6px;">
          本篇草稿链接:<a :href="draftLink" target="_blank">{{ draftLink }}</a>
        </div>

        <!-- 方案 B -->
        <div v-if="planB" class="planb">
          <div class="row between">
            <strong style="font-size:13px;">方案 B:手动粘贴({{ planB.title }}）</strong>
            <div class="row">
              <button class="btn sm" @click="copyRich">复制富文本</button>
              <button class="btn sm" @click="copyMarkdown">复制 Markdown</button>
              <button class="btn sm" @click="planB = null">关闭</button>
            </div>
          </div>
          <div class="meta" style="margin-top:6px;">
            在公众号后台新建图文 → 点「复制富文本」后直接粘贴,排版会保留。
          </div>
          <div class="planb-preview" v-html="planB.html"></div>
        </div>
      </div>

      <!-- 操作条 -->
      <div class="card">
        <template v-if="editMode">
          <div class="row">
            <button class="btn success" @click="saveEdit">保存编辑(回待审核)</button>
            <button class="btn" @click="editMode = false">取消编辑</button>
          </div>
        </template>
        <template v-else>
          <div v-if="['approved', 'pushed'].includes(current.status)" class="row" style="margin-bottom:10px;">
            <label style="margin:0 6px 0 0; white-space:nowrap;">草稿链接(可选):</label>
            <input type="text" class="grow" v-model="draftUrl" placeholder="https://mp.weixin.qq.com/... 留空则标记待人工处理" />
          </div>
          <div class="row wrap">
            <button v-if="can.approve(current.status)" class="btn success" @click="approve">✓ 通过</button>
            <button v-if="can.push(current.status)" class="btn primary" @click="pushDraft">推送草稿</button>
            <button v-if="can.publish(current.status)" class="btn success" @click="publishConfirm">确认人工发布</button>
            <button v-if="can.backfill(current.status)" class="btn primary" @click="backfillOne">立即回流效果数据</button>
            <button v-if="can.editable(current.status)" class="btn" @click="editMode = true">在线编辑</button>
            <input v-if="can.editable(current.status)" type="text" class="grow" v-model="rejectReason"
              placeholder="驳回原因(必填)" style="min-width:170px;" />
            <button v-if="can.editable(current.status)" class="btn danger" @click="rejectOne">驳回</button>
            <button v-if="can.clean(current.status)" class="btn danger" @click="cleanOne">清理删除</button>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.refs-card { border-left: 4px solid var(--primary); }
.refs-block { border-top: 1px dashed var(--border); padding-top: 10px; margin-top: 10px; }
.refs-block:first-of-type { border-top: 0; padding-top: 6px; }
.refs-title { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.refs-summary {
  font-size: 12px; color: var(--text-sub); margin-top: 6px;
  background: #fafaf8; border-radius: 6px; padding: 8px 10px; line-height: 1.7;
}
.related-item { padding: 6px 0; border-bottom: 1px dotted var(--border); font-size: 13px; }
.related-item:last-child { border-bottom: 0; }
.material {
  margin-top: 8px; max-height: 320px; overflow: auto; white-space: pre-wrap;
  font-size: 12px; line-height: 1.7; background: #fafaf8;
  border: 1px solid var(--border); border-radius: 6px; padding: 10px;
}

/* ---- 公众号推送 ---- */
.push-card { border-left: 4px solid #07c160; }
/* 封面预览:左侧按 2.35:1 展示,右侧放按钮与说明 */
.cover-box { display: flex; gap: 14px; margin-top: 12px; align-items: flex-start; }
.cover-frame {
  width: 240px; flex: none; aspect-ratio: 900 / 383; border-radius: 8px;
  overflow: hidden; background: #f2f4f7; border: 1px solid var(--border);
  display: flex; align-items: center; justify-content: center;
}
.cover-frame img { width: 100%; height: 100%; object-fit: cover; display: block; }
.cover-empty { color: var(--text-sub); font-size: 12px; }
.cover-side { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
/* 核心论点:全文最该被看到的一句 */
.thesis-box {
  border-left: 3px solid var(--primary);
  background: #f0f5fb;
  padding: 10px 12px;
  border-radius: 0 6px 6px 0;
  margin-top: 4px;
  font-size: 14px;
  font-weight: 600;
  line-height: 1.75;
}
.warn-text { color: var(--warn, #b7791f); }
.ok-text { color: var(--success, #0f6e56); }
/* 标题候选:一次给 3 个不同形态,让人挑 */
.title-cands {
  border: 1px dashed var(--border); border-radius: 8px;
  padding: 8px 10px; margin-bottom: 10px; background: #fbfaf7;
}
.title-cand { padding: 7px 0; border-bottom: 1px dotted var(--border); }
.title-cand:last-child { border-bottom: 0; padding-bottom: 0; }
.push-progress { margin-top: 12px; border-top: 1px dashed var(--border); padding-top: 10px; }
.steps { display: flex; align-items: stretch; gap: 5px; flex-wrap: wrap; margin-top: 10px; }
.step {
  display: flex; gap: 6px; align-items: flex-start; padding: 7px 10px;
  border: 1px solid var(--border); border-radius: 8px; background: #fafaf8; min-width: 120px;
}
.step .dot { font-weight: 700; font-size: 13px; color: var(--text-sub); }
.step.running { border-color: var(--primary); background: var(--primary-light); }
.step.running .dot { color: var(--primary); }
.step.success { border-color: #2e7d32; background: #eef7ee; }
.step.success .dot { color: #2e7d32; }
.step.failed { border-color: #d9534f; background: #fdeeee; }
.step.failed .dot { color: #d9534f; }
.step.skipped { opacity: 0.7; }
.step-name { font-size: 12px; font-weight: 600; }
.step-detail { font-size: 11px; color: var(--text-sub); margin-top: 1px; max-width: 190px; }
.arrow { align-self: center; color: var(--text-sub); font-size: 12px; }
.err-line { color: #b3261e; font-size: 12px; margin-top: 6px; word-break: break-all; }
.hint-line { color: #7a5c00; font-size: 12px; margin-top: 4px; }
.note-line { color: #1b5e20; font-size: 12px; margin-top: 6px; word-break: break-all; }
.planb { margin-top: 12px; border-top: 1px dashed var(--border); padding-top: 10px; }
.planb-preview {
  margin-top: 8px; max-height: 340px; overflow: auto; background: #fff;
  border: 1px solid var(--border); border-radius: 6px; padding: 14px 16px;
}
</style>
