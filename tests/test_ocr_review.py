from ocr.views import ConfirmForm


def test_invoice_check_does_not_adjust_shared_template_by_default():
    assert ConfirmForm(values={}).fields["learn"].initial is False
