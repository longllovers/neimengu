import { useEffect, useMemo, useState } from 'react'
import { Link, NavLink, Navigate, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  Activity, ArrowRight, CheckCircle2, ChevronRight, CircleStop, Database,
  Code2, FileCheck2, FolderInput, Gauge, Layers3, Menu, PanelLeftClose, Play,
  Search, Server, Sparkles, TerminalSquare, X, XCircle,
} from 'lucide-react'
import { useTask } from './hooks/useTask'
import CodeRepository from './pages/CodeRepository'
import ResultGallery from './pages/ResultGallery'

const CATEGORIES = ['全部工具', '裁剪与转换', '质量自检', '矢量处理', '栅格分析', '统计分析', '数据整理']

function useCatalog() {
  const [state, setState] = useState({ tools: [], loading: true, error: '' })
  useEffect(() => {
    fetch('/api/tools')
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('无法读取工具目录')))
      .then((data) => setState({ tools: data.tools, loading: false, error: '' }))
      .catch((error) => setState({ tools: [], loading: false, error: error.message }))
  }, [])
  return state
}

function App() {
  const catalog = useCatalog()
  const [navOpen, setNavOpen] = useState(false)
  return (
    <div className="app-shell">
      <Sidebar tools={catalog.tools} open={navOpen} close={() => setNavOpen(false)} />
      <div className="main-frame">
        <Topbar openNav={() => setNavOpen(true)} />
        <Routes>
          <Route path="/" element={<Dashboard catalog={catalog} />} />
          <Route path="/tools/:toolId" element={<ToolRoute catalog={catalog} />} />
          <Route path="/tasks" element={<TaskHistory />} />
          <Route path="/code" element={<CodeRepository />} />
          <Route path="/results" element={<ResultGallery />} />
          <Route path="/code/esa" element={<Navigate to="/code" replace />} />
          <Route path="/code/town-clip" element={<Navigate to="/code" replace />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
    </div>
  )
}

function Sidebar({ tools, open, close }) {
  const grouped = useMemo(() => tools.reduce((result, tool) => {
    ;(result[tool.category] ||= []).push(tool)
    return result
  }, {}), [tools])
  return <>
    <button className={`nav-scrim ${open ? 'visible' : ''}`} onClick={close} aria-label="关闭导航" />
    <aside className={`sidebar ${open ? 'open' : ''}`}>
      <div className="brand-block">
        <Link to="/" className="brand" onClick={close}>
          <span className="brand-mark"><Layers3 size={20} /></span>
          <span><b>工具台</b><small>GEO WORKBENCH</small></span>
        </Link>
        <button className="mobile-close" onClick={close} aria-label="关闭导航"><X size={20} /></button>
      </div>
      <nav className="primary-nav" aria-label="主导航">
        <NavLink to="/" end onClick={close}><Gauge size={17} />工作台概览</NavLink>
        <NavLink to="/tasks" onClick={close}><Activity size={17} />运行记录</NavLink>
      </nav>
      <div className="tool-nav">
        {Object.entries(grouped).map(([category, items]) => (
          <section key={category}>
            <h2>{category}</h2>
            {items.map((tool) => <NavLink key={tool.id} to={`/tools/${tool.id}`} onClick={close}>{tool.name}</NavLink>)}
          </section>
        ))}
        <section className="result-nav-section">
          <h2>成果</h2>
          <NavLink to="/results" onClick={close}><Layers3 size={15} />结果展示</NavLink>
        </section>
        <section className="code-nav-section">
          <h2>代码块</h2>
          <NavLink to="/code" onClick={close}><Code2 size={15} />代码浏览</NavLink>
        </section>
      </div>
      <div className="offline-card"><span className="status-dot" /><div><b>离线运行模式</b><small>所有数据保留在内网服务器</small></div></div>
    </aside>
  </>
}

function Topbar({ openNav }) {
  const location = useLocation()
  const isHome = location.pathname === '/'
  return <header className="topbar">
    <button className="menu-button" onClick={openNav} aria-label="打开导航"><Menu size={21} /></button>
    <div className="crumb"><span>{isHome ? '总览' : location.pathname.startsWith('/code') ? '代码资源' : location.pathname === '/results' ? '成果' : '地理处理'}</span><ChevronRight size={14} /><strong>{isHome ? '工作台' : location.pathname === '/tasks' ? '运行记录' : location.pathname.startsWith('/code') ? '代码预览' : location.pathname === '/results' ? '结果展示' : '工具配置'}</strong></div>
    <div className="server-pill"><Server size={14} /><span>本地服务</span><i /></div>
  </header>
}

function Dashboard({ catalog }) {
  const [category, setCategory] = useState('全部工具')
  const [query, setQuery] = useState('')
  const visible = catalog.tools.filter((tool) => (category === '全部工具' || tool.category === category) && `${tool.name}${tool.description}`.toLowerCase().includes(query.toLowerCase()))
  const featured = catalog.tools.filter((tool) => tool.featured)
  return <main className="page dashboard-page">
    <section className="hero">
      <div className="hero-copy">
        <span className="eyebrow"><Sparkles size={14} />内网地理数据流水线</span>
        <h1><em>工作台</em></h1>
        <p>裁剪、检查、抽样、统计与转换使用同一套任务流程。路径、进度和结果都清楚可追溯。</p>
      </div>
      <div className="hero-metric"><span>{String(catalog.tools.length).padStart(2, '0')}</span><p>个工具<br />统一接入</p><div className="contour-lines" /></div>
    </section>

    {featured.length > 0 && <section className="featured-row">
      {featured.map((tool, index) => <Link to={`/tools/${tool.id}`} key={tool.id} className={`featured-card ${tool.accent}`}>
        <div><span className="card-index">0{index + 1} / 保留工作流</span><h2>{tool.name}</h2><p>{tool.description}</p></div>
        <span className="round-arrow"><ArrowRight size={19} /></span>
      </Link>)}
    </section>}

    <section className="catalog-section">
      <div className="section-head"><div><span className="section-kicker">TOOL INDEX</span><h2>全部处理工具</h2></div><label className="search-box"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索工具" aria-label="搜索工具" /></label></div>
      <div className="category-tabs" role="tablist">{CATEGORIES.map((item) => <button key={item} className={category === item ? 'active' : ''} onClick={() => setCategory(item)}>{item}</button>)}</div>
      {catalog.loading ? <LoadingGrid /> : catalog.error ? <EmptyState title="后端尚未连接" message="请先启动 FastAPI 服务，再刷新此页面。" /> : <div className="tool-grid">{visible.map((tool, index) => <ToolCard key={tool.id} tool={tool} index={index} />)}</div>}
    </section>
  </main>
}

function ToolCard({ tool, index }) {
  const icons = { '质量自检': FileCheck2, '裁剪与转换': Layers3, '栅格分析': Database, '数据整理': FolderInput }
  const Icon = icons[tool.category] || Activity
  return <Link to={`/tools/${tool.id}`} className={`tool-card ${tool.accent}`}>
    <div className="tool-card-top"><span className="tool-icon"><Icon size={19} /></span><span className="tool-number">{String(index + 1).padStart(2, '0')}</span></div>
    <span className="tool-category">{tool.category}</span><h3>{tool.name}</h3><p>{tool.description}</p>
    <span className="tool-link">打开工具 <ArrowRight size={15} /></span>
  </Link>
}

function ToolRoute({ catalog }) {
  const { toolId } = useParams()
  if (catalog.loading) return <main className="page"><LoadingGrid /></main>
  const tool = catalog.tools.find((item) => item.id === toolId)
  return tool ? <ToolPage key={tool.id} tool={tool} /> : <Navigate to="/" replace />
}

function defaultsFor(tool) {
  return Object.fromEntries(tool.fields.map((item) => [item.name, item.default ?? (item.type === 'checkbox' ? false : '')]))
}

function matchesCondition(condition, values) {
  if (!condition) return true
  const conditions = Array.isArray(condition[0]) ? condition : [condition]
  return conditions.every(([name, expected]) => values[name] === expected)
}

function ToolPage({ tool }) {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const restoredTaskId = searchParams.get('task') || ''
  const storageKey = `geo-workbench:v2:${tool.id}`
  const [values, setValues] = useState(() => {
    try { return { ...defaultsFor(tool), ...JSON.parse(localStorage.getItem(storageKey) || '{}') } } catch { return defaultsFor(tool) }
  })
  const [formError, setFormError] = useState('')
  const { task, logs, progress, start, cancel } = useTask(tool.id, restoredTaskId)
  const running = ['submitting', 'queued', 'running', 'cancelling'].includes(task?.status)
  const canTerminate = Boolean(task?.id) && ['queued', 'running'].includes(task?.status)

  useEffect(() => {
    if (!restoredTaskId) return
    fetch(`/api/tasks/${restoredTaskId}`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('无法恢复该次任务')))
      .then((savedTask) => {
        if (savedTask.tool_id === tool.id) setValues({ ...defaultsFor(tool), ...savedTask.parameters })
      })
      .catch((error) => setFormError(error.message))
  }, [restoredTaskId, tool])

  const submit = async (event) => {
    event.preventDefault(); setFormError('')
    const missing = tool.fields.filter((field) => matchesCondition(field.visibleWhen, values) && (field.required || matchesCondition(field.requiredWhen, values) && field.requiredWhen) && !String(values[field.name] ?? '').trim())
    if (missing.length) { setFormError(`请填写：${missing.map((field) => field.label).join('、')}`); return }
    localStorage.setItem(storageKey, JSON.stringify(values))
    try {
      const created = await start(values)
      navigate(`/tools/${tool.id}?task=${created.id}`, { replace: true })
    } catch (error) { setFormError(error.message) }
  }
  const terminate = async () => {
    setFormError('')
    try { await cancel() } catch (error) { setFormError(error.message) }
  }
  return <main className="page tool-page">
    <header className="tool-hero">
      <div><Link to="/" className="back-link">工作台 / {tool.category}</Link><span className={`large-tool-icon ${tool.accent}`}><Layers3 size={24} /></span><h1>{tool.name}</h1><p>{tool.description}</p></div>
      <div className="folder-stamp"><small>原始功能目录</small><b>{tool.folder}</b><span>统一 API 已接入</span></div>
    </header>
    <div className="workspace-grid">
      <form className="config-panel" onSubmit={submit}>
        <div className="panel-heading"><div><span>01</span><div><h2>任务配置</h2><p>填写服务器可访问的文件路径</p></div></div><span className="saved-note">自动记忆</span></div>
        <div className="form-grid">{tool.fields.filter((field) => matchesCondition(field.visibleWhen, values)).map((field) => <FormField key={field.name} field={field} required={field.required || Boolean(field.requiredWhen && matchesCondition(field.requiredWhen, values))} value={values[field.name]} onChange={(value) => setValues((current) => ({ ...current, [field.name]: value }))} />)}</div>
        {tool.id === 'shp-shift' && <OffsetPreview values={values} />}
        {formError && <div className="form-error"><XCircle size={16} />{formError}</div>}
        <div className="form-actions"><button className="primary-button" type="submit" disabled={running}><Play size={17} />{running ? '任务运行中' : '开始运行'}</button><button className="stop-button" type="button" onClick={terminate} disabled={!canTerminate}><CircleStop size={17} />{task?.status === 'cancelling' ? '正在终止' : '终止当前任务'}</button></div>
      </form>
      <TaskPanel tool={tool} task={task} logs={logs} progress={progress} />
    </div>
  </main>
}

