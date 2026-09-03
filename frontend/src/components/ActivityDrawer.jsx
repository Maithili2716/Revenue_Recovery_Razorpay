const label = (value) => value ? String(value).replaceAll('_', ' ') : 'Not available'

export function ActivityDrawer({ open, onClose, snapshot }) {
  return <aside className={`activity-drawer ${open ? 'open' : ''}`} aria-hidden={!open}>
    <div className="drawer-head"><div><p className="eyebrow">Audit & learning</p><h2>Recovery activity</h2></div><button onClick={onClose} aria-label="Close activity drawer">×</button></div>
    <section><h3>Activity</h3>{snapshot?.activity?.length ? <ol className="drawer-events">{snapshot.activity.slice().reverse().map((event, index) => <li key={`${event.event_type}-${index}`}><i /><div><b>{label(event.event_type)}</b><p>{Object.entries(event.metadata || {}).slice(0, 2).map(([key, value]) => `${label(key)}: ${Array.isArray(value) ? value.join(', ') : value}`).join(' · ') || 'Recorded event'}</p></div><time>{new Date(event.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time></li>)}</ol> : <p className="empty-copy">No audit activity has arrived yet.</p>}</section>
    <section className="learning-box"><h3>Learning</h3>{snapshot?.learning ? <dl><dt>Strategy</dt><dd>{label(snapshot.learning.strategy)}</dd><dt>Outcome</dt><dd>{label(snapshot.learning.outcome)}</dd><dt>Updated</dt><dd>{snapshot.learning.updated ? 'Yes' : 'No'}</dd><dt>Context</dt><dd>{snapshot.learning.context_key || 'Not returned'}</dd></dl> : <p className="empty-copy">Learning remains empty until a backend learning event is recorded.</p>}</section>
  </aside>
}
