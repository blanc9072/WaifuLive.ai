WaifuLive.ai — an AI desktop companion app built around a fine-tuned Gemini model with a Live2D avatar.

What it is
An always-on-screen anime companion ("Pistachio / Tachi") that lives in the corner of your desktop as a Live2D model, responds to text and voice, watches your screen, and remembers things about you across sessions — per user, stored in a real database.

Architecture

Python backend (main.py)
├── FastAPI HTTP API  :8000   — chat / memory / sleep
└── Gemini Live relay :8765   — voice WebSocket bridge

Electron desktop app
├── main.js           — auth, session storage, API proxy
├── preload.js        — IPC bridge (secrets never reach renderer)
└── src/
    ├── login.html    — sign in / create account
    ├── chat.html     — text chat + mic toggle + screenshot
    ├── model.html    — Live2D avatar 
    └── settings.html — persona / voice / avatar picker

