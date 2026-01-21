# 📁 app/schema/settings.py
from pydantic import BaseModel, Field, field_validator, ConfigDict
from typing import List, Optional, Dict

class AppSettingsModel(BaseModel):
    # This prevents the Pydantic warning about "model_" prefixed fields.
    model_config = ConfigDict(protected_namespaces=())

    # --- BRANDING SETTINGS ---
    branding_title: str = "Ollama Proxy"
    branding_logo_url: Optional[str] = Field(default=None, validate_default=True)

    # --- THEME SETTINGS ---
    ui_style: str = "dark-glass"  # 'dark-glass', 'dark-flat', 'light-glass', 'light-flat'
    selected_theme: str = "indigo"
    
    # Static property, not stored in DB, but available for the app
    @property
    def available_themes(self) -> Dict[str, Dict[str, str]]:
        return {
            "indigo": { "500": "#6366f1", "600": "#4f46e5", "700": "#4338ca", "800": "#3730a3" },
            "sky": { "500": "#0ea5e9", "600": "#0284c7", "700": "#0369a1", "800": "#075985" },
            "teal": { "500": "#14b8a6", "600": "#0d9488", "700": "#0f766e", "800": "#115e59" },
            "rose": { "500": "#f43f5e", "600": "#e11d48", "700": "#be123c", "800": "#9f1239" },
            "amber": { "500": "#f59e0b", "600": "#d97706", "700": "#b45309", "800": "#92400e" },
            "emerald": { "500": "#10b981", "600": "#059669", "700": "#047857", "800": "#065f46" },
            "fuchsia": { "500": "#d946ef", "600": "#c026d3", "700": "#a21caf", "800": "#86198f" },
            "orange": { "500": "#f97316", "600": "#ea580c", "700": "#c2410c", "800": "#9a3412" },
            "black": { "500": "#e5e7eb", "600": "#d1d5db", "700": "#9ca3af", "800": "#4b5563" },
            "white": { "500": "#4b5563", "600": "#374151", "700": "#1f2937", "800": "#111827" }
        }

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_username: Optional[str] = None
    redis_password: Optional[str] = None
    
    @field_validator('redis_port', mode='before')
    @classmethod
    def validate_redis_port(cls, v):
        """Ensure redis_port is always an integer, even if stored as string"""
        # Handle None
        if v is None:
            return 6379
        
        # Handle string - try to convert, but catch any errors
        if isinstance(v, str):
            # Strip whitespace
            v = v.strip()
            # If it looks like a password (contains special chars, not just digits), return default
            if not v.replace('-', '').replace('+', '').isdigit():
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Invalid redis_port value '{v}' appears to be a password, using default 6379")
                return 6379
            # Try to convert string to int
            try:
                port = int(v)
                # Validate range
                if 1 <= port <= 65535:
                    return port
                else:
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.warning(f"Redis port {port} out of range (1-65535), using default 6379")
                    return 6379
            except (ValueError, TypeError):
                # If conversion fails, return default
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Cannot convert redis_port '{v}' to integer, using default 6379")
                return 6379
        
        # Handle int
        if isinstance(v, int):
            if 1 <= v <= 65535:
                return v
            else:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Redis port {v} out of range (1-65535), using default 6379")
                return 6379
        
        # For any other type, return default
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Invalid redis_port type '{type(v)}', using default 6379")
        return 6379

    rate_limit_requests: int = 100
    rate_limit_window_minutes: int = 1

    allowed_ips: str = ""
    denied_ips: str = ""

    model_update_interval_minutes: int = 10

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
    
    # --- HTTPS/SSL Settings ---
    ssl_keyfile: Optional[str] = Field(default=None, description="Path to the SSL private key file (e.g., key.pem). Requires a restart.")
    ssl_certfile: Optional[str] = Field(default=None, description="Path to the SSL certificate file (e.g., cert.pem). Requires a restart.")
    ssl_keyfile_content: Optional[str] = Field(default=None, description="Content of the uploaded SSL key file.", exclude=True) # Exclude from API responses
    ssl_certfile_content: Optional[str] = Field(default=None, description="Content of the uploaded SSL cert file.", exclude=True) # Exclude from API responses

    # --- SECURITY ---
    blocked_ollama_endpoints: str = Field(
        default="pull,delete,create,copy,push",
        description="Comma-separated list of Ollama API paths to block for API key holders."
    )
    
    # --- OLLAMA WEB SEARCH ---
    ollama_api_key: Optional[str] = Field(
        default=None,
        description="Ollama API key for web search and fetch (Account 1). Get one at https://ollama.com/settings/keys"
    )
    ollama_api_key_2: Optional[str] = Field(
        default=None,
        description="Ollama API key for web search and fetch (Account 2 - backup/load balancing). Get one at https://ollama.com/settings/keys"
    )
    enable_proxy_web_search: bool = Field(
        default=True,
        description="Enable automatic web search for all tools using the proxy. When enabled, chat requests through the proxy will automatically include web search results when needed. Disable if external tools have their own web search logic."
    )
    
    # --- SEARXNG SETTINGS ---
    searxng_url: Optional[str] = Field(
        default=None,
        description="URL for local SearXNG instance (e.g., http://localhost:7019). If not set, will use Ollama cloud search as primary. Get SearXNG at https://github.com/searxng/searxng"
    )
    
    # --- AUTO-ROUTING SETTINGS ---
    auto_routing_priority_mode: str = Field(
        default="free",
        description="Priority mode for auto-routing: 'free' (free models first), 'daily_drive' (Ollama cloud first), 'advanced' (top-tier paid first), or 'luxury' (premium models for high-budget scenarios)"
    )

    @field_validator('retry_total_timeout_seconds')
    @classmethod
    def validate_retry_timeout(cls, v: float, info) -> float:
        """Ensure retry timeout is reasonable."""
        if v <= 0:
            raise ValueError("retry_total_timeout_seconds must be positive")
        return v
    
    @field_validator('branding_logo_url', 'ssl_keyfile', 'ssl_certfile')
    @classmethod
    def validate_empty_string_to_none(cls, v: Optional[str]) -> Optional[str]:
        if v == "":
            return None
        return v
