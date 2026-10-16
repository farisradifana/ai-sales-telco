"""Comparison periods. End dates are exclusive WIB calendar dates. Default = last COMPLETE month by ingestion
watermark (never the system date) vs the most recent earlier calendar month with the SAME number of days."""
from datetime import date, datetime, timedelta, timezone

from . import errors

WIB = timezone(timedelta(hours=7))
MAX_DAYS = 366


def today_wib(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(WIB).date()


def month_bounds(d: date) -> tuple[date, date]:
    first = d.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt


def last_complete_month(watermark: date) -> tuple[date, date]:
    first, nxt = month_bounds(watermark)
    if watermark == nxt - timedelta(days=1):
        return first, nxt
    prev_end = first
    return month_bounds(prev_end - timedelta(days=1))[0], prev_end


def equal_length_baseline(c0: date, c1: date) -> tuple[date, date]:
    days = (c1 - c0).days
    cursor = c0
    for _ in range(24):
        b0, b1 = month_bounds(cursor - timedelta(days=1))
        if (b1 - b0).days == days:
            return b0, b1
        cursor = b0
    return c0 - timedelta(days=days), c0


def validate_pair(b0: date, b1: date, c0: date, c1: date) -> None:
    if not (b0 < b1 and c0 < c1):
        raise errors.bad_request('INVALID_PERIOD', 'Tanggal awal harus sebelum tanggal akhir (akhir eksklusif).')
    if b1 > c0:
        raise errors.bad_request('INVALID_PERIOD', 'Periode baseline harus selesai sebelum periode current dimulai.')
    if (b1 - b0).days != (c1 - c0).days:
        raise errors.bad_request('UNEQUAL_PERIODS', 'Jumlah hari baseline dan current harus sama.',
                                 baseline_days=(b1 - b0).days, current_days=(c1 - c0).days)
    if (c1 - c0).days > MAX_DAYS:
        raise errors.bad_request('INVALID_PERIOD', f'Periode maksimal {MAX_DAYS} hari.')


def resolve(req, watermark: date | None) -> tuple[date, date, date, date, str]:
    if req.explicit:
        b0, b1, c0, c1 = req.baseline_start, req.baseline_end, req.current_start, req.current_end
        validate_pair(b0, b1, c0, c1)
        return b0, b1, c0, c1, 'explicit'
    if watermark is None:
        raise errors.bad_request('NO_WATERMARK', 'Watermark ingestion belum tersedia; sebutkan periode secara eksplisit.')
    c0, c1 = last_complete_month(watermark)
    b0, b1 = equal_length_baseline(c0, c1)
    validate_pair(b0, b1, c0, c1)
    return b0, b1, c0, c1, 'last_complete_month_by_watermark_vs_latest_equal_length_month'


def period_obj(start: date, end: date) -> dict:
    return {'start': start.isoformat(), 'end_exclusive': end.isoformat(), 'days': (end - start).days,
            'timezone': 'Asia/Jakarta'}


ID_MONTHS = ['Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni', 'Juli', 'Agustus', 'September', 'Oktober',
             'November', 'Desember']


def label(start: date, end: date) -> str:
    last = end - timedelta(days=1)
    if start.day == 1 and month_bounds(start)[1] == end:
        return f'{ID_MONTHS[start.month - 1]} {start.year}'
    return f'{start.day} {ID_MONTHS[start.month - 1]} {start.year}–{last.day} {ID_MONTHS[last.month - 1]} {last.year}'
