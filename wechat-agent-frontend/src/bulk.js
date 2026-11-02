// 文章批量操作(文章库 / 人工审核共用)
//
// 两个视图的批量语义必须完全一致,否则同一个按钮在两个页面表现不同 ——
// 所以选择状态、调用路径、提示文案都收在这里,视图只负责表格渲染与二次确认。
import { ref } from 'vue'

import { api } from './api.js'
import { toast } from './toast.js'

// 已发布/已回流不可物理删除(后端 clean_article 也会拒绝)。
// 前端提前禁用按钮并写明原因,避免用户点了才被拒。
export const UNDELETABLE = new Set(['published', 'backfilled'])

export function canDelete(row) {
  return !UNDELETABLE.has(row && row.status)
}

export function useBulk() {
  const picked = ref([])          // 选中的文章 id
  const busy = ref(false)

  const has = (id) => picked.value.includes(id)

  function toggle(id) {
    const i = picked.value.indexOf(id)
    if (i >= 0) picked.value.splice(i, 1)
    else picked.value.push(id)
  }

  /** 全选 / 全不选(以传入的当前列表为准,不会把别的筛选状态也选上) */
  function setAll(rows, on) {
    picked.value = on ? (rows || []).map((r) => r.id) : []
  }

  function clear() {
    picked.value = []
  }

  /** 列表刷新后清掉已不存在的选中项(否则批量会带上幽灵 id) */
  function prune(rows) {
    const alive = new Set((rows || []).map((r) => r.id))
    picked.value = picked.value.filter((id) => alive.has(id))
  }

  /**
   * action: 'discard'(废弃,可恢复) | 'clean'(物理删除,仅未发布)
   * 部分失败不是整体失败:跳过项必须把原因带给用户。
   */
  async function run(action, ids, label) {
    const list = ids && ids.length ? ids : picked.value
    if (!list.length || busy.value) return null
    busy.value = true
    try {
      const r = await api.post('/review/bulk', { action, ids: list, reason: label })
      const skipped = r.skipped || []
      if (skipped.length) {
        const first = skipped[0].reason || '原因未知'
        const more = skipped.length > 1 ? ` 等 ${skipped.length} 条` : ''
        toast(`${label}:成功 ${r.done_count} 条,跳过 ${skipped.length} 条 —— ${first}${more}`, 'err')
      } else {
        toast(`${label}:成功 ${r.done_count} 条`)
      }
      clear()
      return r
    } catch (e) {
      toast(`${label}失败: ${e.message}`, 'err')
      return null
    } finally {
      busy.value = false
    }
  }

  return { picked, busy, has, toggle, setAll, clear, prune, run }
}
