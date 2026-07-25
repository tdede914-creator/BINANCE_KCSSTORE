"""Very lightweight auth for a single-user local install.

If you set ADMIN_PASSWORD in env, the frontend must log in to receive a
JWT. Otherwise, the API is open on localhost.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.security import create_access_token

router = APIRouter()


class LoginRequest(BaseModel):
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest) -> TokenResponse:
    admin_pw = os.getenv("ADMIN_PASSWORD", "")
    if not admin_pw:
        # No password configured → allow anyone (dev mode).
        token = create_access_token("local", extra={"role": "admin"})
        return TokenResponse(access_token=token)

    if body.password != admin_pw:
        raise HTTPException(status_code=401, detail="Invalid password")
    token = create_access_token("admin", extra={"role": "admin"})
    return TokenResponse(access_token=token)
