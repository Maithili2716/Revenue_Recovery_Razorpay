import { useCallback, useEffect, useState } from 'react'
import { createTestPayment } from './api/demo'
import { getCases, getSummary } from './api/dashboard'
import { Sidebar } from './components/Sidebar'
import { RecoveryTimeline } from './components/RecoveryTimeline'
import { ActivityDrawer } from './components/ActivityDrawer'
import { useRecoveryStatus } from './hooks/useRecoveryStatus'
import { Evaluation } from './pages/Evaluation'
import './App.css'

const formatMoney = (minor, currency = 'INR') => typeof minor === 'number' ? new Intl.NumberFormat('en-IN', { style: 'currency', currency, maximumFractionDigits: 2 }).format(minor / 100) : '—'
const label = (value) => value ? String(value).replaceAll('_', ' ') : 'Not available'
const recoveryPresentation = (capabilityId) => capabilityId === 'invoice_recovery'
  ? { created: 'Invoice created', action: 'Invoice Recovery', open: 'Open Invoice Payment' }
  : { created: 'Payment Link created', action: 'Payment Link Recovery', open: 'Open Payment Link' }

function Metric({ name, value, hint, kind }) { return <article className={`metric ${kind || ''}`}><p>{name}</p><strong>{value}</strong><span>{hint}</span></article> }

