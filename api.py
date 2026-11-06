"""Plovetek web API foundation; does not start Gmail monitoring or sending."""
from contextlib import asynccontextmanager
from typing import Annotated
from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field, SecretStr
from web_feature_database import initialize_features
from pathlib import Path
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from web_routes import router
from web_auth import login_owner, logout_owner, require_owner

@asynccontextmanager
async def lifespan(app):
    initialize_features()
    yield

app = FastAPI(title='Plovetek AI Agent API', description='Owner dashboard, inbox review and AI chat.', version='1.1.0', lifespan=lifespan)

@app.middleware('http')
async def response_headers(request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    if request.url.path == '/' or request.url.path.startswith('/assets/'):
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    return response

class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=1024)

class OwnerResponse(BaseModel):
    id: int
    email: str

class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    user: OwnerResponse

@app.get('/', tags=['Health'])
def root():
    return FileResponse(Path(__file__).resolve().parent / 'frontend' / 'index.html')

@app.get('/health', tags=['Health'])
def health():
    return {'status': 'ok'}

@app.post('/auth/login', response_model=LoginResponse, tags=['Authentication'])
def login(data: LoginRequest, request: Request):
    return login_owner(request, data.email, data.password.get_secret_value())

@app.get('/auth/me', response_model=OwnerResponse, tags=['Authentication'])
def current_owner(owner: Annotated[dict, Depends(require_owner)]):
    return {'id': owner['id'], 'email': owner['email']}

@app.post('/auth/logout', tags=['Authentication'])
def logout(owner: Annotated[dict, Depends(require_owner)]):
    logout_owner(owner)
    return {'message': 'Logged out successfully'}

app.include_router(router)
app.mount('/assets', StaticFiles(directory=Path(__file__).resolve().parent / 'frontend'), name='assets')
