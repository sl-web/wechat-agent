import { reactive } from 'vue'

// 全局 toast(供任意组件 import 使用)
export const toastState = reactive({ items: [] })
let seq = 0

export function toast(msg, type = 'ok') {
  const id = ++seq
  toastState.items.push({ id, msg, type })
  setTimeout(() => {
    const i = toastState.items.findIndex((t) => t.id === id)
    if (i >= 0) toastState.items.splice(i, 1)
  }, 2600)
}
