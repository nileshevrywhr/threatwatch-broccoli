import os
import jwt
from fastapi import HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# Initialize the security scheme
security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    """
    Verifies the Supabase JWT and returns the user_id (sub claim).
    Raises HTTPException(401) if token is invalid or missing.
    """
    token = credentials.credentials
    secret = os.environ.get("SUPABASE_JWT_SECRET")

    # This check is technically redundant if we check at startup,
    # but good for safety in the dependency itself.
    if not secret:
        raise HTTPException(status_code=500, detail="Server misconfiguration: Missing JWT secret")

    try:
        # Supabase uses HS256 by default.
        # verify=True is default, but we'll be explicit about what we want.
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"verify_aud": False} # Supabase JWTs might not strictly match aud depending on setup, usually 'authenticated'
        )

        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token: missing sub claim")

        return user_id

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")
    except Exception as e:
        # Catch-all for other JWT errors
        raise HTTPException(status_code=401, detail="Could not validate credentials")
