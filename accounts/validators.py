import re

from django.core.exceptions import ValidationError


class ComplexityValidator:
    """Require upper case, lower case, digit and symbol."""

    rules = [
        (r"[A-Z]", "an upper-case letter"),
        (r"[a-z]", "a lower-case letter"),
        (r"\d", "a digit"),
        (r"[^A-Za-z0-9]", "a symbol"),
    ]

    def validate(self, password, user=None):
        missing = [label for pattern, label in self.rules if not re.search(pattern, password)]
        if missing:
            raise ValidationError("Password must contain " + ", ".join(missing) + ".", code="password_complexity")

    def get_help_text(self):
        return "Your password must mix upper and lower case letters, digits and symbols."
