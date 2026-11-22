import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Home() {
  const [o, setO] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => { api.overview().then(setO).catch(e => setErr(e.message)) }, [])
  if (err) return <p className="err">{err}</p>
  if (!o) return <p className="muted">加载总览…</p>
  const _ok = o && o.surface_ok;
  const cards = [
    ['热档', _ok ? (o.surface_热档 ?? o.热档) : o.热档], ['温档', _ok ? (o.surface_温档 ?? o.温档) : o.温档], ['冷档', _ok ? (o.surface_冷档 ?? o.冷档) : o.冷档],
    ['迁移执行中', o.迁移执行中], ['待销毁候选', o.待销毁候选], ['目录对象', o.目录对象],
  ]
  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">影院素材温层</p>
        <h1>热档片间</h1>
        <p className="lede">分区在册，冷热可迁，片龄到期可销毁，目录与元数据互证。</p>
      </header>
      <div className="stat-grid">
        {cards.map(([k, v]) => (
          <div className="stat" key={k}><span>{k}</span><strong>{v}</strong></div>
        ))}
        <div className={o.当前漂移 ? 'stat drift' : 'stat'}>
          <span>当前漂移</span><strong>{o.当前漂移}</strong>
        </div>
      </div>
      <section className="panel">
        <h2>片库间</h2>
        <ul className="plain">
          {o.片库间.map(v => (
            <li key={v.id}>{v.name} · 额定 {v.capacity} · {v.active ? '启用' : '停用'}</li>
          ))}
        </ul>
        {o.最近互证 && (
          <p className="muted">最近互证：{o.最近互证.status} · 漂移 {o.最近互证.drift_count} · {o.最近互证.created_at}</p>
        )}
        {o.最近修补 && (
          <p className="muted">最近修补：流水 #{o.最近修补.id} · 闭合回执 #{o.最近修补.verify_run_id} · 修正 {o.最近修补.fixed_count} 键 · {o.最近修补.created_at}</p>
        )}
      </section>
    </div>
  )
}
