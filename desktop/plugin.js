// Rewind: timeline scrubber pane for Hermes Desktop.
// No JSX/build: only @hermes/plugin-sdk, react, and react/jsx-runtime are importable.
// Disk plugins are not scanned by Tailwind — layout classes only; Button/EmptyState own chrome.
import {
  Button, EmptyState, ErrorState, ScrollArea, host, haptic, useValue
} from '@hermes/plugin-sdk'
import { useEffect, useState, useCallback } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'rewind'
let CTX = null

function useTimeline(session) {
  const [data, setData] = useState({ steps: [], session: '' })
  const [err, setErr] = useState(null)
  const [loading, setLoading] = useState(true)
  const load = useCallback(() => {
    if (!CTX) return
    const q = session ? `?session=${encodeURIComponent(session)}` : ''
    CTX.rest(`/timeline${q}`).then(
      d => { setData(d || { steps: [] }); setErr(null); setLoading(false) },
      e => { setErr(String(e?.message || e)); setLoading(false) }
    )
  }, [session])
  useEffect(() => {
    setLoading(true)
    load()
    // Plugin events are on the gateway stream (host.onEvent). Polling is the durable fallback.
    const stopEvt = host.onEvent(`plugin.${ID}.step.recorded`, load)
    const stopPoll = CTX.setInterval(load, 4000)
    return () => { stopEvt && stopEvt(); stopPoll && stopPoll() }
  }, [load])
  return [data, err, loading, load]
}

const mark = s => (
  s.mode === 'none' ? '⊘'
    : s.irreversible ? '⚠'
      : s.status === 'error' ? '✗'
        : s.kind === 'safety' ? '◆'
          : s.kind === 'fork' ? '🌿'
            : '•'
)

