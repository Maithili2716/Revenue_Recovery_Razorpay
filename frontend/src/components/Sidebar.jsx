function Icon({ children }) { return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg> }

export function Sidebar({ activePage, onNavigate }) {
  return <aside className="sidebar">
    <div className="brand"><span className="brand-mark"><Icon><path d="m13 2-9 12h7l-1 8 10-13h-7V2Z" /></Icon></span><span>Recovery<span>Lab</span></span></div>
    <nav aria-label="Primary navigation">
      <button className={activePage === 'overview' ? 'nav-active' : ''} onClick={() => onNavigate('overview')}><Icon><path d="M4 13h6V4H4v9Zm0 7h6v-4H4v4Zm10 0h6v-9h-6v9Zm0-16v4h6V4h-6Z" /></Icon>Overview</button>
      <button className={activePage === 'evaluation' ? 'nav-active' : ''} onClick={() => onNavigate('evaluation')}><Icon><path d="M4 19V5m0 14h16" /><path d="m8 15 3-4 3 2 5-7" /></Icon>Evaluation</button>
    </nav>
    <div className="sidebar-bottom"><div className="test-mode"><i />Recovery Test Mode<span>Razorpay sandbox</span></div><p>Bounded recovery workflows for revenue at risk.</p></div>
  </aside>
}
