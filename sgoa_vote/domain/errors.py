"""Domain errors carry the HTTP status and, crucially, a plain next action.

Work order §8: "Every error message ends with a concrete next step." Voter-facing
errors therefore default to pointing at the assistance desk rather than to a
technical explanation.
"""

from __future__ import annotations

ASSISTANCE = "Please visit the voting assistance desk."


class DomainError(Exception):
    status_code = 400
    code = "error"

    def __init__(self, message: str, *, next_action: str | None = None,
                 status_code: int | None = None, code: str | None = None, **extra):
        super().__init__(message)
        self.message = message
        self.next_action = next_action
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        self.extra = extra

    def as_dict(self) -> dict:
        body = {"error": self.code, "message": self.message}
        if self.next_action:
            body["next_action"] = self.next_action
        body.update(self.extra)
        return body


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class Conflict(DomainError):
    """Illegal state transition, or an action the current state forbids."""
    status_code = 409
    code = "conflict"


class Forbidden(DomainError):
    status_code = 403
    code = "forbidden"


class Unauthorized(DomainError):
    status_code = 401
    code = "unauthorized"


class ValidationError(DomainError):
    status_code = 422
    code = "invalid"


class RateLimited(DomainError):
    status_code = 429
    code = "rate_limited"
