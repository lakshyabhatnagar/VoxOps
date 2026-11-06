from pydantic import BaseModel, ConfigDict


# Carries an error that can be returned to an API caller.
# Keeps the public category and message separate from internal exceptions.
class AppError(Exception):
    # Creates an error with a public category and message.
    # Stores the HTTP status alongside those fields.
    def __init__(self, category: str, message: str, status: int = 400):
        super().__init__(message)
        self.category = category
        self.message = message
        self.status = status


# Holds the public details of an error.
# Allows a category and explanation while rejecting extra fields.
class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    message: str


# Holds the response sent when a request fails.
# Places the public error details under the error key.
class ErrorResponse(BaseModel):
    error: ErrorDetail
