import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Check, ChevronDown, ChevronRight, Code2, Copy, Download, FileCode2,
  FileQuestion, Folder, FolderOpen, Maximize2, Minimize2, Search,
} from 'lucide-react'

const SOURCES = [
  { id: 'esa', title: '欧空局完整代码', folder: '欧空局' },
  { id: 'town-clip', title: '镇裁切完整代码', folder: '镇裁切' },
]

function useRepository(repoId) {
  const [state, setState] = useState({ repository: null, selected: '', preview: null, loading: true, error: '' })
  useEffect(() => {
    let active = true
    fetch(`/api/repositories/${repoId}`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('无法读取代码目录')))
      .then((repository) => { const first = preferredFile(repository.tree); if (active) setState({ repository, selected: first, preview: null, loading: false, error: '' }) })
      .catch((error) => active && setState({ repository: null, selected: '', preview: null, loading: false, error: error.message }))
    return () => { active = false }
  }, [repoId])
  useEffect(() => {
    if (!state.selected) return
    const controller = new AbortController()
    fetch(`/api/repositories/${repoId}/preview?path=${encodeURIComponent(state.selected)}`, { signal: controller.signal })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('无法预览文件')))
      .then((preview) => setState((current) => ({ ...current, preview, error: '' })))
      .catch((error) => error.name !== 'AbortError' && setState((current) => ({ ...current, preview: null, error: error.message })))
    return () => controller.abort()
  }, [repoId, state.selected])
  return { ...state, select: (selected) => setState((current) => ({ ...current, selected, preview: null })) }
}

export default function CodeRepository() {
  return <main className="page code-library-page code-stack">
    {SOURCES.map((source) => <CodeWindow source={source} key={source.id} />)}
  </main>
}

function CodeWindow({ source }) {
  const state = useRepository(source.id)
  const [expanded, setExpanded] = useState(() => new Set())
  const [query, setQuery] = useState('')
  const [copied, setCopied] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)
  const shellRef = useRef(null)
  const visibleTree = useMemo(() => filterTree(state.repository?.tree || [], query), [query, state.repository])
  const fileCount = flattenFiles(state.repository?.tree || []).length
  const toggle = (path) => setExpanded((current) => { const next = new Set(current); next.has(path) ? next.delete(path) : next.add(path); return next })
  const copy = async () => { if (!state.preview?.previewable) return; await navigator.clipboard.writeText(state.preview.content); setCopied(true); setTimeout(() => setCopied(false), 1400) }
  const toggleFullscreen = async () => { if (!document.fullscreenElement) await shellRef.current?.requestFullscreen(); else await document.exitFullscreen() }
  useEffect(() => { const update = () => setFullscreen(document.fullscreenElement === shellRef.current); document.addEventListener('fullscreenchange', update); return () => document.removeEventListener('fullscreenchange', update) }, [])

  return <section className="code-browser" ref={shellRef}>
    <header className="code-browser-bar">
      <div><span className="repo-mark"><Code2 size={18} /></span><div><b>{source.title}</b><small>{source.folder}</small></div></div>
      <div className="code-actions"><button onClick={copy} disabled={!state.preview?.previewable} title="复制当前文件">{copied ? <Check size={18} /> : <Copy size={18} />}</button><a href={`/api/repositories/${source.id}/archive`} title={`下载${source.folder}全部代码 ZIP`}><Download size={18} /></a><button onClick={toggleFullscreen} title="全屏预览">{fullscreen ? <Minimize2 size={18} /> : <Maximize2 size={18} />}</button></div>
    </header>
    <div className="code-browser-body">
      <aside className="file-explorer">
        <div className="explorer-title"><span>项目</span><small>{fileCount} 个文件</small></div>
        <label className="file-search"><Search size={14} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="查找文件" /></label>
        <div className="file-tree">{state.loading ? <span className="tree-message">正在读取…</span> : visibleTree.map((node) => <TreeNode key={node.path} node={node} selected={state.selected} expanded={expanded} toggle={toggle} select={state.select} searching={Boolean(query)} />)}</div>
      </aside>
      <section className="code-preview">
        <div className="preview-tab"><FileCode2 size={14} /><span>{state.preview?.name || '选择一个文件'}</span>{state.preview && <small>{formatBytes(state.preview.size)}</small>}</div>
        {state.error ? <PreviewEmpty title="读取失败" detail={state.error} /> : !state.preview ? <PreviewEmpty title="选择文件开始预览" detail="从左侧项目树打开文件" /> : state.preview.previewable ? state.preview.name.toLowerCase().endsWith('.md') ? <MarkdownView content={state.preview.content} /> : <CodeLines content={state.preview.content} name={state.preview.name} /> : <PreviewEmpty title="此文件不支持文本预览" detail={`${state.preview.mime} · ${formatBytes(state.preview.size)}`} />}
      </section>
    </div>
  </section>
}

