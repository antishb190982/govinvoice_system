const base = import.meta.env.BASE_URL;
export default function BankReconciliation() {
return (<div className="w-screen h-screen overflow-hidden relative deck-frame"><div className="brand-lockup">GovBills</div><header className="content-heading"><h1>Post-settlement bank match</h1></header><div className="presentation-content"><section className="notes"><ul><li>Import the bank statement after settlement</li>
<li>Match statement lines to settlement references</li>
<li>Unmatched lines stay open with an owner</li>
<li>Matched results close the loop for audit</li></ul></section><section className="screen-gallery "><figure className="main-shot"><img src={`${base}screens/bank-statement-focus.jpg`} crossOrigin="anonymous" alt="Bank statement — GovBills demo environment" /><figcaption>Bank statement (demo)</figcaption></figure>
<figure className="support-shot"><img src={`${base}screens/bank-focus.jpg`} crossOrigin="anonymous" alt="Bank reconciliation — GovBills demo environment" /><figcaption>Bank reconciliation (demo)</figcaption></figure></section></div><footer className="deck-footer"><span>User journey and operational wins · Demo environment</span><span>11 / 14</span></footer></div>);
}
