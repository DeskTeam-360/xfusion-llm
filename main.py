import logging
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from routers import knowledge, evaluation

# Configure logging
logger = logging.getLogger("xfusion-backend.main")

# Initialize FastAPI with customized metadata for the WordPress external API integration
app = FastAPI(
    title="Xfusion Exam Evaluation API",
    description=(
        "Production-Ready External API for automated employee exam evaluation. "
        "Integrates with WordPress, using FastAPI, LangChain, OpenAI (gpt-4o-mini, text-embedding-3-small), "
        "and ChromaDB."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Enable CORS (Cross-Origin Resource Sharing)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust this in production to only trust your WordPress domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Custom Exception Handler for global unhandled errors
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global unhandled exception on path {request.url.path}: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "detail": "An internal server error occurred. Please contact system administrator."
        }
    )

# Include Routers
app.include_router(knowledge.router)
app.include_router(evaluation.router)

# Health Check & Landing Page
@app.get("/", tags=["Health"])
async def root():
    """
    Service health check and configuration status endpoint.
    """
    has_openai = bool(settings.OPENAI_API_KEY and not settings.OPENAI_API_KEY.startswith("sk-proj-..."))
    return {
        "status": "healthy",
        "app_name": app.title,
        "version": app.version,
        "configuration": {
            "openai_api_key_configured": has_openai,
            "chroma_persist_directory": settings.CHROMA_PERSIST_DIR,
            "security": "API Key Bearer Authentication Enabled"
        }
    }

if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting server on {settings.HOST}:{settings.PORT}")
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, reload=True)
