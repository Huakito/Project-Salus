# Project Salus

Local-first companion robot for people living with depression: an ESP32-S3 board (`firmware/`) connected over USB & WiFi to a Python FastAPI control/audio companion on the computer (`webapp/`, `firmware/server.py`). Nothing runs in or leaves to the cloud.

## Repository Map

- `commands.json` — Shared command/event contract between firmware and webapp. Plain ASCII only; <= 4096 bytes once JSON-encoded.
- `webapp/salus/main.py` — FastAPI server bound strictly to `127.0.0.1:8000` (local-only, rejects cross-origin browser requests).
- `webapp/salus/board.py` — Single worker thread owning the USB serial port (2s reconnect poll, ordered command queue). Only one process may hold the port.
- `webapp/salus/contract.py` — Reloads `commands.json` dynamically and validates commands/bounds.
- `webapp/salus/face.py` — Server-side face text/colour validation & pixel layout rules. Keep in sync with `webapp/static/face.js`.
- `webapp/salus/fake_board.py` — Simulated board for testing without hardware (`--fake`).
- `webapp/static/` — Vanilla HTML/CSS/JS control panel (`app.js`, `face.js`, `font5x7.js`, `index.html`, `style.css`).
- `webapp/tests/` — Pytest suite (`test_api.py`, `test_board.py`, `test_contract.py`, `test_face.py`, `test_serial.py`).
- `firmware/project.yaml` — Atech / PlatformIO ESP32-S3 firmware definition (`st7789_tft` screen on `ports: [1, 2]`, `spk`, `yes_btn`, `no_btn`).
- `firmware/server.py` — TCP audio server on port `9000` (16 kHz 16-bit mono PCM, 30 ms chunks) + FastAPI utterances UI on `127.0.0.1:8001`.

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

1. **Patient privacy & git hygiene:** Never read, log, commit, or expose `*.db*`, `recordings/`, `*.wav/*.mp3/*.pcm`, `webapp/data/`, `exports/`, or `.env*`. Keep all processing 100% local (`127.0.0.1`).
2. **Clinical safety:** Never use an LLM to score mood or improvise self-harm responses. Mood questionnaires must use deterministic validated scoring (PHQ-9 / PHQ-A / WHO-5); any self-harm/crisis signal must trigger scripted escalation to a person or crisis line.
3. **USB serial ownership:** Never bypass `salus/board.py`'s single-worker lock or spawn competing serial readers.
4. **Audio/UI lag-free hot paths:** In `firmware/server.py`, never block the asyncio loop or port-9000 frame reader with synchronous disk/model calls—offload heavy inference/TTS/file I/O cleanly. Keep `webapp/static/` vanilla JS with zero unnecessary polling/DOM thrash.
5. **Contract parity:** Any change to `commands.json` or face rendering rules must update both `webapp/salus/face.py` and `webapp/static/face.js` plus `webapp/tests/`.

## Working Rules

- For non-trivial features or multi-file changes, run `/interview-plan` (or `/feature-dev`) before editing code.
- For single-layer or medium features, delegate disjoint slices to `@implementer` subagents.
- For cross-layer features (e.g., `webapp/salus/` + `webapp/static/` + `firmware/`), spawn an **Agent Team** with plan approval required per teammate and strictly disjoint file ownership (never let two teammates edit the same file).
- Before declaring any task done: run `cd webapp && ../.venv/bin/python -m pytest` and pass `@scrutinizer` on `git diff`.
