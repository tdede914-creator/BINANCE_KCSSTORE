"""FastAPI dependency helpers."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.database import get_session
from app.db.models import UserConfig

SessionDep = Annotated[AsyncSession, Depends(get_session)]

_bearer = HTTPBearer(auto_error=False)


async def optional_token(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> dict | None:
    """Return decoded token claims, or None. We keep it optional for now
    because the app is single-user local install by default."""
    if creds is None:
        return None
    try:
        return decode_access_token(creds.credentials)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)
        ) from e


async def get_or_create_config(session: SessionDep) -> UserConfig:
    cfg = await session.get(UserConfig, 1)
    if cfg is None:
        cfg = UserConfig(id=1)
        session.add(cfg)
        await session.commit()
        await session.refresh(cfg)
    return cfg
