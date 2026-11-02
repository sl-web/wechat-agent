<script setup>
import { ref, onMounted, computed } from 'vue'
import { api } from '../api.js'
import { toast } from '../toast.js'

const cfg = ref({})
const loading = ref(true)
const templates = ref([])
// "生成时用哪个模板" —— 读配置里的 default_template(生成流水线真正读的就是它)。
// 以前面板上根本没有这个开关,新建的模板永远选不上 → 看起来"模板保存了却没作用"。
const defaultTpl = computed(() => cfg.value.default_template || 'tpl_ai_news_v1')

/**
 * 配置分区。
 *
 * ⚠️ 约定:任何进 config.py DEFAULTS 的新键**必须**加进这里 —— 可配置但不暴露 = 等于不可配置。
 * (原先是一维 groups 数组 + 哨兵 {section:...} 行,现在按分区分组,渲染成可折叠卡片。)
 */
const sections = [
  {
    key: 'runtime', label: '运行参数', hint: '抓取 · 评分 · 合规 · 告警',
    items: [
      { key: 'data_sources', label: '数据来源', kind: 'multi', options: [
        { v: 'rss', t: 'RSS 垂直媒体(默认·免费)' }, { v: 'wechat', t: '微信热文' }, { v: 'douyin', t: '抖音热点' }] },
      { key: 'keywords', label: '领域关键词(逗号分隔)', kind: 'csv' },
      { key: 'top_n', label: '热点池规模 top_n', kind: 'number' },
      // ⚠️ 下面三项目前**代码里没有任何地方读取**(已审计):RSS 源没有"按关键词查询"这一步,
      //    定时任务表是空的且没有界面可配,生成阶段一次只产 1 篇。
      //    保留输入框是为了记录意图,但标签必须说清它现在不生效 —— 不能让面板骗人。
      { key: 'per_keyword_limit', label: '每关键词抓取数(RSS 源不适用·暂未生效)', kind: 'number' },
      { key: 'fetch_interval_hours', label: '抓取间隔(小时)(定时任务未启用·暂未生效)', kind: 'number' },
      { key: 'normalize_method', label: '评分归一化方式', kind: 'select', options: [
        { v: 'percentile', t: '排名百分位' }, { v: 'log', t: '对数' }, { v: 'minmax', t: 'Min-Max' }] },
      { key: 'decay_half_life_hours', label: '时效半衰期(小时)', kind: 'number' },
      { key: 'retention_days', label: '合规存储期限(天)', kind: 'number' },
      { key: 'retry_limit', label: '熔断重试上限', kind: 'number' },
      { key: 'daily_articles', label: '每日生成篇数(暂未生效)', kind: 'number' },
      { key: 'backfill_enabled', label: '效果回流开关', kind: 'bool' },
      // 传播潜力:从账号自己的转发数据学「什么标题会被转」。公众号涨粉几乎全靠分享,
      // 而热度并不反映"会不会被转",所以单开一个维度。权重有界,别调太大。
      { key: 'virality_enabled', label: '传播潜力评分(用你自己的转发数据)', kind: 'bool' },
      { key: 'virality_weight', label: '传播潜力权重(建议 0.2~0.5,过高会压过热度)', kind: 'number' },
      { key: 'publish_window', label: '读者活跃时段(逗号分隔,如 07:00-09:00, 20:00-22:00)', kind: 'text' },
      { key: 'review_stale_hours', label: '审核滞留告警(小时)', kind: 'number' },
    ],
  },
  {
    key: 'stance', label: '写作立场', hint: '决定文章有没有观点',
    items: [
      { key: 'writer_persona', label: '作者身份 / 人设', kind: 'text' },
      { key: 'writer_stance', label: '写作立场(先问什么)', kind: 'text' },
      { key: 'writer_lens', label: '关注角度', kind: 'text' },
      { key: 'article_type', label: '论证姿态', kind: 'select', options: [
        { v: 'industry_judgment', t: '行业判断型(说明趋势与依据)' },
        { v: 'critical', t: '批判质疑型(挑战主流叙事)' },
        { v: 'teardown', t: '深度拆解型(拆开运转机制)' },
        { v: 'forecast', t: '趋势预测型(给可被检验的预测)' }] },
      { key: 'default_structure', label: '默认文章结构', kind: 'select', options: [
        { v: 's3', t: '论点驱动式 — 有观点(推荐)' },
        { v: 's1', t: '事件回顾式 — 偏综述' },
        { v: 's2', t: '盘点清单式' }] },
      { key: 'anti_recap_guard', label: '反综述护栏(不合格自动重写)', kind: 'bool' },
      // 转发钩子:强制 quote 可被单独转发、cta 给出转发/关注理由+留言引导、
      // body 含一处可收藏的干货。关掉等于只写字不传播。
      { key: 'share_hooks_enabled', label: '转发钩子(金句可单独转发 + 关注理由 + 留言引导)', kind: 'bool' },
    ],
  },
  {
    key: 'mp_push', label: '公众号草稿推送', hint: '风控相关',
    items: [
      { key: 'mp_daily_draft_limit', label: '每日推送上限(条)', kind: 'number' },
      { key: 'mp_interval_min', label: '推送最小间隔(秒)', kind: 'number' },
      { key: 'mp_interval_max', label: '推送最大间隔(秒)', kind: 'number' },
      { key: 'mp_author', label: '推送作者名', kind: 'text' },
      { key: 'mp_cover_image', label: '默认封面图路径', kind: 'text' },
      { key: 'mp_scan_wait_seconds', label: '扫码等待时长(秒)', kind: 'number' },
    ],
  },
  {
    key: 'layout', label: '正文排版', hint: '推送到公众号的视觉风格',
    items: [
      { key: 'mp_layout_theme', label: '排版主题', kind: 'select', options: [
        { v: 'tech', t: '科技蓝 — 色块标题' },
        { v: 'minimal', t: '简约浅色 — 居中标题' },
        { v: 'vivid', t: '高对比 — 编号徽章标题' }] },
      { key: 'mp_heading_style', label: '小节标题样式', kind: 'select', options: [
        { v: '', t: '跟随排版主题' },
        { v: 'bar', t: '色块标题(左侧色条)' },
        { v: 'badge', t: '编号徽章(①②③ 或不编号时自动序号)' },
        { v: 'center', t: '居中标题(带下划线)' }] },
      { key: 'mp_accent_color', label: '主题色(留空跟随主题)', kind: 'text' },
    ],
  },
  {
    key: 'cover', label: '封面图', hint: '推送时自动生成',
    items: [
      { key: 'mp_cover_enabled', label: '推送时自动生成封面', kind: 'bool' },
      { key: 'mp_cover_style', label: '封面风格', kind: 'select', options: [
        { v: 'dark', t: '科技深色 — 深蓝网格' },
        { v: 'light', t: '简约浅色 — 左侧色条' },
        { v: 'blocks', t: '色块对比 — 大色块分割' }] },
      { key: 'mp_cover_tag', label: '封面栏目标签(留空不显示)', kind: 'text' },
    ],
  },
]

