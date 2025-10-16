# Database Migration Guide

## Overview

The Ollama Proxy Server now includes a robust automatic migration system that ensures backward compatibility when upgrading from older database schemas. This means you can use an old `ollama_proxy.db` file without manual database updates.

## How It Works

On every server startup, the migration system:

1. **Checks for missing columns** in existing tables
2. **Adds missing columns** with appropriate defaults
3. **Creates missing indexes** for performance
4. **Updates JSON settings** with new configuration fields
5. **Reports schema** on errors to help debugging

## Migration Architecture

### Centralized Schema Definition

All expected columns are defined in a single location in `app/database/migrations.py`:

```python
table_schemas = {
    "ollama_servers": {
        "available_models": "JSON",
        "models_last_updated": "DATETIME",
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
}
```

### Automatic Detection

The system automatically:
- Compares existing database columns with expected schema
- Identifies missing columns
- Adds them using `ALTER TABLE` with appropriate SQL types
- Logs all changes for visibility

## Adding New Schema Changes

When you add a new column to a model, follow these steps:

### Step 1: Update SQLAlchemy Model

Add the new column to the model in `app/database/models.py`:

```python
class OllamaServer(Base):
    __tablename__ = "ollama_servers"

    # ... existing columns ...
    new_column = Column(String, nullable=True)  # Your new column
```

### Step 2: Update Migration Schema

Add the column to the migration schema in `app/database/migrations.py`:

```python
table_schemas = {
    "ollama_servers": {
        "available_models": "JSON",
        "models_last_updated": "DATETIME",
        "new_column": "VARCHAR",  # Add your new column here
    },
    # ... other tables ...
}
```

### Step 3: Restart Server

The migration will run automatically on the next startup and add the column to existing databases.

## JSON Settings Migration

For settings stored in the `app_settings.settings_data` JSON field, update `migrate_app_settings_data()`:

```python
default_retry_settings = {
    "max_retries": 5,
    "retry_total_timeout_seconds": 2.0,
    "retry_base_delay_ms": 50,
    "your_new_setting": "default_value",  # Add here
}
```

## Debugging Schema Issues

### Enable Schema Reporting

Uncomment this line in `run_all_migrations()` to see the full schema on startup:

```python
await check_and_report_schema(engine)
```

This will log a detailed report of all tables and columns:

```
============================================================
DATABASE SCHEMA REPORT
============================================================

Table: api_keys
------------------------------------------------------------
  - id: INTEGER [PK] NOT NULL
  - key_name: VARCHAR NOT NULL
  - hashed_key: VARCHAR NOT NULL
  ...
```

### Schema Report on Errors

If a migration fails, the system automatically generates a schema report to help diagnose the issue.

## Migration Safety

The migration system is designed to be **non-destructive**:

- ✅ **Adds** missing columns
- ✅ **Preserves** existing data
- ✅ **Creates** missing indexes
- ❌ **Never deletes** columns
- ❌ **Never modifies** existing data
- ❌ **Never drops** tables

## Common Migration Scenarios

### Scenario 1: Adding a New Feature Column

**Example:** Adding `max_connections` to `ollama_servers`

1. Add to model:
   ```python
   max_connections = Column(Integer, default=10)
   ```

2. Add to migration schema:
   ```python
   "ollama_servers": {
       "max_connections": "INTEGER DEFAULT 10",
   }
   ```

### Scenario 2: Adding Application Settings

**Example:** Adding `enable_caching` setting

1. Add to settings model:
   ```python
   class AppSettingsModel(BaseModel):
       enable_caching: bool = False
   ```

2. Add to migration:
   ```python
   default_settings = {
       "enable_caching": False,
   }
   ```

## Troubleshooting

### Error: "no such column: table.column"

**Cause:** A column is missing from the database schema.

**Solution:**
1. Ensure the column is defined in the migration schema
2. Restart the server to run migrations
3. Check logs for migration status

### Error: Migration fails with "database is locked"

**Cause:** Another process is accessing the database.

**Solution:**
1. Stop all running instances of the server
2. Ensure no other process is accessing the SQLite database
3. Restart the server

### Migration runs but column still missing

**Cause:** Migration may have encountered an error.

**Solution:**
1. Check server logs for migration errors
2. Enable schema reporting to verify current state
3. Manually verify database with: `sqlite3 ollama_proxy.db ".schema table_name"`

## Performance Considerations

- Migrations run **once per startup** before the server accepts requests
- Adding columns is a fast operation in SQLite (< 1ms per column)
- No performance impact after initial migration
- Subsequent startups check but don't re-add existing columns

## Best Practices

1. **Test migrations** with a copy of production database first
2. **Backup database** before major upgrades
3. **Review migration logs** after upgrades
4. **Keep migration schema in sync** with SQLAlchemy models
5. **Use appropriate SQL types** and defaults in migration schema

## Example Migration Log

```
2025-10-15 21:30:45,123 [INFO] Running database migrations for backward compatibility...
2025-10-15 21:30:45,124 [INFO] Checking table 'ollama_servers' for missing columns...
2025-10-15 21:30:45,125 [INFO] Adding missing column 'available_models' to table 'ollama_servers'
2025-10-15 21:30:45,126 [INFO] Successfully added column 'available_models' to table 'ollama_servers'
2025-10-15 21:30:45,127 [INFO] Table 'api_keys' has all expected columns
2025-10-15 21:30:45,128 [INFO] Checking table 'usage_logs' for missing columns...
2025-10-15 21:30:45,129 [INFO] Adding missing column 'model' to table 'usage_logs'
2025-10-15 21:30:45,130 [INFO] All database migrations completed successfully
```

## Future Enhancements

Potential future improvements to the migration system:

- Automatic detection from SQLAlchemy metadata
- Data transformation migrations
- Rollback capability
- Migration versioning
- Pre/post migration hooks
