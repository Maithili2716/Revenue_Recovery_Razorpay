const label = (value) => value ? String(value).replaceAll('_', ' ') : 'Not available'
const formatMoney = (minor, currency = 'INR') => typeof minor === 'number'
  ? new Intl.NumberFormat('en-IN', { style: 'currency', currency, maximumFractionDigits: 2 }).format(minor / 100)
  : '—'

function StageDetail({ stage, snapshot }) {
  const verificationStatus = snapshot?.verification?.verification_status
  const recoveryEscalated = snapshot?.execution?.execution_status === 'recovery_escalated' || snapshot?.execution?.capability_id === 'recovery_escalation'
  const reminderExecuted = snapshot?.reminder?.status === 'executed'
  const currency = snapshot?.case?.currency || snapshot?.signal?.currency || snapshot?.demo?.currency

  if (stage === 'signal') {
    if (!snapshot?.signal) return <span>Waiting for payment event</span>
    return <><b>Payment failure detected</b><span>{formatMoney(snapshot.signal.amount_minor, snapshot.signal.currency)} at risk</span></>
  }
  if (stage === 'risk') {
    if (!snapshot?.case) return <span>Waiting for risk assessment</span>
    return <><b>Revenue at risk identified</b><span>Recoverability: {label(snapshot.case.recoverability)}</span><small>Urgency: {label(snapshot.case.urgency)}</small></>
  }
  if (stage === 'evidence') {
    if (!snapshot?.diagnosis) return <span>Waiting for bounded evidence</span>
    return <><b>{label(snapshot.diagnosis.primary_reason)}</b><span>{Math.round(snapshot.diagnosis.confidence * 100)}% confidence</span><small>{label(snapshot.diagnosis.diagnosis_source)} · {label(snapshot.diagnosis.failure_stage)}</small></>
  }
  if (stage === 'strategy') {
    if (!snapshot?.decision) return <span>Waiting for strategy selection</span>
    return <><b>{label(snapshot.decision.selected_strategy)} selected</b><span>Adaptive selection · {label(snapshot.decision.decision_source)}</span>{snapshot?.learning?.updated && <small>Learning updated · {snapshot.learning.context_key || label(snapshot.learning.strategy)}</small>}</>
  }
  if (stage === 'policy') {
    if (!snapshot?.policy) return <span>Waiting for policy decision</span>
    return <><b className={snapshot.policy.verdict === 'allow' ? 'allow' : 'block'}>{snapshot.policy.verdict === 'allow' ? 'Authorized' : label(snapshot.policy.verdict)}</b><span>{snapshot.policy.reasons?.[0] || 'Policy decision recorded'}</span></>
  }
  if (stage === 'execution') {
    if (!snapshot?.execution) return <span>Waiting for recovery action</span>
    if (recoveryEscalated) return <><b>Automated recovery boundary reached</b><span>Recovery escalation initiated</span></>
    if (snapshot.execution.execution_status === 'failed') return <><b className="block">Recovery action failed</b><span>{snapshot.execution.error_message || 'Capability execution failed'}</span></>
    const action = snapshot.execution.capability_id === 'invoice_recovery' ? 'Invoice created' : snapshot.execution.capability_id === 'payment_link_recovery' ? 'Payment Link created' : label(snapshot.execution.capability_id)
    return <><b>{action}</b><span>{snapshot.execution.provider_reference || label(snapshot.execution.execution_status)}</span></>
  }
  if (stage === 'verification') {
    if (recoveryEscalated) return <><b>Closed</b><span>Automated verification stopped at escalation</span></>
    if (!snapshot?.verification) return <span>Waiting for independent verification</span>
    if (verificationStatus === 'recovered') return <><b className="allow">Independently verified</b><span>Provider payment confirmed</span></>
    if (verificationStatus === 'not_recovered') return <><b className="block">Recovery not completed</b><span>{snapshot.verification.reason || 'Provider confirmed no recovery'}</span></>
    if (verificationStatus === 'pending') return <><b>Verification pending</b><span>{snapshot.verification.reason || 'Payment Link status is created'}</span></>
    return <><b>{label(verificationStatus)}</b><span>{snapshot.verification.reason || 'Verification result recorded'}</span></>
  }
  if (stage === 'follow-up') {
    if (reminderExecuted) return <><b className="allow">✓ Payment reminder sent</b><span>{snapshot.reminder.medium === 'email' ? 'Email' : label(snapshot.reminder.medium)} notification accepted by Razorpay</span><small>Existing Payment Link remains active</small></>
    if (recoveryEscalated || verificationStatus === 'recovered' || snapshot?.execution?.capability_id === 'invoice_recovery') return <><b>Not required</b><span>No Payment Link follow-up needed</span></>
    return <><b>Waiting</b><span>No follow-up sent yet</span></>
  }
  if (recoveryEscalated) return <><b>Recovery escalated</b><span>Merchant follow-up required</span><small>Recovered ₹0 · Learning not updated</small></>
  if (verificationStatus === 'recovered') return <><b className="allow">✓ Payment recovered</b><span>{formatMoney(snapshot.verification.amount_recovered_minor, currency)} independently verified</span>{snapshot?.learning?.updated && <small>Learning updated · {snapshot.learning.context_key || label(snapshot.learning.strategy)}</small>}</>
  if (verificationStatus === 'not_recovered' || snapshot?.execution?.execution_status === 'failed') return <><b className="block">Recovery not completed</b><span>₹0 recovered</span></>
  if (verificationStatus === 'pending') return <><b>● Waiting for customer payment</b><span>Recovered ₹0</span>{reminderExecuted && <small>Follow-up sent · Verification remains pending</small>}</>
  return <><b>Awaiting outcome</b><span>No verified recovery yet</span></>
}

