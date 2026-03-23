"""User profile and API key endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.auth import get_current_user_id, generate_api_key
from core.database import get_db
from models.db import UserDB, ApiKeyDB
from models.schemas import (
    ApiKeyCreateRequest, ApiKeyCreateResponse, ApiKeyOut, UserOut
)

router = APIRouter(prefix="/v1", tags=["users"])


@router.get("/users/me", response_model=UserOut)
async def get_me(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(
        select(ApiKeyDB).where(ApiKeyDB.user_id == user_id)
    )
    return result.scalars().all()


@router.post("/api-keys", response_model=ApiKeyCreateResponse, status_code=201)
async def create_api_key(
    req: ApiKeyCreateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    plaintext, hashed = generate_api_key()
    prefix = plaintext[:12]  # "csk_" + 8 chars

    key = ApiKeyDB(
        user_id=user_id,
        name=req.name,
        key_hash=hashed,
        key_prefix=prefix,
        scopes=req.scopes,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)

    return ApiKeyCreateResponse(
        id=key.id,
        key=plaintext,
        name=key.name,
        created_at=key.created_at,
    )


@router.delete("/api-keys/{key_id}", status_code=204)
async def delete_api_key(
    key_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(
        select(ApiKeyDB).where(ApiKeyDB.id == key_id, ApiKeyDB.user_id == user_id)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    await db.delete(key)
    await db.commit()
