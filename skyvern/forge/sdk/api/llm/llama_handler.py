from typing import Any, Optional
import aiohttp
import base64
import json
from skyvern.forge.sdk.models import Step

async def llama_handler(
    prompt: str,
    step: Step | None = None,
    screenshots: list[bytes] | None = None,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Handler for local Llama 3.2 model running on Ollama"""
    async with aiohttp.ClientSession() as session:
        payload = {
            "model": "llama3",  # Using llama3 model name
            "messages": [{"role": "user", "content": prompt}],
            "stream": False
        }
        
        if screenshots:
            # Convert screenshots to base64 for vision tasks
            payload["images"] = [base64.b64encode(img).decode('utf-8') for img in screenshots]
            
        async with session.post("http://localhost:11434/api/chat", json=payload) as response:
            result = await response.json()
            return {
                "choices": [{
                    "message": {
                        "content": result["message"]["content"]
                    }
                }]
            }