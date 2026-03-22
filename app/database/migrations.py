"""
Database migration utilities for backward compatibility.
Handles schema updates when upgrading from older versions.
"""

import logging
import re
import sys
from typing import Dict, Set, List
from sqlalchemy import text, inspect
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql import quoted_name
from ascii_colors import ASCIIColors

logger = logging.getLogger(__name__)

# --- SQL Injection Protection: Valid SQLite Identifier Pattern ---
# SQLite identifiers must start with letter or underscore, followed by alphanumerics/underscores
# Maximum length is typically limited by SQLite implementation
VALID_IDENTIFIER_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
MAX_IDENTIFIER_LENGTH = 128  # Reasonable limit for safety


def is_valid_sqlite_identifier(identifier: str) -> bool:
    """
    Validate that a string is a safe SQLite identifier.
    Prevents SQL injection via malicious table/column names.
    """
    if not isinstance(identifier, str):
        return False
    if len(identifier) > MAX_IDENTIFIER_LENGTH:
        return False
    return bool(VALID_IDENTIFIER_PATTERN.match(identifier))


def sanitize_identifier(identifier: str) -> str:
    """
    Sanitize an identifier, raising ValueError if unsafe.
    Used for table names, column names in DDL statements.
    """
    if not is_valid_sqlite_identifier(identifier):
        raise ValueError(
            f"Invalid SQLite identifier: '{identifier}'. "
            f"Identifiers must match pattern {VALID_IDENTIFIER_PATTERN.pattern} "
            f"and be at most {MAX_IDENTIFIER_LENGTH} characters."
        )
    return identifier


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
    # Sanitize inputs to prevent SQL injection
    safe_table_name = sanitize_identifier(table_name)
    safe_column_name = sanitize_identifier(column_name)
    
    async with engine.begin() as conn:
        # SQLite-specific query to check for column existence
        # Use quoted_name to ensure proper quoting of the identifier
        quoted_table = quoted_name(safe_table_name, quote=True)
        result = await conn.execute(
            text(f"PRAGMA table_info({quoted_table})")
        )
        columns = result.fetchall()
        column_names = [col[1] for col in columns]  # Column name is at index 1
        return safe_column_name in column_names


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
    # Sanitize all identifier inputs
    safe_table_name = sanitize_identifier(table_name)
    safe_column_name = sanitize_identifier(column_name)
    
    # Validate column definition - only allow specific safe patterns
    # This prevents injection via the column definition itself
    safe_definition = validate_column_definition(column_definition)

    exists = await check_column_exists(engine, table_name, column_name)

    if not exists:
        logger.info(f"Adding missing column '{safe_column_name}' to table '{safe_table_name}'")
        async with engine.begin() as conn:
            # Use quoted identifiers to prevent any remaining injection vectors
            quoted_table = quoted_name(safe_table_name, quote=True)
            quoted_column = quoted_name(safe_column_name, quote=True)
            await conn.execute(
                text(f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_column} {safe_definition}")
            )
        logger.info(f"Successfully added column '{safe_column_name}' to table '{safe_table_name}'")
        return True
    else:
        logger.debug(f"Column '{safe_column_name}' already exists in table '{safe_table_name}'")
        return False


def validate_column_definition(definition: str) -> str:
    """
    Validate and sanitize a column definition string.
    Only allows specific safe SQLite type definitions.
    
    Raises ValueError if the definition contains unsafe content.
    """
    if not isinstance(definition, str):
        raise ValueError("Column definition must be a string")
    
    # Maximum length check
    if len(definition) > 256:
        raise ValueError("Column definition too long")
    
    # Convert to uppercase for case-insensitive matching
    upper_def = definition.upper().strip()
    
    # List of allowed safe patterns (SQLite types with optional constraints)
    # These are strict patterns that don't allow arbitrary SQL injection
    allowed_patterns = [
        r'^JSON(\s+DEFAULT\s+\'[^\']*\')?(\s+NOT\s+NULL)?$',  # JSON type with DEFAULT support
        r'^DATETIME(\s+DEFAULT\s+CURRENT_TIMESTAMP)?(\s+NOT\s+NULL)?$',  # DATETIME
        r'^VARCHAR(\s*\(\s*\d+\s*\))?(\s+DEFAULT\s+\'[^\']*\')?(\s+NOT\s+NULL)?$',  # VARCHAR
        r'^INTEGER(\s+DEFAULT\s+\d+)?(\s+NOT\s+NULL)?$',  # INTEGER
        r'^INTEGER\s+NOT\s+NULL\s+PRIMARY\s+KEY$',  # INTEGER NOT NULL PRIMARY KEY
        r'^BOOLEAN(\s+DEFAULT\s+(0|1))?(\s+NOT\s+NULL)?$',  # BOOLEAN
        r'^BOOLEAN\s+DEFAULT\s+(0|1|TRUE|FALSE)\s+NOT\s+NULL$',  # BOOLEAN constraints
    ]
    
    for pattern in allowed_patterns:
        if re.match(pattern, upper_def, re.IGNORECASE):
            # Return the original (preserve case for any string literals)
            return definition.strip()
    
    # Special case: check for simple type with DEFAULT and NOT NULL
    # Only allow specific safe default values
    safe_default_pattern = re.compile(
        r'^(INTEGER|VARCHAR|TEXT|BOOLEAN)\s+'
        r'DEFAULT\s+'
        r'(\d+|\'[^\']*\'|NULL)\s*'
        r'(NOT\s+NULL)?$',
        re.IGNORECASE
    )
    
    if safe_default_pattern.match(definition.strip()):
        return definition.strip()
    
    # If no pattern matches, reject as potentially unsafe
    raise ValueError(
        f"Unsafe column definition rejected: '{definition}'. "
        f"Only standard SQLite types with safe constraints are allowed."
    )


