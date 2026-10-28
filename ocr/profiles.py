"""
Built-in reading profiles for Mauritian utility bills whose layouts are known.

A profile is recognised from text printed on the bill (VAT registration
number, company name), and gives a regular expression per field; the first
group is the value (two groups for a billing period). Profiles are the most
trusted source for that provider, then the usual cross-checks still apply.
Administrators can add their own patterns per provider (OcrTemplate.patterns).
"""

AMOUNT = r"(-?\d{1,3}(?:,\d{3})*\.\d{2}|-?\d+\.\d{2})"
D_SLASH = r"(\d{2}/\d{2}/\d{4})"
D_DOT = r"(\d{2}\.\d{2}\.\d{2,4})"

PROFILES = [
    {
        "key": "mauritius-telecom",
        "name": "Mauritius Telecom – statement of account",
        "provider_codes": ["MT"],
        "match": [r"VAT20070778", r"Mauritius Telecom Ltd"],
        "invoice_number_pattern": r"^FINV\d{8}-\d{4}[A-Z]$",
        "patterns": {
            "invoice_number": r"Bill Number:?\s*(FINV\d{8}-\d{4}[A-Z])",
            "account_number": r"Bill\.?\s*Acc\.?\s*:?\s*\d+\s*/\s*(\d{5,})",
            "service_number": r"Line number:?\s*(\d{6,})",
            "issue_date": r"Bill issued on:?\s*" + D_SLASH,
            "due_date": r"Last date for payment is\s*" + D_SLASH,
            "period": r"Period:?\s*" + D_SLASH + r"\s*-\s*" + D_SLASH,
            "amount_net": r"TOTAL AMOUNT FOR THE PERIOD \(before VAT\):?\s*" + AMOUNT,
            "amount_tax": r"Value Added Tax:?\s*" + AMOUNT,
            "amount_total": r"TOTAL AMOUNT INCLUSIVE OF VAT:?\s*" + AMOUNT,
            "arrears": r"Arrears:?\s*" + AMOUNT,
        },
        "currency": "MUR",
    },
    {
        "key": "ceb",
        "name": "Central Electricity Board – e-Bill",
        "provider_codes": ["CEB"],
        "match": [r"VAT22000591", r"Central Electricity Board"],
        "invoice_number_pattern": "",  # 12-digit numbers are too generic to identify CEB on their own
        "patterns": {
            "invoice_number": r"Invoice No\.?\s*(\d{8,})",
            "issue_date": r"Invoice No\.?\s*\d{8,}\s*Date\s*" + D_DOT,
            "account_number": r"Contract Acct\.?\s*(\d{6,})",
            "service_number": r"#(\d{6,})\s+[\d,]+\s+[\d,]+\s+\d",
            "current_reading": r"#\d{6,}\s+([\d,]+)\s+[\d,]+\s+\d",
            "previous_reading": r"#\d{6,}\s+[\d,]+\s+([\d,]+)\s+\d",
            "consumption": r"Total units consumed\s*\(kWh\)\s*([\d,]+)",
            "period": r"Consumption Period\s*" + D_DOT + r"\s*to\s*" + D_DOT,
            "due_date": r"Due Date\s*" + D_DOT,
            "amount_total": r"Current amount payable by\s*\d{2}\.\d{2}\.\d{4}\s*\(B\)\s*" + AMOUNT,
            "arrears": r"\(A\)\s*" + AMOUNT,
        },
        "currency": "MUR",
    },
]


def for_text(text, provider=None):
    """The profile that applies to a document, by provider code or by text printed on it."""
    import re

    code = getattr(provider, "short_code", None)
    for profile in PROFILES:
        if code and code in profile["provider_codes"]:
            return profile
    for profile in PROFILES:
        if any(re.search(p, text) for p in profile["match"]):
            return profile
    return None
