"""Gregorian/Jalali date formatting shared by Market Data responses."""


def _gregorian_to_jdn(year, month, day):
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    return (day + (153 * m + 2) // 5 + 365 * y + y // 4
            - y // 100 + y // 400 - 32045)


def _jdn_to_jalali(jdn):
    epoch = 1948320
    cycle_days = 12053
    leap_years = (0, 4, 8, 12, 16, 21, 25, 29)
    days = jdn - epoch
    cycles, rem = divmod(days, cycle_days)
    year = cycles * 33 + 1
    for y in range(33):
        year_days = 366 if y in leap_years else 365
        if rem < year_days:
            year += y
            day_of_year = rem
            break
        rem -= year_days
    else:  # pragma: no cover
        year += 32
        day_of_year = rem
    if day_of_year < 186:
        month = day_of_year // 31 + 1
        day = day_of_year % 31 + 1
    else:
        day_of_year -= 186
        month = day_of_year // 30 + 7
        day = day_of_year % 30 + 1
    return year, month, day


def gregorian_to_jalali_date(value):
    """Convert YYYY-MM-DD or YYYYMMDD to canonical Jalali YYYY-MM-DD."""
    raw = str(value or "").strip().replace("/", "-")
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    parts = raw.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return raw
    year, month, day = map(int, parts)
    jy, jm, jd = _jdn_to_jalali(_gregorian_to_jdn(year, month, day))
    return f"{jy:04d}-{jm:02d}-{jd:02d}"


def normalize_symbol(value):
    """Normalize common Persian/Arabic glyph variants without altering symbols."""
    return str(value or "").strip().replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")


def normalize_gregorian_date(value):
    """Normalize YYYYMMDD, YYYY/MM/DD or YYYY-MM-DD to YYYY-MM-DD."""
    raw = str(value or "").strip().replace("/", "-")
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw
