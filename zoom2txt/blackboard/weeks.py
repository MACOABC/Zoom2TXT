"""Semanas académicas (Semana 1 … 16) a partir de la fecha de inicio
del ciclo configurada por el usuario."""

from __future__ import annotations

from datetime import date, datetime, timedelta

WEEKS_IN_TERM = 16


def parse_start(term_start: str) -> date | None:
    try:
        d = datetime.strptime(term_start, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    # normaliza al lunes de esa semana
    return d - timedelta(days=d.weekday())


def week_bounds(monday_week1: date, week: int) -> tuple[date, date]:
    """(lunes, domingo) de la semana `week` (1-indexado)."""
    start = monday_week1 + timedelta(days=7 * (week - 1))
    return start, start + timedelta(days=6)


def week_range_str(monday_week1: date, week: int) -> tuple[str, str]:
    lo, hi = week_bounds(monday_week1, week)
    return lo.isoformat(), hi.isoformat()


def week_label(monday_week1: date, week: int) -> str:
    lo, hi = week_bounds(monday_week1, week)
    return f"Semana {week}: {lo.strftime('%d/%m')} – {hi.strftime('%d/%m')}"


def week_of_str(monday_week1: date, timestamp: str) -> int | None:
    """Semana académica de un timestamp 'YYYY-MM-DD …' (None si cae
    fuera del ciclo o no se puede interpretar)."""
    try:
        d = datetime.strptime(timestamp[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return current_week(monday_week1, d)


def current_week(monday_week1: date, today: date | None = None) -> int | None:
    today = today or date.today()
    delta = (today - monday_week1).days
    if delta < 0:
        return None
    wk = delta // 7 + 1
    return wk if 1 <= wk <= WEEKS_IN_TERM else None
