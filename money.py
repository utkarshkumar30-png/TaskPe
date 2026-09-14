"""Exact money primitives used by TaskPe financial code.

INR is represented as integer paise (100 paise per rupee), USDT as integer
micro-USDT (1,000,000 units per USDT), and percentages as basis points
(10,000 basis points per 100%).  Conversion rejects values that cannot be
represented at the selected precision; business calculations must apply an
explicit rounding rule before conversion.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

INR_SCALE = 100
USDT_SCALE = 1_000_000
BPS_SCALE = 10_000


def decimal_value(value):
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid monetary value: {value!r}") from exc


def to_scaled(value, scale, places, *, rounding=None):
    amount = decimal_value(value)
    quantum = Decimal(1).scaleb(-places)
    if rounding is None:
        if amount != amount.quantize(quantum):
            raise ValueError(f"Value {amount} exceeds supported precision of {places} decimals")
        normalized = amount
    else:
        normalized = amount.quantize(quantum, rounding=rounding)
    scaled = normalized * Decimal(scale)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"Value {amount} cannot be represented at scale {scale}")
    return int(scaled)


def inr_to_paise(value, *, rounding=None):
    return to_scaled(value, INR_SCALE, 2, rounding=rounding)


def paise_to_inr(value):
    return Decimal(int(value or 0)) / Decimal(INR_SCALE)


def usdt_to_micro(value, *, rounding=None):
    return to_scaled(value, USDT_SCALE, 6, rounding=rounding)


def micro_to_usdt(value):
    return Decimal(int(value or 0)) / Decimal(USDT_SCALE)


def percentage_to_bps(value, *, rounding=None):
    return to_scaled(value, 100, 2, rounding=rounding)


def bps_of_paise(amount_paise, basis_points):
    """Calculate a percentage of paise, rounding once to a whole paise."""
    result = (Decimal(int(amount_paise)) * Decimal(int(basis_points)) / Decimal(BPS_SCALE))
    return int(result.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def rate_paise_per_unit(value):
    return inr_to_paise(value)

