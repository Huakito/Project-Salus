# Project Salus

A small companion robot meant to help people living with depression. It has a
face on a screen, a speaker, a microphone and buttons. It checks in with its
person through questions, quizzes and games, and a web app on the same computer
shows how they are doing, with the option to share a summary with their GP.
Audio, speech recognition (Whisper) and the voice (Piper) all run locally. In
the voice-chat prototype the transcribed chat text is sent to Gemini to write
the replies. Camera tracking and session storage also run locally; camera
frames, video and audio never leave the computer.

This repository holds both halves of the project: the robot's firmware and the
web app.

## How the pieces fit

```
   ROBOT (Atech board, ESP32-S3)                COMPANION COMPUTER (laptop now, Pi later)
  ┌──────────────────────────────┐   USB    ┌────────────────────────────────────────────┐
  │ screen    draws the face     │ <------  │  Salus web app (Python, FastAPI)           │
  │ speaker   plays sound        │ commands │   ├ owns the USB connection to the board   │
  │ buttons   yes / no           │          │   ├ serves the page at 127.0.0.1:8000      │
  │ firmware  firmware/          │ ------>  │   └ later: speech, the model, storage      │
  └──────────────────────────────┘  events  └────────────────────────────────────────────┘
                                                              ▲
                                                              │ browser on the same computer
                                                         the control page
```

The board is the robot's body. It is too small to run speech recognition or a
language model, so the thinking happens on the computer beside it, and the two
talk over the USB cable: the computer sends **commands** ("show this face"),
the board sends back **events** ("the yes button was pressed").

| Folder / file   | What it is                                                         | Who works on it |
|-----------------|--------------------------------------------------------------------|-----------------|
| `firmware/`     | The Atech project (`project.yaml`) that is flashed onto the board   | firmware        |
| `webapp/`       | The web app: Python server in `salus/`, page in `static/`, tests    | web app         |
| `commands.json` | The contract between the two: every command and event, by name      | both            |

## What works today, and what does not yet

Built and tested:

- The web app connects to the board, reconnects by itself, and lets go of the
  USB port on request.
- Commands are sent one at a time and the board's answer is shown.
- A face editor with a live preview in the screen's own font.
- A fake board, so the app can be worked on without the robot.
- A local camera game, **Hand Signals**, with five close-range gestures sampled
  from a larger hand-movement library, calm timed transitions, spoken cues,
  hands-free voice check-ins, robot-face reactions and local result logging.
  Tracking runs in the browser; camera frames are not saved.
- A persistent **Victorian greenhouse** with six plant growth stages, forgiving
  daily care, interactive watering/misting/light/soil/turning activities, a
  care journal, admin time controls, and robot screen, melody and button input.
- Optional, structured OpenAI feedback after a saved run, with an automatic
  local fallback if no key is configured or the request fails.

Not built yet:

- Live button presses on the page (the server already collects them at `/api/events`).
- Storage, the mood and questionnaire pages, and the summary for a GP.
- Speech in and out runs as a prototype (see Voice chat); a first, unreviewed crisis screening runs before the model is asked; a fully local language model does not exist yet.

The face, melody, volume and stop commands have been run from the page against
the real robot over USB, and each was acknowledged by the board.

## Voice chat (prototype)

The robot streams its microphone to `firmware/server.py`, which transcribes it
with Whisper, asks Gemini for a short reply and speaks it with Piper. Only the
transcribed text goes to Gemini; the audio stays on this computer.

```bash
pip install -r firmware/requirements.txt
cp firmware/.env.example firmware/.env      # then put your GEMINI_API_KEY in it
python firmware/chat.py --download-voice    # Piper voice into firmware/models/
python firmware/chat.py "hello" --wav /tmp/reply.wav   # smoke test (calls Gemini)
python firmware/server.py                   # run the audio server
```

The audio server needs Python 3.11 or newer (the web app below still works on
3.10).

