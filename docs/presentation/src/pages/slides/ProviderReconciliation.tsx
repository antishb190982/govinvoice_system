const base = import.meta.env.BASE_URL;
export default function ProviderReconciliation() {
return (<div className="w-screen h-screen overflow-hidden relative deck-frame"><div className="brand-lockup">GovBills</div><header className="content-heading"><h1>Provider XLS/CSV reconciliation and the Unsorted basket</h1></header><div className="presentation-content"><section className="notes"><ul><li>Import the provider's pending-invoice XLS or CSV</li>
<li>Match account, invoice reference and amount</li>
<li>Mismatches go to the Unsorted basket for resolution</li>
<li>Reconciliation comes before approval and settlement</li></ul></section><section className="screen-gallery "><figure className="main-shot"><img src={`${base}screens/reconciliation-focus.jpg`} crossOrigin="anonymous" alt="Provider reconciliation — GovBills demo environment" /><figcaption>Provider reconciliation (demo)</figcaption></figure>
<figure className="support-shot"><img src={`${base}screens/mismatches-unsorted.jpg`} crossOrigin="anonymous" alt="Unsorted exception lines — GovBills demo environment" /><figcaption>Unsorted exception lines (demo)</figcaption></figure></section></div><footer className="deck-footer"><span>User journey and operational wins · Demo environment</span><span>06 / 14</span></footer></div>);
}
