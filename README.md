PROJECT: WaifuLive.ai — agentic Live2D AI companion (desktop app)

What it is: An always-on-screen anime companion that lives on the user's desktop as a Live2D avatar, responds to text and voice, can watch the screen, and remembers things per-user across sessions. Brain is a fine-tuned Gemini 2.5 Flash endpoint on Vertex AI. Target market is lonely/isolated users; positioning leans companionship — NOT clinical therapy (see safety notes). Distribution is a downloadable app from a website, not app stores.

Architecture

Python backend (main.py)
- FastAPI HTTP API  :8000   — chat / memory / sleep
- Gemini Live relay :8765   — voice WebSocket bridge

Electron desktop app
- main.js        — auth, session storage, API proxy
- preload.js     — IPC bridge (secrets never reach renderer)
- src/
    - login.html    — sign in / create account
    - chat.html     — text chat + mic toggle + screenshot
    - model.html    — Live2D avatar 
    - settings.html — persona / voice / avatar picker
