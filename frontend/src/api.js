async function req(path, opts = {}) {
  const r = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  const text = await r.text()
  let data
  try { data = text ? JSON.parse(text) : null } catch { data = { raw: text } }
  if (!r.ok) {
    const detail = data?.detail || data?.raw || r.statusText
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return data
}

export const api = {
  health: () => req('/健康'),
  overview: () => req('/总览'),
  vaults: () => req('/片库间'),
  partitions: () => req('/分区'),
  clips: (id) => req(`/分区/${id}/素材段`),
  createPartition: (body) => req('/分区', { method: 'POST', body: JSON.stringify(body) }),
  migrates: () => req('/迁移'),
  startMigrate: (body) => req('/迁移', { method: 'POST', body: JSON.stringify(body) }),
  finishMigrate: (id, success = true) => req(`/迁移/${id}/完成`, { method: 'POST', body: JSON.stringify({ success }) }),
  policies: () => req('/保留策略'),
  destroys: () => req('/销毁流水'),
  runRetention: (body) => req('/保留执行', { method: 'POST', body: JSON.stringify(body) }),
  inventory: () => req('/目录清单'),
  clipsLedger: () => req('/素材段清单'),
  verifyRuns: () => req('/互证流水'),
  repairs: () => req('/修补流水'),
  runVerify: () => req('/互证', { method: 'POST', body: '{}' }),
  repair: (runId) => req(`/互证/${runId}/修补`, { method: 'POST', body: '{}' }),
  settings: () => req('/设置'),
  setDay: (营业日) => req('/设置/营业日', { method: 'POST', body: JSON.stringify({ 营业日 }) }),
}
