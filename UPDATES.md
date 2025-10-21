# Ollama Proxy Server - Current Branch Updates

This document summarizes all major updates implemented in the current branch, focusing on robustness improvements, backward compatibility, and customization features.

## Table of Contents

1. [Retries Support for Increased Robustness](#retries-support-for-increased-robustness)
2. [Backward Compatibility and Database Migration](#backward-compatibility-and-database-migration)
3. [Branding and GUI Customization](#branding-and-gui-customization)

---

## Retries Support for Increased Robustness

### Overview

The server now includes a comprehensive retry mechanism with exponential backoff to increase resilience against transient failures. This ensures that temporary network issues, brief service outages, or temporary unavailability of backend servers don't immediately fail user requests.

### Key Features

- **Exponential Backoff**: Retry delays increase exponentially to avoid overwhelming servers during recovery
- **Configurable Retry Behavior**: Administrators can customize retry attempts, timeouts, and base delays
- **Timeout Budget**: Total time spent on retries is bounded, preventing indefinite wait times
- **Intelligent Fallback**: Tries multiple servers before giving up
- **Detailed Logging**: Each retry attempt is logged for debugging and monitoring

### Implementation Details

#### Retry Configuration (app/core/retry.py)

The retry mechanism is powered by the `RetryConfig` dataclass that holds all retry parameters:

```python
@dataclass
class RetryConfig:
    """Configuration for retry behavior."""
    max_retries: int = 5
    total_timeout_seconds: float = 2.0
    base_delay_ms: int = 50

    def __post_init__(self):
        """Validate configuration."""
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.total_timeout_seconds <= 0:
            raise ValueError("total_timeout_seconds must be positive")
        if self.base_delay_ms <= 0:
            raise ValueError("base_delay_ms must be positive")
```

**Parameters:**
- `max_retries`: Maximum number of retry attempts (0-20)
- `total_timeout_seconds`: Total time budget for all retry attempts (0.1-30 seconds)
- `base_delay_ms`: Base delay for exponential backoff (10-5000 milliseconds)

#### Retry Result

The `RetryResult` dataclass captures detailed information about each retry attempt:

```python
@dataclass
class RetryResult:
    """Result of a retry operation."""
    success: bool
    result: Optional[any] = None
    attempts: int = 0
    total_duration_ms: float = 0.0
    errors: List[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []
```

#### Core Retry Logic (retry_with_backoff)

The main retry function with exponential backoff:

```python
async def retry_with_backoff(
    func: Callable,
    *args,
    config: RetryConfig,
    retry_on_exceptions: tuple = (Exception,),
    operation_name: str = "operation",
    **kwargs
) -> RetryResult:
    """
    Executes a function with exponential backoff retry logic.
    
    Features:
    - Uses exponential backoff: delay = base_delay * (2 ^ attempt)
    - Respects total timeout budget
    - Logs each retry attempt for debugging
    """
    start_time = time.time()
    errors = []
    attempt = 0

    for attempt in range(config.max_retries + 1):
        # Check if we've exceeded the total timeout budget
        elapsed = time.time() - start_time
        if elapsed >= config.total_timeout_seconds:
            logger.warning(
                f"{operation_name}: Exceeded total timeout of {config.total_timeout_seconds}s "
                f"after {attempt} attempts"
            )
            break

        try:
            # Attempt the operation
            if attempt > 0:
                logger.debug(
                    f"{operation_name}: Retry attempt {attempt}/{config.max_retries}"
                )

            result = await func(*args, **kwargs)

            # Success!
            total_duration_ms = (time.time() - start_time) * 1000
            if attempt > 0:
                logger.info(
                    f"{operation_name}: Succeeded on attempt {attempt + 1} "
                    f"after {total_duration_ms:.1f}ms"
                )

            return RetryResult(
                success=True,
                result=result,
                attempts=attempt + 1,
                total_duration_ms=total_duration_ms,
                errors=errors
            )

        except retry_on_exceptions as e:
            error_msg = f"Attempt {attempt + 1}: {type(e).__name__}: {str(e)}"
            errors.append(error_msg)

            # Log appropriately based on whether more retries are available
            if attempt < config.max_retries:
                logger.debug(f"{operation_name}: {error_msg}")
            else:
                logger.warning(f"{operation_name}: Final attempt failed: {error_msg}")

            # Calculate exponential backoff delay
            if attempt < config.max_retries:
                # Exponential backoff: base_delay * (2 ^ attempt)
                delay_ms = config.base_delay_ms * (2 ** attempt)
                delay_seconds = delay_ms / 1000.0

                # Calculate remaining time in timeout budget
                elapsed = time.time() - start_time
                remaining_time = config.total_timeout_seconds - elapsed

                if remaining_time <= 0:
                    logger.debug(
                        f"{operation_name}: No time remaining for retry delay"
                    )
                    break

                # Cap the delay to not exceed remaining time
                actual_delay = min(delay_seconds, remaining_time)

                logger.debug(
                    f"{operation_name}: Waiting {actual_delay * 1000:.1f}ms before retry "
                    f"({remaining_time:.2f}s remaining of {config.total_timeout_seconds}s budget)"
                )

                await asyncio.sleep(actual_delay)

    # All retries exhausted
    total_duration_ms = (time.time() - start_time) * 1000
    logger.error(
        f"{operation_name}: Failed after {attempt + 1} attempts "
        f"in {total_duration_ms:.1f}ms. Errors: {errors[-3:]}"  # Show last 3 errors
    )

    return RetryResult(
        success=False,
        result=None,
        attempts=attempt + 1,
        total_duration_ms=total_duration_ms,
        errors=errors
    )
```

### Usage in Proxy Requests

The retry mechanism is integrated into the core reverse proxy logic (`app/api/v1/routes/proxy.py`):

```python
# Create retry configuration from app settings
retry_config = RetryConfig(
    max_retries=app_settings.max_retries,
    total_timeout_seconds=app_settings.retry_total_timeout_seconds,
    base_delay_ms=app_settings.retry_base_delay_ms
)

# Try each server in round-robin fashion with retries
for server_attempt in range(num_servers):
    # Select next server using round-robin
    index = request.app.state.backend_server_index
    chosen_server = servers[index]
    request.app.state.backend_server_index = (index + 1) % len(servers)

    # Attempt request with retries to this specific server
    retry_result = await retry_with_backoff(
        _send_backend_request,
        http_client=http_client,
        server=chosen_server,
        path=path,
        method=request.method,
        headers=headers,
        query_params=request.query_params,
        body_bytes=body_bytes,
        config=retry_config,
        retry_on_exceptions=(Exception,),
        operation_name=f"Request to {chosen_server.name}"
    )

    if retry_result.success:
        # Success! Return the response
        backend_response = retry_result.result
        response = StreamingResponse(
            backend_response.aiter_raw(),
            status_code=backend_response.status_code,
            headers=backend_response.headers,
        )
        return response, chosen_server
    else:
        # This server failed, try next one
        logger.warning(
            f"Server '{chosen_server.name}' failed after {retry_result.attempts} "
            f"attempts. Trying next server if available."
        )
```

### Configuration via Settings

Retry settings are stored in `app/schema/settings.py` and can be configured through the database:

```python
class AppSettingsModel(BaseModel):
    # Retry configuration for backend requests
    max_retries: int = Field(
        default=5,
        ge=0,
        le=20,
        description="Maximum number of retry attempts when a backend server request fails"
    )
    retry_total_timeout_seconds: float = Field(
        default=2.0,
        ge=0.1,
        le=30.0,
        description="Total time budget (in seconds) for all retry attempts"
    )
    retry_base_delay_ms: int = Field(
        default=50,
        ge=10,
        le=5000,
        description="Base delay in milliseconds for exponential backoff between retries"
    )
```

### Example Retry Scenarios

**Scenario 1: Transient Network Failure**
- First attempt fails due to temporary network issue
- Waits 50ms, then retries
- Second attempt succeeds
- Result: Request processed successfully despite initial failure

**Scenario 2: Cascading Failure with Fallback**
- Request to Server A fails after 5 retries (2 seconds total)
- Round-robin moves to Server B
- Server B succeeds on first attempt
- Result: User gets response from alternate server

**Scenario 3: Timeout Budget Protection**
- Base delay is 50ms, max_retries is 5, total_timeout is 2 seconds
- Attempt 1: Fails at 100ms
- Attempt 2: Waits 50ms, fails at 200ms
- Attempt 3: Waits 100ms, fails at 400ms
- Attempt 4: Waits 200ms, fails at 800ms
- Attempt 5: Waits 400ms, fails at 1400ms
- Attempt 6: Would need 800ms delay but only 600ms remains, fails at 2000ms
- Result: All retries exhausted within the 2-second budget

### Benefits

✅ **Resilience**: Temporary network issues don't cause request failures  
✅ **User Experience**: Users see successful responses even during brief service disruptions  
✅ **Load Distribution**: Failures are distributed across retry attempts preventing thundering herd  
✅ **Monitoring**: Detailed logging helps identify patterns of failure  
✅ **Configurability**: Can be tuned based on network conditions and requirements  

---

## Backward Compatibility and Database Migration

### Overview

The Ollama Proxy Server uses an automatic database migration system that ensures existing databases from older versions can be upgraded without manual intervention. This means you can use an old `ollama_proxy.db` file with new versions of the code without losing data.

### How Migration Works

On every server startup, the migration system:

1. **Checks for missing columns** in existing tables
2. **Adds missing columns** with appropriate defaults
3. **Creates missing indexes** for performance
4. **Updates JSON settings** with new configuration fields
5. **Reports schema** on errors to help debugging

### Migration Architecture

#### Centralized Schema Definition

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

#### Automatic Detection

The migration system automatically:

- Compares existing database columns with expected schema
- Identifies missing columns
- Adds them using `ALTER TABLE` with appropriate SQL types
- Logs all changes for visibility

### Core Migration Functions

#### Check Column Existence

```python
async def check_column_exists(engine: AsyncEngine, table_name: str, column_name: str) -> bool:
    """
    Check if a column exists in a table.
    
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
```

#### Add Column If Missing

```python
async def add_column_if_missing(
    engine: AsyncEngine,
    table_name: str,
    column_name: str,
    column_definition: str
) -> bool:
    """
    Add a column to a table if it doesn't exist.
    
    Args:
        table_name: Name of the table
        column_name: Name of the column to add
        column_definition: SQL definition (e.g., "JSON", "DATETIME", "BOOLEAN DEFAULT 1")
    
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
```

#### Table-Specific Migrations

Example: Migrating the `ollama_servers` table

```python
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
```

### Adding New Schema Changes

When you add a new column to a model, follow these steps:

#### Step 1: Update SQLAlchemy Model

Add the new column to the model in `app/database/models.py`:

```python
class OllamaServer(Base):
    __tablename__ = "ollama_servers"

    # ... existing columns ...
    new_column = Column(String, nullable=True)  # Your new column
```

#### Step 2: Update Migration Schema

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

#### Step 3: Restart Server

The migration will run automatically on the next startup and add the column to existing databases.

### JSON Settings Migration

For settings stored in the `app_settings.settings_data` JSON field, update `migrate_app_settings_data()`:

```python
default_retry_settings = {
    "max_retries": 5,
    "retry_total_timeout_seconds": 2.0,
    "retry_base_delay_ms": 50,
    "your_new_setting": "default_value",  # Add here
}
```

### Migration Safety

The migration system is designed to be **non-destructive**:

- ✅ **Adds** missing columns
- ✅ **Preserves** existing data
- ✅ **Creates** missing indexes
- ❌ **Never deletes** columns
- ❌ **Never modifies** existing data
- ❌ **Never drops** tables

### Debugging Schema Issues

#### Enable Schema Reporting

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

#### Schema Report on Errors

If a migration fails, the system automatically generates a schema report to help diagnose the issue.

### Example Migration Log

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

### How to Migrate Old Databases

#### Option 1: Automatic Migration (Recommended)

1. **Backup your database** (just in case):
   ```bash
   cp ollama_proxy.db ollama_proxy.db.backup
   ```

2. **Stop the old version** of the server

3. **Update the code** to the new version:
   ```bash
   git pull origin main
   # or download the new version
   ```

4. **Start the new version** of the server:
   ```bash
   ./run.sh  # On Linux/Mac
   run_windows.bat  # On Windows
   ```

5. **Check the logs** to verify migrations completed successfully:
   ```
   All database migrations completed successfully
   ```

The new version will automatically detect your old database schema and add any missing columns with appropriate defaults.

#### Option 2: Manual Verification

If you want to verify the database manually:

1. **Install SQLite tools**:
   ```bash
   # On macOS:
   brew install sqlite3
   
   # On Linux (Ubuntu/Debian):
   sudo apt-get install sqlite3
   
   # On Windows:
   # Download from https://www.sqlite.org/download.html
   ```

2. **Check database schema**:
   ```bash
   sqlite3 ollama_proxy.db ".schema api_keys"
   ```

3. **List all tables**:
   ```bash
   sqlite3 ollama_proxy.db ".tables"
   ```

### Common Migration Scenarios

#### Scenario 1: Adding a New Feature Column

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

3. Restart server - migration happens automatically

**What happens**: Existing databases will get the new column with a default value of 10 for all existing rows.

#### Scenario 2: Adding Application Settings

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

3. Restart server - setting is automatically added to existing settings records

**What happens**: Existing settings records get the new field with the default value.

### Troubleshooting

#### Error: "no such column: table.column"

**Cause:** A column is missing from the database schema.

**Solution:**
1. Ensure the column is defined in the migration schema
2. Restart the server to run migrations
3. Check logs for migration status

#### Error: Migration fails with "database is locked"

**Cause:** Another process is accessing the database.

**Solution:**
1. Stop all running instances of the server
2. Ensure no other process is accessing the SQLite database
3. Restart the server

#### Error: Migration runs but column still missing

**Cause:** Migration may have encountered an error.

**Solution:**
1. Check server logs for migration errors
2. Enable schema reporting to verify current state
3. Manually verify database with: `sqlite3 ollama_proxy.db ".schema table_name"`

### Performance Considerations

- Migrations run **once per startup** before the server accepts requests
- Adding columns is a fast operation in SQLite (< 1ms per column)
- No performance impact after initial migration
- Subsequent startups check but don't re-add existing columns

### Best Practices

1. **Test migrations** with a copy of production database first
2. **Backup database** before major upgrades
3. **Review migration logs** after upgrades
4. **Keep migration schema in sync** with SQLAlchemy models
5. **Use appropriate SQL types** and defaults in migration schema

---

## Branding and GUI Customization

### Overview

The Ollama Proxy Server supports comprehensive GUI customization through environment variables in the `.env` file. This allows organizations to brand the interface with their logos, colors, and titles.

### Available Branding Options

#### 1. BRANDING_TITLE

**Description:** The title displayed in the sidebar header and footer.  
**Type:** String  
**Default:** `Ollama Proxy`  
**Example:**
```
BRANDING_TITLE=My Company AI Proxy
```

#### 2. BRANDING_LOGO_URL

**Description:** URL to a logo image displayed in the sidebar header.  
**Type:** String (URL)  
**Default:** Empty (no logo)  
**Example:**
```
BRANDING_LOGO_URL=https://example.com/logo.png
```

#### 3. BRANDING_SHOW_LOGO

**Description:** Whether to display the logo in the sidebar header.  
**Type:** Boolean (`true` or `false`)  
**Default:** `false`  
**Example:**
```
BRANDING_SHOW_LOGO=true
```

#### 4. BRANDING_SIDEBAR_BG_COLOR (NEW!)

**Description:** Tailwind CSS background color class for the sidebar.  
**Type:** Tailwind CSS class  
**Default:** `bg-gray-800`  
**Supported Values:** Any valid Tailwind dark color class  
**Example:**
```
BRANDING_SIDEBAR_BG_COLOR=bg-blue-900
```

### Sidebar Color Options

The sidebar background color is applied using Tailwind CSS utility classes. For best visual results, use dark background colors to ensure good contrast with the white text.

#### Popular Color Options:

| Class | Color | Use Case |
|-------|-------|----------|
| `bg-gray-800` | Dark Gray | Default, neutral, professional |
| `bg-gray-900` | Darker Gray | High contrast, very dark |
| `bg-blue-900` | Dark Blue | Tech-focused, calm, professional |
| `bg-indigo-900` | Dark Indigo | Modern, sleek appearance |
| `bg-purple-900` | Dark Purple | Creative, modern feel |
| `bg-slate-800` | Dark Slate | Contemporary, sophisticated |
| `bg-zinc-800` | Dark Zinc | Minimalist, clean |
| `bg-stone-800` | Dark Stone | Warm, earthy tone |
| `bg-red-900` | Dark Red | Bold, attention-grabbing |
| `bg-green-900` | Dark Green | Natural, eco-friendly |
| `bg-amber-900` | Dark Amber | Warm, inviting |

### Configuration Implementation

#### Step 1: Configuration Layer

**File:** `app/core/config.py`

The new branding option is defined in the Settings class:

```python
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # --- Bootstrap Settings ---
    DATABASE_URL: str = "sqlite+aiosqlite:///./ollama_proxy.db"
    ADMIN_USER: str = "admin"
    ADMIN_PASSWORD: str = "changeme"
    PROXY_PORT: int = 8080
    SECRET_KEY: str = "..."

    # --- Branding Configuration ---
    BRANDING_TITLE: str = "Ollama Proxy"
    BRANDING_LOGO_URL: Optional[str] = None
    BRANDING_SHOW_LOGO: bool = False
    BRANDING_SIDEBAR_BG_COLOR: str = "bg-gray-800"  # Tailwind CSS class for sidebar background color

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = 'ignore'

settings = Settings()
```

**Key Points:**
- Settings are read from `.env` file at startup
- Bootstrap settings are passed to all templates via `get_template_context()`
- Default value maintains backward compatibility

#### Step 2: Template Integration

**File:** `app/templates/admin/base.html`

The branding configuration is applied directly in the templates using Jinja2:

**Sidebar Element (line 52):**
```html
<div id="sidebar" class="{{ bootstrap_settings.BRANDING_SIDEBAR_BG_COLOR }} text-white w-64 min-w-[16rem] flex-shrink-0 flex flex-col justify-between transition-all duration-300 ease-in-out">
```

**Mobile Open Button (line 44):**
```html
<button id="sidebar-open-btn" class="fixed top-4 left-4 z-50 p-2 {{ bootstrap_settings.BRANDING_SIDEBAR_BG_COLOR }} text-white rounded-md hover:opacity-80 shadow-lg">
```

**Logo Display (lines 56-57):**
```html
{% if bootstrap_settings.BRANDING_SHOW_LOGO and bootstrap_settings.BRANDING_LOGO_URL %}
    <img src="{{ bootstrap_settings.BRANDING_LOGO_URL }}" alt="Logo" class="h-8 w-8 mr-2 object-contain">
{% endif %}
```

**Title Display (line 59):**
```html
{{ bootstrap_settings.BRANDING_TITLE }}
```

#### Step 3: Template Context

**File:** `app/api/v1/routes/admin.py`

Bootstrap settings are provided to all templates through a helper function:

```python
def get_template_context(request: Request) -> dict:
    """Add common context to all templates"""
    return {
        "request": request,
        "is_redis_connected": request.app.state.redis is not None,
        "bootstrap_settings": settings  # <-- Branding settings included here
    }

# Usage in all template responses:
@router.get("/dashboard", response_class=HTMLResponse, name="admin_dashboard")
async def admin_dashboard(request: Request, admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)  # <-- Includes bootstrap_settings
    # ... additional context ...
    return templates.TemplateResponse("admin/dashboard.html", context)
```

### Configuration Examples

#### Example 1: Blue Sidebar with Logo

```bash
BRANDING_TITLE=Acme Corp AI
BRANDING_LOGO_URL=https://acmecorp.com/logo.png
BRANDING_SHOW_LOGO=true
BRANDING_SIDEBAR_BG_COLOR=bg-blue-900
```

**Result:** Sidebar displays in dark blue with company name and logo

#### Example 2: Purple Sidebar (Modern Look)

```bash
BRANDING_TITLE=TechStart AI Hub
BRANDING_SIDEBAR_BG_COLOR=bg-purple-900
```

**Result:** Modern purple sidebar with custom title

#### Example 3: Green Sidebar (Eco-Friendly)

```bash
BRANDING_TITLE=Green Energy AI
BRANDING_SIDEBAR_BG_COLOR=bg-green-900
```

**Result:** Natural green sidebar for eco-conscious organizations

### How to Set Up

1. **Create or edit your `.env` file** in the project root directory:
   ```bash
   cp .env.example .env
   ```

2. **Customize the branding options:**
   ```
   BRANDING_TITLE=Your Company Name
   BRANDING_SIDEBAR_BG_COLOR=bg-blue-900
   ```

3. **Restart the server** for changes to take effect:
   ```bash
   ./run.sh  # On Linux/Mac
   run_windows.bat  # On Windows
   ```

### Docker Deployment

When using Docker, pass the `.env` file as shown:

```bash
docker run -d --name ollama-proxy \
  -p 8080:8080 \
  --env-file ./.env \
  -v ./ollama_proxy.db:/home/app/ollama_proxy.db \
  ollama-proxy-server
```

### Design Considerations

- **Contrast:** All sidebar colors are dark to maintain good contrast with white text
- **Consistency:** The sidebar open button (mobile) uses the same color as the sidebar
- **Responsive:** Branding applies consistently across all screen sizes
- **Performance:** All colors are standard Tailwind classes, no custom CSS needed
- **Accessibility:** Dark colors with white text meet WCAG contrast requirements

### Code Changes Summary

**Modified Files:**

1. **app/core/config.py**
   - Added `BRANDING_SIDEBAR_BG_COLOR` configuration variable
   - Type: String (Tailwind CSS class)
   - Default: `"bg-gray-800"`

2. **app/templates/admin/base.html**
   - Updated sidebar element to use `{{ bootstrap_settings.BRANDING_SIDEBAR_BG_COLOR }}`
   - Updated mobile open button to match sidebar color
   - Applied to both elements for consistency

**Created Files:**

1. **.env.example**
   - Template configuration file with all branding options
   - Includes documentation and examples
   - Lists 10+ popular Tailwind CSS color options

2. **BRANDING_GUIDE.md**
   - Comprehensive user-facing documentation
   - Configuration examples for different use cases
   - Troubleshooting tips and color reference table

### Troubleshooting

**The sidebar color isn't changing:**
- Ensure the `.env` file exists in the project root
- Verify the Tailwind CSS class name is spelled correctly (e.g., `bg-blue-900`, not `bg-blue`)
- Restart the server after making changes
- Clear your browser cache (Ctrl+Shift+Delete or Cmd+Shift+Delete)

**The color doesn't look right:**
- Ensure you're using a dark background color (not light colors like `bg-yellow-100`)
- The sidebar text is white, so light colors will be unreadable
- Test with one of the recommended colors from the table above

### Custom Colors

If you need a color not in the list above, any valid Tailwind CSS dark background class should work. For a complete list of available Tailwind colors, visit: https://tailwindcss.com/docs/background-color

---

## Summary

The current branch brings significant improvements to the Ollama Proxy Server in three main areas:

1. **Robustness** through intelligent retry mechanisms with exponential backoff
2. **Compatibility** through non-destructive automatic database migrations
3. **Customization** through flexible branding and GUI customization options

These updates ensure that the proxy server is more resilient, easier to upgrade, and better suited for enterprise deployments where branding consistency is important.
