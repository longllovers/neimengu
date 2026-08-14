import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChevronRight, Download, FileJson, FileSpreadsheet, FileText, Folder, Layers3, Maximize2, Minimize2, RefreshCw, RotateCcw, Trash2, ZoomIn, ZoomOut } from 'lucide-react'

function useResultGallery() {
  const [state, setState] = useState({ windows: [], loading: true, error: '' })
  const refresh = useCallback(() => {
    setState((current) => ({ ...current, loading: true }))
    fetch('/api/results').then((response) => response.ok ? response.json() : Promise.reject(new Error('无法读取展示结果')))
      .then((data) => setState({ windows: data.windows || [], loading: false, error: '' }))
      .catch((error) => setState({ windows: [], loading: false, error: error.message }))
  }, [])
  useEffect(refresh, [refresh])
  const remove = async (id) => { const response = await fetch(`/api/results/${id}`, { method: 'DELETE' }); if (!response.ok) throw new Error('删除失败'); setState((current) => ({ ...current, windows: current.windows.filter((item) => item.id !== id) })) }
  const clear = async () => { const response = await fetch('/api/results', { method: 'DELETE' }); if (!response.ok) throw new Error('全部删除失败'); setState((current) => ({ ...current, windows: [] })) }
  return { ...state, refresh, remove, clear }
}

export default function ResultGallery() {
  const gallery = useResultGallery()
  return <main className="page result-gallery-page">
    <header className="result-gallery-head"><div><span className="section-kicker">RESULT GALLERY</span><h1>结果展示</h1><p>任务完成后，本次生成的表格、矢量、PDF 与 JSON 会自动发布到独立窗口。删除窗口不会删除原始文件。</p></div><div><button onClick={gallery.refresh}><RefreshCw size={16} />刷新</button><button className="clear-all-results" onClick={gallery.clear} disabled={!gallery.windows.length}><Trash2 size={16} />全部删除</button></div></header>
    {gallery.error ? <GalleryEmpty title="读取失败" detail={gallery.error} /> : gallery.loading && !gallery.windows.length ? <GalleryEmpty title="正在读取结果" detail="请稍候" /> : !gallery.windows.length ? <GalleryEmpty title="暂无展示窗口" detail="运行会产生成果的工具后，本次新生成的文件会自动显示在这里。" /> : <div className="result-window-stack">{gallery.windows.map((window) => <ResultWindow key={window.id} window={window} remove={() => gallery.remove(window.id)} />)}</div>}
  </main>
}

function ResultWindow({ window, remove }) {
  const windowRef = useRef(null)
  const [fullscreen, setFullscreen] = useState(false)
  useEffect(() => {
    const update = () => setFullscreen(document.fullscreenElement === windowRef.current)
    document.addEventListener('fullscreenchange', update)
    return () => document.removeEventListener('fullscreenchange', update)
  }, [])
  const toggleFullscreen = async () => {
    if (document.fullscreenElement === windowRef.current) await document.exitFullscreen()
    else await windowRef.current?.requestFullscreen()
  }
  const summary = Object.entries(window.assets.reduce((counts, asset) => ({ ...counts, [asset.kind]: (counts[asset.kind] || 0) + 1 }), {})).map(([kind, count]) => `${count} 个 ${kindLabel(kind)}`).join(' · ')
  return <section className="result-window" ref={windowRef}>
    <header className="result-window-bar"><div><span className="result-mark"><Layers3 size={18} /></span><div><b>{window.title}</b><small>{summary}</small></div></div><div className="result-window-actions"><a href={`/api/results/${window.id}/download`} title="下载该窗口全部结果"><Download size={18} /></a><button onClick={toggleFullscreen} title={fullscreen ? '退出全屏' : '放大窗口'}>{fullscreen ? <Minimize2 size={18} /> : <Maximize2 size={18} />}</button><button onClick={remove} title="删除展示窗口"><Trash2 size={18} /></button></div></header>
    <div className="result-window-content">{window.assets.length > 1 ? <MultiAssetBrowser windowId={window.id} assets={window.assets} /> : <AssetPanel windowId={window.id} asset={window.assets[0]} />}</div>
  </section>
}

function MultiAssetBrowser({ windowId, assets }) {
  const [selectedId, setSelectedId] = useState(assets[0]?.id)
  useEffect(() => {
    if (!assets.some((asset) => asset.id === selectedId)) setSelectedId(assets[0]?.id)
  }, [assets, selectedId])
  const tree = useMemo(() => buildAssetTree(assets), [assets])
  const selected = assets.find((asset) => asset.id === selectedId) || assets[0]
  return <div className="result-asset-browser">
    <aside className="result-asset-tree"><header><b>结果文件</b><small>{assets.length} 个</small></header><div className="result-tree-scroll">{tree.map((node) => <AssetTreeNode key={node.key} node={node} selectedId={selected?.id} select={setSelectedId} />)}</div></aside>
    <div className="result-asset-preview"><AssetPanel key={selected.id} windowId={windowId} asset={selected} /></div>
  </div>
}

