const base = import.meta.env.BASE_URL;
export default function Insights() {
return (<div className="w-screen h-screen overflow-hidden relative deck-frame dense"><div className="brand-lockup">GovBills</div><header className="content-heading"><h1>Operational visibility and drill-through</h1></header><div className="presentation-content"><section className="notes"><ul><li>Assigned tasks, plans, anomalies and overdues in one place</li>
<li>Key work areas open on an Overview dashboard</li>
<li>All providers by default; explicit filters kept in drills</li>
<li>Operating overview and historical insights kept distinct</li>
<li>Utility providers and Organisation open on Overview with register tabs; legacy URLs kept</li></ul></section><section className="screen-gallery "><figure className="main-shot"><img src={`${base}screens/overview-focus.jpg`} crossOrigin="anonymous" alt="Historical insights overview — GovBills demo environment" /><figcaption>Historical insights overview (demo)</figcaption></figure>
<figure className="support-shot"><img src={`${base}screens/analytics-focus.jpg`} crossOrigin="anonymous" alt="Insight filters — GovBills demo environment" /><figcaption>Insight filters: all units, all providers (demo)</figcaption></figure></section></div><footer className="deck-footer"><span>User journey and operational wins · Demo environment</span><span>13 / 14</span></footer></div>);
}
