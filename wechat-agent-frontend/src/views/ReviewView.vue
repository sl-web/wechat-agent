<script setup>
import { ref, onMounted, onActivated, computed } from 'vue'
import { api } from '../api.js'
import { toast } from '../toast.js'
import { canDelete, useBulk } from '../bulk.js'

// 分组口径与运营看板一致:
//   待审核 = generated/edited   | 待推送 = approved
//   已推送待发布 = pushed       | 终结/异常 = rejected/generation_failed
const tabs = [
  { key: 'generated,edited', label: '待审核(已生成/已编辑)' },
  { key: 'approved', label: '待推送(已通过审核)' },
  { key: 'pushed', label: '已推送待发布' },
  { key: 'rejected,generation_failed', label: '已驳回/生成失败' },
]
const tab = ref('generated,edited')
const list = ref([])
const loading = ref(false)

const { picked, busy, has, toggle, setAll, clear, prune, run } = useBulk()

const allChecked = computed(() => list.value.length > 0 && picked.value.length === list.value.length)
const undeletablePicked = computed(() => list.value.filter((r) => has(r.id) && !canDelete(r)).length)
// 当前分组是否已是"终结态":此时「废弃」没意义(本来就已驳回/失败),按钮置灰避免误点
const isTerminalTab = computed(() => tab.value === 'rejected,generation_failed')

async function load() {
  loading.value = true
  try {
    list.value = await api.get(`/review/queue?status=${encodeURIComponent(tab.value)}`)
    prune(list.value)
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    loading.value = false
  }
}
function switchTab(k) {
  tab.value = k
  clear()
  load()
}

// 打开文章独立处理窗口
function openInWindow(aid) {
  sessionStorage.setItem('articleBackTo', '#/review')
  location.hash = '#/article/' + aid
}

function statusBadge(s) {
  const map = {
    generated: ['blue', '已生成'], pushed: ['amber', '已推送'], pending_review: ['amber', '待审核'],
    edited: ['blue', '已编辑'], approved: ['green', '已通过'], rejected: ['red', '已驳回'],
    published: ['green', '已发布'], backfilled: ['green', '已回流'], generation_failed: ['red', '生成失败'],
  }
  const [cls, label] = map[s] || ['gray', s]
  return `<span class="badge ${cls}">${label}</span>`
}

// ---- 废弃 / 删除 ----
function discardOne(it) {
  if (!confirm(`确认废弃文章 #${it.id}「${it.title}」?\n\n状态将变为「已驳回」,数据保留、可恢复。`)) return
  run('discard', [it.id], '废弃').then(load)
}
function discardPicked() {
  if (!confirm(`确认废弃选中的 ${picked.value.length} 篇文章?\n\n状态将变为「已驳回」,数据保留、可恢复。`)) return
  run('discard', null, '废弃').then(load)
}
function cleanOne(it) {
  if (!canDelete(it)) return
  if (!confirm(`确认永久删除文章 #${it.id}「${it.title}」?\n\n⚠️ 此操作不可恢复。`)) return
  run('clean', [it.id], '删除').then(load)
}
function cleanPicked() {
  const warn = undeletablePicked.value
    ? `\n\n注意:其中 ${undeletablePicked.value} 条已发布/已回流,会被自动跳过(需走合规下架流程)。`
    : ''
  if (!confirm(`确认永久删除选中的 ${picked.value.length} 篇稿件?\n\n⚠️ 此操作不可恢复。${warn}`)) return
  run('clean', null, '删除').then(load)
}

/** 每次"进入本页"都要做的事:刷新队列 + 处理跨视图跳转请求 */
function enter() {
  load()
  // 跨视图跳转(旧机制兼容):运营看板/监控点"处理"时到本页
  const aid = sessionStorage.getItem('reviewOpenAid')
  if (aid) {
    sessionStorage.removeItem('reviewOpenAid')
    openInWindow(Number(aid))
  }
}
onMounted(enter)
// ⚠️ KeepAlive 之后 onMounted 只会跑一次,而"从看板点处理跳过来"和新生成的稿子
//    都要靠**再次进入本页**才能看到 —— 所以这里必须补一个 onActivated(load 会 prune
//    掉已不存在的勾选,不会清掉有效的选择)。
let entered = false
onActivated(() => { if (entered) enter(); entered = true })
</script>

<template>
  <div class="page">
    <div class="page-title">
      人工审核操作台
      <button class="btn" @click="load" :disabled="loading">{{ loading ? '加载中…' : '刷新' }}</button>
    </div>

    <div class="card">
      <div class="row between">
        <div class="row wrap">
          <button v-for="t in tabs" :key="t.key" class="btn sm" :class="{ primary: tab === t.key }"
            @click="switchTab(t.key)">
            {{ t.label }}
          </button>
        </div>
      </div>

      <!-- 批量操作条 -->
      <div v-if="picked.length" class="bulk-bar">
        <div class="row between wrap">
          <span>
            已选 <strong>{{ picked.length }}</strong> 条
            <span v-if="undeletablePicked" class="meta">
              · 其中 {{ undeletablePicked }} 条已发布/已回流,删除时会跳过
            </span>
          </span>
          <div class="row wrap">
            <button class="btn sm" :disabled="busy || isTerminalTab"
              :title="isTerminalTab ? '当前分组已经是终结态,无需再废弃' : ''"
              @click="discardPicked">批量废弃</button>
            <button class="btn sm danger" :disabled="busy" @click="cleanPicked">批量删除</button>
            <button class="btn sm" :disabled="busy" @click="clear()">清空选择</button>
          </div>
        </div>
      </div>

      <table style="margin-top: 12px;">
        <thead>
          <tr>
            <th style="width:34px;">
              <input type="checkbox" :checked="allChecked" title="全选本页"
                @change="setAll(list, $event.target.checked)" />
            </th>
            <th>ID</th><th>标题</th><th>来源热点</th><th>状态</th><th>创建时间</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="it in list" :key="it.id" :class="{ 'row-picked': has(it.id) }">
            <td><input type="checkbox" :checked="has(it.id)" @change="toggle(it.id)" /></td>
            <td>#{{ it.id }}</td>
            <td>{{ it.title }}</td>
            <td class="meta">{{ it.topic_title || '—' }}</td>
            <td v-html="statusBadge(it.status)"></td>
            <td class="meta">{{ it.created_at }}</td>
            <td>
              <div class="row">
                <button class="btn sm primary" @click="openInWindow(it.id)">处理 →</button>
                <button class="btn sm" :disabled="busy || isTerminalTab"
                  :title="isTerminalTab ? '当前分组已经是终结态,无需再废弃' : ''"
                  @click="discardOne(it)">废弃</button>
                <button class="btn sm danger" :disabled="busy || !canDelete(it)"
                  :title="canDelete(it) ? '永久删除(不可恢复)' : '已发布/已回流稿件不可删除,请走合规下架流程'"
                  @click="cleanOne(it)">删除</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!loading && !list.length" class="empty">当前列表无稿件</div>
      <div class="meta mt8">
        点击「处理」将打开独立的文章处理窗口(全文阅读 + 通过/编辑/驳回/推送/发布/回流等操作)。
        <strong>废弃</strong>可恢复;<strong>删除</strong>为物理清除且仅限未发布稿件。
      </div>
    </div>
  </div>
</template>

<style scoped>
.bulk-bar {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--primary-light);
  border-left: 3px solid var(--primary);
}
tr.row-picked { background: var(--primary-light); }
</style>
