"""
Schemas for conversations and messages
"""
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from datetime import datetime


class MessageBase(BaseModel):
    role: str
    content: str
    model_name: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class MessageCreate(MessageBase):
    pass


class MessageResponse(MessageBase):
    id: int
    conversation_id: int
    created_at: datetime
    
    class Config:
        from_attributes = True


class ConversationBase(BaseModel):
    title: Optional[str] = None


class ConversationCreate(ConversationBase):
    pass


class ConversationUpdate(BaseModel):
    title: Optional[str] = None


class ConversationResponse(BaseModel):
    id: int
    user_id: int
    title: Optional[str]
    created_at: datetime
    updated_at: datetime
    message_count: Optional[int] = None
    
    class Config:
        from_attributes = True


class ConversationWithMessages(ConversationResponse):
    messages: List[MessageResponse] = []

