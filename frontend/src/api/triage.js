import client from './client'

export const startTriage = (bugId, sourceId = "", forceRefresh = false) =>
  client.post('/triage', { bug_id: bugId, source_id: sourceId, force_refresh: forceRefresh }).then(r => r.data)

export const openTriageStream = (caseId, onPanel, onComplete, onError) => {
  const token = localStorage.getItem('hpe_token') || ''
  const wsUrl = `ws://localhost:8000/triage/${caseId}/stream?token=${token}`

  let ws = null
  let panelsReceived = 0
  let reconnectAttempts = 0
  const MAX_RECONNECTS = 2
  let closed = false

  const connect = () => {
    ws = new WebSocket(wsUrl)

    ws.onopen = () => {
      console.log(`[WS] Connected for case ${caseId}`)
      reconnectAttempts = 0
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        if (msg.panel) {
          panelsReceived++
          onPanel(msg.panel, msg.data)
        } else if (msg.type === 'pipeline_complete') {
          closed = true
          onComplete(msg)
          ws.close()
        } else if (msg.type === 'error') {
          onError(msg.message)
        }
      } catch (e) {
        console.error('[WS] Parse error:', e)
      }
    }

    ws.onerror = (e) => {
      console.error('[WS] Error:', e)
    }

    ws.onclose = (e) => {
      console.log(`[WS] Closed: code=${e.code} panels=${panelsReceived}`)
      if (closed) return
      if (panelsReceived >= 4) return

      if (reconnectAttempts < MAX_RECONNECTS) {
        reconnectAttempts++
        console.log(`[WS] Reconnecting attempt ${reconnectAttempts}`)
        setTimeout(connect, 1500 * reconnectAttempts)
      } else {
        onError('Connection lost. Please try triaging again.')
      }
    }
  }

  connect()

  const timeout = setTimeout(() => {
    if (!closed) {
      closed = true
      if (ws) ws.close()
      onError('Triage timed out after 90 seconds')
    }
  }, 90000)

  return () => {
    closed = true
    clearTimeout(timeout)
    if (ws && ws.readyState === WebSocket.OPEN) ws.close()
  }
}