function App() {
  const [page, setPage] = useState('overview')
  const [summary, setSummary] = useState(null)
  const [cases, setCases] = useState([])
  const [payment, setPayment] = useState(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [notice, setNotice] = useState('')

  const refreshDashboard = useCallback(async () => {
    const [summaryResult, casesResult] = await Promise.allSettled([getSummary(), getCases()])
    if (summaryResult.status === 'fulfilled') setSummary(summaryResult.value)
    if (casesResult.status === 'fulfilled') setCases(casesResult.value.cases || [])
    if (summaryResult.status === 'rejected') setNotice('Recovery service is unavailable. Start the backend to view live data.')
  }, [])
  useEffect(() => { refreshDashboard() }, [refreshDashboard])

  useEffect(() => {
    if (window.location.pathname !== '/recovery/demo-return') return
    const saved = window.sessionStorage.getItem('recovery-lab-payment')
    if (!saved) return
    try {
      setPayment(JSON.parse(saved))
      setNotice('Returned from Razorpay Checkout. Waiting for the server-side webhook recovery flow.')
    } catch { window.sessionStorage.removeItem('recovery-lab-payment') }
  }, [])

  const snapshot = useRecoveryStatus(payment?.demo_id, setNotice)
  useEffect(() => { if (snapshot) refreshDashboard() }, [snapshot, refreshDashboard])

  const runTestRecovery = async () => {
    setCreating(true); setNotice('')
    try {
      const created = await createTestPayment(10000, 'INR')
      window.sessionStorage.setItem('recovery-lab-payment', JSON.stringify(created))
      setPayment(created)
    } catch (error) { setNotice(error.message) } finally { setCreating(false) }
  }
  const openCheckout = () => {
    if (!payment) return
    if (!window.Razorpay) { setNotice('Razorpay Checkout is not available. Check that its browser script is loaded.'); return }
    try {
      new window.Razorpay({
        key: payment.key_id, amount: payment.amount_minor, currency: payment.currency,
        name: 'Recovery Lab', description: 'Recovery Test Mode', order_id: payment.order_id,
        callback_url: `${window.location.origin}/recovery/demo-return`, redirect: true,
        theme: { color: '#111111' },
      }).open()
    } catch { setNotice('Unable to open Razorpay Checkout.') }
  }
  const reset = () => { window.sessionStorage.removeItem('recovery-lab-payment'); setPayment(null); setNotice(''); setDrawerOpen(false) }

  return <div className="application"><Sidebar activePage={page} onNavigate={setPage} /><main className="main-content">
    {page === 'overview' ? <>
      <header className="page-header"><div><p className="eyebrow">Overview / live recovery</p><h1>Revenue Recovery</h1><p>Detect revenue at risk, execute bounded recovery workflows, and learn only from independently verified outcomes.</p></div><div className="header-actions">{snapshot && <button className="text-button activity-top-button" onClick={() => setDrawerOpen(true)}>View activity &amp; learning <span>→</span></button>}{payment && <button className="text-button" onClick={reset}>Reset demo</button>}<button className="primary-button" onClick={runTestRecovery} disabled={creating}>{creating ? 'Creating order…' : 'Run Test Recovery'} <span>→</span></button></div></header>
      {notice && <div className="notice" role="status">{notice}<button onClick={() => setNotice('')}>×</button></div>}
      <section className="metrics"><Metric name="Revenue at risk" value={formatMoney(summary?.revenue_at_risk_minor)} hint="Known at-risk cases" kind="risk" /><Metric name="Recovered" value={formatMoney(summary?.recovered_minor)} hint="Independently verified only" kind="recovered" /><Metric name="Recovery rate" value={summary ? `${(summary.recovery_rate * 100).toFixed(1)}%` : '—'} hint={summary ? `${summary.total_cases} total cases` : 'Awaiting dashboard'} /></section>
      <section className="demo-section"><div className="demo-callout"><div><p className="eyebrow">Recovery Test Mode</p><h2>{payment ? 'Recovery Test Mode Payment Ready' : 'Run a live recovery'}</h2>{payment ? <><p>Create a failed Recovery Test Mode payment to send the real webhook into the recovery pipeline.</p><dl><dt>Amount</dt><dd>{formatMoney(payment.amount_minor, payment.currency)}</dd><dt>Order ID</dt><dd>{payment.order_id}</dd></dl><button className="primary-button" onClick={openCheckout}>Open Razorpay Checkout <span>→</span></button><small>Complete the payment in Razorpay Test Mode. Use a failing test payment method to trigger recovery.</small></> : <p>A real Recovery Test Mode order starts the live demo. The recovery cascade remains empty until the backend emits events.</p>}</div><span className="test-badge"><i />Recovery Test Mode</span></div>
        {snapshot?.execution?.execution_status === 'executed' && snapshot.execution.payment_link_url && <div className="recovery-action"><p className="eyebrow">Recovery action ready</p><h2>{recoveryPresentation(snapshot.execution.capability_id).created}</h2><strong>{formatMoney(snapshot.case?.amount_at_risk_minor, snapshot.demo.currency)}</strong><p>{recoveryPresentation(snapshot.execution.capability_id).action} · {snapshot.execution.provider_reference || 'Provider reference available in the recovery event'}</p><a className="secondary-button" href={snapshot.execution.payment_link_url} target="_blank" rel="noreferrer">{recoveryPresentation(snapshot.execution.capability_id).open} <span>↗</span></a></div>}
      </section>
      <RecoveryTimeline snapshot={snapshot} />
      <section className="cases-section"><div className="section-head"><div><p className="eyebrow">Recovery queue</p><h2>Recent Recovery Cases</h2></div><span>{cases.length} known cases</span></div>{cases.length ? <div className="case-table"><div className="case-table-head"><span>Case</span><span>At risk</span><span>Signal / urgency</span><span>Recovery status</span></div>{cases.slice(0, 7).map((item) => <div className="case-item" key={item.case_id}><div><b>{item.case_id}</b><small>{item.reason_codes?.map(label).join(', ') || 'No reason code'}</small></div><b>{formatMoney(item.amount_at_risk_minor, item.currency)}</b><div><span>{label(item.risk_status)}</span><small>{label(item.urgency)}</small></div><span className={`status ${item.recovery_status}`}>{label(item.recovery_status)}</span></div>)}</div> : <div className="empty-state">No recovery cases have been recorded by the backend.</div>}</section>
    </> : <Evaluation />}
  </main><ActivityDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} snapshot={snapshot} /></div>
}

export default App
