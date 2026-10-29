const base = import.meta.env.BASE_URL;
export default function Approvals() {
return (<div className="w-screen h-screen overflow-hidden relative deck-frame"><div className="brand-lockup">GovBills</div><header className="content-heading"><h1>Batch-only approvals, process map and stamped copy</h1></header><div className="presentation-content"><section className="notes"><ul><li>Approvals act on the whole batch, never single invoices</li>
<li>Process map shows the current stage and owner</li>
<li>Approval produces a stamped copy kept as evidence</li>
<li>Overdue approval tasks surface in My Work</li></ul></section><section className="screen-gallery "><figure className="main-shot"><img src={`${base}screens/bpm-focus.jpg`} crossOrigin="anonymous" alt="Invoice process map — GovBills demo environment" /><figcaption>Invoice process map (demo)</figcaption></figure>
<figure className="support-shot"><img src={`${base}screens/approval-task-focus.jpg`} crossOrigin="anonymous" alt="Approval task — GovBills demo environment" /><figcaption>Approval task (demo)</figcaption></figure></section></div><footer className="deck-footer"><span>User journey and operational wins · Demo environment</span><span>08 / 14</span></footer></div>);
}
