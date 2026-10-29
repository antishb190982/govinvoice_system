const base = import.meta.env.BASE_URL;
export default function Settlement() {
return (<div className="w-screen h-screen overflow-hidden relative deck-frame"><div className="brand-lockup">GovBills</div><header className="content-heading"><h1>Grouping approved batches of the same provider</h1></header><div className="presentation-content"><section className="notes"><ul><li>Finance groups approved batches for one provider</li>
<li>Mixed-provider grouping is not allowed</li>
<li>Validate approval and reconciliation evidence first</li>
<li>Batch amounts and history stay traceable</li></ul><p className="execution-note">Demo settlement records; no live transfer.</p></section><section className="screen-gallery "><figure className="main-shot"><img src={`${base}screens/settlement-focus.jpg`} crossOrigin="anonymous" alt="Settlement instruction — GovBills demo environment" /><figcaption>Settlement instruction (demo)</figcaption></figure>
<figure className="support-shot"><img src={`${base}screens/batch-history-focus.jpg`} crossOrigin="anonymous" alt="Batch history — GovBills demo environment" /><figcaption>Batch history (demo)</figcaption></figure></section></div><footer className="deck-footer"><span>User journey and operational wins · Demo environment</span><span>09 / 14</span></footer></div>);
}
