import asyncio
import logging
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

log = logging.getLogger(__name__)

load_dotenv()
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "google-key.json"

GEMINI_MODEL = "projects/andrewgpt-490605/locations/us-west1/endpoints/9200944198671400960"
TEMPERATURE = 1.5

gemini_client = genai.Client(
    vertexai=True,
    project="andrewgpt-490605",
    location="us-west1",
    http_options=types.HttpOptions(api_version="v1"),
)

SAFETY_SETTINGS = [
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,       threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,        threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
]


async def generate_reply(chat_session: list[types.Content], system_prompt: str) -> str | None:
    """Send chat history to Gemini and return the reply text, or None if blocked."""
    response = await asyncio.wait_for(
        gemini_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=chat_session,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=500,
                temperature=TEMPERATURE,
                stop_sequences=["[blanc2]:", "[Pistachio.ai]:"],
                safety_settings=SAFETY_SETTINGS,
            ),
        ),
        timeout=30.0,
    )

    if not response.candidates or not response.candidates[0].content.parts:
        log.debug("Response blocked: %s", response)
        return None

    return response.text.strip() if response.text else None