export default function Journey() {
  return (
    <div className="w-screen h-screen overflow-hidden relative deck-frame">
      <div className="brand-lockup">GovBills</div>
      <header className="content-heading">
        <div className="concept-label">Conceptual journey — roles and handoffs, not a screenshot</div>
        <h1>From invoice receipt to confirmed settlement</h1>
      </header>
      <div className="receipt-journey">
        <div className="receipt-grid">
          <section className="receipt-step"><span className="receipt-number">01 · CLERK / RELAY</span><h2>Receive records</h2><p>Files, folders, ZIPs or verified e-bills.</p></section>
          <section className="receipt-step"><span className="receipt-number">02 · UNIT ADMIN / CLERK</span><h2>Check &amp; register</h2><p>Accounts, dates, amounts and surcharges.</p></section>
          <section className="receipt-step"><span className="receipt-number">03 · RECONCILIATION</span><h2>Reconcile</h2><p>Match provider lists; clear exceptions.</p></section>
          <section className="receipt-step"><span className="receipt-number">04 · PROCESSOR</span><h2>Prepare a batch</h2><p>One provider; authorised ministry scope.</p></section>
          <section className="receipt-step"><span className="receipt-number">05 · APPROVAL ROLES</span><h2>Approve the batch</h2><p>Batch decisions and stamped evidence.</p></section>
          <section className="receipt-step"><span className="receipt-number">06 · FINANCE + AUTH.</span><h2>Group &amp; authorise</h2><p>Same provider; independent authorisation.</p></section>
          <section className="receipt-step"><span className="receipt-number">07 · FINANCE</span><h2>Record settlement</h2><p>Execution, amounts and bank reference.</p></section>
          <section className="receipt-step"><span className="receipt-number">08 · BANK / AUDIT</span><h2>Match &amp; inspect</h2><p>Bank matching and discrepancy ownership.</p></section>
        </div>
        <p className="receipt-control">Required: reconciliation at both gates · batch approval · independent authorisation</p>
      </div>
      <footer className="deck-footer"><span>Read left to right, then next row · Demo records; no live transfer</span><span>02 / 14</span></footer>
    </div>
  );
}
