"""
API routes for conversation/thread management
"""
import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.database.models import User
from app.api.v1.routes.admin import require_admin_user
from app.crud import conversation_crud
from app.schema.conversation import ConversationResponse, ConversationWithMessages, MessageResponse
from app.core.rag_service import RAGService

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/conversations", response_model=List[ConversationResponse])
async def get_conversations(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    limit: int = 50,
    offset: int = 0
):
    """Get all conversations for the current user"""
    from sqlalchemy import select, func
    from app.database.models import Message
    
    conversations = await conversation_crud.get_user_conversations(
        db, admin_user.id, limit=limit, offset=offset
    )
    
    # Get message counts for all conversations in one query
    conversation_ids = [conv.id for conv in conversations]
    message_counts = {}
    if conversation_ids:
        result = await db.execute(
            select(
                Message.conversation_id,
                func.count(Message.id).label('count')
            )
            .where(Message.conversation_id.in_(conversation_ids))
            .group_by(Message.conversation_id)
        )
        message_counts = {row.conversation_id: row.count for row in result.all()}
    
    # Build response with message counts
    result = []
    for conv in conversations:
        conv_dict = {
            "id": conv.id,
            "user_id": conv.user_id,
            "title": conv.title,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
            "message_count": message_counts.get(conv.id, 0)
        }
        result.append(conv_dict)
    
    return result


@router.get("/conversations/{conversation_id}", response_model=ConversationWithMessages)
async def get_conversation(
    request: Request,
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Get a specific conversation with all its messages"""
    conversation = await conversation_crud.get_conversation(db, conversation_id, admin_user.id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    messages = await conversation_crud.get_conversation_messages(db, conversation_id)
    
    return {
        "id": conversation.id,
        "user_id": conversation.user_id,
        "title": conversation.title,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
        "message_count": len(messages),
        "messages": [
            {
                "id": msg.id,
                "conversation_id": msg.conversation_id,
                "role": msg.role,
                "content": msg.content,
                "model_name": msg.model_name,
                "metadata": msg.message_metadata,
                "created_at": msg.created_at
            }
            for msg in messages
        ]
    }


@router.post("/conversations", response_model=ConversationResponse)
async def create_conversation(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Create a new conversation thread"""
    conversation = await conversation_crud.create_conversation(db, admin_user.id)
    return {
        "id": conversation.id,
        "user_id": conversation.user_id,
        "title": conversation.title,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
        "message_count": 0
    }


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    request: Request,
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Delete a conversation and all its messages"""
    # Also delete from RAG vector DB
    if hasattr(request.app.state, 'rag_service') and request.app.state.rag_service:
        await request.app.state.rag_service.delete_conversation_embedding(conversation_id)
    
    success = await conversation_crud.delete_conversation(db, conversation_id, admin_user.id)
    if not success:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    return {"success": True}


@router.post("/conversations/{conversation_id}/messages")
async def add_message(
    request: Request,
    conversation_id: int,
    role: str,
    content: str,
    model_name: Optional[str] = None,
    metadata: Optional[dict] = None,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Add a message to a conversation"""
    # Verify conversation belongs to user
    conversation = await conversation_crud.get_conversation(db, conversation_id, admin_user.id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # Generate embedding if RAG is available
    embedding = None
    if hasattr(request.app.state, 'rag_service') and request.app.state.rag_service and request.app.state.rag_service.initialized:
        try:
            embedding = request.app.state.rag_service.generate_embedding(content)
        except Exception as e:
            logger.warning(f"Failed to generate embedding: {e}")
    
    message = await conversation_crud.add_message(
        db, conversation_id, role, content, model_name, metadata, embedding
    )
    
    # If this is the first exchange, generate title and embedding
    messages = await conversation_crud.get_conversation_messages(db, conversation_id)
    if len(messages) == 2:  # User message + assistant response
        first_exchange = await conversation_crud.get_first_exchange(db, conversation_id)
        if first_exchange:
            # Generate title from first exchange using AI (simplified - use first 50 chars for now)
            title = content[:50] + "..." if len(content) > 50 else content
            
            # Generate embedding for first exchange
            first_exchange_embedding = None
            if hasattr(request.app.state, 'rag_service') and request.app.state.rag_service and request.app.state.rag_service.initialized:
                try:
                    first_exchange_embedding = request.app.state.rag_service.generate_embedding(first_exchange)
                    # Store in vector DB
                    await request.app.state.rag_service.add_conversation_embedding(
                        conversation_id, title, first_exchange
                    )
                except Exception as e:
                    logger.warning(f"Failed to generate first exchange embedding: {e}")
            
            await conversation_crud.update_conversation(
                db, conversation_id, title=title, first_exchange_embedding=first_exchange_embedding
            )
    
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "role": message.role,
        "content": message.content,
        "model_name": message.model_name,
        "metadata": message.message_metadata,
        "created_at": message.created_at
    }


@router.get("/conversations/search")
async def search_conversations(
    request: Request,
    q: str,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    limit: int = 5
):
    """Search conversations using semantic search (RAG)"""
    if not hasattr(request.app.state, 'rag_service') or not request.app.state.rag_service or not request.app.state.rag_service.initialized:
        return []
    
    similar = await request.app.state.rag_service.search_similar_conversations(q, limit=limit)
    
    # Fetch full conversation details
    results = []
    for item in similar:
        conv = await conversation_crud.get_conversation(db, item["conversation_id"], admin_user.id)
        if conv:
            results.append({
                "id": conv.id,
                "title": conv.title,
                "created_at": conv.created_at,
                "updated_at": conv.updated_at,
                "similarity": item["similarity"],
                "first_exchange": item["first_exchange"]
            })
    
    return results

