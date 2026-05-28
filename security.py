from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from config import settings

# HTTPBearer is a standard FastAPI security dependency that checks for "Authorization: Bearer <token>"
security = HTTPBearer()

def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """
    Dependency to validate the Bearer token passed in the Authorization header.
    Validates it against the API_KEY set in the environment settings.
    """
    token = credentials.credentials
    if token != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key Bearer token."
        )
    return token