function TreeNode({ node, selected, expanded, toggle, select, searching }) {
  if (node.type === 'directory') { const open = searching || expanded.has(node.path); return <div className="tree-branch"><button className="tree-row directory" onClick={() => toggle(node.path)}>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}{open ? <FolderOpen size={16} /> : <Folder size={16} />}<span>{node.name}</span></button>{open && <div className="tree-children">{node.children.map((child) => <TreeNode key={child.path} node={child} selected={selected} expanded={expanded} toggle={toggle} select={select} searching={searching} />)}</div>}</div> }
  return <button className={`tree-row file ${selected === node.path ? 'selected' : ''}`} onClick={() => select(node.path)}><i /><FileCode2 size={15} /><span>{node.name}</span></button>
}
function flattenFiles(nodes) { return nodes.flatMap((node) => node.type === 'directory' ? flattenFiles(node.children) : [node]) }
function preferredFile(nodes) { const files = flattenFiles(nodes); return files.find((file) => file.name.toLowerCase() === 'readme.md')?.path || files.find((file) => file.name.toLowerCase() === 'main.py')?.path || files[0]?.path || '' }
function filterTree(nodes, query) { const term = query.trim().toLowerCase(); if (!term) return nodes; return nodes.flatMap((node) => { if (node.type === 'file') return node.name.toLowerCase().includes(term) ? [node] : []; const children = filterTree(node.children, term); return node.name.toLowerCase().includes(term) ? [node] : children.length ? [{ ...node, children }] : [] }) }
function CodeLines({ content, name }) { return <div className={`code-lines language-${name.split('.').pop()?.toLowerCase()}`}>{content.split('\n').map((line, index) => <div className="code-line" key={index}><span>{index + 1}</span><code>{colorize(line)}</code></div>)}</div> }

function MarkdownView({ content }) {
  const lines = content.replace(/\r\n/g, '\n').split('\n')
  const blocks = []
  let index = 0
  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) { index += 1; continue }
    if (line.startsWith('```')) {
      const language = line.slice(3).trim(); const code = []; index += 1
      while (index < lines.length && !lines[index].startsWith('```')) { code.push(lines[index]); index += 1 }
      index += 1; blocks.push(<pre className="md-code-block" key={blocks.length}><small>{language || 'code'}</small><code>{code.join('\n')}</code></pre>); continue
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/)
    if (heading) { const Tag = `h${heading[1].length}`; blocks.push(<Tag key={blocks.length}>{inlineMarkdown(heading[2])}</Tag>); index += 1; continue }
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { blocks.push(<hr key={blocks.length} />); index += 1; continue }
    if (/^>\s?/.test(line)) { const quote = []; while (index < lines.length && /^>\s?/.test(lines[index])) { quote.push(lines[index].replace(/^>\s?/, '')); index += 1 } blocks.push(<blockquote key={blocks.length}>{inlineMarkdown(quote.join(' '))}</blockquote>); continue }
    if (/^\s*[-*+]\s+/.test(line)) { const items = []; while (index < lines.length && /^\s*[-*+]\s+/.test(lines[index])) { items.push(lines[index].replace(/^\s*[-*+]\s+/, '')); index += 1 } blocks.push(<ul key={blocks.length}>{items.map((item, itemIndex) => <li key={itemIndex}>{inlineMarkdown(item)}</li>)}</ul>); continue }
    if (/^\s*\d+[.)]\s+/.test(line)) { const items = []; while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) { items.push(lines[index].replace(/^\s*\d+[.)]\s+/, '')); index += 1 } blocks.push(<ol key={blocks.length}>{items.map((item, itemIndex) => <li key={itemIndex}>{inlineMarkdown(item)}</li>)}</ol>); continue }
    const paragraph = [line]; index += 1
    while (index < lines.length && lines[index].trim() && !/^(#{1,6})\s|^```|^>|^\s*[-*+]\s+|^\s*\d+[.)]\s+/.test(lines[index])) { paragraph.push(lines[index]); index += 1 }
    blocks.push(<p key={blocks.length}>{inlineMarkdown(paragraph.join(' '))}</p>)
  }
  return <article className="markdown-view">{blocks}</article>
}

function inlineMarkdown(text) {
  const tokens = text.split(/(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\[[^\]]+\]\([^)]+\)|~~[^~]+~~)/g).filter(Boolean)
  return tokens.map((token, index) => {
    if (token.startsWith('`')) return <code key={index}>{token.slice(1, -1)}</code>
    if (token.startsWith('**') || token.startsWith('__')) return <strong key={index}>{token.slice(2, -2)}</strong>
    if (token.startsWith('~~')) return <del key={index}>{token.slice(2, -2)}</del>
    const link = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/)
    if (link) return <a href={link[2]} target="_blank" rel="noreferrer" key={index}>{link[1]}</a>
    return token
  })
}
function colorize(line) { const pattern = /(#[^"']*$|\/\/.*$|\b(?:from|import|def|class|return|if|else|elif|for|while|try|except|finally|with|as|in|is|not|and|or|True|False|None|const|let|var|function|async|await|export|default|new)\b|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b\d+(?:\.\d+)?\b)/g; return line.split(pattern).filter(Boolean).map((part, index) => { let kind = ''; if (/^(#|\/\/)/.test(part)) kind = 'comment'; else if (/^["']/.test(part)) kind = 'string'; else if (/^\d/.test(part)) kind = 'number'; else if (/^[A-Za-z]/.test(part)) kind = 'keyword'; return <span className={kind} key={index}>{part}</span> }) }
function PreviewEmpty({ title, detail, action }) { return <div className="preview-empty"><FileQuestion size={32} /><h3>{title}</h3><p>{detail}</p>{action}</div> }
function formatBytes(value = 0) { return value < 1024 ? `${value} B` : value < 1048576 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1048576).toFixed(1)} MB` }
