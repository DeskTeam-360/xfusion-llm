import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from langchain_text_splitters import RecursiveCharacterTextSplitter
from database import get_vector_store, delete_by_wordpress_post_id
from security import verify_api_key

# Configure logging
logger = logging.getLogger("xfusion-backend.routers.knowledge")

router = APIRouter(
    prefix="/api/v1/knowledge",
    tags=["Knowledge Base"]
    # dependencies=[Depends(verify_api_key)]  # Temporarily disabled for testing
)

# Request Models
class KnowledgeUpsertRequest(BaseModel):
    wordpress_post_id: int = Field(..., description="The unique ID of the WordPress post/data source", example=101)
    category: str = Field(..., description="The category of the knowledge content", example="Standard Operating Procedure")
    content: str = Field(..., description="The full body content of the knowledge article", example="Employees must greet customers within 3 seconds...")

# Response Models
class UpsertResponse(BaseModel):
    status: str = Field(default="success")
    message: str = Field(..., description="Detailed message about the operation")
    wordpress_post_id: int = Field(...)
    chunks_added: int = Field(..., description="Number of new chunks added to the vector store")
    chunks_deleted: int = Field(..., description="Number of duplicate chunks deleted before indexing")

class DeleteResponse(BaseModel):
    status: str = Field(default="success")
    message: str = Field(..., description="Detailed message about the operation")
    wordpress_post_id: int = Field(...)
    chunks_deleted: int = Field(..., description="Number of chunks deleted from the vector store")


@router.get("/list", status_code=status.HTTP_200_OK)
async def list_knowledge():
    """
    Retrieves and lists all currently stored company knowledge chunks from ChromaDB for testing and debugging.
    """
    try:
        db = get_vector_store()
        # ChromaDB .get() fetches all documents and their metadata
        data = db.get()
        
        results = []
        if data and "ids" in data:
            for idx in range(len(data["ids"])):
                results.append({
                    "id": data["ids"][idx],
                    "metadata": data["metadatas"][idx] if "metadatas" in data and data["metadatas"] else {},
                    "document": data["documents"][idx] if "documents" in data and data["documents"] else ""
                })
        
        return {
            "total_chunks": len(results),
            "chunks": results
        }
    except ValueError as val_err:
        logger.error(f"Configuration or initialization error: {str(val_err)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err)
        )
    except Exception as e:
        logger.error(f"Failed to retrieve knowledge chunks: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while listing knowledge base: {str(e)}"
        )


@router.post("/upsert", response_model=UpsertResponse, status_code=status.HTTP_201_CREATED)
async def upsert_knowledge(payload: KnowledgeUpsertRequest):
    """
    Upsert a post from WordPress into the Vector Database.
    
    1. Removes any existing documents/chunks belonging to this `wordpress_post_id` to avoid duplicates.
    2. Chunks the text using `RecursiveCharacterTextSplitter`.
    3. Adds metadata for indexing.
    4. Computes embeddings and saves to ChromaDB.
    """
    try:
        # 1. Initialize Vector Store
        db = get_vector_store()
        
        # 2. Deduplicate: Delete existing documents for this wordpress_post_id
        chunks_deleted = delete_by_wordpress_post_id(db, payload.wordpress_post_id)
        
        # Check if content is empty
        if not payload.content.strip():
            return UpsertResponse(
                status="success",
                message="Knowledge post deduplicated and cleaned up, but no new content was added because 'content' was empty.",
                wordpress_post_id=payload.wordpress_post_id,
                chunks_added=0,
                chunks_deleted=chunks_deleted
            )
            
        # 3. Chunk the new 'content'
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200
        )
        texts = text_splitter.split_text(payload.content)
        
        # 4. Prepare metadata with current UTC timestamp
        current_time = datetime.now(timezone.utc).isoformat()
        metadatas = [
            {
                "wordpress_post_id": payload.wordpress_post_id,
                "category": payload.category,
                "updated_at": current_time
            }
            for _ in range(len(texts))
        ]
        
        # 5. Add to ChromaDB
        db.add_texts(texts=texts, metadatas=metadatas)
        logger.info(f"Successfully upserted {len(texts)} chunks for wordpress_post_id: {payload.wordpress_post_id}")
        
        return UpsertResponse(
            message="Knowledge base content successfully updated.",
            wordpress_post_id=payload.wordpress_post_id,
            chunks_added=len(texts),
            chunks_deleted=chunks_deleted
        )
        
    except ValueError as val_err:
        # Handles missing API key and validation errors
        logger.error(f"Validation error during upsert: {str(val_err)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err)
        )
    except Exception as e:
        logger.error(f"Unexpected error during upsert for post {payload.wordpress_post_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while indexing knowledge: {str(e)}"
        )


@router.delete("/delete/{post_id}", response_model=DeleteResponse, status_code=status.HTTP_200_OK)
async def delete_knowledge(post_id: int):
    """
    Deletes all indexed chunks belonging to a specific WordPress post ID from the vector database.
    """
    try:
        db = get_vector_store()
        chunks_deleted = delete_by_wordpress_post_id(db, post_id)
        
        if chunks_deleted == 0:
            return DeleteResponse(
                message=f"No indexed knowledge chunks found for WordPress post ID: {post_id}.",
                wordpress_post_id=post_id,
                chunks_deleted=0
            )
            
        return DeleteResponse(
            message=f"Successfully deleted all indexed knowledge associated with WordPress post ID: {post_id}.",
            wordpress_post_id=post_id,
            chunks_deleted=chunks_deleted
        )
        
    except ValueError as val_err:
        logger.error(f"Validation error during delete: {str(val_err)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err)
        )
    except Exception as e:
        logger.error(f"Unexpected error during delete for post {post_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while deleting knowledge chunks: {str(e)}"
        )

