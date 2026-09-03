import { useCallback, useEffect, useState } from 'react'
import { getLatestEvaluation, runEvaluation } from '../api/evaluation'

const money = (minor) => typeof minor === 'number'
  ? new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 }).format(minor / 100)
  : '—'
const percent = (value) => typeof value === 'number' ? `${(value * 100).toFixed(2)}%` : '—'
const name = (value) => String(value).replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())

function validResult(value) {
  return value && typeof value === 'object'
    && typeof value.batch_size === 'number'
    && typeof value.total_amount_at_risk_minor === 'number'
    && typeof value.baseline_amount_recovered_minor === 'number'
    && typeof value.adaptive_amount_recovered_minor === 'number'
    && typeof value.baseline_recovery_rate === 'number'
    && typeof value.adaptive_recovery_rate === 'number'
    && value.baseline_strategy_performance && value.adaptive_strategy_performance
}

function SummaryMetric({ label, value, tone }) {
  return <article className={`evaluation-metric ${tone || ''}`}><p>{label}</p><strong>{value}</strong></article>
}

function StrategyRow({ strategy, performance }) {
  const selected = performance.selected_count || 0
  const rate = selected ? performance.recovered_cases / selected : 0
  return <article className="strategy-row"><div><b>{name(strategy)}</b><span>{performance.selected_count} selected</span></div><div><small>Recovered cases</small><b>{performance.recovered_cases}</b></div><div><small>Not recovered</small><b>{performance.not_recovered_cases}</b></div><div><small>Amount recovered</small><b>{money(performance.amount_recovered_minor)}</b></div><div><small>Recovery rate</small><b>{percent(rate)}</b></div></article>
}

