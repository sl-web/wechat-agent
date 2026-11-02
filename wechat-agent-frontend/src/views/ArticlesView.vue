<script setup>
import { ref, onMounted, onActivated, computed } from 'vue'
import { api } from '../api.js'
import { toast } from '../toast.js'
import { canDelete, useBulk } from '../bulk.js'
import RangePicker from '../components/RangePicker.vue'

const loading = ref(false)
const lib = ref(null)
const curFilter = ref('all')
// 时间范围:默认**当天**(每天信息都会累积,不筛选的话列表会越来越长)
const range = ref('today')

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

// 解构出来用:嵌套在对象里的 ref 在模板中**不会自动解包**,
// 写成 bulk.picked.length 会静默失效(取到的是 ref 对象)。
const { picked, busy, has, toggle, setAll, clear, prune, run } = useBulk()

const rows = computed(() => (lib.value && lib.value.rows) || [])
const allChecked = computed(() => rows.value.length > 0 && picked.value.length === rows.value.length)
// 选中项里有几条是"删不掉"的(已发布/已回流)—— 批量删除前要如实告知,而不是点了才发现被跳过
const undeletablePicked = computed(() => rows.value.filter((r) => has(r.id) && !canDelete(r)).length)

async function load() {
  loading.value = true
  try {
    lib.value = await api.get(
      `/review/articles?status=${curFilter.value}&range=${range.value}&limit=500`)
    prune(rows.value)               // 刷新后清掉已不存在的选中项
  } catch (e) {
    toast('加载失败: ' + e.message, 'err')
  } finally {
    loading.value = false
  }
}
function setFilter(s) {
  curFilter.value = s
  clear()                           // 换筛选条件时选择无意义,直接清空
  load()
}
function onRangeChange() {
  clear()                           // 换时间范围同理,选择也失效
  load()
}

// 反查发布状态:文章发出去之后系统本来不知道(一直停在"已推送"),
// 这个动作去公众号后台的「发表记录」确认哪些真的发出去了,并回填发表时间/公开链接/是否群发。
const syncing = ref(false)
async function syncPublished() {
  syncing.value = true
  try {
    const r = await api.post('/backfill/sync-published', {}, 180000)
    if (!r || r.ok === false) {
      toast('反查失败: ' + ((r && r.error) || '未知原因'), 'err')
      return
    }
    const bits = [`匹配上 ${r.matched} 篇`]
    if (r.by_draft) bits.push(`精确 ${r.by_draft}`)
    if (r.by_title) bits.push(`标题 ${r.by_title}`)
    toast(`反查完成:${bits.join(' / ')}`)
    if (r.missing_in_mp && r.missing_in_mp.length) {
      toast(`有 ${r.missing_in_mp.length} 篇本地标记已发布、但后台记录里找不到,建议人工核对`, 'err')
    }
    await load()
  } catch (e) {
    toast('反查失败: ' + e.message, 'err')
  } finally {
    syncing.value = false
  }
}

// 打开文章独立处理窗口
function openInWindow(aid) {
  sessionStorage.setItem('articleBackTo', '#/articles')
  location.hash = '#/article/' + aid
}

// ---- 废弃(可恢复) ----
function discardOne(it) {
  if (!confirm(`确认废弃文章 #${it.id}「${it.title}」?\n\n状态将变为「已驳回」,数据保留、可恢复。`)) return
  run('discard', [it.id], '废弃').then(load)
}
function discardPicked() {
  const n = picked.value.length
  if (!confirm(`确认废弃选中的 ${n} 篇文章?\n\n状态将变为「已驳回」,数据保留、可恢复。`)) return
  run('discard', null, '废弃').then(load)
}

// ---- 删除(物理,仅未发布) ----
function cleanOne(it) {
  if (!canDelete(it)) return
  if (!confirm(`确认永久删除文章 #${it.id}「${it.title}」?\n\n⚠️ 此操作不可恢复。`)) return
  run('clean', [it.id], '删除').then(load)
}
function cleanPicked() {
  const n = picked.value.length
  const warn = undeletablePicked.value
    ? `\n\n注意:其中 ${undeletablePicked.value} 条已发布/已回流,会被自动跳过(需走合规下架流程)。`
    : ''
  if (!confirm(`确认永久删除选中的 ${n} 篇稿件?\n\n⚠️ 此操作不可恢复。${warn}`)) return
  run('clean', null, '删除').then(load)
}

onMounted(load)

// KeepAlive:切走再回来只刷新列表(筛选条件在 ref 里、勾选会被 prune 掉已不存在的项),
// 不会清掉用户在这一页做的事。首次进入由 onMounted 负责,别重复请求。
let entered = false
onActivated(() => { if (entered) load(); entered = true })
</script>