// 扁平索引:保存时要按 key 找到表单项的定义(kind 决定怎么序列化)
const itemMap = computed(() => {
  const m = {}
  for (const s of sections) for (const it of s.items) m[it.key] = it
  return m
})

// ---------------- 折叠 ----------------
const COLLAPSE_KEY = 'panelCollapsedSections'
const collapsed = ref(readCollapsed())

function readCollapsed() {
  try {
    const raw = localStorage.getItem(COLLAPSE_KEY)
    if (raw) {
      const v = JSON.parse(raw)
      if (v && typeof v === 'object') return v
    }
  } catch (_) { /* 隐私模式下 localStorage 不可用,退回默认即可 */ }
  // 默认只展开第一组:一屏塞下所有配置正是这次要解决的问题
  return { stance: true, mp_push: true, layout: true, cover: true }
}
function persistCollapsed() {
  try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify(collapsed.value)) } catch (_) {}
}
function toggleSection(k) {
  collapsed.value[k] = !collapsed.value[k]
  persistCollapsed()
}
function setAllCollapsed(v) {
  for (const s of sections) collapsed.value[s.key] = v
  persistCollapsed()
}
const allCollapsed = computed(() => sections.every((s) => collapsed.value[s.key]))

const dirty = ref({})

/** 该分区里未保存的项数 —— 折叠起来时也要能看见,否则改动会被忘掉 */
function dirtyCount(sec) {
  return sec.items.filter((it) => dirty.value[it.key] !== undefined).length
}

