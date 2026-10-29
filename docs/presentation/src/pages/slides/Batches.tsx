const base = import.meta.env.BASE_URL;
export default function Batches() {
return (<div className="w-screen h-screen overflow-hidden relative deck-frame"><div className="brand-lockup">GovBills</div><header className="content-heading"><h1>Single-provider batch preparation</h1></header><div className="presentation-content"><section className="notes"><ul><li>Each batch holds invoices for one provider only</li>
<li>Preparation, Work in Progress and Rejected folders</li>
<li>Inspect invoices, amounts and lifecycle history</li>
<li>Ministry access boundaries apply throughout</li></ul></section><section className="screen-gallery "><figure className="main-shot"><img src={`${base}screens/batch-workspace-focus.jpg`} crossOrigin="anonymous" alt="Batch workspace — GovBills demo environment" /><figcaption>Batch workspace (demo)</figcaption></figure>
<figure className="support-shot"><img src={`${base}screens/batch-focus.jpg`} crossOrigin="anonymous" alt="Batch detail — GovBills demo environment" /><figcaption>Batch detail (demo)</figcaption></figure></section></div><footer className="deck-footer"><span>User journey and operational wins · Demo environment</span><span>07 / 14</span></footer></div>);
}
