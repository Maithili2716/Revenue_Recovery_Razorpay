const stageDefinitions = [
  ['signal', 'Signal detected', 'Payment failure received'], ['case', 'Revenue risk', 'Recovery case assessed'],
  ['diagnosis', 'Diagnosis', 'Bounded failure evidence'], ['decision', 'Strategies', 'Eligible actions generated'],
  ['decision', 'Adaptive decision', 'Bandit selection recorded'], ['policy', 'Policy check', 'Authorization boundary'],
  ['execution', 'Capability execution', 'Capability execution'], ['verification', 'Independent outcome check', 'Independent outcome check'],
  ['learning', 'Verified outcome / learning', 'Verified outcome / learning'],
]

const label = (value) => value ? String(value).replaceAll('_', ' ') : 'Awaiting event'

function Detail({ type, snapshot, skipped, capabilityId, outcome, reminder }) {
  if (type === 'escalation') return <><b>Automated recovery boundary reached</b><span>Next action: merchant follow-up</span><small>Recovered ₹0 · Learning not updated</small></>
  if (type === 'verification-closed') return <><b>Closed</b><span>Automated verification stopped at escalation</span></>
  if (skipped) return <><b>Not run</b><span>{type === 'verification' ? 'Execution failed' : 'Learning not updated'}</span></>
  if (!snapshot) return <span>Waiting for backend event</span>
  if (type === 'diagnosis') return <><b>{label(snapshot.category)}</b><span>{label(snapshot.primary_reason)} · {Math.round(snapshot.confidence * 100)}% confidence</span><small>{label(snapshot.failure_stage)} · {label(snapshot.diagnosis_source)}</small></>
  if (type === 'decision') return <><b>{label(snapshot.selected_strategy)}</b><span>{label(snapshot.decision_source)}</span><small>{snapshot.reason}</small></>
  if (type === 'policy') return <><b className={snapshot.verdict === 'allow' ? 'allow' : 'block'}>{label(snapshot.verdict)}</b><span>{snapshot.reasons?.[0] || 'Policy result recorded'}</span></>
  if (type === 'execution' && reminder?.status === 'executed') return <><b>Payment Link reminder sent</b><span>{label(reminder.medium)} notification completed</span><small>Existing Payment Link remains pending</small></>
  if (type === 'execution') return snapshot.execution_status === 'failed' ? <><b className="block">Execution failed</b><span>{snapshot.error_message || 'Capability execution failed'}</span></> : <><b>{snapshot.capability_id === 'recovery_escalation' ? 'Recovery escalation' : snapshot.capability_id === 'invoice_recovery' ? 'Invoice Recovery' : snapshot.capability_id === 'payment_link_reminder' ? 'Reminder action' : 'Payment Link Recovery'}</b><span>{snapshot.provider_reference || label(snapshot.execution_status)}</span></>
  if (type === 'verification') return <><b className={snapshot.verification_status === 'recovered' ? 'allow' : ''}>{capabilityId === 'invoice_recovery' ? 'Invoice Verification' : label(snapshot.verification_status)}</b><span>{snapshot.verification_status === 'recovered' ? 'Independently verified recovery' : snapshot.reason || 'Awaiting verified outcome'}</span></>
  if (type === 'learning') return snapshot.updated ? <><b>Learning updated</b><span>{label(snapshot.outcome)}</span><small>{snapshot.context_key || 'No learning context returned'}</small></> : <><b className={outcome?.verification_status === 'recovered' ? 'allow' : ''}>{label(outcome?.verification_status)}</b><span>Independently verified outcome. Learning not updated.</span></>
  return <><b>{label(type)}</b><span>Recorded by the recovery service</span></>
}

export function RecoveryTimeline({ snapshot }) {
  const recoveryEscalated = snapshot?.execution?.execution_status === 'recovery_escalated' || snapshot?.execution?.capability_id === 'recovery_escalation'
  const executionFailed = snapshot?.execution?.execution_status === 'failed'
  const verificationSkipped = executionFailed && snapshot?.activity?.some((event) => event.event_type === 'verification_skipped')
  const learningSkipped = verificationSkipped && snapshot?.learning?.updated === false
  const terminalVerification = ['recovered', 'not_recovered'].includes(snapshot?.verification?.verification_status)
  const stageData = (key) => recoveryEscalated && key === 'verification' ? {} : key === 'learning' ? (recoveryEscalated ? {} : terminalVerification ? snapshot?.learning || snapshot?.verification : null) : snapshot?.[key]
  const currentIndex = stageDefinitions.reduce((index, [key], next) => stageData(key) ? next : index, -1)
  return <section className="timeline-panel"><div className="panel-head"><div><p className="eyebrow">Recovery cascade</p></div>{snapshot && <span className="polling"><i />polling every second</span>}</div>
    {!snapshot ? <div className="timeline-empty"><strong>Awaiting a Recovery Test Mode run</strong><p>Create an order to activate the live backend timeline.</p></div> : <ol className="timeline">{stageDefinitions.map(([key, title, description], index) => {
      const data = stageData(key)
      const escalationStage = recoveryEscalated && key === 'learning'
      const verificationClosed = recoveryEscalated && key === 'verification'
      const skipped = (key === 'verification' && verificationSkipped) || (key === 'learning' && learningSkipped)
      const state = escalationStage ? 'escalated' : skipped ? 'skipped' : key === 'execution' && executionFailed ? 'failed' : data ? 'complete' : index === currentIndex + 1 ? 'active' : 'waiting'
      return <li className={state} key={`${title}-${index}`}><span className="timeline-node" /><div className="timeline-copy"><p className="eyebrow">{String(index + 1).padStart(2, '0')} · {escalationStage ? 'Automated recovery boundary' : description}</p><h3>{escalationStage ? 'Recovery escalated' : title}</h3><Detail type={escalationStage ? 'escalation' : verificationClosed ? 'verification-closed' : key} snapshot={data} skipped={skipped} capabilityId={snapshot?.execution?.capability_id} outcome={snapshot?.verification} reminder={snapshot?.reminder} /></div></li>
    })}</ol>}
  </section>
}
