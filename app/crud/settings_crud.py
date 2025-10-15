import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.database.models import AppSettings
from app.schema.settings import AppSettingsModel

async def get_app_settings(db: AsyncSession) -> AppSettings | None:
    """Retrieves the application settings from the database."""
    result = await db.execute(select(AppSettings).filter(AppSettings.id == 1))
    return result.scalars().first()

async def update_app_settings(db: AsyncSession, settings_data: AppSettingsModel) -> AppSettings:
    """Updates the application settings in the database."""
    db_settings = await get_app_settings(db)
    if not db_settings:
        db_settings = AppSettings(id=1)
        db.add(db_settings)

    # Update fields from the Pydantic model
    db_settings.settings_data = json.loads(settings_data.model_dump_json())
    
    await db.commit()
    await db.refresh(db_settings)
    return db_settings

async def create_initial_settings(db: AsyncSession) -> AppSettings:
    """Creates the very first default settings if none exist."""
    existing_settings = await get_app_settings(db)
    if existing_settings:
        return existing_settings

    # No longer contains a default server
    default_settings = AppSettingsModel()
    
    new_db_settings = AppSettings(
        id=1,
        settings_data=json.loads(default_settings.model_dump_json())
    )
    db.add(new_db_settings)
    await db.commit()
    await db.refresh(new_db_settings)
    return new_db_settings