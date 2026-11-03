"""Numbers printed into forms must round-trip: a grouped "1,297" broke batch decisions."""
from decimal import Decimal

from django.template import Context, Template


def test_ids_and_amounts_render_machine_readable():
    html = Template('<input value="{{ pk }}" data-amount="{{ amount }}">').render(
        Context({"pk": 1297, "amount": Decimal("301200.00")}))
    assert 'value="1297"' in html
    assert 'data-amount="301200.00"' in html


def test_displayed_money_is_still_grouped():
    html = Template("{% load gov %}{{ amount|money:'MUR' }} {{ big|compact }}").render(
        Context({"amount": Decimal("301200"), "big": 1500}))
    assert "MUR 301,200.00" in html
    assert "1.5K" in html