async function load() {
  loading.value = true
  try {
    const [c, t] = await Promise.all([api.get('/config'), api.get('/templates')])
    cfg.value = c
    templates.value = t
  } catch (e) {
    toast('加载配置失败: ' + e.message, 'err')
  } finally {
    loading.value = false
  }
}

async function save(key) {
  const g = itemMap.value[key]
  if (!g) return
  let val = dirty.value[key]
  try {
    if (g.kind === 'number') val = Number(val)
    if (g.kind === 'csv') val = String(val).split(/[,，]/).map((s) => s.trim()).filter(Boolean)
    await api.post('/config/' + key, { value: val })
    toast(`已保存: ${g.label}`)
    delete dirty.value[key]
    await load()
  } catch (e) { toast('保存失败: ' + e.message, 'err') }
}

/** 跳到一个还没保存的配置项:展开所属分区并滚过去 */
function jumpToItem(key) {
  const sec = sections.find((s) => s.items.some((it) => it.key === key))
  if (!sec) return
  collapsed.value[sec.key] = false
  persistCollapsed()
  requestAnimationFrame(() => {
    const el = document.getElementById('cfg-' + key)
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  })
}
const pendingKeys = computed(() => Object.keys(dirty.value))

// 模板
const showTpl = ref(false)
const tplText = ref('')
const tplId = ref('')

function editTpl(t) {
  tplId.value = t.id
  tplText.value = JSON.stringify(t.definition, null, 2)
  showTpl.value = true
}
function newTpl() {
  tplId.value = ''
  tplText.value = JSON.stringify({
    template_id: 'tpl_custom_v1', name: '自定义模板', scene: '通用',
    structures: [{ structure_id: 's1', name: '标准结构', sections: [
      { key: 'title', desc: '标题,20字内' }, { key: 'lead', desc: '导语 80~120 字' },
      // ⚠️ 正文字数请用 min_len/max_len 显式声明:描述里的数字分不清是全文还是"每条",
      //    没有显式声明时系统**不会**自动校验正文字数(宁可少一条护栏,也不误杀好稿)。
      { key: 'body', desc: '正文,分 3 个小节', min_len: 800, max_len: 1200 },
      { key: 'cta', desc: '结尾引导' }] }],
    style: { tone: '专业', persona: '科技观察者' },
  }, null, 2)
  showTpl.value = true
}
async function saveTpl() {
  let def
  try {
    def = JSON.parse(tplText.value)
  } catch (e) { toast('JSON 格式有误:' + e.message, 'err'); return }
  if (!def || typeof def !== 'object' || Array.isArray(def)) {
    toast('模板必须是一个 JSON 对象', 'err'); return
  }
  // ⚠️ 后端以 template_id 当模板主键(save_template: tid = definition.template_id);
  //    用户很容易把这一行改掉或删掉,那就变成"点了保存其实没保存",所以这里兜住并说清楚。
  if (!def.template_id) def.template_id = tplId.value
  if (!def.template_id) { toast('缺少 template_id:模板需要一个唯一 id,例如 tpl_custom_v1', 'err'); return }
  if (!def.name) def.name = def.template_id
  if (!Array.isArray(def.structures) || !def.structures.length) {
    toast('缺少 structures:至少要有 1 个结构(结构里要有 sections)', 'err'); return
  }
  try {
    await api.post('/templates', def)
    tplId.value = def.template_id
    tplText.value = JSON.stringify(def, null, 2)   // 回填补全后的内容,便于核对
    // ⚠️ 保存 ≠ 启用。生成时读的是配置 default_template,保存一个别的模板不会自动改它 ——
    //    用户很容易以为"保存了就会用",所以要在这里把话说清楚(实测反馈:"保存了却没发挥作用")。
    if (def.template_id !== defaultTpl.value) {
      toast(`模板已保存:${def.name}。但生成仍使用「${defaultTpl.value}」,要用它请点「设为生成默认」`)
    } else {
      toast(`模板已保存:${def.name}(${def.template_id})`)
    }
    await load()
  } catch (e) { toast('保存失败: ' + e.message, 'err') }
}
/** 设为「生成时使用的模板」—— 写配置 default_template,pipeline 读的就是它 */
async function setDefaultTpl(t) {
  try {
    await api.post('/config/default_template', { value: t.id })
    toast(`生成将使用模板:${t.name}`)
    await load()
  } catch (e) { toast('设置失败: ' + e.message, 'err') }
}
async function toggleTpl(t) {
  try {
    await api.post(`/templates/${t.id}/toggle`, { enabled: !t.enabled })
    await load()
  } catch (e) { toast(e.message, 'err') }
}
async function removeTpl(t) {
  if (!confirm(`确认删除模板「${t.name}」?`)) return
  try {
    await api.del('/templates/' + t.id)
    toast('已删除')
    await load()
  } catch (e) { toast(e.message, 'err') }
}
async function seedTpl() {
  try {
    await api.post('/templates/seed')
    toast('已注入默认模板')
    await load()
  } catch (e) { toast(e.message, 'err') }
}

