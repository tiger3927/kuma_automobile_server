"""金额与积分：一律整数最小货币单位（分），禁止浮点（Task-Plan.md §7.1）。"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def to_minor(amount: str | int | float | Decimal, *, exponent: int = 2) -> int:
    d = Decimal(str(amount))
    return int((d * (10 ** exponent)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor(minor: int, *, exponent: int = 2) -> Decimal:
    return (Decimal(minor) / (10 ** exponent)).quantize(Decimal("0.01"))


def fmt(minor: int, currency: str, *, exponent: int = 2) -> str:
    return f"{from_minor(minor, exponent=exponent)} {currency}"


def split(minor: int, ratios: dict[str, float]) -> dict[str, int]:
    """按比例拆分并保证**总和等于原值**（余数给最大项，避免分账丢分）。"""
    total = sum(ratios.values())
    if not ratios or total <= 0:
        raise ValueError("ratios 不能为空且之和必须 > 0")
    out = {k: int(minor * (v / total)) for k, v in ratios.items()}
    remainder = minor - sum(out.values())
    if remainder:
        biggest = max(out, key=lambda k: out[k])
        out[biggest] += remainder
    return out
