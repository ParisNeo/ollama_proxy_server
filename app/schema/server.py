from pydantic import BaseModel, AnyHttpUrl
import datetime

class ServerBase(BaseModel):
    name: str
    url: AnyHttpUrl

class ServerCreate(ServerBase):
    pass

class Server(ServerBase):
    id: int
    is_active: bool
    created_at: datetime.datetime

    class Config:
        from_attributes = True