function stageState(stage, snapshot) {
  const execution = snapshot?.execution
  const verificationStatus = snapshot?.verification?.verification_status
  const escalated = execution?.execution_status === 'recovery_escalated' || execution?.capability_id === 'recovery_escalation'
  const present = {
    signal: Boolean(snapshot?.signal), risk: Boolean(snapshot?.case), evidence: Boolean(snapshot?.diagnosis),
    strategy: Boolean(snapshot?.decision), policy: Boolean(snapshot?.policy), execution: Boolean(execution),
  }
  if (stage === 'verification') {
    if (escalated) return 'closed'
    if (verificationStatus === 'pending') return 'active'
    if (verificationStatus === 'not_recovered') return 'failed'
    if (verificationStatus === 'recovered') return 'complete'
    return execution ? 'active' : 'waiting'
  }
  if (stage === 'follow-up') {
    if (snapshot?.reminder?.status === 'executed') return 'complete'
    if (escalated || verificationStatus === 'recovered' || execution?.capability_id === 'invoice_recovery') return 'closed'
    return verificationStatus === 'pending' ? 'active' : 'waiting'
  }
  if (stage === 'outcome') {
    if (escalated) return 'escalated'
    if (verificationStatus === 'recovered') return 'complete'
    if (verificationStatus === 'not_recovered' || execution?.execution_status === 'failed') return 'failed'
    return verificationStatus === 'pending' ? 'active' : 'waiting'
  }
  if (stage === 'policy' && snapshot?.policy && snapshot.policy.verdict !== 'allow') return 'failed'
  if (stage === 'execution' && execution?.execution_status === 'failed') return 'failed'
  if (stage === 'execution' && escalated) return 'closed'
  if (present[stage]) return 'complete'
  const order = ['signal', 'risk', 'evidence', 'strategy', 'policy', 'execution']
  const index = order.indexOf(stage)
  return index === 0 || present[order[index - 1]] ? 'active' : 'waiting'
}

const stages = [
  ['signal', 'Payment failure'], ['risk', 'Risk assessed'], ['evidence', 'Evidence bounded'],
  ['strategy', 'Recovery strategy'], ['policy', 'Policy check'], ['execution', 'Recovery action'],
  ['verification', 'Verification'], ['follow-up', 'Follow-up'], ['outcome', 'Current outcome'],
]

export function RecoveryTimeline({ snapshot }) {
  return <section className="timeline-panel"><div className="panel-head"><div><p className="eyebrow">Recovery cascade</p></div>{snapshot && <span className="polling"><i />polling every second</span>}</div>
    {!snapshot ? <div className="timeline-empty"><strong>Awaiting a Recovery Test Mode run</strong><p>Create an order to activate the live backend timeline.</p></div> : <ol className="timeline">{stages.map(([stage, title], index) => <li className={stageState(stage, snapshot)} key={stage}><span className="timeline-node" /><div className="timeline-copy"><p className="eyebrow">{String(index + 1).padStart(2, '0')}</p><h3>{title}</h3><StageDetail stage={stage} snapshot={snapshot} /></div></li>)}</ol>}
  </section>
}
