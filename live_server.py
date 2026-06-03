"""
live_server.py — Gemini Live API relay server
Bridges the Electron/Flutter client (WebSocket) <-> Gemini Live API

Run with:
    python live_server.py

Listens on ws://localhost:8765
"""

import asyncio
import json
import os
import traceback

import websockets
from google import genai
from google.genai import types

from core.prompts import build_dynamic_prompt
from core.memory import init_memory

PROJECT_ID  = 'andrewgpt-490605'
LOCATION    = 'us-west1'
MODEL       = 'gemini-live-2.5-flash-native-audio'
SAMPLE_RATE = 16000   # Hz — what the client mic sends

os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'google-key.json'

client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)


def make_session_config():
    return types.LiveConnectConfig(
        response_modalities=['AUDIO'],
        system_instruction=build_dynamic_prompt(),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name='Aoede')
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


async def handle_client(ws):
    print(f'[Live] Client connected: {ws.remote_address}')

    try:
        async with client.aio.live.connect(model=MODEL, config=make_session_config()) as session:
            print('[Live] Gemini session opened.')

            async def recv_from_client():
                async for msg in ws:
                    if isinstance(msg, bytes):
                        await session.send_realtime_input(
                            audio=types.Blob(data=msg, mime_type=f'audio/pcm;rate={SAMPLE_RATE}')
                        )
                    elif isinstance(msg, str):
                        data = json.loads(msg)
                        if data.get('type') == 'text':
                            await session.send_client_content(
                                turns=[types.Content(role='user', parts=[types.Part(text=data['text'])])],
                                turn_complete=True,
                            )

            async def recv_from_gemini():
                async for response in session.receive():
                    if response.data:
                        await ws.send(response.data)
                    if response.server_content:
                        sc = response.server_content
                        if sc.output_transcription and sc.output_transcription.text:
                            await ws.send(json.dumps({
                                'type': 'transcript',
                                'text': sc.output_transcription.text,
                            }))
                        if sc.input_transcription and sc.input_transcription.text:
                            await ws.send(json.dumps({
                                'type': 'input_transcript',
                                'text': sc.input_transcription.text,
                            }))

            await asyncio.gather(recv_from_client(), recv_from_gemini())

    except websockets.exceptions.ConnectionClosedOK:
        print('[Live] Client disconnected cleanly.')
    except Exception as e:
        print(f'[Live] Error: {e}')
        traceback.print_exc()


async def main():
    init_memory()
    print('[Live] Starting relay server on ws://localhost:8765')
    async with websockets.serve(handle_client, 'localhost', 8765):
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
