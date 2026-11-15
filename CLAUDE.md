# Project Salus

Local-first companion robot for people living with depression: an ESP32-S3 board (`firmware/`) connected over USB & WiFi to a Python FastAPI control/audio companion on the computer (`webapp/`, `firmware/server.py`). Audio and speech recognition run on this computer; the voice chat sends the transcribed text (never audio) to Gemini to write replies, and, only when `SALUS_TTS=elevenlabs`, the reply text to ElevenLabs to speak it (Piper, on this computer, otherwise).

## Repository Map

- `commands.json` — Shared command/event contract between firmware and webapp. Plain ASCII only; <= 4096 bytes once JSON-encoded.
- `webapp/salus/main.py` — FastAPI server bound strictly to `127.0.0.1:8000` (local-only, rejects cross-origin browser requests).
- `webapp/salus/board.py` — Single worker thread owning the USB serial port (2s reconnect poll, ordered command queue). Only one process may hold the port.
- `webapp/salus/contract.py` — Reloads `commands.json` dynamically and validates commands/bounds.
- `webapp/salus/face.py` — Server-side face text/colour validation & pixel layout rules. Keep in sync with `webapp/static/face.js`.
- `webapp/salus/fake_board.py` — Simulated board for testing without hardware (`--fake`).
- `webapp/salus/wifi.py` — The Home tab's WiFi module: checks the network name/password/laptop address and finds this computer's address (and its WiFi name where the system allows; recent macOS hides it). `POST /api/wifi` sends them to the board as `wifi_set`; the password is never stored or logged on this computer.
- `webapp/static/` — Vanilla HTML/CSS/JS control panel (`app.js`, `face.js`, `font5x7.js`, `index.html`, `style.css`, `fonts/` holding JetBrains Mono so the page works offline). The page is a console (face editor) wired to modules: melody, volume slider, lights, and the Turnstile knob game.
- `webapp/tests/` — Pytest suite (`test_api.py`, `test_board.py`, `test_contract.py`, `test_face.py`, `test_serial.py`, `test_wifi.py`, plus `test_chat.py`, `test_chat_server.py` and `test_crisis.py` for `firmware/*.py`; the chat ones skip without numpy).
- `firmware/project.yaml` — Atech / PlatformIO ESP32-S3 firmware definition for the 14-port board. Modules: `screen` (`st7735_tft`, 160x80, `ports: [10, 11]`; a face is at most 26 characters by 10 lines), `spk` (speaker, `[5, 6]`), `mic` (`[3, 4]`), `knob` (rotary encoder, `[1, 2]`: turn = volume, push = mute the mic), `led` (3x3 NeoPixel grid, port 7: sound level meter unless the web app sets a colour), `btn` (port 13: meter on/off).
  - Hand-written actions in `code: loop:` (not shown by `atech describe`): `face`, `led_colour`, `led_animation`, `led_meter`, `wifi_set` (saves network, password and laptop address in the board's flash with Preferences, then rejoins). Built-in ones the page uses: `spk_play_rtttl`, `spk_set_volume`, `spk_stop`.
  - Events: `knob`, `knob_btn`, `btn`, `mic_level` (built in) and `mic_muted`, `volume`, `wifi` (printed by the loop on change and every 2 s; `salus/board.py` puts them in `/api/status`). `wifi` never carries the password.
  - The WiFi name, password and laptop address at the top of `code: globals:` are only the fallback for a board never set up from the Home tab's WiFi module. They get committed with the file, so leave them empty (`""`) or check before pushing.
- `firmware/server.py` — TCP audio server on port `9000` (16 kHz 16-bit mono PCM, 30 ms chunks) + FastAPI utterances UI on `127.0.0.1:8001`. Each utterance: Whisper -> crisis screening -> Gemini -> Piper.
- `firmware/chat.py` — The voice pipeline's parts (Whisper transcriber, Gemini chatter and its system prompt, Piper speaker, and the optional ElevenLabs speaker with Piper as its backup). Settings come from `firmware/.env` (see `.env.example`); the ElevenLabs key may also sit in the project's `.env`, which the web app's Movement voice (`webapp/salus/voice.py`) reads. `python firmware/chat.py --find-voice NAME` looks up an ElevenLabs voice ID. The crisis message and the apology are always spoken by Piper.
- `firmware/crisis.py` — Deterministic crisis screening: a phrase list and the scripted message. `server.py` calls it on every transcript before Gemini; a match is answered with the scripted message, Gemini is not called, and the turn is kept out of the chat history. Not yet reviewed by a clinician.

## Commands

Run from repo root (`.venv` holds all Python + `atech` + `pytest` dependencies):

- **Full test suite (fast, ~4.5s):** `cd webapp && ../.venv/bin/python -m pytest`
- **Single test file:** `cd webapp && ../.venv/bin/python -m pytest tests/test_api.py`
- **Run webapp (no hardware):** `cd webapp && ../.venv/bin/python -m salus --fake --reload`
- **Run webapp (USB robot):** `cd webapp && ../.venv/bin/python -m salus`
- **Run audio WiFi server:** `.venv/bin/python firmware/server.py`
- **Inspect firmware actions/events:** `.venv/bin/atech describe firmware/`
- **Flash firmware (disconnect in UI first):** `.venv/bin/atech upload firmware/`

## Critical Invariants & Safety Rules

1. **Patient privacy & git hygiene:** Never read, log, commit, or expose `*.db*`, `recordings/`, `*.wav/*.mp3/*.pcm`, `webapp/data/`, `exports/`, or `.env*`. Keep processing local (`127.0.0.1`); the exceptions are the transcribed chat text sent to Gemini by `firmware/chat.py`, and text to be spoken sent to ElevenLabs when it is configured (`firmware/chat.py` with `SALUS_TTS=elevenlabs`; `webapp/salus/voice.py` for the Movement page). Nothing else may leave this computer. Never send audio, and never let the crisis message depend on a network service.
2. **Clinical safety:** Never use an LLM to score mood or improvise self-harm responses. Mood questionnaires must use deterministic validated scoring (PHQ-9 / PHQ-A / WHO-5); any self-harm/crisis signal must trigger scripted escalation to a person or crisis line. `firmware/crisis.py` does this for the voice chat: never call the language model on a transcript that has not been through `crisis.is_crisis()` first, and never weaken its phrase list without adding tests.
3. **USB serial ownership:** Never bypass `salus/board.py`'s single-worker lock or spawn competing serial readers.
4. **Audio/UI lag-free hot paths:** In `firmware/server.py`, never block the asyncio loop or port-9000 frame reader with synchronous disk/model calls—offload heavy inference/TTS/file I/O cleanly. Keep `webapp/static/` vanilla JS with zero unnecessary polling/DOM thrash.
5. **Contract parity:** Any change to `commands.json` or face rendering rules must update both `webapp/salus/face.py` and `webapp/static/face.js` plus `webapp/tests/`. `screen` in `commands.json` must match the firmware's screen (160x80). The volume screen and the light animation are drawn twice, in `firmware/project.yaml` and in `webapp/static/` (`face.js` `drawVolume`, `app.js` `playGridAnimation`): change both.

## Working Rules

- For non-trivial features or multi-file changes, run `/interview-plan` (or `/feature-dev`) before editing code.
- For single-layer or medium features, delegate disjoint slices to `@implementer` subagents.
- For cross-layer features (e.g., `webapp/salus/` + `webapp/static/` + `firmware/`), spawn an **Agent Team** with plan approval required per teammate and strictly disjoint file ownership (never let two teammates edit the same file).
- Before declaring any task done: run `cd webapp && ../.venv/bin/python -m pytest` and pass `@scrutinizer` on `git diff`.