async def migrate_ollama_servers_table(engine: AsyncEngine) -> None:
    """
    Migrate the ollama_servers table to add new columns if they don't exist.
    This ensures backward compatibility with older database schemas.
    """
    logger.info("Checking ollama_servers table for missing columns...")

    # Check if table exists first
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": "ollama_servers"}
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
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": "api_keys"}
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
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": "usage_logs"}
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

    # Add token usage columns if missing
    await add_column_if_missing(
        engine,
        "usage_logs",
        "prompt_tokens",
        "INTEGER"
    )
    await add_column_if_missing(
        engine,
        "usage_logs",
        "completion_tokens",
        "INTEGER"
    )
    await add_column_if_missing(
        engine,
        "usage_logs",
        "total_tokens",
        "INTEGER"
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
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": "app_settings"}
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
            "max_retries": 2,
            "retry_total_timeout_seconds": 1.0,
            "retry_base_delay_ms": 10
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
    # Sanitize table name
    safe_table_name = sanitize_identifier(table_name)
    
    async with engine.begin() as conn:
        quoted_table = quoted_name(safe_table_name, quote=True)
        result = await conn.execute(
            text(f"PRAGMA table_info({quoted_table})")
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
    # Sanitize table name
    safe_table_name = sanitize_identifier(table_name)
    
    # Check if table exists
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": safe_table_name}
        )
        table_exists = result.fetchone() is not None

    if not table_exists:
        logger.info(f"Table '{safe_table_name}' does not exist yet, skipping auto-migration")
        return

    # Get existing columns
    existing_columns = await get_table_columns(engine, safe_table_name)

    # Validate and sanitize expected column names
    sanitized_expected = {}
    for col_name, col_def in expected_columns.items():
        try:
            safe_col_name = sanitize_identifier(col_name)
            safe_col_def = validate_column_definition(col_def)
            sanitized_expected[safe_col_name] = safe_col_def
        except ValueError as e:
            logger.error(f"Invalid column definition in schema: {e}")
            continue

    # Find missing columns
    missing_columns = set(sanitized_expected.keys()) - existing_columns

    if not missing_columns:
        logger.debug(f"Table '{safe_table_name}' has all expected columns")
        return

    # Add missing columns
    logger.info(f"Table '{safe_table_name}' is missing {len(missing_columns)} column(s): {missing_columns}")

    for col_name in missing_columns:
        col_definition = sanitized_expected[col_name]
        await add_column_if_missing(engine, safe_table_name, col_name, col_definition)


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

            # Sanitize for display (but we know it's from sqlite_master so it's safe)
            logger.info(f"\nTable: {table_name}")
            logger.info("-" * 60)

            # Get columns for this table
            quoted_table = quoted_name(table_name, quote=True)
            result = await conn.execute(
                text(f"PRAGMA table_info({quoted_table})")
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
    Run all database migrations with a stylized ASCIIColors panel using Rich tags.
    """
    report = []
    
    try:
        # First, handle legacy column renames/drops for model_pools
        await fix_model_pools_legacy_schema(engine)
        
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
                "prompt_tokens": "INTEGER",
                "completion_tokens": "INTEGER",
                "total_tokens": "INTEGER",
                "server_id": "INTEGER",
            },
            "model_bundles": {
                "parallel_participants": "JSON DEFAULT '[]' NOT NULL",
                "parallel_models": "JSON",
                "master_model": "VARCHAR NOT NULL",
                "vision_processor": "VARCHAR",  # NEW: For vision-enabled bundles
                "show_monologue": "BOOLEAN DEFAULT 0 NOT NULL",
                "send_status_update": "BOOLEAN DEFAULT 0 NOT NULL",
                "report_success_failure": "BOOLEAN DEFAULT 0 NOT NULL",
                "is_active": "BOOLEAN DEFAULT 1 NOT NULL",
                "created_at": "DATETIME",
            },
            "model_pools": {
                "targets": "JSON DEFAULT '[]' NOT NULL",
                "classifier_model": "VARCHAR",
                "created_at": "DATETIME",
            },
            "model_metadata": {
                "model_name": "VARCHAR NOT NULL",
                "supports_images": "BOOLEAN NOT NULL DEFAULT 0",
                "is_reasoning_model": "BOOLEAN NOT NULL DEFAULT 0",
                "priority": "INTEGER NOT NULL DEFAULT 10",
            },
            "managed_instances": {
                "backend_type": "VARCHAR DEFAULT 'ollama'",
                "model_path": "VARCHAR",
                "n_gpu_layers": "INTEGER DEFAULT 99",
                "ctx_size": "INTEGER DEFAULT 8192",
                "threads": "INTEGER DEFAULT 8",
                "tensor_parallel_size": "INTEGER DEFAULT 1",
            },
            "virtual_agents": {
                "name": "VARCHAR NOT NULL",
                "base_model": "VARCHAR NOT NULL",
                "system_prompt": "TEXT NOT NULL",
                "mcp_servers": "JSON",
                "is_active": "BOOLEAN DEFAULT 1 NOT NULL",
            }
        }

        for table_name, expected_columns in table_schemas.items():
            existing_columns = await get_table_columns(engine, table_name)
            
            if not existing_columns:
                report.append(f" [blue]INIT[/blue]  Table '{table_name}' will be created.")
                continue

            for col_name, col_def in expected_columns.items():
                if col_name not in existing_columns:
                    try:
                        await add_column_if_missing(engine, table_name, col_name, col_def)
                        report.append(f" [green]FIXED[/green] Added '{col_name}' to '{table_name}'")
                    except Exception as col_err:
                        report.append(f" [red]ERROR[/red] Failed '{col_name}' in '{table_name}': {col_err}")
        
        await migrate_app_settings_data(engine)
        await create_missing_indexes(engine)
        
        if report:
            ASCIIColors.panel("\n".join(report), title="Fortress Database Migration Engine")

    except Exception as e:
        ASCIIColors.error(f"Critical Migration Failure: {str(e)}")
        logger.error(f"Error running database migrations: {e}", exc_info=True)
        raise


async def fix_model_pools_legacy_schema(engine: AsyncEngine) -> None:
    """
    Fix legacy schema issues with model_pools table.
    The old table had a 'models' column that was renamed to 'targets'.
    This ensures compatibility with old databases by keeping both columns in sync.
    """
    async with engine.begin() as conn:
        # Check current schema
        result = await conn.execute(
            text("PRAGMA table_info(model_pools)")
        )
        columns_info = result.fetchall()
        column_names = {row[1] for row in columns_info}
        
        has_models = "models" in column_names
        has_targets = "targets" in column_names
        
        if not has_models and not has_targets:
            # Neither exists - table doesn't exist yet
            logger.info("model_pools table doesn't exist yet, will be created fresh")
            return
            
        if has_models and not has_targets:
            # Old schema: has 'models' but no 'targets' - need to add targets
            logger.warning("Legacy model_pools schema detected (models without targets). Adding targets column...")
            try:
                await conn.execute(text("""
                    ALTER TABLE model_pools ADD COLUMN targets JSON DEFAULT '[]' NOT NULL
                """))
                # Copy data from models to targets
                await conn.execute(text("""
                    UPDATE model_pools SET targets = models WHERE targets IS NULL OR targets = '[]'
                """))
                logger.info("Successfully added targets column and migrated data")
            except Exception as e:
                logger.error(f"Failed to add targets column: {e}")
                raise
        
        elif not has_models and has_targets:
            # New schema: has 'targets' but no 'models' - add models for backward compat
            logger.info("Adding legacy 'models' column for backward compatibility...")
            try:
                await conn.execute(text("""
                    ALTER TABLE model_pools ADD COLUMN models JSON
                """))
                # Sync data from targets to models
                await conn.execute(text("""
                    UPDATE model_pools SET models = targets WHERE models IS NULL
                """))
                logger.info("Successfully added models column")
            except Exception as e:
                logger.warning(f"Could not add models column (may already exist): {e}")
        
        elif has_models and has_targets:
            # Both exist - ensure they're in sync
            logger.debug("Both models and targets columns exist, ensuring sync...")
            try:
                # Update models from targets where models is null
                await conn.execute(text("""
                    UPDATE model_pools SET models = targets WHERE models IS NULL AND targets IS NOT NULL
                """))
                # Update targets from models where targets is null (shouldn't happen but just in case)
                await conn.execute(text("""
                    UPDATE model_pools SET targets = models WHERE targets IS NULL OR targets = '[]' AND models IS NOT NULL
                """))
            except Exception as e:
                logger.warning(f"Sync issue: {e}")


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
                # Sanitize all identifiers
                safe_index = sanitize_identifier(index_name)
                safe_table = sanitize_identifier(table_name)
                safe_column = sanitize_identifier(column_name)
                
                quoted_index = quoted_name(safe_index, quote=True)
                quoted_table = quoted_name(safe_table, quote=True)
                quoted_column = quoted_name(safe_column, quote=True)
                
                await conn.execute(
                    text(f"CREATE INDEX IF NOT EXISTS {quoted_index} ON {quoted_table} ({quoted_column})")
                )
                logger.debug(f"Ensured index {safe_index} exists on {safe_table}.{safe_column}")
            except ValueError as e:
                logger.warning(f"Invalid identifier in index definition: {e}")
            except Exception as e:
                logger.warning(f"Could not create index {index_name}: {e}")

    logger.info("Index check complete")
