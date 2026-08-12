import asyncio
import logging
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

log = logging.getLogger(__name__)

load_dotenv()
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "google-key.json"

# Fine-tuned Vertex 2.5 chat brain (fallback) — set CHAT_MODEL to this to swap back:
#   projects/andrewgpt-490605/locations/us-west1/endpoints/9200944198671400960
CHAT_MODEL = os.getenv("CHAT_MODEL", "gemini-3.6-flash")
CHAT_TEMPERATURE = float(os.getenv("CHAT_TEMPERATURE", "1.0"))
GEMINI_MODEL = CHAT_MODEL  # back-compat alias; api/routes.py imports this name

# Gemini 3.x is not served in us-west1 — the stock chat brain requires location="global".
# The fine-tuned endpoint requires location="us-west1". Location is derived from CHAT_MODEL
# so the two can never drift out of sync; CHAT_MODEL_LOCATION is a manual override escape hatch.
is_finetune = CHAT_MODEL.startswith("projects/")
location = os.getenv("CHAT_MODEL_LOCATION") or ("us-west1" if is_finetune else "global")

gemini_client = genai.Client(
    vertexai=True,
    project="andrewgpt-490605",
    location=location,
    http_options=types.HttpOptions(api_version="v1"),
)

SAFETY_SETTINGS = [
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,       threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,        threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
]


async def generate_reply(chat_session: list[types.Content], system_prompt: str, grounding: bool = True) -> str | None:
    """Send chat history to Gemini and return the reply text, or None if blocked."""
    config_kwargs = dict(
        system_instruction=system_prompt,
        max_output_tokens=500,
        temperature=CHAT_TEMPERATURE,
        stop_sequences=["[blanc2]:", "[Pistachio.ai]:"],
        safety_settings=SAFETY_SETTINGS,
        tools=[types.Tool(google_search=types.GoogleSearch())] if grounding else None,
    )
    if not is_finetune:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL)

    response = await asyncio.wait_for(
        gemini_client.aio.models.generate_content(
            model=CHAT_MODEL,
            contents=chat_session,
            config=types.GenerateContentConfig(**config_kwargs),
        ),
        timeout=30.0,
    )

    if not response.candidates or not response.candidates[0].content.parts:
        log.debug("Response blocked: %s", response)
        return None

    return response.text.strip() if response.text else None