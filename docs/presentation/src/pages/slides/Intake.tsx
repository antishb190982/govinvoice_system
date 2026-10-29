const base = import.meta.env.BASE_URL;
export default function Intake() {
return (<div className="w-screen h-screen overflow-hidden relative deck-frame"><div className="brand-lockup">GovBills</div><header className="content-heading"><h1>Folder and ZIP intake with account allocation</h1></header><div className="presentation-content"><section className="notes"><ul><li>Upload a complete folder or ZIP in one step</li>
<li>Source documents preserved with each staged record</li>
<li>Unknown accounts routed to unit administrators for allocation</li>
<li>Dedicated mailbox: exact sender-to-provider rules under Admin</li>
<li>Trusted relay and clean-scan evidence required; Outlook is not connected</li></ul></section><section className="screen-gallery "><figure className="main-shot"><img src={`${base}screens/intake-focus.jpg`} crossOrigin="anonymous" alt="Invoice intake — GovBills demo environment" /><figcaption>Invoice intake (demo capture)</figcaption></figure>
<figure className="support-shot"><img src={`${base}screens/registration-focus.jpg`} crossOrigin="anonymous" alt="Import registration status — GovBills demo environment" /><figcaption>Import registration status (demo)</figcaption></figure></section></div><footer className="deck-footer"><span>User journey and operational wins · Demo environment</span><span>04 / 14</span></footer></div>);
}
