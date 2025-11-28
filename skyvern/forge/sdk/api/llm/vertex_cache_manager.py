"""
Vertex AI Context Caching Manager.

This module implements the CORRECT caching pattern for Vertex AI using the /cachedContents API.
Unlike the Anthropic-style cache_control markers, Vertex AI requires:
1. Creating a cache object via POST to /cachedContents
2. Getting the cache resource name
3. Referencing that cache name in subsequent requests
"""

import json
from datetime import datetime
from typing import Any

import google.auth
import requests
import structlog
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from skyvern.config import settings

LOG = structlog.get_logger()


class VertexCacheManager:
    """
    Manages Vertex AI context caching using the correct /cachedContents API.

    This provides guaranteed cache hits for static content across requests,
    unlike implicit caching which requires exact prompt matches.
    """

    def __init__(self, project_id: str, location: str = "global", credentials_json: str | None = None):
        self.project_id = project_id
        self.location = location
        # Use regional endpoint for non-global locations, global endpoint for global
        if location == "global":
            self.api_endpoint = "aiplatform.googleapis.com"
        else:
            self.api_endpoint = f"{location}-aiplatform.googleapis.com"
        self._cache_registry: dict[str, dict[str, Any]] = {}  # Maps cache_key -> cache_data
        self._scopes = ["https://www.googleapis.com/auth/cloud-platform"]
        self._default_credentials = None
        self._service_account_credentials = None
        self._service_account_info: dict[str, Any] | None = None

        if credentials_json:
            try:
                self._service_account_info = json.loads(credentials_json)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Failed to parse Vertex credentials JSON, falling back to ADC", error=str(exc))

    def _get_access_token(self) -> str:
        """Get Google Cloud access token for API calls."""
        try:
            credentials: Credentials | None = None
            if self._service_account_info:
                if not self._service_account_credentials:
                    self._service_account_credentials = service_account.Credentials.from_service_account_info(
                        self._service_account_info,
                        scopes=self._scopes,
                    )
                credentials = self._service_account_credentials
            else:
                if not self._default_credentials:
                    self._default_credentials, _ = google.auth.default(scopes=self._scopes)
                credentials = self._default_credentials

            if credentials is None:
                raise RuntimeError("Unable to initialize Google credentials for Vertex cache manager")

            if not credentials.valid or credentials.expired:
                credentials.refresh(Request())

            return credentials.token
        except Exception as e:
            LOG.error("Failed to get access token", error=str(e))
            raise

    def create_cache(
        self,
        model_name: str,
        static_content: str,
        cache_key: str,
        ttl_seconds: int = 3600,
        system_instruction: str | None = None,
    ) -> dict[str, Any]:
        """
        Create a cache object using Vertex AI's /cachedContents API.

        Args:
            model_name: Full model path (e.g., "gemini-2.5-flash")
            static_content: The static content to cache
            cache_key: Unique key to identify this cache (e.g., f"task_{task_id}")
            ttl_seconds: Time to live in seconds (default: 1 hour)
            system_instruction: Optional system instruction to include

        Returns:
            Cache data with 'name', 'expireTime', etc.
        """
        # Check if cache already exists for this key
        if cache_key in self._cache_registry:
            cache_data = self._cache_registry[cache_key]
            # Check if still valid
            expire_time = datetime.fromisoformat(cache_data["expireTime"].replace("Z", "+00:00"))
            if expire_time > datetime.now(expire_time.tzinfo):
                LOG.info("Reusing existing cache", cache_key=cache_key, cache_name=cache_data["name"])
                return cache_data
            else:
                LOG.info("Cache expired, creating new one", cache_key=cache_key)
                # Clean up expired cache
                try:
                    self.delete_cache(cache_key)
                except Exception:
                    pass  # Best effort cleanup

        url = f"https://{self.api_endpoint}/v1/projects/{self.project_id}/locations/{self.location}/cachedContents"

        # Build the model path
        full_model_path = f"projects/{self.project_id}/locations/{self.location}/publishers/google/models/{model_name}"

        # Create payload
        payload: dict[str, Any] = {
            "model": full_model_path,
            "contents": [{"role": "user", "parts": [{"text": static_content}]}],
            "ttl": f"{ttl_seconds}s",
        }

        # Add system instruction if provided
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        headers = {"Authorization": f"Bearer {self._get_access_token()}", "Content-Type": "application/json"}

        LOG.info(
            "Creating Vertex AI cache object",
            cache_key=cache_key,
            model=model_name,
            content_size=len(static_content),
            ttl_seconds=ttl_seconds,
        )

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)

            if response.status_code != 200:
                LOG.error(
                    "Failed to create cache",
                    cache_key=cache_key,
                    status_code=response.status_code,
                    response=response.text,
                )
                raise Exception(f"Cache creation failed: {response.text}")

            cache_data = response.json()
            cache_name = cache_data["name"]

            # Store in registry
            self._cache_registry[cache_key] = cache_data

            LOG.info(
                "Cache created successfully",
                cache_key=cache_key,
                cache_name=cache_name,
                expires_at=cache_data.get("expireTime"),
            )

            return cache_data

        except requests.exceptions.Timeout:
            LOG.error("Cache creation timed out", cache_key=cache_key)
            raise
        except Exception as e:
            LOG.error("Cache creation failed", cache_key=cache_key, error=str(e))
            raise

    def delete_cache(self, cache_key: str) -> bool:
        """Delete a cache object."""
        cache_data = self._cache_registry.get(cache_key)
        if not cache_data:
            LOG.warning("Cache not found in registry", cache_key=cache_key)
            return False

        cache_name = cache_data["name"]
        url = f"https://{self.api_endpoint}/v1/{cache_name}"

        headers = {
            "Authorization": f"Bearer {self._get_access_token()}",
        }

        LOG.info("Deleting cache", cache_key=cache_key, cache_name=cache_name)

        try:
            response = requests.delete(url, headers=headers, timeout=10)

            if response.status_code in (200, 204):
                # Remove from registry
                del self._cache_registry[cache_key]
                LOG.info("Cache deleted successfully", cache_key=cache_key)
                return True
            else:
                LOG.warning(
                    "Failed to delete cache",
                    cache_key=cache_key,
                    status_code=response.status_code,
                    response=response.text,
                )
                return False
        except Exception as e:
            LOG.error("Cache deletion failed", cache_key=cache_key, error=str(e))
            return False


# Global cache manager instance
_global_cache_manager: VertexCacheManager | None = None


def get_cache_manager() -> VertexCacheManager:
    """Get or create the global cache manager instance."""
    global _global_cache_manager

    if _global_cache_manager is None:
        project_id = settings.VERTEX_PROJECT_ID or "skyvern-production"
        # Default to "global" to match the model configs in cloud/__init__.py
        # Can be overridden with VERTEX_LOCATION (e.g., "us-central1" for better caching)
        location = settings.VERTEX_LOCATION or "global"
        _global_cache_manager = VertexCacheManager(
            project_id=project_id,
            location=location,
            credentials_json=settings.VERTEX_CREDENTIALS,
        )
        LOG.info("Created global cache manager", project_id=project_id, location=location)

    return _global_cache_manager
