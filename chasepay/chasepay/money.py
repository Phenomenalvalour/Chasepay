from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "CA$", "AUD": "A$", "NZD": "NZ$", "NGN": "₦"}


def parse_amount(text):
    """'750' or '1,250.50' -> cents. Raises ValueError on bad or non-positive input."""
    try:
        value = Decimal(text.replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        raise ValueError("Enter the amount as a number, like 750 or 1250.50.")
    if value <= 0:
        raise ValueError("The amount must be more than zero.")
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fmt(cents, currency="USD"):
    return f"{SYMBOLS.get(currency, currency + ' ')}{cents / 100:,.2f}"
