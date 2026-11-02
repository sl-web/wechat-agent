<script setup>
import { ref, onMounted, onActivated } from 'vue'
import { api } from '../api.js'
import { toast } from '../toast.js'

const data = ref(null)
const stale = ref([])
const loading = ref(false)

const runDetail = ref(null)

async function load() {
  loading.value = true
  try {
    data.value = await api.get('/dashboard')
    // 不传 hours:阈值由后端按配置 `review_stale_hours` 决定(写死 24 会让面板那个设置失效)
    stale.value = await api.get('/dashboard/stale')
  } catch (e) {
    toast('加载失败: ' + e.message, 'err')
  } finally {
    loading.value = false
  }
}

function fmt(n) { return n == null ? 0 : n }

// 从运行记录 detail 提取文章 id
function runArticleId(detail) {
  const m = /文章 #(\d+)/.exec(detail || '')
  return m ? Number(m[1]) : null
}
function showRunDetail(rr) { runDetail.value = rr }
function closeRunDetail() { runDetail.value = null }
function goArticle() {
  const aid = runDetail.value ? runArticleId(runDetail.value.detail) : null
  if (!aid) return
  sessionStorage.setItem('articleBackTo', '#/monitor')
  location.hash = '#/article/' + aid
  closeRunDetail()
}
function goHandle(sid) {
  sessionStorage.setItem('articleBackTo', '#/monitor')
  location.hash = '#/article/' + sid
}

onMounted(load)
// KeepAlive:监控页回到前台就刷新一次(全是服务端状态,没有用户输入)
let entered = false
onActivated(() => { if (entered) load(); entered = true })
</script>

<template>
  <div class="page">
    <div class="page-title">
      运行监控
      <button class="btn" @click="load" :disabled="loading">{{ loading ? '加载中…' : '刷新' }}</button>
    </div>

    <!-- 最近运行记录 -->
    <div class="card">
      <h3>最近运行记录</h3>
      <div v-if="data && data.recent_runs && data.recent_runs.length">
        <div v-for="rr in data.recent_runs" :key="rr.id" class="row between run-row"
          style="padding: 8px 0; border-bottom: 1px solid #e0ded6;" @click="showRunDetail(rr)">
          <span class="row" style="flex:1; min-width:0;">
            <span class="badge" :class="rr.status === 'success' ? 'green' : 'red'">{{ rr.status === 'success' ? '成功' : '失败' }}</span>
            <strong style="min-width:64px;">{{ rr.stage }}</strong>
            <span class="meta" style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">{{ rr.detail }}</span>
          </span>
          <span class="meta" style="white-space:nowrap;">{{ rr.started_at }} ›</span>
        </div>
      </div>
      <div v-else class="empty">暂无运行记录(可在运营看板触发流水线)</div>
      <div class="meta mt8">点击任意一条可查看执行详情</div>
    </div>

    <!-- 效果数据 -->
    <div class="card">
      <h3>效果数据 Top5</h3>
      <div v-if="data && data.top_articles && data.top_articles.length">
        <table>
          <thead><tr><th>标题</th><th>阅读</th><th>点赞</th><th>转发</th></tr></thead>
          <tbody>
            <tr v-for="a in data.top_articles" :key="a.id">
              <td>{{ a.title }}</td>
              <td>{{ fmt(a.read_count) }}</td>
              <td>{{ fmt(a.like_count) }}</td>
              <td>{{ fmt(a.share_count) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="empty">暂无回流数据(发布文章后第 1/3/7 天自动回流,也可在审核台手动回流)</div>
    </div>

    <!-- 审核滞留告警 -->
    <div class="card">
      <h3>审核滞留告警(超 24h 未处理)</h3>
      <div v-if="stale.length">
        <table>
          <thead><tr><th>ID</th><th>标题</th><th>创建时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="s in stale" :key="s.id">
              <td>#{{ s.id }}</td>
              <td>{{ s.title }}</td>
              <td class="meta">{{ s.created_at }}</td>
              <td><button class="btn sm" @click="goHandle(s.id)">去处理</button></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="empty">无滞留稿件,全部待办都在时限内</div>
    </div>

    <!-- 运行详情弹层 -->
    <div v-if="runDetail" class="modal-mask" @click.self="closeRunDetail">
      <div class="modal">
        <div class="row between">
          <h3 style="margin:0;">运行结果详情
            <span class="badge" :class="runDetail.status === 'success' ? 'green' : 'red'"
              style="margin-left:8px;">{{ runDetail.status === 'success' ? '成功' : '失败' }}</span>
          </h3>
          <button class="btn sm" @click="closeRunDetail">关闭</button>
        </div>
        <div class="meta-row" style="margin-top:10px;">
          环节: <strong>{{ runDetail.stage }}</strong> &nbsp;·&nbsp; 时间: {{ runDetail.started_at }}
        </div>
        <div v-if="runDetail.run_id" class="meta">run_id: {{ runDetail.run_id }}</div>
        <div class="note mt8" style="white-space:pre-wrap;">{{ runDetail.detail }}</div>
        <div v-if="runArticleId(runDetail.detail)" class="row mt16">
          <button class="btn primary sm" @click="goArticle">在审核台查看该文章全文 ›</button>
        </div>
      </div>
    </div>
  </div>
</template>
