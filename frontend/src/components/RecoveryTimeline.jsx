const stageDefinitions = [
  ['signal', 'Signal detected', 'Payment failure received'], ['case', 'Revenue risk', 'Recovery case assessed'],
  ['diagnosis', 'Diagnosis', 'Bounded failure evidence'], ['decision', 'Strategies', 'Eligible actions generated'],
  ['decision', 'Adaptive decision', 'Bandit selection recorded'], ['policy', 'Policy check', 'Authorization boundary'],
  ['execution', 'Recovery action', 'Capability execution'], ['verification', 'Verification', 'Independent outcome check'],
  ['learning', 'Learning', 'Verified outcome feedback'], ['complete', 'Complete', 'Recovery lifecycle complete'],
]

const label = (value) => value ? String(value).replaceAll('_', ' ') : 'Awaiting event'

function Detail({ type, snapshot }) {
  if (!snapshot) return <span>Waiting for backend event</span>
  if (type === 'diagnosis') return <><b>{label(snapshot.category)}</b><span>{label(snapshot.primary_reason)} · {Math.round(snapshot.confidence * 100)}% confidence</span><small>{label(snapshot.failure_stage)} · {label(snapshot.diagnosis_source)}</small></>
  if (type === 'decision') return <><b>{label(snapshot.selected_strategy)}</b><span>{label(snapshot.decision_source)}</span><small>{snapshot.reason}</small></>
  if (type === 'policy') return <><b className={snapshot.verdict === 'allow' ? 'allow' : 'block'}>{label(snapshot.verdict)}</b><span>{snapshot.reasons?.[0] || 'Policy result recorded'}</span></>
  if (type === 'execution') return <><b>{snapshot.capability_id === 'payment_link_reminder' ? 'Reminder action' : 'Payment Link created'}</b><span>{label(snapshot.execution_status)}</span></>
  if (type === 'verification') return <><b className={snapshot.verification_status === 'recovered' ? 'allow' : ''}>{label(snapshot.verification_status)}</b><span>{snapshot.verification_status === 'recovered' ? 'Independently verified recovery' : snapshot.reason || 'Awaiting verified outcome'}</span></>
  if (type === 'learning') return <><b>{snapshot.updated ? 'Learning updated' : 'Learning not updated'}</b><span>{label(snapshot.outcome)}</span><small>{snapshot.context_key || 'No learning context returned'}</small></>
  return <><b>{label(type)}</b><span>Recorded by the recovery service</span></>
}

export function RecoveryTimeline({ snapshot }) {
  const complete = Boolean(snapshot?.verification?.verification_status === 'recovered' && snapshot?.learning?.updated)
  const currentIndex = stageDefinitions.reduce((index, [key], next) => (key === 'complete' ? (complete ? next : index) : snapshot?.[key] ? next : index), -1)
  return <section className="timeline-panel"><div className="panel-head"><div><p className="eyebrow">Recovery cascade</p></div>{snapshot && <span className="polling"><i />polling every second</span>}</div>
    {!snapshot ? <div className="timeline-empty"><strong>Awaiting a Recovery Test Mode run</strong><p>Create an order to activate the live backend timeline.</p></div> : <ol className="timeline">{stageDefinitions.map(([key, title, description], index) => {
      const data = key === 'complete' ? (complete ? {} : null) : snapshot[key]
      const state = data ? 'complete' : index === currentIndex + 1 ? 'active' : 'waiting'
      return <li className={state} key={`${title}-${index}`}><span className="timeline-node" /><div className="timeline-copy"><p className="eyebrow">{String(index + 1).padStart(2, '0')} · {description}</p><h3>{title}</h3><Detail type={key} snapshot={data} /></div></li>
    })}</ol>}
  </section>
}
