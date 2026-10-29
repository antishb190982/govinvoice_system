const base = import.meta.env.BASE_URL;
export default function Registration() {
return (<div className="w-screen h-screen overflow-hidden relative deck-frame"><div className="brand-lockup">GovBills</div><header className="content-heading"><h1>Map the provider layout once, then check every invoice</h1></header><div className="presentation-content"><section className="notes"><ul><li>Map each provider's invoice layout once; reuse it for later imports</li>
<li>Check extracted invoice values against the source</li>
<li>Flag receipt-date anomalies and due-date urgency</li>
<li>Show surcharges alongside invoice totals</li></ul></section><section className="screen-gallery "><figure className="main-shot"><img src={`${base}screens/staged-import-focus.jpg`} crossOrigin="anonymous" alt="Staged import — GovBills demo environment" /><figcaption>Staged import (demo)</figcaption></figure>
<figure className="support-shot"><img src={`${base}screens/invoice-focus.jpg`} crossOrigin="anonymous" alt="Invoice detail — GovBills demo environment" /><figcaption>Invoice detail (demo)</figcaption></figure></section></div><footer className="deck-footer"><span>User journey and operational wins · Demo environment</span><span>05 / 14</span></footer></div>);
}
