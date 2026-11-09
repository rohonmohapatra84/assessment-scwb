"""Errors safe to disclose across the HTTP boundary."""


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def body(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": self.message}}


class CodeConflict(Exception):
    """Repository insert collided with an existing, possibly deleted, code."""
