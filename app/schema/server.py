from pydantic import BaseModel, AnyHttpUrl, Field, ConfigDict
import datetime
from typing import Literal, Optional

class ServerBase(BaseModel):
    name: str
    url: AnyHttpUrl
    server_type: Literal["ollama", "vllm"] = "ollama"

class ServerCreate(ServerBase):
    api_key: Optional[str] = Field(None, description="Optional API key for connecting to the server.")

class ServerUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[AnyHttpUrl] = None
    server_type: Optional[Literal["ollama", "vllm"]] = None
    api_key: Optional[str] = Field(None, description="Provide a new key to update, or an empty string to remove.")


class Server(ServerBase):
    id: int
    is_active: bool
    has_api_key: bool = False
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())