Crisis screening runs on every transcript before Gemini is called
(`firmware/crisis.py`). If the words match its list of self-harm phrases, the
robot says a fixed message pointing to a trusted person, a crisis line and the
emergency number; Gemini is not asked and the words are not kept in the chat
history. By default the message ends with Finland's crisis line (MIELI,
09 2525 0111, in Finnish, 24/7) and emergency number (112); set
`SALUS_CRISIS_HELPLINE` in `firmware/.env` to name the helpline for somewhere
else instead. The phrase list and the message are a first version and have not
been reviewed by a clinician.

## Run the web app

You need Python 3.10 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r webapp/requirements-dev.txt

cd webapp
python -m salus --fake                 # no robot needed
```

Open http://127.0.0.1:8000.

For optional generated feedback, copy `.env.example` to `.env` and put an
OpenAI API key in `OPENAI_API_KEY`. Keep `.env` private. Without a key, every
feature still works and the app uses a local response.

For the movement game's spoken guide, add `ELEVENLABS_API_KEY` to the same
`.env` file. `ELEVENLABS_VOICE_ID` selects the voice (George is the default)
and `ELEVENLABS_MODEL` defaults to the low-latency `eleven_flash_v2_5` model.
Changes to either setting in `.env` are picked up on the next spoken line; the
server does not need to be restarted.
If ElevenLabs is not configured or cannot be reached, the page automatically
uses the browser's built-in voice instead. The API key is never sent to the page.

| Command                                   | What it does                                        |
|-------------------------------------------|-----------------------------------------------------|
| `python -m salus --fake`                  | Uses a pretend board. The page says so in a banner. |
| `python -m salus`                         | Uses the real board on USB.                         |
| `python -m salus --reload`                | Restarts whenever the Python code changes.          |
| `python -m salus --board-port COM3`       | Uses that serial port instead of finding one.       |
| `python -m salus --http-port 8001`        | Serves the page on another port.                    |
| `python -m pytest`                        | Runs the tests (from `webapp/`).                    |

## How the web app works

One click on the page travels like this:

```
page (static/app.js)
  → POST /api/face or /api/command        salus/main.py      checks the request
  → is it in commands.json? value valid?  salus/contract.py  refuses what is not agreed
  → queue                                 salus/board.py     one command at a time
  → Atech SDK → USB → firmware                               board answers "ok" or why not
  ← the answer, shown next to the button
