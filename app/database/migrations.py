"""
Database migration utilities for backward compatibility.
Handles schema updates when upgrading from older versions.
"""

import logging
from typing import Dict, Set, List
from sqlalchemy import text, inspect
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


async def migrate_usage_logs_table(engine: AsyncEngine) -> None:
    """
    Migrate the usage_logs table to add new columns if they don't exist.
    This ensures backward compatibility with older database schemas.
    """
    logger.info("Checking usage_logs table for missing columns...")

    # Check if table exists first
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='usage_logs'")
        )
        table_exists = result.fetchone() is not None

    if not table_exists:
        logger.info("Table 'usage_logs' does not exist yet, skipping migration")
        return

    # Add model column if missing
    await add_column_if_missing(
        engine,
        "usage_logs",
        "model",
        "VARCHAR"
    )

    # Create index on model column if it doesn't exist
    # Note: SQLite will silently ignore if index already exists
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_usage_logs_model ON usage_logs (model)")
            )
            logger.info("Ensured index ix_usage_logs_model exists")
        except Exception as e:
            logger.debug(f"Index creation note: {e}")

    logger.info("usage_logs table migration complete")


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


async def get_table_columns(engine: AsyncEngine, table_name: str) -> Set[str]:
    """
    Get all column names for a given table.

    Args:
        engine: SQLAlchemy async engine
        table_name: Name of the table

    Returns:
        Set of column names in the table
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            text(f"PRAGMA table_info({table_name})")
        )
        columns = result.fetchall()
        return {col[1] for col in columns}  # Column name is at index 1


async def auto_migrate_table(
    engine: AsyncEngine,
    table_name: str,
    expected_columns: Dict[str, str]
) -> None:
    """
    Automatically add missing columns to a table based on expected schema.

    Args:
        engine: SQLAlchemy async engine
        table_name: Name of the table to migrate
        expected_columns: Dict mapping column names to their SQL type definitions
                         Example: {"model": "VARCHAR", "is_active": "BOOLEAN DEFAULT 1"}
    """
    # Check if table exists
    async with engine.begin() as conn:
        result = await conn.execute(
            text(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
        )
        table_exists = result.fetchone() is not None

    if not table_exists:
        logger.info(f"Table '{table_name}' does not exist yet, skipping auto-migration")
        return

    # Get existing columns
    existing_columns = await get_table_columns(engine, table_name)

    # Find missing columns
    missing_columns = set(expected_columns.keys()) - existing_columns

    if not missing_columns:
        logger.debug(f"Table '{table_name}' has all expected columns")
        return

    # Add missing columns
    logger.info(f"Table '{table_name}' is missing {len(missing_columns)} column(s): {missing_columns}")

    for col_name in missing_columns:
        col_definition = expected_columns[col_name]
        await add_column_if_missing(engine, table_name, col_name, col_definition)


async def check_and_report_schema(engine: AsyncEngine) -> None:
    """
    Comprehensive schema check that reports all table structures.
    Useful for debugging schema mismatches.
    """
    logger.info("=" * 60)
    logger.info("DATABASE SCHEMA REPORT")
    logger.info("=" * 60)

    async with engine.begin() as conn:
        # Get all tables
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        )
        tables = [row[0] for row in result.fetchall()]

        for table_name in tables:
            if table_name.startswith('sqlite_'):
                continue

            logger.info(f"\nTable: {table_name}")
            logger.info("-" * 60)

            # Get columns for this table
            result = await conn.execute(
                text(f"PRAGMA table_info({table_name})")
            )
            columns = result.fetchall()

            for col in columns:
                col_id, col_name, col_type, not_null, default_val, pk = col
                pk_marker = " [PK]" if pk else ""
                null_marker = " NOT NULL" if not_null else ""
                default_marker = f" DEFAULT {default_val}" if default_val else ""
                logger.info(
                    f"  - {col_name}: {col_type}{pk_marker}{null_marker}{default_marker}"
                )

    logger.info("=" * 60)


async def run_all_migrations(engine: AsyncEngine) -> None:
    """
    Run all database migrations to ensure backward compatibility.
    This function should be called before Base.metadata.create_all()

    This uses a centralized schema definition to automatically detect
    and add any missing columns across all tables.
    """
    logger.info("Running database migrations for backward compatibility...")

    try:
        # Define expected schemas for all tables
        # This is the single source of truth for what columns should exist
        table_schemas = {
            "ollama_servers": {
                "available_models": "JSON",
                "models_last_updated": "DATETIME",
                "last_error": "VARCHAR",
                "server_type": "VARCHAR DEFAULT 'ollama' NOT NULL",
                "encrypted_api_key": "VARCHAR",
            },
            "api_keys": {
                "is_active": "BOOLEAN DEFAULT 1 NOT NULL",
                "is_revoked": "BOOLEAN DEFAULT 0 NOT NULL",
                "rate_limit_requests": "INTEGER",
                "rate_limit_window_minutes": "INTEGER",
            },
            "usage_logs": {
                "model": "VARCHAR",
                "server_id": "INTEGER",
            },
            "model_metadata": {
                "id": "INTEGER NOT NULL PRIMARY KEY",
                "model_name": "VARCHAR NOT NULL",
                "description": "VARCHAR",
                "supports_images": "BOOLEAN NOT NULL DEFAULT 0",
                "is_code_model": "BOOLEAN NOT NULL DEFAULT 0",
                "is_chat_model": "BOOLEAN NOT NULL DEFAULT 1",
                "is_fast_model": "BOOLEAN NOT NULL DEFAULT 0",
                "priority": "INTEGER NOT NULL DEFAULT 10",
            },
        }

        # Auto-migrate all tables
        for table_name, expected_columns in table_schemas.items():
            logger.info(f"Checking table '{table_name}' for missing columns...")
            await auto_migrate_table(engine, table_name, expected_columns)

        # Handle app_settings JSON data migration separately
        # (since it's stored as JSON, not columns)
        await migrate_app_settings_data(engine)

        # Create any missing indexes
        await create_missing_indexes(engine)

        logger.info("All database migrations completed successfully")

        # Optional: Enable this for debugging schema issues
        # Uncomment the next line if you want to see the full schema on startup
        # await check_and_report_schema(engine)

    except Exception as e:
        logger.error(f"Error running database migrations: {e}", exc_info=True)
        # Print schema report on error to help debugging
        try:
            logger.error("Generating schema report to help diagnose the issue...")
            await check_and_report_schema(engine)
        except Exception as report_error:
            logger.error(f"Could not generate schema report: {report_error}")
        raise


async def create_missing_indexes(engine: AsyncEngine) -> None:
    """
    Create any missing indexes that should exist.
    SQLite will silently ignore if index already exists (using IF NOT EXISTS).
    """
    logger.info("Checking for missing indexes...")

    indexes = [
        ("ix_usage_logs_model", "usage_logs", "model"),
        ("ix_model_metadata_model_name", "model_metadata", "model_name"),
    ]

    async with engine.begin() as conn:
        for index_name, table_name, column_name in indexes:
            try:
                await conn.execute(
                    text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({column_name})")
                )
                logger.debug(f"Ensured index {index_name} exists on {table_name}.{column_name}")
            except Exception as e:
                logger.warning(f"Could not create index {index_name}: {e}")

    logger.info("Index check complete")
