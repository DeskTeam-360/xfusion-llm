import os
import logging
from config import settings

# Configure logging
logger = logging.getLogger("xfusion-backend.database")

# Try to import Chroma from langchain_chroma (modern), fallback to langchain_community
try:
    from langchain_chroma import Chroma
    logger.info("Successfully imported Chroma from langchain_chroma")
except ImportError:
    try:
        from langchain_community.vectorstores import Chroma
        logger.info("Successfully imported Chroma from langchain_community.vectorstores")
    except ImportError as e:
        logger.error("Failed to import Chroma from either langchain_chroma or langchain_community. Please ensure your dependencies are installed.")
        raise e

from langchain_openai import OpenAIEmbeddings

# Global instances for lazy initialization
_embeddings = None
_vector_store = None

def get_embeddings() -> OpenAIEmbeddings:
    """
    Get or initialize OpenAI Embeddings with the configured model.
    """
    global _embeddings
    if not settings.OPENAI_API_KEY or settings.OPENAI_API_KEY.strip() == "" or settings.OPENAI_API_KEY.startswith("sk-proj-..."):
        raise ValueError("OPENAI_API_KEY is not configured or is using a placeholder. Please configure it in your .env file.")
    
    if _embeddings is None:
        _embeddings = OpenAIEmbeddings(
            model="text-embedding-3-small",
            openai_api_key=settings.OPENAI_API_KEY
        )
    return _embeddings

def get_vector_store() -> Chroma:
    """
    Get or initialize Chroma Vector Store with persistence and embeddings.
    """
    global _vector_store
    if _vector_store is None:
        embeddings = get_embeddings()
        
        # Ensure persist directory exists
        os.makedirs(settings.CHROMA_PERSIST_DIR, exist_ok=True)
        
        _vector_store = Chroma(
            persist_directory=settings.CHROMA_PERSIST_DIR,
            embedding_function=embeddings
        )
        logger.info(f"ChromaDB initialized and persisted at '{settings.CHROMA_PERSIST_DIR}'")
    
    return _vector_store

def get_chunks_by_wordpress_post_id(db: Chroma, post_id: int) -> list[str]:
    """
    Return all indexed document texts for a WordPress post ID.
    """
    try:
        result = db.get(where={"wordpress_post_id": post_id})
        if not result or "documents" not in result or not result["documents"]:
            return []
        return [doc for doc in result["documents"] if doc]
    except Exception as e:
        logger.error(f"Error fetching chunks for wordpress_post_id {post_id}: {str(e)}")
        raise e

def delete_by_wordpress_post_id(db: Chroma, post_id: int) -> int:
    """
    Query for documents in ChromaDB with metadata {"wordpress_post_id": post_id} and delete them.
    Returns the number of deleted document chunks.
    """
    try:
        # Chroma's get() method accepts a standard mongo-like where filter
        # e.g. {"wordpress_post_id": post_id}
        result = db.get(where={"wordpress_post_id": post_id})
        
        if result and "ids" in result and result["ids"]:
            ids_to_delete = result["ids"]
            db.delete(ids=ids_to_delete)
            logger.info(f"Deleted {len(ids_to_delete)} existing chunks for wordpress_post_id: {post_id}")
            return len(ids_to_delete)
        
        logger.info(f"No existing chunks found to delete for wordpress_post_id: {post_id}")
        return 0
    except Exception as e:
        logger.error(f"Error deleting chunks for wordpress_post_id {post_id}: {str(e)}")
        raise e
