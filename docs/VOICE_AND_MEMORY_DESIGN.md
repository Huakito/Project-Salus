# Voice and memory design

## What is implemented

- Browser speech recognition listens without keyboard input; browser speech synthesis speaks the reply.
- Questions come from `webapp/salus/conversation_library.json`, with recently used questions avoided.
- Every answer and reply is written immediately to local SQLite (`webapp/data/salus.db`).
- `webapp/data/history.md` is an inspectable summary plus recent answers. A bounded copy is provided to each AI reply.
- The model may add one explicit, durable preference or fact to memory. It is told not to infer diagnoses, traits or emotions.
- App settings are stored in the same database. The schema is intentionally general enough for robot colour and later visual choices.

## Best route to more natural live voice

The current speech-recognition → text model → speech-synthesis chain takes turns and can feel mechanical. The strongest next prototype is OpenAI Realtime over WebRTC: it streams audio in both directions, supports voice activity detection, and can handle interruption when the user starts speaking. The server should mint a short-lived client token so the API key never enters browser code.

Keep the local question library even with Realtime. It gives the voice a bounded set of useful openings, lets the app choose from prior answers, and provides a predictable fallback when the network or model is unavailable.

Suggested progression:

1. Current browser speech stack: free and simple, but browser-dependent and turn-based.
2. Realtime WebRTC with semantic VAD: lower latency, interruptions, and a more conversational rhythm.
3. Add explicit user controls to view, correct, export and clear local memory before treating the prototype as anything beyond a school demo.

## Data boundary

Camera frames and microphone audio are not written to the database. Transcripts, model replies, run results, settings and deliberately selected memory notes are local. A bounded history excerpt is sent to the model only when an AI reply is requested.
