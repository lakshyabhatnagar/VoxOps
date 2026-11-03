from pydantic import BaseModel, ConfigDict


# Why: Keep error categories stable without exposing exception text or machine paths.
# How: Carry an allowlisted category, explanation and HTTP status across boundaries.
class AppError(Exception):
    # Why: Preserve actionable errors through graph and API handling.
    # How: Store safe fields separately from any internal exception cause.
    def __init__(self, category: str, message: str, status: int = 400):
        super().__init__(message)
        self.category = category
        self.message = message
        self.status = status


# Why: Define a serializable public error envelope.
# How: Permit only category and safe explanation, rejecting unknown fields.
class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    message: str


# Why: Give every unsuccessful API request a consistent typed response.
# How: Wrap the public error detail under one stable error key.
class ErrorResponse(BaseModel):
    error: ErrorDetail
