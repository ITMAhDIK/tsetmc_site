"""Shared application errors used by HTTP routing and API handlers."""


class ApiError(Exception):
    """An expected API failure with an HTTP status and public-safe detail."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail
