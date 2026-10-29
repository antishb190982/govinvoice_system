const base = import.meta.env.BASE_URL;
export default function Disputes() {
return (<div className="w-screen h-screen overflow-hidden relative deck-frame"><div className="brand-lockup">GovBills</div><header className="content-heading"><h1>Disputes, waivers and refunds with preserved evidence</h1></header><div className="presentation-content"><section className="notes"><ul><li>Open a dispute case against a provider invoice</li>
<li>Record waivers and refunds as tracked outcomes</li>
<li>Historical evidence is preserved, never overwritten</li>
<li>Case history links back to batch and invoice</li></ul></section><section className="screen-gallery "><figure className="main-shot"><img src={`${base}screens/dispute-case-focus.jpg`} crossOrigin="anonymous" alt="Dispute case — GovBills demo environment" /><figcaption>Dispute case (demo)</figcaption></figure>
<figure className="support-shot"><img src={`${base}screens/disputes-focus.jpg`} crossOrigin="anonymous" alt="Disputes register — GovBills demo environment" /><figcaption>Disputes register (demo)</figcaption></figure></section></div><footer className="deck-footer"><span>User journey and operational wins · Demo environment</span><span>12 / 14</span></footer></div>);
}
