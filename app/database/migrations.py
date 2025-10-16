"""
Database migration utilities for backward compatibility.
Handles schema updates when upgrading from older versions.
"""

import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


async def check_column_exists(engine: AsyncEngine, table_name: str, column_name: str) -> bool:
    """
    Check if a column exists in a table.

    Args:
        engine: SQLAlchemy async engine
        table_name: Name of the table to check
        column_name: Name of the column to check

    Returns:
        True if column exists, False otherwise
    """
    async with engine.begin() as conn:
        # SQLite-specific query to check for column existence
        result = await conn.execute(
            text(f"PRAGMA table_info({table_name})")
        )
        columns = result.fetchall()
        column_names = [col[1] for col in columns]  # Column name is at index 1
        return column_name in column_names


async def add_column_if_missing(
    engine: AsyncEngine,
    table_name: str,
    column_name: str,
    column_definition: str
) -> bool:
    """
    Add a column to a table if it doesn't exist.

    Args:
        engine: SQLAlchemy async engine
        table_name: Name of the table
        column_name: Name of the column to add
        column_definition: SQL definition of the column (e.g., "JSON")

    Returns:
        True if column was added, False if it already existed
    """
    exists = await check_column_exists(engine, table_name, column_name)

    if not exists:
        logger.info(f"Adding missing column '{column_name}' to table '{table_name}'")
        async with engine.begin() as conn:
            await conn.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")
            )
        logger.info(f"Successfully added column '{column_name}' to table '{table_name}'")
        return True
    else:
        logger.debug(f"Column '{column_name}' already exists in table '{table_name}'")
        return False


async def migrate_ollama_servers_table(engine: AsyncEngine) -> None:
    """
    Migrate the ollama_servers table to add new columns if they don't exist.
    This ensures backward compatibility with older database schemas.
    """
    logger.info("Checking ollama_servers table for missing columns...")

    # Check if table exists first
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='ollama_servers'")
        )
        table_exists = result.fetchone() is not None

    if not table_exists:
        logger.info("Table 'ollama_servers' does not exist yet, skipping migration")
        return

    # Add available_models column if missing
    await add_column_if_missing(
        engine,
        "ollama_servers",
        "available_models",
        "JSON"
    )

    # Add models_last_updated column if missing
    await add_column_if_missing(
        engine,
        "ollama_servers",
        "models_last_updated",
        "DATETIME"
    )

    logger.info("ollama_servers table migration complete")


async def migrate_api_keys_table(engine: AsyncEngine) -> None:
    """
    Migrate the api_keys table to add new columns if they don't exist.
    This ensures backward compatibility with older database schemas.
    """
    logger.info("Checking api_keys table for missing columns...")

    # Check if table exists first
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='api_keys'")
        )
        table_exists = result.fetchone() is not None

    if not table_exists:
        logger.info("Table 'api_keys' does not exist yet, skipping migration")
        return

    # Add is_active column if missing
    await add_column_if_missing(
        engine,
        "api_keys",
        "is_active",
        "BOOLEAN DEFAULT 1 NOT NULL"
    )

    # Add is_revoked column if missing
    await add_column_if_missing(
        engine,
        "api_keys",
        "is_revoked",
        "BOOLEAN DEFAULT 0 NOT NULL"
    )

    # Add rate_limit_requests column if missing
    await add_column_if_missing(
        engine,
        "api_keys",
        "rate_limit_requests",
        "INTEGER"
    )

    # Add rate_limit_window_minutes column if missing
    await add_column_if_missing(
        engine,
        "api_keys",
        "rate_limit_window_minutes",
        "INTEGER"
    )

    logger.info("api_keys table migration complete")


async def migrate_app_settings_data(engine: AsyncEngine) -> None:
    """
    Migrate the app_settings table to add new fields to the JSON settings_data.
    This ensures backward compatibility when new settings are added.
    """
    logger.info("Checking app_settings for missing configuration fields...")

    # Check if table exists first
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='app_settings'")
        )
        table_exists = result.fetchone() is not None

    if not table_exists:
        logger.info("Table 'app_settings' does not exist yet, skipping migration")
        return

    # Fetch current settings
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT id, settings_data FROM app_settings WHERE id = 1")
        )
        row = result.fetchone()

        if not row:
            logger.info("No settings record found, default settings will be created on startup")
            return

        settings_id, settings_json = row
        import json
        settings_data = json.loads(settings_json) if settings_json else {}

        # Default values for new retry settings
        default_retry_settings = {
            "max_retries": 5,
            "retry_total_timeout_seconds": 2.0,
            "retry_base_delay_ms": 50
        }

        # Add missing fields
        updated = False
        for key, default_value in default_retry_settings.items():
            if key not in settings_data:
                settings_data[key] = default_value
                updated = True
                logger.info(f"Added missing setting '{key}' with default value: {default_value}")

        # Update the database if any fields were added
        if updated:
            updated_json = json.dumps(settings_data)
            await conn.execute(
                text("UPDATE app_settings SET settings_data = :settings WHERE id = :id"),
                {"settings": updated_json, "id": settings_id}
            )
            logger.info("Updated app_settings with new retry configuration fields")
        else:
            logger.debug("app_settings already has all required fields")

    logger.info("app_settings migration complete")


async def run_all_migrations(engine: AsyncEngine) -> None:
    """
    Run all database migrations to ensure backward compatibility.
    This function should be called before Base.metadata.create_all()
    """
    logger.info("Running database migrations for backward compatibility...")

    try:
        await migrate_ollama_servers_table(engine)
        await migrate_api_keys_table(engine)
        await migrate_app_settings_data(engine)
        logger.info("All database migrations completed successfully")
    except Exception as e:
        logger.error(f"Error running database migrations: {e}", exc_info=True)
        raise
