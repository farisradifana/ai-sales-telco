"""Decimal-only money helpers. API emits money as decimal strings, never binary floats."""
import json
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

CENT = Decimal('0.01')


def dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError('float money is not allowed')
    return Decimal(str(value))


def money_str(value: Any) -> str:
    return str(dec(value).quantize(CENT, rounding=ROUND_HALF_UP))


def rupiah(value: Any) -> str:
    """Indonesian display: Rp7.332.900 or -Rp5.173.100 (cents shown only if non-zero)."""
    d = dec(value).quantize(CENT, rounding=ROUND_HALF_UP)
    sign = '-' if d < 0 else ''
    d = abs(d)
    whole = int(d)
    cents = int((d - whole) * 100)
    body = f'{whole:,}'.replace(',', '.')
    if cents:
        body += f',{cents:02d}'
    return f'{sign}Rp{body}'


def pct_id(ratio: Any, places: int = 2) -> str:
    q = Decimal(1).scaleb(-places)
    v = (dec(ratio) * 100).quantize(q, rounding=ROUND_HALF_UP)
    return f'{v}'.replace('.', ',') + '%'


def json_loads(s: str | bytes) -> Any:
    return json.loads(s, parse_float=Decimal)


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return str(o)
        return str(o)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), cls=DecimalEncoder, ensure_ascii=False)


def stringify_money(obj: Any) -> Any:
    """Recursively convert Decimal to str for API responses (keeps ints/bools/None)."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, float):
        raise TypeError('float in evidence payload')
    if isinstance(obj, dict):
        return {k: stringify_money(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [stringify_money(v) for v in obj]
    return obj