function AssetTreeNode({ node, selectedId, select, depth = 0 }) {
  const [expanded, setExpanded] = useState(true)
  if (node.asset) return <button className={`result-tree-file ${selectedId === node.asset.id ? 'active' : ''}`} style={{ paddingLeft: 13 + depth * 15 }} onClick={() => select(node.asset.id)} title={node.asset.relative_path}><FileSpreadsheet size={14} /><span>{node.name}</span><small>{kindLabel(node.asset.kind)}</small></button>
  return <div className="result-tree-folder"><button style={{ paddingLeft: 9 + depth * 15 }} onClick={() => setExpanded((value) => !value)} title={node.name}><ChevronRight className={expanded ? 'expanded' : ''} size={13} /><Folder size={14} /><span>{node.name}</span></button>{expanded && node.children.map((child) => <AssetTreeNode key={child.key} node={child} selectedId={selectedId} select={select} depth={depth + 1} />)}</div>
}

function buildAssetTree(assets) {
  const root = { children: new Map() }
  assets.forEach((asset) => {
    const segments = asset.relative_path.split('/').filter(Boolean)
    let cursor = root
    segments.forEach((name, index) => {
      const isFile = index === segments.length - 1
      if (!cursor.children.has(name)) cursor.children.set(name, { name, children: new Map(), asset: isFile ? asset : null })
      cursor = cursor.children.get(name)
      if (isFile) cursor.asset = asset
    })
  })
  const serialize = (node, prefix = '') => [...node.children.values()].sort((a, b) => Number(Boolean(a.asset)) - Number(Boolean(b.asset)) || a.name.localeCompare(b.name, 'zh-CN')).map((child) => ({ ...child, key: `${prefix}/${child.name}`, children: child.asset ? [] : serialize(child, `${prefix}/${child.name}`) }))
  return serialize(root)
}

function AssetPanel({ windowId, asset }) {
  if (asset.kind === 'csv') return <CsvPanel windowId={windowId} asset={asset} />
  if (asset.kind === 'shp') return <ShapePanel windowId={windowId} asset={asset} />
  if (asset.kind === 'excel') return <ExcelPanel windowId={windowId} asset={asset} />
  if (asset.kind === 'json') return <JsonPanel windowId={windowId} asset={asset} />
  if (asset.kind === 'pdf') return <PdfPanel windowId={windowId} asset={asset} />
  if (asset.kind === 'svg') return <SvgPanel windowId={windowId} asset={asset} />
  return null
}

function useAsset(url) {
  const [state, setState] = useState({ data: null, error: '', loading: true })
  useEffect(() => { const controller = new AbortController(); fetch(url, { signal: controller.signal }).then((response) => response.ok ? response.json() : response.json().then((data) => Promise.reject(new Error(data.detail || '读取失败')))).then((data) => setState({ data, error: '', loading: false })).catch((error) => error.name !== 'AbortError' && setState({ data: null, error: error.message, loading: false })); return () => controller.abort() }, [url])
  return state
}

function CsvPanel({ windowId, asset }) {
  const state = useAsset(`/api/results/${windowId}/csv/${asset.id}`)
  return <article className="result-asset csv-asset"><AssetHeader icon={<FileSpreadsheet size={16} />} asset={asset} />{state.loading ? <AssetMessage text="正在读取 CSV…" /> : state.error ? <AssetMessage text={state.error} /> : <DataTable data={state.data} />}</article>
}

function ShapePanel({ windowId, asset }) {
  const state = useAsset(`/api/results/${windowId}/shp/${asset.id}`)
  return <article className="result-asset shape-asset"><AssetHeader icon={<Layers3 size={16} />} asset={asset} />{state.loading ? <AssetMessage text="正在生成矢量预览…" /> : state.error ? <AssetMessage text={state.error} /> : <ShapeMap data={state.data} />}</article>
}

function ExcelPanel({ windowId, asset }) {
  const state = useAsset(`/api/results/${windowId}/excel/${asset.id}`)
  return <article className="result-asset excel-asset"><AssetHeader icon={<FileSpreadsheet size={16} />} asset={asset} />{state.loading ? <AssetMessage text="正在读取工作簿…" /> : state.error ? <AssetMessage text={state.error} /> : <div className="excel-sheet-stack">{state.data.sheets.map((sheet) => <section key={sheet.name}><h3>{sheet.name}</h3><DataTable data={sheet} /></section>)}</div>}</article>
}

