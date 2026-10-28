"""
Field analysis: decides which part of the invoice holds each value.

For every field the analyser gathers candidates from several sources and keeps
the most credible one:

  template  – the provider's learned/drawn zone (anchored to a nearby label)
  registry  – an account number already registered for the identified provider
  label     – a value to the right of, or under, a recognised label
              ("Total Amount Due", "المبلغ المستحق", "Account No", …)
  computed  – derived from other fields (net + tax)
  pattern   – last resort: first plausible value anywhere on the page

Confidence combines label match quality, layout position, OCR word confidence
and cross-checks (net + tax = total, readings = consumption, registered account).
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

from django.conf import settings

from ocr.engine import lines_of, normalise_digits, union, words_in

LABELS = {
    "account_number": ["account no", "account number", "acct no", "a/c no", "account", "contract account",
                       "contract no", "customer no", "customer number", "customer account", "subscriber no",
                       "service no", "service number", "consumer no", "premise no", "meter account",
                       "رقم الحساب", "رقم المشترك", "رقم العقد", "رقم الاشتراك",
                       "numéro de compte", "n° de compte", "no de compte", "compte client", "numéro client", "n° client", "réf client"],
    "invoice_number": ["invoice no", "invoice number", "invoice #", "tax invoice no", "bill no", "bill number",
                       "bill ref", "document no", "inv no", "رقم الفاتورة", "numéro de facture", "n° de facture", "no facture", "facture n°"],
    "issue_date": ["invoice date", "bill date", "issue date", "date of issue", "billing date", "document date",
                   "تاريخ الفاتورة", "تاريخ الإصدار", "date de facture", "date de facturation", "date d émission"],
    "due_date": ["due date", "payment due date", "payment due", "pay by", "last date for payment",
                 "last payment date", "due on", "تاريخ الاستحقاق", "آخر موعد للدفع",
                 "date d échéance", "date limite de paiement", "à payer avant le", "payable avant le"],
    "period": ["billing period", "service period", "period", "consumption period", "فترة الفاتورة", "الفترة", "période de facturation", "période"],
    "amount_total": ["total amount due", "amount due", "total due", "net payable", "total payable",
                     "amount payable", "grand total", "balance due", "total amount", "invoice total",
                     "المبلغ المستحق", "الإجمالي", "المبلغ الإجمالي", "المجموع",
                     "montant dû", "montant à payer", "total à payer", "net à payer", "total ttc", "montant total"],
    "amount_tax": ["vat", "vat amount", "value added tax", "tax amount", "tax", "ضريبة القيمة المضافة", "الضريبة", "tva", "montant tva"],
    "amount_net": ["amount before tax", "amount before vat", "subtotal", "sub total", "sub-total", "net amount",
                   "amount excl vat", "taxable amount", "charges", "المبلغ قبل الضريبة", "montant ht", "total ht", "hors taxes"],
    "consumption": ["consumption", "units consumed", "total consumption", "usage", "units", "الاستهلاك", "consommation"],
    "previous_reading": ["previous reading", "prev reading", "opening reading", "last reading", "القراءة السابقة", "ancien index", "index précédent"],
    "current_reading": ["current reading", "present reading", "closing reading", "القراءة الحالية", "nouvel index", "index actuel"],
}
# Stronger labels win ties between candidates of the same field.
LABEL_STRENGTH = {"amount_total": {"total amount due": 1.0, "amount due": 0.97, "net payable": 0.97,
                                   "total payable": 0.97, "amount payable": 0.95, "balance due": 0.93,
                                   "grand total": 0.92, "total due": 0.95, "total amount": 0.85,
                                   "invoice total": 0.85, "المبلغ المستحق": 0.97, "الإجمالي": 0.85,
                                   "المبلغ الإجمالي": 0.9, "المجموع": 0.8, "montant dû": 0.97, "net à payer": 0.97,
                                   "total à payer": 0.97, "montant à payer": 0.95, "total ttc": 0.93,
                                   "montant total": 0.85},
                  "account_number": {"account": 0.75, "units": 0.6}}
TYPES = {"account_number": "id", "invoice_number": "id", "issue_date": "date", "due_date": "date",
         "period_start": "date", "period_end": "date", "amount_total": "amount", "amount_tax": "amount",
         "amount_net": "amount", "consumption": "number", "previous_reading": "number",
         "current_reading": "number", "currency": "currency"}
REQUIRED = ("provider", "account_number", "invoice_number", "issue_date", "due_date", "amount_total")
CURRENCIES = {"MUR", "RS", "OMR", "AED", "SAR", "ZAR", "INR", "USD", "EUR", "GBP"}
CURRENCY_ALIASES = {"RS": "MUR", "RS.": "MUR", "RO": "OMR", "ر.ع": "OMR"}
MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|" \
         "august|september|october|november|december"
DATE_RE = re.compile(
    rf"\b(\d{{1,2}}[/.\-]\d{{1,2}}[/.\-]\d{{2,4}}|\d{{4}}[/.\-]\d{{1,2}}[/.\-]\d{{1,2}}|"
    rf"\d{{1,2}}[ \-](?:{MONTHS})[a-z]*[ \-,]+\d{{2,4}}|(?:{MONTHS})[a-z]* \d{{1,2}},? \d{{4}})\b", re.I)
AMOUNT_RE = re.compile(r"\(?-?\d{1,3}(?:[, ]\d{3})+(?:\.\d{1,3})?\)?|\(?-?\d+(?:[.,]\d{1,3})?\)?")
ID_RE = re.compile(r"\b[A-Z0-9][A-Z0-9\-/]{3,}[A-Z0-9]\b", re.I)
OCR_CONFUSIONS = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "I": "1", "l": "1", "|": "1", "S": "5",
                                "s": "5", "B": "8", "Z": "2", "z": "2", "G": "6"})


def _norm(text):
    text = normalise_digits(text).lower()
    text = re.sub(r"[^\w؀-ۿ/#%]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def compact_id(value):
    return re.sub(r"[\s\-/.]", "", (value or "")).upper()


def ocr_equivalent(a, b):
    """Equality tolerant to typical OCR character confusions."""
    a, b = compact_id(a), compact_id(b)
    return a == b or a.translate(OCR_CONFUSIONS) == b.translate(OCR_CONFUSIONS)


# --- Typed parsing ------------------------------------------------------------------

PERCENT_RE = re.compile(r"\(?\s*\d+(?:[.,]\d+)?\s*%\s*\)?")
SEPARATORS = {":", "-", "=", "–", "—", "#", "|"}


def parse_amount(text, decimal_comma=False):
    text = PERCENT_RE.sub(" ", normalise_digits(text))
    for token in AMOUNT_RE.findall(text.replace(" ", " ")):
        neg = token.startswith("(") and token.endswith(")") or token.startswith("-")
        raw = token.strip("()-").strip()
        if decimal_comma:
            raw = raw.replace(".", "").replace(" ", "").replace(",", ".")
        elif "," in raw and "." not in raw and re.fullmatch(r"\d+,\d{1,2}", raw):
            raw = raw.replace(",", ".")  # 12,50 style decimal
        else:
            raw = raw.replace(",", "").replace(" ", "")
        try:
            value = Decimal(raw)
        except InvalidOperation:
            continue
        return (-value if neg else value).quantize(Decimal("0.001")), token
    return None, None


def parse_date_text(text):
    from imports.parsers import parse_date

    text = normalise_digits(text)
    for match in DATE_RE.finditer(text):
        try:
            return parse_date(match.group(1).replace(",", ", ").replace("  ", " ")), match.group(1)
        except ValueError:
            try:
                return parse_date(match.group(1)), match.group(1)
            except ValueError:
                continue
    return None, None


def parse_dates(text):
    out = []
    for match in DATE_RE.finditer(normalise_digits(text)):
        value, raw = parse_date_text(match.group(1))
        if value:
            out.append((value, raw))
    return out


def parse_id(text):
    for match in ID_RE.finditer(normalise_digits(text)):
        token = match.group(0)
        if any(ch.isdigit() for ch in token) and not DATE_RE.fullmatch(token):
            return token.strip("-/"), token
    return None, None


def parse_currency(text):
    up = normalise_digits(text).upper()
    for alias, code in CURRENCY_ALIASES.items():
        if alias.upper() in up.split() or alias in text:
            return code, alias
    for code in CURRENCIES:
        if re.search(rf"\b{code}\b", up):
            return CURRENCY_ALIASES.get(code, code), code
    return None, None


def parse_typed(field, text, decimal_comma=False):
    kind = TYPES[field]
    if kind == "amount":
        value, raw = parse_amount(text, decimal_comma)
        return (value, raw) if value is not None and value >= 0 else (None, None)
    if kind == "number":
        value, raw = parse_amount(text, decimal_comma)
        return (value, raw) if value is not None and value >= 0 else (None, None)
    if kind == "date":
        return parse_date_text(text)
    if kind == "currency":
        return parse_currency(text)
    return parse_id(text)


# --- Label search --------------------------------------------------------------------

def find_labels(line, labels):
    """Yield (label, score, label_word_slice_end) for labels found in a line, fuzzy."""
    words = line["words"]
    norms = [_norm(w["t"]) for w in words]
    for label in labels:
        target = _norm(label)
        n = len(target.split())
        for i in range(0, max(1, len(norms) - n + 1)):
            window = " ".join(x for x in norms[i:i + n] if x)
            if not window:
                continue
            if window == target:
                score = 1.0
            else:
                score = SequenceMatcher(None, window, target).ratio()
                if score < 0.86 or len(target) < 4:
                    continue
            yield label, score, i, i + n


def _starts_like_value(words, field):
    """The first meaningful token after a label must look like the field's type,
    so "Tax Invoice No" never yields a VAT amount."""
    for w in words:
        t = w["t"].strip()
        if not t or t in SEPARATORS or "%" in t or t.rstrip(":").upper() in CURRENCIES | set(CURRENCY_ALIASES):
            continue
        t = t.strip(":#")
        if not t:
            continue
        if TYPES[field] == "date":
            return t[0].isdigit() or bool(re.match(MONTHS, t.lower()))
        if TYPES[field] == "currency":
            return True
        return any(ch.isdigit() for ch in t)
    return False


def _value_after(line, end_index, field, decimal_comma):
    rest = line["words"][end_index:]
    if not _starts_like_value(rest, field):
        return None
    # Skip separators such as ":" or "-" and stop at a large horizontal gap.
    picked, last_x = [], None
    for w in rest:
        if last_x is not None and w["b"][0] - last_x > 0.5:
            break
        picked.append(w)
        last_x = w["b"][2]
    text = " ".join(w["t"] for w in picked)
    value, raw = parse_typed(field, text, decimal_comma)
    if value is None:
        return None
    used = [w for w in picked if raw and _norm(w["t"]) and _norm(w["t"]) in _norm(raw)] or picked[:1]
    return value, raw, used


def _value_below(lines, idx, label_box, field, decimal_comma):
    for nxt in lines[idx + 1: idx + 3]:
        if nxt["bbox"][1] - label_box[3] > 0.05:
            break
        column = [w for w in nxt["words"] if w["b"][2] >= label_box[0] - 0.02 and w["b"][0] <= label_box[2] + 0.12]
        if not column or not _starts_like_value(column, field):
            continue
        text = " ".join(w["t"] for w in column)
        value, raw = parse_typed(field, text, decimal_comma)
        if value is not None:
            used = [w for w in column if _norm(w["t"]) and _norm(w["t"]) in _norm(raw)] or column[:1]
            return value, raw, used
    return None


def _label_key(field):
    return "period" if field in ("period_start", "period_end") else field


def _covered_by_other(line, field, start, end):
    """True if a longer label of another field spans these words, e.g. "tax" inside
    "Amount before tax", or "account" inside "Account No" handled by its own field."""
    own = _label_key(field)
    for other, labels in LABELS.items():
        if other == own:
            continue
        for label, score, s2, e2 in find_labels(line, labels):
            if s2 <= start and e2 >= end and (e2 - s2) > (end - start):
                return True
    return False


def label_candidates(pages, field, decimal_comma=False):
    out = []
    labels = LABELS[_label_key(field)]
    strengths = LABEL_STRENGTH.get(field, {})
    for page in pages:
        lines = lines_of(page)
        for idx, line in enumerate(lines):
            for label, score, start, end in find_labels(line, labels):
                if _covered_by_other(line, field, start, end):
                    continue
                label_box = union([w["b"] for w in line["words"][start:end]])
                strength = strengths.get(label, 0.95) * score
                if field in ("period_start", "period_end"):
                    text = " ".join(w["t"] for w in line["words"][end:])
                    dates = parse_dates(text)
                    if len(dates) < 2 and idx + 1 < len(lines):
                        dates += parse_dates(lines[idx + 1]["text"])
                    if len(dates) >= 2:
                        value, raw = dates[0] if field == "period_start" else dates[1]
                        used = [w for w in line["words"][end:] if _norm(raw) in _norm(w["t"]) or _norm(w["t"]) in _norm(raw)]
                        out.append(_cand(value, raw, used or line["words"][end:end + 1], page["index"],
                                         strength * 0.95, "label", f"after “{label}”", label_box))
                    continue
                found = _value_after(line, end, field, decimal_comma)
                position = 1.0
                if not found:
                    found = _value_below(lines, idx, label_box, field, decimal_comma)
                    position = 0.85
                if found:
                    value, raw, used = found
                    out.append(_cand(value, raw, used, page["index"], strength * position, "label",
                                     f"{'right of' if position == 1.0 else 'below'} label “{label}”", label_box))
    return out


def _cand(value, raw, words, page, score, source, evidence, label_box=None):
    ocr_conf = sum(w["c"] for w in words) / len(words) / 100 if words else 0.6
    return {"value": value, "raw": raw, "page": page, "bbox": union([w["b"] for w in words]) if words else None,
            "label_bbox": label_box, "confidence": round(min(0.99, score * (0.65 + 0.35 * ocr_conf)), 3),
            "source": source, "evidence": evidence}


# --- Provider identification -------------------------------------------------------------

def _best_line_match(lines, needle):
    needle = _norm(needle)
    best, best_line = 0.0, None
    for line in lines:
        hay = _norm(line["text"])
        if not hay or not needle:
            continue
        if needle in hay:
            return 1.0, line
        score = SequenceMatcher(None, needle, hay).ratio()
        if len(hay) > len(needle):  # compare against the best window of the line
            n = len(needle)
            for start in range(0, len(hay) - n + 1, max(1, n // 4)):
                score = max(score, SequenceMatcher(None, needle, hay[start:start + n]).ratio())
        if score > best:
            best, best_line = score, line
    return best, best_line


def identify_provider(pages, providers, hint=None):
    """Score each provider by header name/aliases, tax number, IBAN and known account numbers."""
    first_lines = lines_of(pages[0]) if pages else []
    header = [ln for ln in first_lines if ln["bbox"][1] < 0.3]
    all_text = compact_id(" ".join(w["t"] for p in pages for w in p["words"]))
    tokens = {compact_id(w["t"]) for p in pages for w in p["words"]}
    results = []
    for provider in providers:
        score, evidence, box = 0.0, [], None
        names = [provider.name] + [s for s in (provider.identifiers or "").splitlines() if s.strip()]
        best_name, best_line = 0.0, None
        for name in names:
            s, line = _best_line_match(header, name)
            if s < 0.8:
                s2, line2 = _best_line_match(first_lines, name)
                s, line = (s2 * 0.8, line2) if s2 * 0.8 > s else (s, line)
            if s > best_name:
                best_name, best_line = s, line
        if best_name >= 0.75:
            score += 0.55 * best_name
            evidence.append(f"name in {'header' if best_line in header else 'text'} ({int(best_name * 100)}%)")
            box = best_line["bbox"] if best_line else None
        if provider.tax_number and compact_id(provider.tax_number) in all_text:
            score += 0.35
            evidence.append("tax number")
        if provider.bank_iban and compact_id(provider.bank_iban) in all_text:
            score += 0.3
            evidence.append("IBAN")
        accounts = list(provider.accounts.filter(is_active=True).values_list("account_number", flat=True)[:5000])
        if any(any(ocr_equivalent(t, a) for t in tokens) for a in accounts if len(compact_id(a)) >= 4):
            score += 0.45
            evidence.append("registered account number")
        if provider.short_code and compact_id(provider.short_code) in tokens:
            score += 0.15
            evidence.append("provider code")
        results.append((min(score, 0.99), provider, evidence, box))
    results.sort(key=lambda r: r[0], reverse=True)
    best = results[0] if results else (0, None, [], None)
    if hint is not None:
        hinted = next((r for r in results if r[1] == hint), (0, hint, [], None))
        conf = max(hinted[0], 0.9)
        evidence = ["chosen at upload"] + hinted[2]
        warning = None
        if best[1] != hint and best[0] >= 0.6 and hinted[0] < 0.3:
            warning = f"The document looks like {best[1].name}, not {hint.name}."
        return hint, conf, evidence, hinted[3], warning
    if best[0] < 0.35:
        return None, best[0], best[2], None, None
    return best[1], best[0], best[2], best[3], None


def registry_account(pages, provider):
    """Find a registered account number of this provider anywhere in the text."""
    accounts = list(provider.accounts.filter(is_active=True).values_list("account_number", flat=True)[:5000])
    if not accounts:
        return None
    for page in pages:
        words = page["words"]
        for i, w in enumerate(words):
            for span in (1, 2, 3):  # numbers split by spaces in print
                group = words[i:i + span]
                text = "".join(x["t"] for x in group)
                for acct in accounts:
                    if len(compact_id(acct)) >= 4 and ocr_equivalent(text, acct):
                        exact = compact_id(text) == compact_id(acct)
                        c = _cand(acct, text, group, page["index"], 0.99 if exact else 0.9, "registry",
                                  "registered account of the provider" + ("" if exact else " (OCR-corrected)"))
                        return c
    return None


# --- Templates -------------------------------------------------------------------------

def template_candidates(pages, template, decimal_comma=False):
    out = {}
    if not template or not template.is_active:
        return out
    for field, zone in (template.zones or {}).items():
        if field not in TYPES or zone.get("page", 0) >= len(pages):
            continue
        page = pages[zone.get("page", 0)]
        bbox, anchored = list(zone["bbox"]), False
        if zone.get("anchor") and zone.get("anchor_bbox"):
            for line in lines_of(page):
                for label, score, start, end in find_labels(line, [_norm(zone["anchor"])]):
                    found = union([w["b"] for w in line["words"][start:end]])
                    dx, dy = found[0] - zone["anchor_bbox"][0], found[1] - zone["anchor_bbox"][1]
                    bbox = [bbox[0] + dx, bbox[1] + dy, bbox[2] + dx, bbox[3] + dy]
                    anchored = True
                    break
                if anchored:
                    break
        words = words_in(page, bbox)
        if not words:
            continue
        value, raw = parse_typed(field, " ".join(w["t"] for w in words), decimal_comma)
        if value is None:
            continue
        used = [w for w in words if _norm(w["t"]) and _norm(w["t"]) in _norm(raw)] or words
        out[field] = _cand(value, raw, used, page["index"], 0.97 if anchored else 0.88, "template",
                           "provider template zone" + (" (anchored to label)" if anchored else ""))
    return out


def pattern_candidate(pages, field, decimal_comma=False):
    for page in pages:
        for line in lines_of(page):
            value, raw = parse_typed(field, line["text"], decimal_comma)
            if value is not None:
                used = [w for w in line["words"] if _norm(w["t"]) and _norm(w["t"]) in _norm(raw)] or line["words"][:1]
                return _cand(value, raw, used, page["index"], 0.35, "pattern", "first plausible value on the page")
    return None


# --- Orchestration ------------------------------------------------------------------------

def analyse(pages, providers, hint=None, template_lookup=None):
    """Return (extraction, provider, provider_confidence, template, warnings)."""
    warnings = []
    provider, p_conf, p_evidence, p_box, p_warning = identify_provider(pages, providers, hint)
    if p_warning:
        warnings.append(p_warning)
    extraction = {"provider": {"value": provider.short_code if provider else None,
                               "display": provider.name if provider else None, "raw": None,
                               "confidence": round(p_conf, 3), "page": 0, "bbox": p_box,
                               "source": "identification", "evidence": ", ".join(p_evidence) or "no match"}}
    template = template_lookup(provider) if (template_lookup and provider) else None
    decimal_comma = False
    zoned = template_candidates(pages, template, decimal_comma)

    for field in TYPES:
        if field == "currency":
            continue
        candidates = []
        if field in zoned:
            candidates.append(zoned[field])
        if field == "account_number" and provider:
            reg = registry_account(pages, provider)
            if reg:
                candidates.append(reg)
        candidates += label_candidates(pages, field, decimal_comma)
        if field == "amount_total":
            # Among strong "total" labels the payable amount is usually the largest.
            strong = [c for c in candidates if c["source"] == "label" and c["confidence"] >= 0.6]
            if len(strong) > 1:
                top = max(strong, key=lambda c: (c["value"], c["confidence"]))
                top["confidence"] = round(min(0.99, top["confidence"] + 0.05), 3)
        if not candidates and field in ("invoice_number", "issue_date", "due_date", "amount_total"):
            fallback = pattern_candidate(pages, field, decimal_comma)
            if fallback:
                candidates.append(fallback)
        if candidates:
            best = max(candidates, key=lambda c: c["confidence"])
            best["alternatives"] = len(candidates) - 1
            extraction[field] = best

    # Account number seen only via its label: snap to a registered account if it is an OCR variant.
    acct = extraction.get("account_number")
    if acct and acct["source"] == "label" and provider:
        for registered in provider.accounts.filter(is_active=True).values_list("account_number", flat=True):
            if ocr_equivalent(str(acct["value"]), registered):
                acct.update(value=registered, confidence=round(min(0.97, acct["confidence"] + 0.15), 3),
                            evidence=acct["evidence"] + "; matches registered account")
                break

    currency, raw = parse_currency(" ".join(w["t"] for p in pages for w in p["words"]))
    if currency:
        extraction["currency"] = {"value": currency, "raw": raw, "confidence": 0.8, "page": 0, "bbox": None,
                                  "source": "pattern", "evidence": "currency code on the document"}
    _cross_check(extraction, warnings)
    for field in REQUIRED:
        item = extraction.get(field)
        if not item or item.get("value") in (None, ""):
            warnings.append(f"{field.replace('_', ' ').capitalize()} not found.")
        elif item["confidence"] < settings.OCR_REVIEW_THRESHOLD:
            warnings.append(f"{field.replace('_', ' ').capitalize()} is uncertain ({int(item['confidence'] * 100)}%).")
    return _jsonable(extraction), provider, p_conf, template, warnings


def _boost(item, by, note):
    item["confidence"] = round(min(0.99, item["confidence"] + by), 3)
    item["evidence"] = (item.get("evidence") or "") + f"; {note}"


def _cross_check(ex, warnings):
    net, tax, total = (ex.get(k) for k in ("amount_net", "amount_tax", "amount_total"))
    if net and tax and total:
        if abs(net["value"] + tax["value"] - total["value"]) <= Decimal("0.01"):
            for item in (net, tax, total):
                _boost(item, 0.12, "net + tax = total")
        else:
            warnings.append("Net + tax does not equal the total found.")
            total["confidence"] = round(total["confidence"] * 0.85, 3)
    elif net and tax and not total:
        ex["amount_total"] = {"value": net["value"] + tax["value"], "raw": None,
                              "confidence": round(min(net["confidence"], tax["confidence"]) * 0.8, 3),
                              "page": net["page"], "bbox": None, "source": "computed", "evidence": "net + tax"}
    prev, cur, cons = (ex.get(k) for k in ("previous_reading", "current_reading", "consumption"))
    if prev and cur and cons and abs(cur["value"] - prev["value"] - cons["value"]) <= Decimal("1"):
        for item in (prev, cur, cons):
            _boost(item, 0.1, "readings match consumption")
    issue, due = ex.get("issue_date"), ex.get("due_date")
    if issue and due and due["value"] < issue["value"]:
        warnings.append("Due date found is before the issue date.")
        due["confidence"] = round(due["confidence"] * 0.6, 3)
    elif issue and due and 5 <= (due["value"] - issue["value"]).days <= 120:
        for item in (issue, due):
            _boost(item, 0.07, "plausible payment terms")
    ps, pe = ex.get("period_start"), ex.get("period_end")
    if ps and pe and pe["value"] < ps["value"]:
        ex["period_start"], ex["period_end"] = pe, ps


def _jsonable(extraction):
    out = {}
    for field, item in extraction.items():
        item = dict(item)
        v = item.get("value")
        if isinstance(v, Decimal):
            item["value"] = str(v)
        elif isinstance(v, date):
            item["value"] = v.isoformat()
        out[field] = item
    return out