function FormField({ field, value, onChange, required = field.required }) {
  if (field.type === 'checkbox') return <label className={`check-field ${field.wide ? 'wide' : ''}`}><input type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} /><span className="check-box"><CheckCircle2 size={15} /></span><span><b>{field.label}</b>{field.help && <small>{field.help}</small>}</span></label>
  const common = { id: field.name, name: field.name, value: value ?? '', required, placeholder: field.placeholder || '', onChange: (event) => onChange(event.target.value) }
  return <label className={`form-field ${field.wide ? 'wide' : ''}`} htmlFor={field.name}><span>{field.label}{required && <i>*</i>}</span>
    {field.type === 'select' ? <select {...common}>{field.options?.map(([optionValue, label]) => <option value={optionValue} key={optionValue}>{label}</option>)}</select> : field.type === 'textarea' ? <textarea {...common} rows="3" /> : <input {...common} type={field.type || 'text'} min={field.min} max={field.max} step={field.step} />}
    {field.help && <small>{field.help}</small>}
  </label>
}

function OffsetPreview({ values }) {
  const raw = [values.original_x, values.original_y, values.correct_x, values.correct_y]
  const complete = raw.every((value) => String(value ?? '').trim() !== '')
  const points = raw.map(Number)
  const valid = complete && points.every(Number.isFinite)
  const dx = valid ? points[2] - points[0] : null
  const dy = valid ? points[3] - points[1] : null
  return <div className={`offset-preview ${valid ? 'ready' : ''}`}>
    <span>位移量预览</span>
    <b>{valid ? `dx = ${dx.toFixed(6)}　dy = ${dy.toFixed(6)}` : '等待输入两组坐标'}</b>
  </div>
}

