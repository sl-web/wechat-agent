<script setup>
// 数据回流看板:追踪**本系统发布**文章的反馈数据。
//
// 数据源是公众号后台「发表记录」页内联的指标 —— 见 app/pusher/mp_api.py 的 STAT_MAP。
// 范围 = **本系统已确认发布**的文章(状态 published/backfilled + 有发布时间),
// 由后端 `_published_articles()` 定义 —— 这就是需求里"只追踪通过本系统发布的文章"。
import { ref, computed, onMounted, onActivated } from 'vue'
import { api } from '../api.js'
import { toast } from '../toast.js'
import RangePicker from '../components/RangePicker.vue'

const range = ref('all')          // 发文不频繁,默认看全量更实用
const data = ref(null)
const loading = ref(false)
const refreshing = ref(false)
const expanded = ref(0)           // 展开哪一篇看历史趋势

const METRICS = [
  { k: 'read_count', t: '阅读', cls: 'blue' },
  { k: 'like_count', t: '点赞', cls: 'red' },
  { k: 'share_count', t: '分享', cls: 'amber' },
  { k: 'recommend_count', t: '推荐', cls: 'green' },
  { k: 'comment_count', t: '留言', cls: '' },
]
// 后台还有「划线人数」,但它不在「发表记录」页里,要逐篇打开详情页拿 ——
// 暂不纳入自动抓取(没必要为它多开几十次请求),故不显示这一列。

const summary = computed(() => (data.value && data.value.summary) || {})
const rows = computed(() => (data.value && data.value.rows) || [])
const totals = computed(() => summary.value.totals || {})
const maxRead = computed(() => Math.max(1, ...rows.value.map((r) => (r.latest && r.latest.read_count) || 0)))
// 真正"后台找不到"的 = 没回填链接 且 连指标都没有;有指标的是"待核对"
const missingReal = computed(() => Math.max(
  0, (summary.value.not_in_mp || 0) - (summary.value.pending_check || 0)))

async function load() {
  loading.value = true
  try {
    data.value = await api.get(`/backfill/stats?range=${range.value}`)
  } catch (e) {
    toast('加载回流数据失败: ' + e.message, 'err')
  } finally {
    loading.value = false
  }
}

async function refresh() {
  refreshing.value = true
  toast('正在从公众号后台同步发布状态并抓取最新数据…')
  try {
    // 真抓后台,给足时间。后端会**先反查发布状态、再抓指标**(一个动作做完两件事),
    // 否则会出现"有反馈数据、却标着未找到"的矛盾行。
    const r = await api.post('/backfill/refresh', {}, 120000)
    if (r && r.error) {
      toast(r.error + (r.hint ? ' | ' + r.hint : ''), 'err')
    } else {
      const n = (r && r.backfilled) || 0
      const sk = (r && r.skipped) || []
      const matched = (r && r.sync && r.sync.matched) || 0
      toast(`已回流 ${n} 篇`
        + (matched ? `,同步发布状态 ${matched} 篇` : '')
        + (sk.length ? `,跳过 ${sk.length} 篇(后台没有)` : ''))
    }
    await load()
  } catch (e) {
    toast('刷新失败: ' + e.message, 'err')
  } finally {
    refreshing.value = false
  }
}

function fmt(v) { return v === null || v === undefined || v === '' ? '—' : v }

function barWidth(v) {
  return Math.max(2, Math.round(((v || 0) / maxRead.value) * 100)) + '%'
}

function toggle(id) { expanded.value = expanded.value === id ? 0 : id }

onMounted(load)
// KeepAlive:切走再回来刷新一次数据(这一页没有用户输入,刷新不会丢东西)
let entered = false
onActivated(() => { if (entered) load(); entered = true })
</script>

