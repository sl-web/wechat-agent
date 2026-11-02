<script setup>
// 全局统一的时间范围选择器(默认当天)。
//
// 为什么要抽成组件:分析报告与文章库都要用,而"今天"到底怎么算必须两边一致 ——
// 否则一个页面说今日 57 条、另一个说今日 3 篇,用户没法对账。
// 取值与后端 app/core/daterange.py 的白名单严格对应。
const RANGES = [
  { v: 'today', t: '今天' },
  { v: '7d', t: '近 7 天' },
  { v: '30d', t: '近 30 天' },
  { v: 'all', t: '全部时间' },
]

defineProps({
  modelValue: { type: String, default: 'today' },
  // 可选:在标签前显示一行说明,例如"素材 59 条"
  hint: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])
</script>

<template>
  <div class="range-picker">
    <span class="rp-label">时间范围</span>
    <button v-for="r in RANGES" :key="r.v" class="chip"
      :class="{ active: modelValue === r.v }"
      @click="emit('update:modelValue', r.v)">{{ r.t }}</button>
    <span v-if="hint" class="rp-hint">{{ hint }}</span>
  </div>
</template>

<style scoped>
.range-picker {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
}
.rp-label { font-size: 12px; color: var(--text-sub); }
.rp-hint { font-size: 12px; color: var(--text-sub); }
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
</style>
