<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import ToastHost from './components/ToastHost.vue'
import DashboardView from './views/DashboardView.vue'
import AnalyzeView from './views/AnalyzeView.vue'
import ArticlesView from './views/ArticlesView.vue'
import ReviewView from './views/ReviewView.vue'
import MonitorView from './views/MonitorView.vue'
import PanelView from './views/PanelView.vue'
import BackfillView from './views/BackfillView.vue'
import ArticlePage from './views/ArticlePage.vue'
import { STEP_LABEL, attachPipeline, onPipelineDone } from './pipeline.js'
import { toast } from './toast.js'

const views = [
  { key: 'dashboard', label: '运营看板', comp: DashboardView },
  { key: 'analyze', label: '分析报告', comp: AnalyzeView },
  { key: 'articles', label: '文章库', comp: ArticlesView },
  { key: 'backfill', label: '数据回流', comp: BackfillView },
  { key: 'review', label: '人工审核', comp: ReviewView },
  { key: 'monitor', label: '运行监控', comp: MonitorView },
  { key: 'panel', label: '控制面板', comp: PanelView },
]

// hash 路由解析:支持 #/article/{id} 独立窗口
function parseHash() {
  const raw = location.hash.replace('#/', '')
  const m = /^article\/(\d+)$/.exec(raw)
  if (m) return { kind: 'article', articleId: Number(m[1]) }
  return { kind: 'tab', key: raw }
}

const route = ref(parseHash())
// 直接从路由派生:写成独立 ref 的话,「刷新 / 直接打开 #/article/24」时它会停在初始值 0
// (只有 hashchange 才会更新),页面显示「文章处理 #0」且加载不出内容。
const articleId = computed(() => (route.value.kind === 'article' ? route.value.articleId : 0))

const current = computed(() => {
  if (route.value.kind === 'article') {
    return { key: 'article-' + articleId.value, comp: ArticlePage, articleId: articleId.value }
  }
  return views.find((v) => v.key === route.value.key) || views[0]
})

const activeKey = computed(() => {
  // 文章独立窗口时不高亮任何 tab(顶栏显示为「文章处理窗口」)
  return route.value.kind === 'article' ? '' : current.value.key
})

function go(key) {
  location.hash = `#/${key}`
}

// 打开文章独立窗口,记录来源 tab 供返回
function openArticle(id, fromKey) {
  sessionStorage.setItem('articleBackTo', '#/' + (fromKey || route.value.key || 'review'))
  location.hash = '#/article/' + id
}

// ---- 任务状态:全局接管 ----
// 抓取/分析/生成可能长达几分钟,而切标签会卸载视图组件。这里在应用级
// 挂一次全局轮询与完成通知,所以:①切到任何标签都能收到完成提示;
// ②刷新页面后如果后端还在跑,也能自动接着显示进度。
let offDone = null

onMounted(async () => {
  window.addEventListener('hashchange', () => {
    route.value = parseHash()
    window.scrollTo(0, 0)
  })
  await attachPipeline()
  offDone = onPipelineDone((p) => {
    const label = (p && STEP_LABEL[p.stage]) || (p && p.stage) || '任务'
    const res = (p && p.result) || {}
    if (p && p.ok) {
      toast(res.article_id ? `${label}完成:文章 #${res.article_id}` : `${label}完成`)
    } else {
      toast(`${label}失败:${(p && p.error) || '未知原因'}`, 'err')
    }
  })
})
onUnmounted(() => { if (offDone) offDone() })

// 提供全局打开文章的辅助(子组件可通过 inject 或 hash 直接跳)
window.__openArticle = openArticle
</script>

<template>
  <header class="topbar">
    <span class="brand">公众号文章 Agent</span>
    <nav v-if="route.kind !== 'article'">
      <a
        v-for="v in views"
        :key="v.key"
        class="tab"
        :class="{ active: activeKey === v.key }"
        href="#"
        @click.prevent="go(v.key)"
      >{{ v.label }}</a>
    </nav>
    <nav v-else>
      <span class="tab active">文章处理窗口</span>
    </nav>
    <span class="spacer"></span>
    <span class="hint">机器采编排 · 人工审发</span>
  </header>

  <main>
    <!-- ⚠️ 必须套 <KeepAlive>:切标签只是**隐藏**,不能销毁视图。
         否则用户在某个标签里做的事——勾好的素材、生成结果、没保存的参数表单、
         编辑到一半的模板 JSON、审核台的选择——都会随组件卸载一起消失
         (实测反馈:"切换标签,用户执行动作的渲染就没了")。
         代价与约定:①视图的 onUnmounted **不再**因切标签触发,清理要用 onDeactivated;
         ②"回到标签要刷新数据"改用 onActivated,并且**只刷服务端列表、别覆盖用户输入**。 -->
    <KeepAlive>
      <component :is="current.comp" :key="current.key" v-bind="route.kind === 'article' ? { articleId: articleId } : {}" />
    </KeepAlive>
  </main>

  <ToastHost />
</template>

<style scoped>
/* 顶栏标签统一外观:选中态与未选中态必须明显区分。
   .topbar nav a 只覆盖 <a>,文章窗口用的是 <span>,故统一成 .tab。 */
.tab {
  display: inline-block;
  padding: 6px 14px;
  border-radius: 7px;
  color: var(--text-sub);
  font-size: 13px;
  white-space: nowrap;
}
.tab:hover:not(.active) {
  background: var(--primary-light);
  color: var(--primary);
}
.tab.active {
  background: var(--primary);
  color: #fff;
  font-weight: 600;
}
</style>
