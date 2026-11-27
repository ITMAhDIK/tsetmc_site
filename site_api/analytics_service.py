"""Backward-compatible import path for the extracted Screener domain service.

The AnalyticsService name remains available to existing Market API callers;
new service code should import ScreenerService from screener_service.service.
"""
from screener_service.service import (
    SCREENER_PUBLIC_COLUMNS,
    ScreenerService as AnalyticsService,
    _build_screener_xlsx,
    _screener_public_row,
    _xlsx_column_name,
)

__all__ = [
    "AnalyticsService",
    "SCREENER_PUBLIC_COLUMNS",
    "_build_screener_xlsx",
    "_screener_public_row",
    "_xlsx_column_name",
]