export function Evaluation() {
  const [state, setState] = useState('loading')
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')

  const loadLatest = useCallback(async () => {
    setState('loading')
    try {
      const latest = await getLatestEvaluation()
      if (!validResult(latest)) throw new Error('The evaluation service returned an unexpected result.')
      setResult(latest)
      setState('success')
    } catch (requestError) {
      if (requestError.status === 404) { setResult(null); setState('idle'); return }
      setError(requestError.message || 'Unable to load the latest evaluation.')
      setState('error')
    }
  }, [])

  useEffect(() => { loadLatest() }, [loadLatest])

  const run = async () => {
    setState('running'); setError('')
    try {
      const next = await runEvaluation()
      if (!validResult(next)) throw new Error('The evaluation service returned an unexpected result.')
      setResult(next)
      setState('success')
    } catch (requestError) {
      setError(requestError.message || 'Unable to run the held-out evaluation.')
      setState('error')
    }
  }

  const busy = state === 'loading' || state === 'running'
  return <section className="evaluation-page">
    <header className="page-header evaluation-header"><div><p className="eyebrow">Evaluation</p><h1>Held-out comparison</h1><p>Compare a fixed recovery strategy with the adaptive agent on a deterministic simulated batch. These are not live recovered funds.</p></div><div className="header-actions"><span className="simulation-badge">Simulated · held-out</span><button className="primary-button" onClick={run} disabled={busy}>{state === 'running' ? 'Running evaluation…' : 'Run Evaluation'} <span>→</span></button></div></header>
    {state === 'error' && <div className="evaluation-error"><div><b>Evaluation unavailable</b><p>{error}</p></div><button className="secondary-button" onClick={state === 'error' && !result ? loadLatest : run}>Retry</button></div>}
    {(state === 'idle' || state === 'loading') && !result && <div className="evaluation-empty"><span className="evaluation-mark">↗</span><h2>{state === 'loading' ? 'Loading latest evaluation…' : 'No evaluation result yet'}</h2><p>{state === 'loading' ? 'Reading the last explicitly-run held-out benchmark.' : 'Run the deterministic held-out benchmark to compare the fixed baseline against the adaptive agent.'}</p>{state === 'idle' && <button className="primary-button" onClick={run}>Run Evaluation <span>→</span></button>}</div>}
    {result && <>
      <section className="evaluation-summary"><div className="section-head"><div><p className="eyebrow">Evaluation summary</p><h2>One held-out simulated batch</h2></div><span>{result.batch_size} cases evaluated</span></div><div className="evaluation-metrics"><SummaryMetric label="Cases evaluated" value={result.batch_size} /><SummaryMetric label="Revenue at risk" value={money(result.total_amount_at_risk_minor)} /><SummaryMetric label="Baseline recovered" value={money(result.baseline_amount_recovered_minor)} tone="baseline" /><SummaryMetric label="Adaptive recovered" value={money(result.adaptive_amount_recovered_minor)} tone="adaptive" /><SummaryMetric label="Baseline recovery rate" value={percent(result.baseline_recovery_rate)} /><SummaryMetric label="Adaptive recovery rate" value={percent(result.adaptive_recovery_rate)} tone="adaptive" /></div></section>
      <section className="comparison-card"><div className="section-head"><div><p className="eyebrow">Headline comparison</p><h2>Fixed baseline vs. adaptive agent</h2></div><p className="comparison-note">Same simulated held-out batch</p></div><div className="comparison-table"><div className="comparison-row comparison-head"><span>Measure</span><b>Baseline <small>Fixed</small></b><b>Adaptive <small>Contextual</small></b></div><div className="comparison-row"><span>Strategy</span><b>Payment Link Recovery</b><b>Eligible strategy selection</b></div><div className="comparison-row"><span>Recovered</span><b>{money(result.baseline_amount_recovered_minor)}</b><b>{money(result.adaptive_amount_recovered_minor)}</b></div><div className="comparison-row"><span>Recovery rate</span><b>{percent(result.baseline_recovery_rate)}</b><b>{percent(result.adaptive_recovery_rate)}</b></div><div className="comparison-row"><span>Recovered cases</span><b>{result.baseline_recovered_cases}</b><b>{result.adaptive_recovered_cases}</b></div><div className="comparison-row"><span>Not recovered cases</span><b>{result.baseline_not_recovered_cases}</b><b>{result.adaptive_not_recovered_cases}</b></div></div><div className="improvement"><div><span>Absolute recovered amount improvement</span><strong>{result.absolute_improvement_minor >= 0 ? '+' : ''}{money(result.absolute_improvement_minor)}</strong></div><div><span>Relative improvement</span><strong>{result.relative_improvement >= 0 ? '+' : ''}{percent(result.relative_improvement)}</strong></div><p>Result for this simulated held-out batch only.</p></div></section>
      <section className="strategy-section"><div className="section-head"><div><p className="eyebrow">Strategy performance</p><h2>Observed simulated outcomes by strategy</h2></div></div><div className="strategy-group"><p>Adaptive agent</p>{Object.entries(result.adaptive_strategy_performance).map(([strategy, performance]) => <StrategyRow key={strategy} strategy={strategy} performance={performance} />)}</div><div className="strategy-group"><p>Fixed baseline</p>{Object.entries(result.baseline_strategy_performance).map(([strategy, performance]) => <StrategyRow key={strategy} strategy={strategy} performance={performance} />)}</div></section>
      <section className="evaluation-context"><div><p className="eyebrow">Why adaptive selection matters</p><h2>Outcomes, not hidden reasoning</h2><p>The adaptive agent evaluates eligible recovery strategies using merchant- and context-specific learning. The baseline always uses Payment Link Recovery. This comparison reports simulated outcomes; it does not expose prompts or internal reasoning.</p></div><div className="safety-list"><p className="eyebrow">Evaluation integrity</p><span>✓ Synthetic held-out cases</span><span>✓ Same batch compared across both approaches</span><span>✓ No live Razorpay capability execution</span><span>✓ No live recovery verification</span><span>✓ Evaluation-local learning state only</span><span>✓ Policy safety violations: {result.policy_safety_violation_count}</span><span>✓ Escalations: {result.escalation_count}</span></div></section>
    </>}
  </section>
}
