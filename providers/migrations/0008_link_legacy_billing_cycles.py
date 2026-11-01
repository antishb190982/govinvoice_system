from django.db import migrations


def link_existing(apps, schema_editor):
    """Classify legacy metadata as unknown, never invent historical cycle evidence."""
    db = schema_editor.connection.alias
    Cycle = apps.get_model("providers", "BillingCycle")
    Provider = apps.get_model("providers", "Provider")
    Invoice = apps.get_model("invoices", "Invoice")
    Statement = apps.get_model("reconciliation", "Statement")
    Batch = apps.get_model("imports", "ImportBatch")
    Row = apps.get_model("imports", "ImportRow")
    ids = {}
    for pk in Provider.objects.using(db).values_list("pk", flat=True):
        cycle, _ = Cycle.objects.using(db).get_or_create(
            provider_id=pk, code="UNASSIGNED", defaults={"name": "Awaiting cycle assignment"})
        ids[pk] = cycle.pk
        Invoice.objects.using(db).filter(provider_id=pk, billing_cycle__isnull=True).update(billing_cycle=cycle)
        Statement.objects.using(db).filter(provider_id=pk, billing_cycle__isnull=True).update(billing_cycle=cycle)
        Batch.objects.using(db).filter(provider_id=pk, billing_cycle__isnull=True).update(billing_cycle=cycle)
    through = Batch.billing_cycles.through
    for row in Row.objects.using(db).select_related("batch", "invoice").iterator(chunk_size=500):
        cycle_id = (row.invoice.billing_cycle_id if row.invoice_id else None) or row.batch.billing_cycle_id
        if not cycle_id:
            cycle_id = ids.get((row.data or {}).get("provider_id"))
        if cycle_id:
            Row.objects.using(db).filter(pk=row.pk).update(billing_cycle_id=cycle_id)
            through.objects.using(db).get_or_create(importbatch_id=row.batch_id, billingcycle_id=cycle_id)
    for batch in Batch.objects.using(db).exclude(billing_cycle__isnull=True).iterator(chunk_size=500):
        through.objects.using(db).get_or_create(importbatch_id=batch.pk, billingcycle_id=batch.billing_cycle_id)


class Migration(migrations.Migration):
    dependencies = [
        ("providers", "0007_billingcycle"),
        ("invoices", "0009_invoice_billing_cycle"),
        ("reconciliation", "0004_statement_billing_cycle"),
        ("imports", "0006_importbatch_billing_cycle_importbatch_billing_cycles_and_more"),
    ]
    operations = [migrations.RunPython(link_existing, migrations.RunPython.noop)]
