from pydantic import BaseModel, Field
from typing import List, Optional

class AppSettingsModel(BaseModel):
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_username: Optional[str] = None
    redis_password: Optional[str] = None
    
    rate_limit_requests: int = 100
    rate_limit_window_minutes: int = 1
    
    allowed_ips: str = ""
    denied_ips: str = ""
    
    model_update_interval_minutes: int = 10

    class Config:
        from_attributes = True