```

- **`salus/board.py`** is the centre. Only one program can hold the board's USB
  port, and the board loses commands if several arrive at once, so a single
  worker thread does everything that touches the board. It looks for the board
  every 2 seconds, sends the waiting commands in order, and notices when the
  cable is pulled.
- **`salus/contract.py`** reads `commands.json` afresh each time, so an edit
  shows up when the page is refreshed.
- **`salus/face.py`** holds the rules for a face, and `static/face.js` repeats
  them for the preview. The server checks again because it has the last word.
- **`salus/fake_board.py`** stands in for the cable and the firmware. It accepts
  the commands `commands.json` lists and refuses everything else with
  "unknown action", as the real firmware does. A command passing there says
  nothing about the real firmware.
- **`salus/main.py`** is the web server. It listens on `127.0.0.1` only, and
  refuses requests that come from other websites.

The API, for scripts and for the next features:

| Endpoint               | What it does                                                      |
|------------------------|-------------------------------------------------------------------|
| `GET /api/status`      | `connected`, `searching` or `released`, the port, a reason, `mic_muted` and `volume` |
| `POST /api/connect`    | Start looking for the board again                                 |
| `POST /api/disconnect` | Let go of the USB port                                            |
| `GET /api/commands`    | The contents of `commands.json`                                   |
| `POST /api/command`    | Send `{"key": "spk_stop", "value": ...}`                          |
| `POST /api/face`       | Send `{"text": "(^_^)", "colour": "#00ff00"}`                     |
| `GET /api/events`      | The latest events the board sent                                  |

Errors come back as `{"detail": "a sentence"}`: 422 for a request that is wrong,
409 when no board is connected, 502 when the board refused or did not answer.

## The contract: commands.json

Both sides edit this file. A command looks like this:

```json
{
  "key": "spk_set_volume",
  "label": "Set volume",
  "description": "Speaker volume from 0 (silent) to 1 (loudest).",
  "value": "number",
  "min": 0, "max": 1, "step": 0.05,
  "example": 0.4
}
```

- `key` is the action's name in the firmware. `value` is `none`, `text`,
  `number` or `json`.
- The page makes one control per command, so adding a command here adds a
  button there. Two optional fields change that: `"show": false` keeps a
  command off the page (it can still be sent through the API), and `"editor"`
  names a purpose-built module in `static/index.html` that replaces the plain
  control (`face`, `volume`, `lights`).
- The app will not send a command that is not in the file.
- `screen` is the size of the robot's screen in pixels, as the firmware has it
  rotated.

Two limits come from the firmware that the Atech SDK generates, and the app
enforces both:

- **Plain keyboard characters only.** Anything else arrives garbled (`é`
  becomes `u00e9`).
- **At most 4096 characters per command** once encoded.

The file is a first draft. Its names assume these instances in
`firmware/project.yaml`: `screen` (the 160x80 colour screen, which the `face`
action draws on), `spk` (speaker), `btn` (button), `knob` and `mic`. If the
firmware names them differently, change the file to match.

## For the firmware

- `atech describe firmware/` prints the real action and event names for the
  modules. Actions written by hand in `code: loop:` do not appear there, so
  list those in `commands.json` yourself.
- The robot's screen is the small one (`st7735_tft`, 160x80), so `screen` in
  `commands.json` is 160 by 80: a face can be at most 26 characters wide and
  10 lines tall. If the firmware moves to another screen, change `screen` to
  match and the page's preview and limits follow.
- The one hand-written action so far is **`face`**. Its value is JSON:
  `{"text": "(^_^)", "colour": 2016}`.
  - `text` is the ASCII art, with `\n` between lines.
  - `colour` is RGB565.
  - Draw it with the built-in font, where a character is 6x8 pixels times the
    text size. Use the biggest size at which the whole block fits, centre the
    block, and start every line at the same x.
  - Draw one line per `displayText` call: `print` sends every line after the
    first back to x = 0.
  - In `code: loop:`, `atechNum(atech_value, "colour", 0xFFFF)` reads the
    colour. `atechJsonString(atech_value, "text")` reads the text; that helper
    is in the generated firmware but not in the SDK's documented list.
  - Answer a missing text with `atechFail("text missing")`.

  The page's preview assumes exactly these rules.

- The firmware also sends two events of its own, on every change and every 2
  seconds: **`mic_muted`** (1 or 0) and **`volume`** (0 to 100). The robot's
  screen does not show that the microphone is muted; the page shows a red
  "Mic muted" notice instead. The page's volume slider follows `volume`, so it
  moves when the robot's knob is turned.
- Three more hand-written actions drive the 3x3 light grid: **`led_colour`**
  and **`led_animation`** take `{"r": 0, "g": 160, "b": 255}`, and
  **`led_meter`** gives the grid back to the sound level meter (the robot's
  button does the same). The animation's timings are repeated in
  `static/app.js` for the page's copy of the grid.

### Flashing while the app is running

Only one program can use the board's USB port. While the app is connected,
`atech upload`, `atech monitor` and `atech send` cannot reach the board.

1. Press **Disconnect** on the page (or **Stop looking**, if it shows that).
2. Flash: `atech upload firmware/`.
3. Press **Connect**.

## Before anyone relies on it

These are open, and need settling before the robot is used with patients:

- Mood should be scored with validated questionnaires (PHQ-9, PHQ-A for
  teenagers, WHO-5), not judged by a language model.
- What the robot does when someone mentions self-harm must be scripted, point
  to a person or a crisis line, and never be improvised by the model.
- A clinician should be involved early, and parental consent applies for
  younger users.
- Health data is specially protected by law. `.gitignore` already keeps
  databases, recordings and exports out of GitHub; encryption, consent and
  deletion still need designing.
- The app has no login. Keep it on `127.0.0.1` until it has one.