function JsonPanel({ windowId, asset }) {
  const state = useAsset(`/api/results/${windowId}/json/${asset.id}`)
  return <article className="result-asset json-asset"><AssetHeader icon={<FileJson size={16} />} asset={asset} />{state.loading ? <AssetMessage text="正在读取 JSON…" /> : state.error ? <AssetMessage text={state.error} /> : <><pre>{state.data.content}</pre>{state.data.truncated && <p className="asset-note">内容较大，仅显示前 300,000 个字符。</p>}</>}</article>
}

function PdfPanel({ windowId, asset }) {
  return <article className="result-asset pdf-asset"><AssetHeader icon={<FileText size={16} />} asset={asset} /><iframe title={asset.name} src={`/api/results/${windowId}/file/${asset.id}`} /></article>
}

function SvgPanel({ windowId, asset }) {
  const [zoom, setZoom] = useState(1)
  const changeZoom = (delta) => setZoom((value) => Math.min(8, Math.max(.5, Number((value + delta).toFixed(2)))))
  return <article className="result-asset svg-asset"><AssetHeader icon={<Layers3 size={16} />} asset={asset} /><div className="svg-viewer"><div className="svg-zoom-bar"><button onClick={() => changeZoom(-.25)} disabled={zoom <= .5} title="缩小 SVG"><ZoomOut size={16} /></button><span>{Math.round(zoom * 100)}%</span><button onClick={() => changeZoom(.25)} disabled={zoom >= 8} title="放大 SVG"><ZoomIn size={16} /></button><button onClick={() => setZoom(1)} title="恢复 100%"><RotateCcw size={15} /></button></div><div className="svg-result-canvas"><img style={{ width: `${zoom * 100}%`, maxHeight: zoom === 1 ? '760px' : 'none' }} src={`/api/results/${windowId}/file/${asset.id}`} alt={asset.name} /></div></div></article>
}

function AssetHeader({ icon, asset }) { return <header><span>{icon}{asset.relative_path}</span><small>{formatBytes(asset.size)}</small></header> }
function DataTable({ data }) { return <div className="csv-table-wrap"><table><thead><tr>{data.headers.map((header, index) => <th key={index}>{header || `列 ${index + 1}`}</th>)}</tr></thead><tbody>{data.rows.map((row, rowIndex) => <tr key={rowIndex}>{data.headers.map((_, columnIndex) => <td key={columnIndex}>{row[columnIndex] ?? ''}</td>)}</tr>)}</tbody></table>{data.truncated && <p className="asset-note">仅预览前 500 行，下载包中保留完整文件。</p>}</div> }

function ShapeMap({ data }) {
  const geometries = useMemo(() => data.features.flatMap((feature) => geometryRings(feature.geometry)), [data])
  const points = geometries.flatMap((ring) => ring)
  if (!points.length) return <AssetMessage text="没有可显示的几何" />
  const xs = points.map((point) => point[0]), ys = points.map((point) => point[1]); const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys); const width = maxX - minX || 1, height = maxY - minY || 1; const pad = Math.max(width, height) * .04
  const pathFor = (ring) => ring.map(([x, y], index) => `${index ? 'L' : 'M'}${x},${-y}`).join(' ') + ' Z'
  return <div className="shape-map"><svg viewBox={`${minX - pad} ${-(maxY + pad)} ${width + pad * 2} ${height + pad * 2}`} preserveAspectRatio="xMidYMid meet">{geometries.map((ring, index) => <path d={pathFor(ring)} key={index} vectorEffect="non-scaling-stroke" />)}</svg><span>{data.total} 个要素{data.truncated ? ' · 已简化预览' : ''}</span></div>
}

function geometryRings(geometry) { if (!geometry) return []; if (geometry.type === 'Polygon') return geometry.coordinates; if (geometry.type === 'MultiPolygon') return geometry.coordinates.flat(); if (geometry.type === 'LineString') return [geometry.coordinates]; if (geometry.type === 'MultiLineString') return geometry.coordinates; if (geometry.type === 'GeometryCollection') return geometry.geometries.flatMap(geometryRings); return [] }
function AssetMessage({ text }) { return <div className="asset-message">{text}</div> }
function GalleryEmpty({ title, detail }) { return <div className="gallery-empty"><Layers3 size={30} /><h2>{title}</h2><p>{detail}</p></div> }
function formatBytes(value = 0) { return value < 1024 ? `${value} B` : value < 1048576 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1048576).toFixed(1)} MB` }
function kindLabel(kind) { return ({ csv: 'CSV', shp: 'SHP', pdf: 'PDF', json: 'JSON', excel: 'Excel', svg: 'SVG' })[kind] || kind.toUpperCase() }