<template>
  <div class="page">
    <div class="page-title">
      数据回流
      <button class="btn" @click="load" :disabled="loading">{{ loading ? '加载中…' : '刷新列表' }}</button>
      <button class="btn primary" @click="refresh" :disabled="refreshing">
        {{ refreshing ? '抓取中…' : '立即抓取最新数据' }}
      </button>
    </div>

    <div class="card" style="padding:12px 16px;">
      <RangePicker v-model="range" @update:modelValue="load"
        :hint="`共追踪 ${summary.published_total || 0} 篇` + (summary.last_fetched_at ? ` · 上次抓取 ${summary.last_fetched_at}` : '')" />
    </div>

    <!-- 汇总 -->
    <div class="grid grid-4" style="margin-bottom:16px;">
      <div class="stat-card blue">
        <div class="num">{{ summary.published_total || 0 }}</div>
        <div class="label">已发布(本系统)</div>
        <div class="click-hint">
          其中 {{ summary.with_stats || 0 }} 篇已有反馈数据
          <span v-if="summary.pending_check">· {{ summary.pending_check }} 篇待核对</span>
          <span v-if="missingReal">· {{ missingReal }} 篇后台未找到</span>
        </div>
      </div>
      <div class="stat-card green">
        <div class="num">{{ totals.read_count || 0 }}</div>
        <div class="label">总阅读人数</div>
      </div>
      <div class="stat-card red">
        <div class="num">{{ totals.like_count || 0 }}</div>
        <div class="label">总点赞人数</div>
      </div>
      <div class="stat-card amber">
        <div class="num">{{ (totals.share_count || 0) + (totals.recommend_count || 0) }}</div>
        <div class="label">总分享 + 推荐</div>
      </div>
    </div>

    <div class="card">
      <div class="row between" style="align-items:flex-start;">
        <h3 style="margin:0;">各篇文章反馈</h3>
        <span class="meta">数据来自公众号后台「发表记录」;发布当天的数据通常要到次日中午才稳定。点某一行可展开看趋势。</span>
      </div>

      <div v-if="!loading && !rows.length" class="empty">
        还没有可追踪的文章<br />
        <span class="meta">先到「文章库」点「同步发布状态」,把已发表的文章认回来</span>
      </div>

      <table v-else style="margin-top:12px;">
        <thead>
          <tr>
            <th>ID</th><th>标题</th><th>发表时间</th><th>群发</th><th>后台核对</th>
            <th v-for="m in METRICS" :key="m.k">{{ m.t }}</th>
            <th>统计日</th><th>原文</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="r in rows" :key="r.id">
            <tr style="cursor:pointer;" @click="toggle(r.id)">
              <td>{{ r.id }}</td>
              <td style="max-width:260px;">{{ r.title }}</td>
              <td class="meta" style="white-space:nowrap;">{{ (r.published_at || '').slice(0, 16) }}</td>
              <td>
                <span v-if="r.in_mp" class="badge" :class="r.mass_sent ? 'amber' : 'gray'">
                  {{ r.mass_sent ? '已群发' : '未群发' }}
                </span>
                <span v-else class="meta">—</span>
              </td>
              <td>
                <span v-if="r.in_mp" class="badge green" title="已在公众号后台「发表记录」里找到">已确认</span>
                <!-- 有反馈数据 = 抓取时明明在后台找到了它,只是还没反查回链接。
                     单独标出来,避免和"真的找不到"混为一谈。点「立即抓取最新数据」即可补齐。 -->
                <span v-else-if="r.latest" class="badge amber"
                      title="已抓到反馈数据,说明后台有这篇;但还没回填公开链接。点「立即抓取最新数据」即可同步。">
                  待核对
                </span>
                <span v-else class="badge red"
                      title="本地记为已发布,但公众号后台的发表记录里找不到它 —— 可能未真正发表、已被删除,或标题被改过。建议核对后处理。">
                  未找到
                </span>
              </td>
              <td v-for="m in METRICS" :key="m.k">
                <span v-if="r.latest">{{ fmt(r.latest[m.k]) }}</span>
                <span v-else class="meta">—</span>
              </td>
              <td class="meta">{{ r.stat_date || '—' }}</td>
              <td>
                <a v-if="r.published_url" :href="r.published_url" target="_blank" @click.stop>打开</a>
                <span v-else class="meta">—</span>
              </td>
            </tr>
            <tr v-if="expanded === r.id">
              <td colspan="13" style="background:#faf9f5;">
                <div class="row" style="gap:14px; align-items:flex-start;">
                  <div class="grow">
                    <div class="meta" style="margin-bottom:6px;">阅读量对比(相对最高的一篇)</div>
                    <div class="bar-row">
                      <span class="bar-label">阅读</span>
                      <div class="bar"><div class="bar-fill" :style="{ width: barWidth(r.latest && r.latest.read_count) }"></div></div>
                      <span class="bar-num">{{ fmt(r.latest && r.latest.read_count) }}</span>
                    </div>
                  </div>
                </div>
                <div v-if="r.history && r.history.length > 1" style="margin-top:10px;">
                  <div class="meta" style="margin-bottom:4px;">历史趋势({{ r.history.length }} 次抓取)</div>
                  <table style="width:auto;">
                    <thead>
                      <tr><th>日期</th><th v-for="m in METRICS" :key="m.k">{{ m.t }}</th></tr>
                    </thead>
                    <tbody>
                      <tr v-for="h in r.history" :key="h.stat_date">
                        <td>{{ h.stat_date }}</td>
                        <td v-for="m in METRICS" :key="m.k">{{ fmt(h[m.k]) }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <div v-else class="meta" style="margin-top:10px;">
                  目前只有一次抓取记录,连续几天回流后这里会显示趋势
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.bar-row { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }
.bar-label { width: 34px; font-size: 12px; color: var(--text-sub); }
.bar { flex: 1; max-width: 420px; height: 14px; background: #ecebe4; border-radius: 7px; overflow: hidden; }
.bar-fill { height: 100%; background: var(--primary); border-radius: 7px; }
.bar-num { font-size: 12px; color: var(--text-sub); min-width: 34px; }
</style>
