# Project Salus

A small companion robot meant to help people living with depression. It has a
face on a screen, a speaker, a microphone and buttons. It checks in with its
person through questions, quizzes and games, and a web app on the same computer
shows how they are doing, with the option to share a summary with their GP.
Everything runs locally: nothing is sent to the cloud.

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
- A local camera movement game, **Salus Says**, with standing and seated modes,
  five pose challenges, spoken cues, robot-face reactions and local result
  logging. Pose tracking runs in the browser; camera frames are not saved.

Not built yet:

- Live button presses on the page (the server already collects them at `/api/events`).
- Storage, the mood and questionnaire pages, and the summary for a GP.
- Speech in and out, and the local language model.
- The firmware itself: `firmware/` is empty in this repository so far.

The app has so far only run against the fake board and a simulated serial port.
It has not yet been tried with a real board.

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
| `GET /api/status`      | `connected`, `searching` or `released`, plus the port and a reason |
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
  button there.
- The app will not send a command that is not in the file.
- `screen` is the size of the robot's screen in pixels, as the firmware has it
  rotated.

Two limits come from the firmware that the Atech SDK generates, and the app
enforces both:

- **Plain keyboard characters only.** Anything else arrives garbled (`é`
  becomes `u00e9`).
- **At most 4096 characters per command** once encoded.

The file is a first draft. Its names assume these instances in
`firmware/project.yaml`: `face` (Big Screen), `spk` (speaker), `yes_btn` and
`no_btn` (buttons). If the firmware names them differently, change the file to
match.

## For the firmware

- `atech describe firmware/` prints the real action and event names for the
  modules. Actions written by hand in `code: loop:` do not appear there, so
  list those in `commands.json` yourself.
- The Big Screen (`st7789_tft`) is two ports wide: `ports: [1, 2]`, not
  `port: 1`.
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
