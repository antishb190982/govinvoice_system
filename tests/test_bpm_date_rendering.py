from datetime import date, datetime

from django.template.loader import render_to_string


def test_process_map_accepts_date_and_datetime_events():
    html = render_to_string("workflow/_process_map.html", {
        "nodes": [
            {"label": "Settlement", "state": "complete", "date": date(2026, 10, 29)},
            {"label": "Approval", "state": "complete",
             "date": datetime(2026, 10, 28, 14, 37)},
        ],
    })
    assert "29 Oct 2026" in html
    assert "28 Oct 2026 14:37" in html