<template>
  <div class="page">
    <div class="page-title">
      文章库(全部状态)
      <button class="btn" @click="syncPublished" :disabled="syncing">
        {{ syncing ? '反查中…' : '同步发布状态' }}
      </button>
      <button class="btn" @click="load" :disabled="loading">{{ loading ? '加载中…' : '刷新' }}</button>
    </div>

    <!-- 时间范围 + 状态筛选 -->
    <div class="card" style="padding: 12px 16px;">
      <RangePicker v-model="range" @update:modelValue="onRangeChange" />
      <div class="row wrap" style="margin-top:10px;">
        <button class="chip" :class="{ active: curFilter === 'all' }" @click="setFilter('all')">
          <!-- 用 all_total(库里总数);不能用 total —— 那是"当前筛选命中的条数",
               选中某个状态后「全部」会跟着变成该状态的数量 -->
          全部<span class="chip-n">({{ (lib && lib.all_total) || 0 }})</span>
        </button>
        <button v-for="m in STATUS_META" :key="m.key"
          v-show="lib && lib.status_counts && lib.status_counts[m.key]"
          class="chip" :class="{ active: curFilter === m.key }" @click="setFilter(m.key)">
          <span class="badge" :class="m.cls">{{ m.label }}</span>
          <span class="chip-n">({{ (lib && lib.status_counts && lib.status_counts[m.key]) || 0 }})</span>
        </button>
      </div>
    </div>

    <!-- 批量操作条:有选中才出现,避免平时占地方 -->
    <div v-if="picked.length" class="card bulk-bar">
      <div class="row between wrap">
        <span>
          已选 <strong>{{ picked.length }}</strong> 条
          <span v-if="undeletablePicked" class="meta">
            · 其中 {{ undeletablePicked }} 条已发布/已回流,删除时会跳过
          </span>
        </span>
        <div class="row wrap">
          <button class="btn sm" :disabled="busy" @click="discardPicked">批量废弃</button>
          <button class="btn sm danger" :disabled="busy" @click="cleanPicked">批量删除</button>
          <button class="btn sm" :disabled="busy" @click="clear()">清空选择</button>
        </div>
      </div>
    </div>

    <div class="card">
      <table>
        <thead>
          <tr>
            <th style="width:34px;">
              <input type="checkbox" :checked="allChecked" title="全选本页"
                @change="setAll(rows, $event.target.checked)" />
            </th>
            <th>ID</th><th>标题</th><th>来源热点</th><th>状态</th><th>群发/原文</th><th>参考素材</th>
            <th>发布/失败信息</th><th>创建时间</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="it in rows" :key="it.id" :class="{ 'row-picked': has(it.id) }">
            <td><input type="checkbox" :checked="has(it.id)" @change="toggle(it.id)" /></td>
            <td>#{{ it.id }}</td>
            <td style="max-width:340px;">{{ it.title }}</td>
            <td class="meta" style="max-width:180px;">{{ it.topic_title || '—' }}</td>
            <td><span class="badge" :class="clsOf(it.status)">{{ labelOf(it.status) }}</span></td>
            <td>
              <!-- 反查后台发表记录得到的结果:群发过 / 仅发表(未通知)。未知就不显示,别瞎标 -->
              <span v-if="it.mp_mass_sent === 1" class="badge amber">已群发</span>
              <span v-else-if="it.mp_mass_sent === 0" class="badge gray">未群发</span>
              <a v-if="it.published_url" :href="it.published_url" target="_blank"
                 rel="noopener" class="meta" style="margin-left:4px;">原文 ↗</a>
              <span v-if="!it.published_url && it.mp_mass_sent == null" class="meta">—</span>
            </td>
            <td>
              <span v-if="it.ref_count" class="badge gray">{{ it.ref_count }} 条</span>
              <span v-else class="meta">—</span>
            </td>
            <td class="meta" style="max-width:150px;">
              <span v-if="it.status === 'rejected'">{{ it.reject_reason }}</span>
              <span v-else-if="it.status === 'generation_failed'">失败</span>
              <span v-else-if="it.published_at">{{ it.published_at }}</span>
              <span v-else>—</span>
            </td>
            <td class="meta">{{ it.created_at }}</td>
            <td>
              <div class="row">
                <button class="btn sm" @click="openInWindow(it.id)">查看/处理</button>
                <button class="btn sm" :disabled="busy" @click="discardOne(it)">废弃</button>
                <button class="btn sm danger" :disabled="busy || !canDelete(it)"
                  :title="canDelete(it) ? '永久删除(不可恢复)' : '已发布/已回流稿件不可删除,请走合规下架流程'"
                  @click="cleanOne(it)">删除</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!loading && !rows.length" class="empty">
        该时间范围内没有符合条件的文章 ——
        可以放宽时间范围(现在是「{{ (lib && lib.range_label) || '今天' }}」),
        或换个状态。历史文章并没有被删除。
      </div>
      <div class="meta mt8">
        <strong>废弃</strong>=状态改为「已驳回」,数据保留、可恢复;<strong>删除</strong>=物理清除,
        <strong>仅限未发布稿件</strong>(已发布/已回流走合规下架)。所有操作均写入审计日志。
      </div>
    </div>
  </div>
</template>

<style scoped>
.chip {
  border: 1px solid var(--border);
  background: #fff;
  padding: 5px 12px;
  border-radius: 18px;
  cursor: pointer;
  font-size: 13px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.chip:hover { border-color: var(--primary); }
.chip.active { border-color: var(--primary); background: var(--primary-light); }
.chip-n { color: var(--text-sub); font-size: 12px; }

/* 批量条吸顶,滚动长列表时仍能看到已选数量 */
.bulk-bar {
  position: sticky;
  top: 0;
  z-index: 5;
  padding: 10px 16px;
  border-left: 3px solid var(--primary);
}
tr.row-picked { background: var(--primary-light); }
</style>