function TaskPanel({ tool, task, logs, progress }) {
  const status = task?.status || 'idle'
  const statusText = { idle: '等待配置', submitting: '正在提交', queued: '排队中', running: '运行中', cancelling: '正在停止', completed: '已完成', failed: '运行失败', cancelled: '已取消' }[status] || status
  return <section className="run-panel">
    <div className="panel-heading"><div><span>02</span><div><h2>运行状态</h2><p>进度与脚本输出实时同步</p></div></div><span className={`run-status ${status}`}><i />{statusText}</span></div>
    {(tool.featured || tool.showProgress) && <ClipProgress events={progress} />}
    <div className="terminal-head"><span><TerminalSquare size={15} />终端日志</span><small>{logs.length} 行</small></div>
    <div className="terminal" aria-live="polite">{logs.length ? logs.map((entry, index) => <div className={entry.level || ''} key={`${entry.sequence}-${index}`}><span>{String(index + 1).padStart(3, '0')}</span>{entry.message || ' '}</div>) : <div className="terminal-empty"><TerminalSquare size={25} /><span>任务启动后，日志会显示在这里</span></div>}</div>
    {task?.error && <div className="task-error"><XCircle size={17} />{task.error}</div>}
    {status === 'completed' && <div className="task-success"><CheckCircle2 size={17} /><span><b>处理完成</b>结果已写入你配置的输出路径{task?.result?.gallery_window_id && <Link className="gallery-jump" to="/results">查看结果展示</Link>}</span></div>}
  </section>
}

