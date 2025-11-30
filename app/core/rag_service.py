"""
RAG (Retrieval-Augmented Generation) Service
Handles embeddings and vector search for conversation threads
"""
import logging
import json
import os
from typing import List, Dict, Any, Optional
import numpy as np
from pathlib import Path

# Disable ChromaDB telemetry before importing (must be set early)
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

try:
    import chromadb
    from chromadb.config import Settings
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    logging.warning("ChromaDB not available. Install with: pip install chromadb")

try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    logging.warning("sentence-transformers not available. Install with: pip install sentence-transformers")

logger = logging.getLogger(__name__)


class RAGService:
    """RAG service for conversation embeddings and semantic search"""
    
    def __init__(self):
        self.embedding_model: Optional[SentenceTransformer] = None
        self.chroma_client: Optional[Any] = None
        self.collection: Optional[Any] = None
        self.codebase_collection: Optional[Any] = None
        self.codebase_indexer: Optional[Any] = None
        self.initialized = False
        
    async def initialize(self):
        """Initialize the RAG service with embedding model and vector DB"""
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            raise ImportError("sentence-transformers not installed")
        
        if not CHROMADB_AVAILABLE:
            raise ImportError("chromadb not installed")
        
        try:
            # Initialize embedding model (using a lightweight model)
            logger.info("Loading embedding model...")
            self.embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
            logger.info("Embedding model loaded")
            
            # Initialize ChromaDB
            chroma_dir = Path("chroma_db")
            chroma_dir.mkdir(exist_ok=True)
            
            self.chroma_client = chromadb.PersistentClient(
                path=str(chroma_dir),
                settings=Settings(anonymized_telemetry=False)
            )
            
            # Get or create collection for conversations
            self.collection = self.chroma_client.get_or_create_collection(
                name="conversations",
                metadata={"description": "Conversation thread embeddings for semantic search"}
            )
            
            # Get or create collection for codebase
            try:
                self.codebase_collection = self.chroma_client.get_or_create_collection(
                    name="codebase",
                    metadata={"description": "Codebase embeddings for intelligent code awareness"}
                )
            except Exception as e:
                logger.warning(f"Could not create codebase collection: {e}")
                self.codebase_collection = None
            
            # Initialize codebase indexer
            try:
                from app.core.codebase_indexer import CodebaseIndexer
                self.codebase_indexer = CodebaseIndexer()
                # Index codebase immediately (synchronous) to ensure it's ready
                # This is critical for self-awareness to work
                try:
                    logger.info("Indexing codebase for self-awareness...")
                    result = self.codebase_indexer.index_codebase(max_files=200)  # Index up to 200 files
                    files_indexed = result.get('files_indexed', 0)
                    logger.info(f"Codebase indexed: {files_indexed} files ready for self-awareness")
                    if files_indexed == 0:
                        logger.warning("Codebase indexing returned 0 files - self-awareness may be limited")
                except Exception as e:
                    logger.error(f"Codebase indexing failed: {e}", exc_info=True)
                    # Don't fail initialization, but log the error
            except Exception as e:
                logger.error(f"Could not initialize codebase indexer: {e}", exc_info=True)
                self.codebase_indexer = None
            
            self.initialized = True
            logger.info("RAG service initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize RAG service: {e}", exc_info=True)
            raise
    
    def generate_embedding(self, text: str) -> List[float]:
        """Generate embedding vector for text"""
        if not self.initialized or not self.embedding_model:
            raise RuntimeError("RAG service not initialized")
        
        if not text or not text.strip():
            # Return zero vector for empty text
            return [0.0] * 384  # all-MiniLM-L6-v2 produces 384-dimensional vectors
        
        embedding = self.embedding_model.encode(text, normalize_embeddings=True)
        return embedding.tolist()
    
    async def add_conversation_embedding(
        self,
        conversation_id: int,
        title: str,
        first_exchange: str
    ):
        """Add or update conversation embedding in vector DB"""
        if not self.initialized:
            return
        
        try:
            embedding = self.generate_embedding(first_exchange)
            
            # Store in ChromaDB
            self.collection.upsert(
                ids=[str(conversation_id)],
                embeddings=[embedding],
                documents=[first_exchange],
                metadatas=[{"title": title, "conversation_id": conversation_id}]
            )
            
            logger.debug(f"Added embedding for conversation {conversation_id}")
            
        except Exception as e:
            logger.error(f"Error adding conversation embedding: {e}", exc_info=True)
    
    async def search_similar_conversations(
        self,
        query: str,
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Search for similar conversations using semantic search
        
        This searches both conversation titles/first exchanges AND message content
        by searching the documents stored in ChromaDB which include the full first exchange.
        """
        if not self.initialized:
            return []
        
        try:
            query_embedding = self.generate_embedding(query)
            
            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=limit
            )
            
            similar_conversations = []
            if results['ids'] and len(results['ids'][0]) > 0:
                for i, conv_id in enumerate(results['ids'][0]):
                    # Calculate similarity score (ChromaDB returns distances, convert to similarity)
                    distance = results['distances'][0][i] if 'distances' in results and results['distances'][0] else 1.0
                    similarity = 1.0 - distance  # Convert distance to similarity
                    
                    similar_conversations.append({
                        "conversation_id": int(conv_id),
                        "title": results['metadatas'][0][i].get("title", "") if results.get('metadatas') and results['metadatas'][0] else "",
                        "similarity": similarity,
                        "first_exchange": results['documents'][0][i] if 'documents' in results and results['documents'][0] else ""
                    })
            
            # Sort by similarity (highest first)
            similar_conversations.sort(key=lambda x: x['similarity'], reverse=True)
            
            return similar_conversations
            
        except Exception as e:
            logger.error(f"Error searching similar conversations: {e}", exc_info=True)
            return []
    
    async def delete_conversation_embedding(self, conversation_id: int):
        """Remove conversation embedding from vector DB"""
        if not self.initialized:
            return
        
        try:
            self.collection.delete(ids=[str(conversation_id)])
            logger.debug(f"Deleted embedding for conversation {conversation_id}")
        except Exception as e:
            logger.error(f"Error deleting conversation embedding: {e}", exc_info=True)
    
    def embedding_to_json(self, embedding: List[float]) -> str:
        """Convert embedding list to JSON string for database storage"""
        return json.dumps(embedding)
    
    def json_to_embedding(self, json_str: str) -> List[float]:
        """Convert JSON string to embedding list"""
        if not json_str:
            return []
        return json.loads(json_str)
    
    async def search_codebase(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Search codebase using semantic search and text matching"""
        results = []
        
        # First, try text-based search (faster, works immediately)
        if self.codebase_indexer:
            try:
                text_results = self.codebase_indexer.search_codebase(query, limit=limit * 2)
                results.extend(text_results)
            except Exception as e:
                logger.warning(f"Text-based codebase search failed: {e}")
        
        # Then, try semantic search if codebase collection exists and is populated
        if self.codebase_collection and self.initialized:
            try:
                query_embedding = self.generate_embedding(query)
                semantic_results = self.codebase_collection.query(
                    query_embeddings=[query_embedding],
                    n_results=limit
                )
                
                if semantic_results['ids'] and len(semantic_results['ids'][0]) > 0:
                    for i, doc_id in enumerate(semantic_results['ids'][0]):
                        distance = semantic_results['distances'][0][i] if 'distances' in semantic_results and semantic_results['distances'][0] else 1.0
                        similarity = 1.0 - distance
                        
                        results.append({
                            'file_path': doc_id,
                            'score': similarity * 100,  # Convert to score
                            'content': semantic_results['documents'][0][i] if 'documents' in semantic_results and semantic_results['documents'][0] else "",
                            'type': 'semantic'
                        })
            except Exception as e:
                logger.warning(f"Semantic codebase search failed: {e}")
        
        # Deduplicate and sort results
        seen_files = set()
        unique_results = []
        for result in results:
            file_path = result.get('file_path', '')
            if file_path and file_path not in seen_files:
                seen_files.add(file_path)
                unique_results.append(result)
        
        unique_results.sort(key=lambda x: x.get('score', 0), reverse=True)
        return unique_results[:limit]

