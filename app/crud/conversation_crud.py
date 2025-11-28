"""
CRUD operations for conversations and messages
"""
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc, func
from typing import List, Optional, Dict, Any
from datetime import datetime

from app.database.models import Conversation, Message, User
from app.schema.conversation import ConversationCreate, ConversationUpdate, MessageCreate

logger = logging.getLogger(__name__)


async def create_conversation(
    db: AsyncSession,
    user_id: int,
    title: Optional[str] = None
) -> Conversation:
    """Create a new conversation thread"""
    conversation = Conversation(
        user_id=user_id,
        title=title,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def get_conversation(
    db: AsyncSession,
    conversation_id: int,
    user_id: Optional[int] = None
) -> Optional[Conversation]:
    """Get a conversation by ID, optionally filtered by user"""
    query = select(Conversation).filter(Conversation.id == conversation_id)
    if user_id:
        query = query.filter(Conversation.user_id == user_id)
    
    result = await db.execute(query)
    return result.scalars().first()


async def get_user_conversations(
    db: AsyncSession,
    user_id: int,
    limit: int = 50,
    offset: int = 0
) -> List[Conversation]:
    """Get all conversations for a user, ordered by most recent"""
    result = await db.execute(
        select(Conversation)
        .filter(Conversation.user_id == user_id)
        .order_by(desc(Conversation.updated_at))
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def update_conversation(
    db: AsyncSession,
    conversation_id: int,
    title: Optional[str] = None,
    first_exchange_embedding: Optional[List[float]] = None
) -> Optional[Conversation]:
    """Update conversation metadata"""
    conversation = await get_conversation(db, conversation_id)
    if not conversation:
        return None
    
    if title is not None:
        conversation.title = title
    if first_exchange_embedding is not None:
        conversation.first_exchange_embedding = first_exchange_embedding
    
    conversation.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def delete_conversation(
    db: AsyncSession,
    conversation_id: int,
    user_id: Optional[int] = None
) -> bool:
    """Delete a conversation and all its messages"""
    conversation = await get_conversation(db, conversation_id, user_id)
    if not conversation:
        return False
    
    await db.delete(conversation)
    await db.commit()
    return True


async def add_message(
    db: AsyncSession,
    conversation_id: int,
    role: str,
    content: str,
    model_name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    embedding: Optional[List[float]] = None
) -> Message:
    """Add a message to a conversation"""
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        model_name=model_name,
        message_metadata=metadata or {},
        embedding=embedding,
        created_at=datetime.utcnow()
    )
    db.add(message)
    
    # Update conversation's updated_at timestamp
    conversation = await get_conversation(db, conversation_id)
    if conversation:
        conversation.updated_at = datetime.utcnow()
    
    await db.commit()
    await db.refresh(message)
    return message


async def get_conversation_messages(
    db: AsyncSession,
    conversation_id: int,
    limit: Optional[int] = None,
    offset: int = 0
) -> List[Message]:
    """Get all messages for a conversation, ordered by creation time"""
    query = (
        select(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
        .offset(offset)
    )
    
    if limit:
        query = query.limit(limit)
    
    result = await db.execute(query)
    return list(result.scalars().all())


async def get_first_exchange(
    db: AsyncSession,
    conversation_id: int
) -> Optional[str]:
    """Get the first user message and assistant response for auto-naming"""
    messages = await get_conversation_messages(db, conversation_id, limit=2)
    
    if len(messages) >= 2:
        user_msg = next((m for m in messages if m.role == "user"), None)
        assistant_msg = next((m for m in messages if m.role == "assistant"), None)
        
        if user_msg and assistant_msg:
            return f"{user_msg.content}\n\n{assistant_msg.content[:200]}"
    
    if messages and messages[0].role == "user":
        return messages[0].content
    
    return None

