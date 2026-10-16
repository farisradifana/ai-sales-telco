"""Stable error contract: {"error": {"code", "message", "correlation_id", "details"}}."""
from typing import Any


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def bad_request(code: str, message: str, **details) -> AppError:
    return AppError(400, code, message, details)


def unauthenticated(message: str = 'Autentikasi diperlukan.') -> AppError:
    return AppError(401, 'UNAUTHENTICATED', message)


def denied(message: str = 'Cakupan tidak diizinkan.') -> AppError:
    # Never echo outlet/district names for scopes the principal cannot see.
    return AppError(403, 'SCOPE_DENIED', message)


def role_denied(message: str = 'Peran tidak diizinkan untuk aksi ini.') -> AppError:
    return AppError(403, 'ROLE_DENIED', message)


def not_found(message: str = 'Tidak ditemukan.') -> AppError:
    return AppError(404, 'NOT_FOUND', message)


def conflict(code: str, message: str, **details) -> AppError:
    return AppError(409, code, message, details)


def gone(code: str, message: str, **details) -> AppError:
    return AppError(410, code, message, details)


def unprocessable(code: str, message: str, **details) -> AppError:
    return AppError(422, code, message, details)


def rate_limited() -> AppError:
    return AppError(429, 'RATE_LIMITED', 'Terlalu banyak permintaan; coba lagi sebentar.')


def unavailable(code: str = 'UPSTREAM_UNAVAILABLE', message: str = 'Layanan sementara tidak tersedia.') -> AppError:
    return AppError(503, code, message)
