---
title: "Surprise Launch Week - Day 2 - OpenRouter Support is Live in Skyvern"
description: null
excerpt: "TL;DR\n\nPlug any OpenRouter-hosted model into your automations with one env toggle. No code changes, no redeploys.\n\n# docker-compose.env\nENABLE_OPENROUTER=true\nLLM_KEY=OPENROUTER\nOPENROUTER_API_KEY=sk-…\nOPENROUTER_MODEL=qwen/qwen2.5-vl-32b-instruct\n\n\n\n\nWhy you might care\n\n * BYOM (Bring your own model) – swap models in minutes.\n * Full Skyvern feature set – async tasks, vision support, streaming, same API.\n\n\n\nUnder the hood\n\nLLMConfigRegistry.register_config(\n    \"OPENROUTER\",\n    LLMConfig(\n    "
slug: "surprise-launch-day-2-openrouter-support-is-live-in-skyvern"
publicationState: "published"
publishedAt: "2025-06-03T23:30:59.000Z"
updatedAt: "2025-06-05T04:56:27.000Z"
author: "suchintan"
tags: ["hash-kaitlyn"]
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/ed533501ba69e6a5aca9ae03e71232c944e5ee03e0258bf0dfdad1a4a4205321-v1-day-2.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "TL;DR\n\nPlug any OpenRouter-hosted model into your automations with one env toggle. No code changes, no redeploys.\n\n# docker-compose.env\nENABLE_OPENROUTER=true\nLLM_KEY=OPENROUTER\nOPENROUTER_API_KEY=sk-…\nOPENROUTER_MODEL=qwen/qwen2.5-vl-32b-instruct\n\n\n\n\nWhy you might care\n\n * BYOM (Bring your own model) – swap"
twitterLabel2: "Filed under"
twitterData2: ""
---
**TL;DR**

Plug any OpenRouter-hosted model into your automations with one env toggle. No code changes, no redeploys.



<pre><code># docker-compose.env
ENABLE_OPENROUTER=true
LLM_KEY=OPENROUTER
OPENROUTER_API_KEY=sk-…
OPENROUTER_MODEL=qwen/qwen2.5-vl-32b-instruct
</code></pre>



**Why you might care**

-   <strong>BYOM (Bring your own model)</strong> – swap models in minutes.
-   <strong>Full Skyvern feature set</strong> – async tasks, vision support, streaming, same API.

**Under the hood**



<pre><code>LLMConfigRegistry.register_config(
    "OPENROUTER",
    LLMConfig(
        "openrouter/{model_name}",
        ["OPENROUTER_API_KEY", "OPENROUTER_MODEL"],
        supports_vision=settings.LLM_CONFIG_SUPPORT_VISION,
        max_completion_tokens=settings.LLM_CONFIG_MAX_TOKENS,
        litellm_params=LiteLLMParams(
            api_key=settings.OPENROUTER_API_KEY,
            api_base=settings.OPENROUTER_API_BASE,
            model_info={"model_name": f"openrouter/{model_name}"},
        ),
    ),
)
</code></pre>



**Try it**



<pre><code>pip install skyvern
skyvern quickstart

# Make sure these are set in your .env
ENABLE_OPENROUTER=true
LLM_KEY=OPENROUTER
OPENROUTER_API_KEY=sk-…
OPENROUTER_MODEL=qwen/qwen2.5-vl-32b-instruct

# Go into Python and you're good to go!
from skyvern import Skyvern

skyvern = Skyvern()
task = await skyvern.run_task(prompt="Find the top post on hackernews today")
print(task)
</code></pre>



That’s it—new model, same workflow. Questions or feedback?
