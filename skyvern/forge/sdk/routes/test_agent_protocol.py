import time
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch
from jose import jwt

from skyvern.forge.sdk.routes.agent_protocol import base_router
from skyvern.webeye.models import BrowserSessionResponse
from skyvern.config import settings
from skyvern.webeye.persistent_sessions_manager import PersistentSessionsManager
# Setup test app
app = FastAPI()
app.include_router(base_router)
client = TestClient(app)

@pytest.mark.asyncio
async def test_get_browser_session_by_id_success():
    """Test successful retrieval of a browser session"""
    browser_session_id = "test-session-123"
    org_id = "test-org-456"
    
    token_data = {
        "sub": org_id,  
        "exp": time.time() + 3600,  # expires in 1 hour
    }
    x_api_key = jwt.encode(token_data, settings.SECRET_KEY, algorithm="HS256")
    
    mock_org = AsyncMock()
    mock_org.organization_id = org_id
    mock_get_current_org = AsyncMock(return_value=mock_org)
    
    # Create a test browser session
    persistent_sessions_manager = PersistentSessionsManager()
    persistent_sessions_manager.sessions[org_id] = {
        browser_session_id: AsyncMock()  # Mock browser state
    }

    with patch('skyvern.forge.sdk.services.org_auth_service.get_current_org', new=mock_get_current_org), \
         patch('skyvern.forge.sdk.services.org_auth_service._get_current_org_cached', 
               new_callable=AsyncMock, return_value=mock_org), \
         patch('skyvern.forge.app.PERSISTENT_SESSIONS_MANAGER', 
               new=persistent_sessions_manager):
        
        response = client.get(
            f"/browser_sessions/{browser_session_id}", 
            headers={"X-API-Key": x_api_key}
        )
        
        assert response.status_code == 200
        response_data = response.json()
        assert response_data["session_id"] == browser_session_id
        assert response_data["organization_id"] == org_id