function ClipProgress({ events }) {
  const latest = events.at(-1)
  const data = latest ? Object.fromEntries(Object.entries(latest).filter(([key]) => !['sequence', 'type', 'time'].includes(key))) : {}
  const items = Object.entries(data).slice(0, 4)
  return <div className="clip-progress"><div className="progress-visual"><div className="map-grid"><i /><i /><i /><i /><i /></div><span>{events.length ? `${events.length} 条进度事件` : '等待任务进度'}</span></div><div className="progress-stats">{items.length ? items.map(([key, value]) => <div key={key}><small>{key}</small><b>{typeof value === 'object' ? '已更新' : String(value)}</b></div>) : <><div><small>已处理</small><b>—</b></div><div><small>当前阶段</small><b>待启动</b></div></>}</div></div>
}

function TaskHistory() {
  const navigate = useNavigate()
  const [tasks, setTasks] = useState([])
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    const load = () => fetch('/api/tasks?limit=50')
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('无法读取运行记录')))
      .then((data) => { if (active) { setTasks(data.tasks || []); setError('') } })
      .catch((reason) => { if (active) setError(reason.message) })
    load()
    const timer = window.setInterval(load, 3000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])
  return <main className="page history-page"><header className="simple-head"><span className="section-kicker">RUN ARCHIVE</span><h1>运行记录</h1><p>最近 50 次任务的状态与运行时间。双击记录可返回任务页面，日志保存在 log/tasks/日期/任务ID-工具名称.log。</p></header>{error ? <EmptyState title="无法读取记录" message={error} /> : tasks.length ? <div className="history-table"><div className="history-row header"><span>工具</span><span>任务 ID</span><span>状态</span><span>开始时间</span><span>结束时间</span></div>{tasks.map((task) => { const target = `/tools/${task.tool_id}?task=${task.id}`; return <div className="history-row history-link" role="link" tabIndex="0" title="双击查看任务" onDoubleClick={() => navigate(target)} onKeyDown={(event) => { if (event.key === 'Enter') navigate(target) }} key={task.id}><span><b>{task.tool_name}</b></span><span className="history-task-id" title={task.id}><code>{task.id.slice(0, 10)}</code></span><span><i className={`history-status ${task.status}`} />{task.status}</span><span>{formatTime(task.started_at || task.created_at)}</span><span>{formatTime(task.finished_at) || '—'}</span></div> })}</div> : <EmptyState title="还没有运行记录" message="从任一工具页面启动任务后，记录会出现在这里。" />}</main>
}

function formatTime(value) { return value ? value.replace('T', ' ') : '' }
function EmptyState({ title, message }) { return <div className="empty-state"><Database size={26} /><h3>{title}</h3><p>{message}</p></div> }
function LoadingGrid() { return <div className="loading-grid">{[1, 2, 3, 4, 5, 6].map((item) => <i key={item} />)}</div> }

export default App
