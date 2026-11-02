// 后端 API 统一封装
const BASE = '/api'

async function request(method, path, body, timeoutMs = 0) {
  const opt = { method, headers: {} }
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json'
    opt.body = JSON.stringify(body)
  }
  // 超时支持:0=不限制;长任务(generate)给足时间
  if (timeoutMs > 0) {
    const ctrl = new AbortController()
    opt.signal = ctrl.signal
    setTimeout(() => ctrl.abort(), timeoutMs)
  }
  let resp
  try {
    resp = await fetch(BASE + path, opt)
  } catch (e) {
    if (e.name === 'AbortError') throw new Error(`请求超时(${timeoutMs / 1000}s),请稍后刷新查看结果`)
    throw e
  }
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      const j = await resp.json()
      detail = j.detail || j.message || detail
    } catch (_) { /* ignore */ }
    throw new Error(detail)
  }
  return resp.json()
}

export const api = {
  get: (p) => request('GET', p),
  post: (p, b, timeoutMs) => request('POST', p, b, timeoutMs),
  del: (p) => request('DELETE', p),
}