function setMulti(key, optVal, checked) {
  const arr = Array.isArray(dirty.value[key]) ? [...dirty.value[key]]
    : Array.isArray(cfg.value[key]) ? [...cfg.value[key]] : []
  const i = arr.indexOf(optVal)
  if (checked && i < 0) arr.push(optVal)
  if (!checked && i >= 0) arr.splice(i, 1)
  dirty.value[key] = arr
}

const val = (k) => dirty.value[k] ?? cfg.value[k]

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-title">
      控制面板
      <button class="btn" @click="load" :disabled="loading">{{ loading ? '加载中…' : '刷新' }}</button>
    </div>

    <div class="grid grid-2">
      <!-- 左:运行配置(按分区折叠) -->
      <div>
        <div class="card">
          <div class="row between wrap">
            <h3 style="margin:0;">运行参数</h3>
            <div class="row wrap">
              <span v-if="pendingKeys.length" class="badge amber">{{ pendingKeys.length }} 项未保存</span>
              <button class="btn sm" @click="setAllCollapsed(!allCollapsed)">
                {{ allCollapsed ? '全部展开' : '全部折叠' }}
              </button>
            </div>
          </div>

          <!-- 未保存改动可点击跳转 —— 折叠后最容易"改完忘了存" -->
          <div v-if="pendingKeys.length" class="pending-bar">
            有改动未保存:
            <button v-for="k in pendingKeys" :key="k" class="link-btn" @click="jumpToItem(k)">
              {{ (itemMap[k] && itemMap[k].label) || k }}
            </button>
          </div>
        </div>

        <div v-for="sec in sections" :key="sec.key" class="card sec-card">
          <div class="sec-head" @click="toggleSection(sec.key)">
            <span class="sec-caret" :class="{ open: !collapsed[sec.key] }">▶</span>
            <span class="sec-title">{{ sec.label }}</span>
            <span v-if="sec.hint" class="sec-hint">{{ sec.hint }}</span>
            <span class="spacer"></span>
            <span v-if="dirtyCount(sec)" class="badge amber">未保存 {{ dirtyCount(sec) }}</span>
            <span class="meta sec-count">{{ sec.items.length }} 项</span>
          </div>

          <div v-show="!collapsed[sec.key]" class="sec-body">
            <div v-for="g in sec.items" :key="g.key" class="form-row" :id="'cfg-' + g.key">
              <label>{{ g.label }}</label>

              <div v-if="g.kind === 'select'">
                <select :value="String(val(g.key))" @change="dirty[g.key] = $event.target.value">
                  <option v-for="o in g.options" :key="o.v" :value="o.v">{{ o.t }}</option>
                </select>
              </div>

              <div v-else-if="g.kind === 'multi'" class="row wrap">
                <label v-for="o in g.options" :key="o.v" style="display:flex;align-items:center;gap:6px;margin:0 12px 0 0;">
                  <input type="checkbox" :checked="(val(g.key)||[]).includes(o.v)"
                    @change="setMulti(g.key, o.v, $event.target.checked)" /> {{ o.t }}
                </label>
              </div>

              <div v-else-if="g.kind === 'csv'">
                <input type="text" :value="(val(g.key)||[]).join(', ')" @input="dirty[g.key] = $event.target.value" />
              </div>

              <div v-else-if="g.kind === 'bool'">
                <select :value="val(g.key) ? '1' : '0'" @change="dirty[g.key] = $event.target.value === '1'">
                  <option value="1">开</option><option value="0">关</option>
                </select>
              </div>

              <div v-else>
                <input type="text" :value="val(g.key)" @input="dirty[g.key] = $event.target.value" />
              </div>

              <button v-if="dirty[g.key] !== undefined" class="btn sm success mt8" @click="save(g.key)">保存</button>
            </div>
          </div>
        </div>
      </div>

      <!-- 右:模板管理 -->
      <div class="card" style="align-self:start;">
        <div class="row between">
          <h3 style="margin:0;">模板库(人工设计)</h3>
          <div class="row">
            <button class="btn sm" @click="seedTpl">注入默认模板</button>
            <button class="btn sm primary" @click="newTpl">+ 新建</button>
          </div>
        </div>

        <table style="margin-top:10px;">
          <thead><tr><th>名称</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="t in templates" :key="t.id">
              <td>{{ t.name }}<div class="meta">{{ t.id }}</div></td>
              <td>
                <span class="badge" :class="t.enabled ? 'green' : 'gray'">{{ t.enabled ? '启用' : '停用' }}</span>
                <span v-if="t.id === defaultTpl" class="badge blue">生成用</span>
              </td>
              <td class="row">
                <button class="btn sm" @click="editTpl(t)">编辑</button>
                <button class="btn sm" @click="setDefaultTpl(t)" :disabled="t.id === defaultTpl">设为生成默认</button>
                <button class="btn sm" @click="toggleTpl(t)">{{ t.enabled ? '停用' : '启用' }}</button>
                <button class="btn sm danger" @click="removeTpl(t)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="!templates.length" class="empty">暂无模板,点「注入默认模板」</div>
        <div class="meta" style="margin-top:8px; line-height:1.7;">
          生成时使用:<b>{{ defaultTpl }}</b>。模板是每次生成时从库里读的,<b>改完保存即生效,不用重启</b>。
          正文字数请在 body 段上用 <code>min_len</code>/<code>max_len</code> 声明 ——
          只写在描述里的数字,系统分不清是"全文"还是"每条",没有声明就不会自动校验字数。
        </div>
      </div>
    </div>

    <!-- 模板编辑弹层 -->
    <div v-if="showTpl" class="modal-mask" @click.self="showTpl = false">
      <div class="modal">
        <h3>{{ tplId ? '编辑模板' : '新建模板' }}</h3>
        <div class="meta">
          人工设计、系统使用。<code>template_id</code> 是模板主键(别删);
          结构里的 <code>sections</code> 决定模型输出哪些字段与正文字数区间
          (body 段用 <code>min_len</code>/<code>max_len</code> 显式声明字数)。
        </div>
        <textarea v-model="tplText" rows="22" style="font-family: Consolas, monospace; font-size: 12px;"></textarea>
        <div class="row mt16">
          <button class="btn success" @click="saveTpl">保存</button>
          <button class="btn" @click="showTpl = false">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sec-card { padding: 0; overflow: hidden; }
.sec-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  cursor: pointer;
  user-select: none;
  background: #fbfaf6;
  border-bottom: 1px solid var(--border);
}
.sec-head:hover { background: var(--primary-light); }
.sec-caret {
  font-size: 10px;
  color: var(--text-sub);
  transition: transform 0.15s;
  display: inline-block;
}
.sec-caret.open { transform: rotate(90deg); }
.sec-title { font-weight: 600; font-size: 13px; }
.sec-hint { font-size: 12px; color: var(--text-sub); }
.sec-count { font-size: 12px; }
.sec-body { padding: 14px 16px 6px; }
/* 全局的 .spacer 只作用在 .topbar 下,这里要自己来一份把右侧徽章顶到行尾 */
.spacer { flex: 1; }

.pending-bar {
  margin-top: 10px;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--warn-light);
  font-size: 12px;
}
.link-btn {
  border: none;
  background: none;
  padding: 0 6px 0 0;
  color: var(--primary);
  cursor: pointer;
  font-size: 12px;
  text-decoration: underline;
}
</style>
