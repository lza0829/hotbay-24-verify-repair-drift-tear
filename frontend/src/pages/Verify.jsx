import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Verify() {
  const [inv, setInv] = useState([])
  const [clips, setClips] = useState([])
  const [runs, setRuns] = useState([])
  const [repairs, setRepairs] = useState([])
  const [selRepair, setSelRepair] = useState(null)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  // 互证入口、修补入口、素材段、湖对象行、修补流水同刷
  const reload = () => Promise.all([
    api.inventory().then(setInv),
    api.clipsLedger().then(setClips),
    api.verifyRuns().then(setRuns),
    api.repairs().then(rs => { setRepairs(rs); setSelRepair(cur => cur ?? rs[0]?.id ?? null) }),
  ])
  useEffect(() => { reload().catch(e => setMsg(e.message)) }, [])

  const latest = runs[0] || null
  const selected = repairs.find(r => r.id === selRepair) || repairs[0] || null

  async function guard(fn) {
    if (busy) return
    setBusy(true)
    setMsg('')
    try { await fn() } catch (e) { setMsg(e.message) } finally { setBusy(false) }
  }
  const run = () => guard(async () => {
    const r = await api.runVerify()
    setMsg(`互证${r.status} · 漂移 ${r.drift_count}`)
    await reload()
  })
  const repair = (runId) => guard(async () => {
    const r = await api.repair(runId)
    setMsg(`回执 #${runId} 修补闭合 · 修正 ${r.fixed_count} 键 · 修补流水 #${r.repair_id}`)
    await reload()
  })

  return (
    <div className="page">
      <h1>目录互证</h1>
      <p className="lede">
        对照湖目录对象清单与分区素材元数据。修补真源全局唯一：<strong>以在册素材段为准</strong>——按在册素材段补齐或删除湖对象行、对齐字节；
        在册素材段不因修补删改；修补与复证同事务同成败，逐键入修补流水可重放。
      </p>

      <section className="panel">
        <h2>互证入口 · 漂移回执</h2>
        <button type="button" disabled={busy} onClick={run}>跑一次互证</button>
        {msg && <p className="msg">{msg}</p>}
        {latest && (
          <div className="receipt">
            <p>
              最新回执 #{latest.id} · {latest.created_at} ·
              <strong className={latest.drift_count ? 'err' : 'msg'}> {latest.status} · 漂移 {latest.drift_count}</strong>
            </p>
            {latest.drifts?.length > 0 && (
              <ul className="plain">
                {latest.drifts.map((d, i) => (
                  <li key={i}>{d.对象键} · {d.问题}
                    {d.元数据字节 != null && d.目录字节 != null && ` · ${d.目录字节} → ${d.元数据字节}`}
                  </li>
                ))}
              </ul>
            )}
            {latest.status === '漂移' && !latest.repair_id && (
              <button type="button" disabled={busy} onClick={() => repair(latest.id)}>一键修补（以在册素材段为准）</button>
            )}
            {latest.repair_id && <p className="muted">已被修补流水 #{latest.repair_id} 闭合</p>}
          </div>
        )}
      </section>

      <h2>素材段表（修补真源 · 修补不得删改）</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>对象键</th><th>字节</th><th>所属分区</th><th>片库间</th><th>备注</th></tr></thead>
          <tbody>{clips.map(x => <tr key={x.id}><td>{x.object_key}</td><td>{x.bytes}</td><td>{x.part_key}</td><td>{x.vault_name}</td><td>{x.note}</td></tr>)}</tbody>
        </table>
      </div>

      <h2>湖对象行（目录清单 · 修补对齐目标）</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>对象键</th><th>字节</th><th>片库间</th><th>扫描时刻</th></tr></thead>
          <tbody>{inv.map(x => <tr key={x.id}><td>{x.object_key}</td><td>{x.bytes}</td><td>{x.vault_name}</td><td>{x.scanned_at}</td></tr>)}</tbody>
        </table>
      </div>

      <h2>修补流水</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>编号</th><th>互证回执</th><th>状态</th><th>修正键数</th><th>时间</th><th>明细</th></tr></thead>
          <tbody>
            {repairs.map(r => (
              <tr key={r.id}>
                <td>{r.id}</td><td>#{r.verify_run_id}（漂移 {r.receipt_drift_count}）</td><td>{r.status}</td>
                <td>{r.fixed_count}</td><td>{r.created_at}</td>
                <td><button type="button" className="ghost" onClick={() => setSelRepair(r.id)}>查看</button></td>
              </tr>
            ))}
            {!repairs.length && <tr><td colSpan="6" className="muted">暂无修补</td></tr>}
          </tbody>
        </table>
      </div>
      {selected && (
        <section className="panel">
          <h2>修补 #{selected.id} 修正明细（可重放）</h2>
          {selected.fixes.length ? (
            <ul className="plain">
              {selected.fixes.map((f, i) => (
                <li key={i}>{f.对象键} · {f.问题} · {f.动作} · {f.前字节 ?? '—'} → {f.后字节 ?? '—'}</li>
              ))}
            </ul>
          ) : <p className="muted">落锤时漂移已愈合，无需逐键修正。</p>}
        </section>
      )}

      <h2>互证流水</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>编号</th><th>状态</th><th>漂移数</th><th>时间</th><th>修补</th></tr></thead>
          <tbody>
            {runs.map(r => (
              <tr key={r.id}>
                <td>{r.id}</td><td>{r.status}</td><td>{r.drift_count}</td><td>{r.created_at}</td>
                <td>
                  {r.repair_id
                    ? <span className="muted">修补#{r.repair_id} 已闭合</span>
                    : r.status === '漂移'
                      ? <button type="button" className="ghost" disabled={busy} onClick={() => repair(r.id)}>一键修补</button>
                      : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
