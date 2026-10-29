const base = import.meta.env.BASE_URL;
export default function Authorisation() {
return (<div className="w-screen h-screen overflow-hidden relative deck-frame"><div className="brand-lockup">GovBills</div><header className="content-heading"><h1>Independent authorisation and bank reference</h1></header><div className="presentation-content"><section className="notes"><ul><li>A separate authoriser approves the settlement</li>
<li>Preparer and authoriser remain distinct people</li>
<li>Bank reference recorded against the settlement</li>
<li>Execution history retained for audit</li></ul><p className="execution-note">Demo references only; no real bank details are shown.</p></section><section className="screen-gallery single-shot"><figure className="main-shot"><img src={`${base}screens/bank-records-focus.jpg`} crossOrigin="anonymous" alt="Settlement bank records — GovBills demo environment" /><figcaption>Settlement bank records (demo)</figcaption></figure></section></div><footer className="deck-footer"><span>User journey and operational wins · Demo environment</span><span>10 / 14</span></footer></div>);
}
