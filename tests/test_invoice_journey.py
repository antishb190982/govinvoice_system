import pytest
from django.utils import timezone
from django.urls import reverse
from accounts.middleware import OTP_SESSION_KEY
from invoices.models import Invoice
from invoices.evidence import approval_evidence
from workflow.process import invoice_process

pytestmark = pytest.mark.django_db


def test_invoice_process_page_uses_compact_journey(client, people, make_invoice):
    invoice = make_invoice()
    client.force_login(people["clerk"])
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()

    response = client.get(reverse("workflow:process", args=[invoice.pk]))
    assert response.status_code == 200
    assert b'class="journey-flow"' in response.content
    assert b"Invoice journey" in response.content
    assert b"Current step" in response.content
    assert b'class="bpm-scroll"' not in response.content
    assert b'<details class="card journey-panel journey-collapse" id="invoice-journey">' in response.content
    copy_step = next(step for step in response.context["nodes"] if step.get("watermark_copy"))
    assert not copy_step["copy_ready"]
    assert any("Final approval" in missing for missing in copy_step["missing_fields"])


def test_approved_invoice_journey_links_the_watermarked_copy(make_invoice, people):
    invoice = make_invoice()
    invoice.status = Invoice.Status.APPROVED
    invoice.approved_at = timezone.now()
    invoice.save(update_fields=["status", "approved_at"])

    journey = invoice_process(invoice, people["clerk"])
    copy_step = next(step for step in journey["nodes"] if step.get("watermark_copy"))
    assert copy_step["copy_ready"]
    assert copy_step["url"] == reverse("invoices:approved_copy", args=[invoice.pk])
    assert copy_step["bank_reference"] == "Pending bank settlement"
    assert any("Bank settlement reference" in missing for missing in copy_step["missing_fields"])
    assert approval_evidence(invoice)


def test_department_journey_uses_scoped_workflow_roles(make_invoice, people, policy):
    invoice = make_invoice()
    finance = invoice_process(invoice, people["finance"])
    clerk = invoice_process(invoice, people["clerk"])
    verifier = invoice_process(invoice, people["verifier"])
    admin = invoice_process(invoice, people["admin"])

    def labels(process):
        return {step["label"] for step in process["department_nodes"]}

    assert "Prepare and sign payment instruction" in labels(finance)
    assert "Verify" not in labels(finance)
    assert all(step["handoff"] for step in finance["department_nodes"]
               if step["label"] == "Review / correction and submission")
    assert "Review / correction and submission" in labels(clerk)
    assert "Prepare and sign payment instruction" not in labels(clerk)
    assert "Verify" in labels(verifier)
    assert "Authorise payment" not in labels(verifier)
    assert len(admin["department_nodes"]) == len(admin["nodes"])
    assert all(process["current"] in process["department_nodes"]
               for process in [finance, clerk, verifier, admin])
    assert all("Watermarked invoice copy" in labels(process)
               for process in [finance, clerk, verifier, admin])
    assert any(step["assigned_to_you"] for step in clerk["department_nodes"])


def test_batch_journey_collapsed_and_other_ministry_cannot_open_it(client, people, make_invoice):
    from operations import services

    invoice = make_invoice()
    batch = services.create_batch(people["clerk"], invoice.org_unit, "Journey batch", [invoice.pk])

    def sign_in(user):
        client.force_login(user)
        session = client.session
        session[OTP_SESSION_KEY] = True
        session.save()

    sign_in(people["clerk"])
    response = client.get(batch.get_absolute_url())
    assert response.status_code == 200
    assert b'<details class="card journey-collapse" id="batch-journey">' in response.content
    assert len(response.context["batch_journey"]["processes"]) == 1
    assert response.context["batch_journey"]["page"].paginator.per_page == 20
    assert response.context["batch_journey"]["processes"][0]["invoice"] == invoice
    review = client.get(reverse("workflow:batch_review", args=[batch.pk]))
    assert review.status_code == 200
    assert b'id="batch-journey"' in review.content

    sign_in(people["outsider"])
    assert client.get(batch.get_absolute_url()).status_code == 404
    assert client.get(reverse("workflow:process", args=[invoice.pk])).status_code == 404
    assert client.get(reverse("workflow:batch_process", args=[batch.pk])).status_code == 404


def test_role_in_another_ministry_does_not_expand_department_journey(make_invoice, people, tree, policy):
    from org.models import Membership, Role

    invoice = make_invoice()
    Membership.objects.create(user=people["verifier"], org_unit=tree["moe"], role=Role.FINANCE)
    process = invoice_process(invoice, people["verifier"])
    assert "Prepare and sign payment instruction" not in {
        step["label"] for step in process["department_nodes"]}
    assert "Finance officer" not in process["journey_scope"]


def test_department_can_inspect_own_part_of_parent_batch_without_sibling_records(
        client, make_invoice, people, tree):
    from operations import services
    from org.models import Membership, OrgUnit
    from providers.models import ServiceAccount

    invoice, other = make_invoice(), make_invoice()
    sibling = OrgUnit.objects.create(kind="department", code="MOH-OTHER", name="Other department",
                                    parent=tree["moh"])
    sibling_account = ServiceAccount.objects.create(provider=invoice.provider, org_unit=sibling,
                                                    account_number="OTHER-DEPT-ACCOUNT")
    Invoice.objects.filter(pk=other.pk).update(org_unit=sibling, service_account=sibling_account)
    batch = services.create_batch(people["clerk"], tree["moh"], "Ministry batch", [invoice.pk, other.pk])
    Membership.objects.filter(user=people["verifier"]).update(org_unit=tree["hosp"])
    client.force_login(people["verifier"])
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()

    response = client.get(reverse("workflow:batch_process", args=[batch.pk]))
    assert response.status_code == 200
    assert [p["invoice"].pk for p in response.context["batch_journey"]["processes"]] == [invoice.pk]
    assert other.reference.encode() not in response.content
    assert b"OTHER-DEPT-ACCOUNT" not in response.content
    assert client.get(batch.get_absolute_url()).status_code == 404


def test_expired_assignment_does_not_grant_journey_access(client, people, make_invoice):
    from datetime import timedelta
    from org.models import Membership

    invoice = make_invoice()
    Membership.objects.filter(user=people["verifier"]).update(
        valid_until=timezone.localdate() - timedelta(days=1))
    client.force_login(people["verifier"])
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()
    assert client.get(reverse("workflow:process", args=[invoice.pk])).status_code == 404


def test_paid_invoice_journey_accepts_date_only_settlement(client, people, make_invoice):
    invoice = make_invoice()
    invoice.status = Invoice.Status.PAID
    invoice.approved_at = timezone.now()
    invoice.paid_at = timezone.localdate()
    invoice.save()
    client.force_login(people["clerk"])
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()
    response = client.get(reverse("workflow:process", args=[invoice.pk]))
    assert response.status_code == 200
    paid_step = next(step for step in response.context["nodes"] if step["label"] == "Paid (invoice ledger)")
    assert paid_step["date_only"] is True
    assert paid_step["date"] == timezone.localdate()
