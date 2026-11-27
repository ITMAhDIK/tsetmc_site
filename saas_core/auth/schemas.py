"""Shared response shapes for authentication and account APIs."""
from pydantic import BaseModel

class PublicUser(BaseModel):
    id: str
    public_id: str
    username: str | None = None
    display_name: str | None = None
    account_type: str
