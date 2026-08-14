import { useCallback, useEffect, useRef, useState } from 'react'

// 页面间切换时 React 会卸载工具页。这里按工具保存当前浏览器会话的输出；
// 模块在整页刷新时会重新加载，因此刷新 9000 页面会自然清空全部输出。
const toolSessions = new Map()

function sessionFor(toolId) {
  if (!toolSessions.has(toolId)) {
    toolSessions.set(toolId, { task: null, logs: [], progress: [], cursor: 0 })
  }
  return toolSessions.get(toolId)
}

export function useTask(toolId) {
  const initial = sessionFor(toolId)
  const [task, setTaskState] = useState(initial.task)
  const [logs, setLogsState] = useState(initial.logs)
  const [progress, setProgressState] = useState(initial.progress)
  const sourceRef = useRef(null)

  const setTask = useCallback((value) => {
    const session = sessionFor(toolId)
    const next = typeof value === 'function' ? value(session.task) : value
    session.task = next
    setTaskState(next)
  }, [toolId])

  const appendLog = useCallback((event) => {
    const session = sessionFor(toolId)
    session.logs = [...session.logs.slice(-1998), event]
    session.cursor = Math.max(session.cursor, Number(event.sequence) || 0)
    setLogsState(session.logs)
  }, [toolId])

  const appendProgress = useCallback((event) => {
    const session = sessionFor(toolId)
    session.progress = [...session.progress.slice(-499), event]
    session.cursor = Math.max(session.cursor, Number(event.sequence) || 0)
    setProgressState(session.progress)
  }, [toolId])

  const closeStream = useCallback(() => {
    sourceRef.current?.close()
    sourceRef.current = null
  }, [])

  const follow = useCallback((taskId, after = 0) => {
    closeStream()
    const source = new EventSource(`/api/tasks/${taskId}/events?after=${after}`)
    sourceRef.current = source
    source.addEventListener('log', (message) => appendLog(JSON.parse(message.data)))
    source.addEventListener('progress', (message) => appendProgress(JSON.parse(message.data)))
    source.addEventListener('status', (message) => {
      const event = JSON.parse(message.data)
      const session = sessionFor(toolId)
      session.cursor = Math.max(session.cursor, Number(event.sequence) || 0)
      setTask((current) => ({ ...current, ...event, status: event.status }))
      if (['completed', 'failed', 'cancelled'].includes(event.status)) {
        closeStream()
        fetch(`/api/tasks/${taskId}`)
          .then((response) => response.json())
          .then(setTask)
          .catch(() => {})
      }
    })
    source.onerror = () => {
      source.close()
      if (sourceRef.current === source) sourceRef.current = null
    }
  }, [appendLog, appendProgress, closeStream, setTask, toolId])

  useEffect(() => {
    const session = sessionFor(toolId)
    if (session.task?.id && ['queued', 'running', 'cancelling'].includes(session.task.status)) {
      follow(session.task.id, session.cursor)
    }
    return closeStream
  }, [closeStream, follow, toolId])

  const start = useCallback(async (parameters) => {
    closeStream()
    const session = sessionFor(toolId)
    session.logs = []
    session.progress = []
    session.cursor = 0
    setLogsState([])
    setProgressState([])
    setTask({ status: 'submitting' })
    const response = await fetch(`/api/tasks/${toolId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parameters }),
    })
    const data = await response.json()
    if (!response.ok) {
      setTask({ status: 'failed', error: data.detail || '任务提交失败' })
      throw new Error(data.detail || '任务提交失败')
    }
    setTask(data)
    follow(data.id, 0)
    return data
  }, [closeStream, follow, setTask, toolId])

  const cancel = useCallback(async () => {
    const currentTask = sessionFor(toolId).task
    if (!currentTask?.id) return
    const response = await fetch(`/api/tasks/${currentTask.id}/cancel`, { method: 'POST' })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '终止失败')
    setTask(data)
  }, [setTask, toolId])

  return { task, logs, progress, start, cancel }
}
