"""SMS gateway: a generic HTTP/JSON connector (for local operators' gateways) or Twilio."""

import base64
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from integrations import config

log = logging.getLogger(__name__)
LEVELS = {"info": 0, "warning": 1, "critical": 2}


class SmsError(Exception):
    pass


def normalise_number(number):
    digits = re.sub(r"[^\d+]", "", number or "")
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        digits = (config.get("SMS_COUNTRY_CODE") or "+230") + digits.lstrip("0")
    return digits if 8 <= len(digits) <= 16 else ""


def mask(number):
    return number[:4] + "•" * max(0, len(number) - 7) + number[-3:] if len(number) > 7 else "•••"


def _post(url, body, headers, timeout=15):
    if not url.startswith("https://") and not url.startswith("http://localhost"):
        raise SmsError("The SMS gateway URL must use HTTPS.")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (scheme checked above)
            return resp.status, resp.read(2000).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        raise SmsError(f"Gateway answered HTTP {exc.code}: {exc.read(300).decode(errors='replace')}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SmsError(f"Gateway unreachable: {exc}")


def _json_escape(text):
    return json.dumps(text)[1:-1]


def send_raw(to, text):
    """Send one SMS through the configured gateway; returns provider reference."""
    provider = config.get("SMS_PROVIDER")
    url, token, sender = config.get("SMS_URL"), config.get("SMS_TOKEN"), config.get("SMS_SENDER")
    if not url:
        raise SmsError("No SMS gateway URL is configured.")
    text = text[:459]  # three concatenated SMS
    if provider == "twilio":
        account = config.get("SMS_ACCOUNT")
        endpoint = f"{url.rstrip('/')}/2010-04-01/Accounts/{account}/Messages.json"
        body = urllib.parse.urlencode({"To": to, "From": sender, "Body": text}).encode()
        auth = base64.b64encode(f"{account}:{token}".encode()).decode()
        status, payload = _post(endpoint, body, {"Authorization": f"Basic {auth}",
                                                 "Content-Type": "application/x-www-form-urlencoded"})
        try:
            return json.loads(payload).get("sid", "")
        except ValueError:
            return ""
    template = config.get("SMS_BODY_TEMPLATE") or '{"to": "{to}", "message": "{text}", "from": "{sender}"}'
    body = template.replace("{to}", _json_escape(to)).replace("{text}", _json_escape(text)).replace(
        "{sender}", _json_escape(sender or ""))
    try:
        json.loads(body)
    except ValueError:
        raise SmsError("The SMS body template is not valid JSON.")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, payload = _post(url, body.encode(), headers)
    return payload[:120] if status < 300 else ""


def send(number, text, subject=""):
    from integrations.models import OutboundMessage

    to = normalise_number(number)
    if not to:
        return None
    if not config.get("SMS_ENABLED"):
        OutboundMessage.objects.create(channel="sms", recipient=mask(to), subject=subject[:200],
                                       status=OutboundMessage.Status.SKIPPED, error="SMS disabled")
        return None
    try:
        ref = send_raw(to, text)
        OutboundMessage.objects.create(channel="sms", recipient=mask(to), subject=subject[:200],
                                       status=OutboundMessage.Status.SENT, provider_reference=str(ref)[:120])
        return True
    except SmsError as exc:
        log.warning("SMS to %s failed: %s", mask(to), exc)
        OutboundMessage.objects.create(channel="sms", recipient=mask(to), subject=subject[:200],
                                       status=OutboundMessage.Status.FAILED, error=str(exc)[:2000])
        return False


def wanted(level):
    return config.get("SMS_ENABLED") and LEVELS.get(level, 0) >= LEVELS.get(config.get("SMS_MIN_LEVEL") or "critical", 2)
