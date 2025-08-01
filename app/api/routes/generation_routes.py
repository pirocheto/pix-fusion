import uuid
from datetime import timedelta
from typing import Annotated, Any

import obstore as obs
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.config import get_settings
from app.core.storage import store
from app.db.models import GenerationORM, OutputFormat, Ratio, Status, UserORM
from app.schemas.generations import (
    DownloadURLResponse,
    GenerationCreateResponse,
    GenerationData,
    GenerationList,
    GenerationStatus,
)
from app.tasks import generate_image_task

settings = get_settings()

router = APIRouter()


@router.post("/generations/create", status_code=status.HTTP_202_ACCEPTED, response_model=GenerationCreateResponse)
async def create_generation(
    background_tasks: BackgroundTasks,
    current_user: Annotated[UserORM, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    prompt: Annotated[str, Form()],
    output_format: Annotated[OutputFormat, Form()] = OutputFormat.PNG,
    ratio: Annotated[Ratio, Form()] = Ratio.RATIO_1_1,
) -> Any:
    """Create a new generation request."""

    if current_user.credits <= 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have enough credits to create a generation request.",
        )

    generation_orm = GenerationORM(
        prompt=prompt,
        user_id=current_user.id,
        output_format=output_format,
        ratio=ratio,
        status=Status.PENDING,
    )

    async with session.begin():
        session.add(generation_orm)

    background_tasks.add_task(generate_image_task, generation_id=generation_orm.id)

    return GenerationCreateResponse(
        message="Generation request created successfully",
        generation_id=generation_orm.id,
    )


@router.get("/generations", response_model=GenerationList)
async def get_generations(
    current_user: Annotated[UserORM, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(le=100)] = 10,
) -> Any:
    """Retrieve all generations for the current user."""

    # Get the count of generations for the current user
    count_statement = select(func.count()).select_from(GenerationORM).where(GenerationORM.user_id == current_user.id)
    count_result = await session.execute(count_statement)
    count = count_result.scalars().one()

    # Get the list of generations for the current user with pagination
    select_statement = (
        select(GenerationORM)
        .where(GenerationORM.user_id == current_user.id)
        .offset(offset)
        .limit(limit)
        .order_by(GenerationORM.created_at.desc())
    )
    result = await session.execute(select_statement)
    generations_orm = result.scalars().all()

    data = []
    for generation_orm in generations_orm:
        generation_data = GenerationData.model_validate(generation_orm)
        if generation_orm.filename:
            generation_data.preview_url = await obs.sign_async(
                store, "GET", generation_orm.filename, expires_in=timedelta(minutes=5)
            )
        data.append(generation_data)

    return GenerationList(count=count, data=data)


@router.get("/generations/{generation_id}", response_model=GenerationData)
async def get_generation(
    generation_id: uuid.UUID,
    current_user: Annotated[UserORM, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Any:
    """Retrieve a specific generation by ID."""

    statement = select(GenerationORM).where(
        GenerationORM.id == generation_id,
        GenerationORM.user_id == current_user.id,
    )
    result = await session.execute(statement)
    generation_orm = result.scalars().one_or_none()

    if not generation_orm:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation not found")

    generation_data = GenerationData.model_validate(generation_orm)
    if generation_orm.filename:
        generation_data.preview_url = await obs.sign_async(
            store, "GET", generation_orm.filename, expires_in=timedelta(minutes=5)
        )
    return generation_data


@router.get("/generations/{generation_id}/download", response_model=DownloadURLResponse)
async def download_generation(
    generation_id: uuid.UUID,
    current_user: Annotated[UserORM, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Any:
    """Download a specific generation by ID."""

    statement = select(GenerationORM).where(
        GenerationORM.id == generation_id,
        GenerationORM.user_id == current_user.id,
    )
    result = await session.execute(statement)
    generation_orm = result.scalars().one_or_none()

    if not generation_orm:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation not found")
    if not generation_orm.filename:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No file available for this generation")

    expires_in = timedelta(minutes=5)
    download_url = await obs.sign_async(store, "GET", generation_orm.filename, expires_in=expires_in)
    filename = f"{uuid.uuid4()}.{generation_orm.output_format.value}"

    return {
        "url": download_url,
        "filename": filename,
        "size": generation_orm.size,
        "content_type": generation_orm.content_type,
        "expires_in": expires_in.total_seconds(),
    }


@router.get("/generations/{generation_id}/status", response_model=GenerationStatus)
async def get_generation_status(
    generation_id: uuid.UUID,
    current_user: Annotated[UserORM, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Any:
    """Retrieve the status of a specific generation by ID."""

    statement = select(GenerationORM).where(
        GenerationORM.id == generation_id,
        GenerationORM.user_id == current_user.id,
    )
    result = await session.execute(statement)
    generation_orm = result.scalar_one_or_none()

    if generation_orm is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation not found")

    return generation_orm


@router.delete("/generations/{generation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_generation(
    generation_id: uuid.UUID,
    current_user: Annotated[UserORM, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Delete a specific generation by ID."""

    async with session.begin():
        statement = select(GenerationORM).where(
            GenerationORM.id == generation_id,
            GenerationORM.user_id == current_user.id,
        )
        result = await session.execute(statement)
        generation_orm = result.scalars().one_or_none()

        if not generation_orm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation not found")

        await session.delete(generation_orm)
        if generation_orm.filename:
            await obs.delete_async(store, generation_orm.filename)
