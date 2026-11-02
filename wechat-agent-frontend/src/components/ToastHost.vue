<script setup>
import { toastState } from '../toast.js'
</script>

<template>
  <div class="toast-wrap">
    <div v-for="t in toastState.items" :key="t.id" class="toast" :class="t.type">{{ t.msg }}</div>
  </div>
</template>

<style scoped>
.toast { animation: slidein 0.2s ease; }
@keyframes slidein { from { transform: translateX(30px); opacity: 0; } to { transform: none; opacity: 1; } }
</style>