function RewindPane() {
  // Always call both hooks (Rules of Hooks). Prefer the durable stored id for list matching.
  const stored = useValue(host.state.focusedStoredSessionId)
  const active = useValue(host.state.activeSessionId)
  const session = stored || active || ''
  const [data, err, loading, reload] = useTimeline(session)
  const [sel, setSel] = useState(null)
  const [diff, setDiff] = useState([])
  const [busy, setBusy] = useState(false)
  const [busyVerb, setBusyVerb] = useState('')
  const steps = data.steps || []
  const cur = sel ?? (steps.length ? steps[steps.length - 1].seq : null)
  const curStep = steps.find(s => s.seq === cur)
  const canAct = cur != null && !busy && curStep && curStep.kind !== 'fork' && curStep.mode !== 'none'

  useEffect(() => {
    if (cur == null || !CTX) { setDiff([]); return }
    const sid = data.session || session || ''
    CTX.rest(`/diff?step=${cur}&session=${encodeURIComponent(sid)}`).then(
      d => setDiff(d.lines || []),
      () => setDiff([])
    )
  }, [cur, data.session, session])

  const act = (path, verb) => {
    if (!canAct) return
    haptic('tap')
    setBusy(true)
    setBusyVerb(verb)
    CTX.rest(path, { method: 'POST', body: { step: cur, session: data.session || session } }).then(r => {
      const notes = []
      if (r && r.hand_edits_overwritten && r.hand_edits_overwritten.length) notes.push('overwrote hand-edits')
      if (r && r.stays_outside && r.stays_outside.length) notes.push(`${r.stays_outside.length} side-effect(s) remain outside`)
      else if (r && r.not_reversible && r.not_reversible.length) notes.push('some side-effects not undone')
      if (r && r.restore_mode === 'worktree_only') notes.push('HEAD left in place')
      host.notify({
        kind: r && r.ok ? 'success' : 'error',
        message: r && r.ok
          ? `${verb} step ${cur}${notes.length ? ' — ' + notes.join('; ') : ''}`
          : (r && r.error) || 'request failed'
      })
      reload()
    }, e => host.notify({ kind: 'error', message: String(e?.message || e) })).finally(() => {
      setBusy(false)
      setBusyVerb('')
    })
  }

  if (err) {
    return jsx(ErrorState, {
      title: 'Rewind backend unavailable',
      description: 'Enable the rewind agent plugin (and its dashboard API) so this pane can read the timeline. Then click Retry.',
      children: jsx(Button, { type: 'button', size: 'sm', onClick: () => { setLoading(true); reload() }, children: 'Retry' })
    })
  }

  if (loading && steps.length === 0) {
    return jsx(EmptyState, {
      title: 'Loading timeline…',
      description: session
        ? `Fetching steps for session ${String(session).slice(0, 16)}…`
        : 'Waiting for an active session.'
    })
  }

  if (!loading && steps.length === 0) {
    return jsx(EmptyState, {
      title: 'No steps yet',
      description: session
        ? `Session ${session} has no recorded file/terminal steps. Ask the agent to write a file or run a command.`
        : 'Open a session and let the agent write a file or run a command.'
    })
  }

  const irrNote = curStep && curStep.irreversible
    ? `⚠ ${curStep.irreversible}`
    : (curStep && curStep.mode === 'none' ? '⊘ remote — cannot restore this step' : '')
  const forkNote = curStep && curStep.kind === 'fork'
    ? '🌿 fork marker — use /rewind unfork in chat to remove the worktree'
    : ''

  return jsxs('div', {
    className: 'flex h-full flex-col gap-2 p-3 text-sm',
    children: [
      jsxs('div', {
        className: 'flex items-baseline justify-between gap-2',
        children: [
          jsx('div', { className: 'font-medium', children: `⏪ Rewind · ${steps.length} step(s)` }),
          jsx('div', {
            className: 'truncate text-[0.6875rem] text-(--ui-text-quaternary)',
            title: session || '',
            children: busy
              ? `busy: ${busyVerb}…`
              : (session ? `session ${String(session).slice(0, 12)}…` : 'no session')
          })
        ]
      }),
      steps.length > 1 && jsx('input', {
        type: 'range',
        min: steps[0].seq,
        max: steps[steps.length - 1].seq,
        value: cur ?? 0,
        disabled: busy,
        onChange: e => {
          const raw = Number(e.target.value)
          // Snap to nearest real step (seq gaps from deletes/safety keep the slider honest).
          let best = steps[0].seq, bestD = Math.abs(steps[0].seq - raw)
          for (const s of steps) {
            const d = Math.abs(s.seq - raw)
            if (d < bestD) { best = s.seq; bestD = d }
          }
          setSel(best)
        },
        'aria-label': 'Scrub timeline',
        className: 'w-full'
      }),
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1',
        children: jsx('div', {
          className: 'font-mono text-[0.75rem]',
          children: steps.map(s => jsx('div', {
            key: s.seq,
            role: 'button',
            tabIndex: 0,
            onClick: () => !busy && setSel(s.seq),
            onKeyDown: e => { if (!busy && (e.key === 'Enter' || e.key === ' ')) setSel(s.seq) },
            className: `cursor-pointer rounded px-1 py-0.5 ${s.seq === cur ? 'bg-(--ui-bg-secondary)' : 'hover:bg-(--chrome-action-hover)'} ${busy ? 'opacity-60' : ''}`,
            title: s.irreversible || '',
            children: `${mark(s)} ${s.seq}  ${s.kind === 'tool' ? s.tool : '[' + s.kind + ']'}  ${s.summary || ''}`
          }))
        })
      }),
      (irrNote || forkNote) && jsx('div', {
        className: 'rounded border border-(--ui-stroke-secondary) px-2 py-1 text-[0.7rem] text-(--ui-text-tertiary)',
        children: irrNote || forkNote
      }),
      jsx('pre', {
        className: 'max-h-32 overflow-auto rounded border border-(--ui-stroke-secondary) p-2 text-[0.7rem] text-(--ui-text-tertiary)',
        children: busy
          ? `(${busyVerb} in progress…)`
          : (diff.length ? diff.join('\n') : (cur != null ? '(no diff)' : ''))
      }),
      jsxs('div', {
        className: 'flex flex-wrap gap-2',
        children: [
          jsx(Button, {
            type: 'button', size: 'sm', disabled: !canAct,
            onClick: () => act('/rewind', 'Rewound to before'),
            children: busy && busyVerb.startsWith('Rewound') ? 'Rewinding…' : `Rewind to before #${cur ?? '—'}`
          }),
          jsx(Button, {
            type: 'button', size: 'sm', variant: 'secondary', disabled: !canAct,
            onClick: () => act('/fork', 'Forked'),
            children: busy && busyVerb.startsWith('Forked') ? 'Forking…' : 'Fork here'
          })
        ]
      })
    ]
  })
}

export default {
  id: ID,
  name: 'Rewind',
  // Unified packages ship the Desktop half opt-in (Hermes policy).
  defaultEnabled: false,
  register(ctx) {
    CTX = ctx
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'rewind',
      data: { placement: 'right', width: '320px' },
      render: () => jsx(RewindPane, {})
    })
  }
}
