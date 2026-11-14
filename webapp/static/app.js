// The page: shows whether the board is connected, sends it commands, and edits
// the face on its screen. Everything goes through the server's /api endpoints.

import { checkFace, drawFace, rgb565, rgb565ToCss } from "./face.js";

const POLL_MS = 1500; // how often to ask whether the board is still connected
const ZOOM = 3; // preview pixels per screen pixel, so small text stays sharp

const PRESETS = [
  { name: "Happy", text: "(^_^)" },
  { name: "Talking", text: "(^o^)" },
  { name: "Listening", text: "(o_o)" },
  { name: "Wink", text: "(^_~)" },
  { name: "Sleepy", text: "(-_-) zZ" },
  { name: "Sad", text: "(T_T)" },
  { name: "Cat", text: " /\\_/\\\n( o.o )\n > ^ <" },
  { name: "Robot", text: " .-----.\n| o   o |\n|   ^   |\n|  \\_/  |\n '-----'" },
];

const STATUS_TEXT = {
  connected: "Connected",
  searching: "Looking for the board…",
  released: "Disconnected",
  offline: "App not running",
};

const LINK_BUTTON_TEXT = {
  connected: "Disconnect",
  searching: "Stop looking",
  released: "Connect",
};

let status = null; // the last answer from /api/status; null while the app cannot be reached
let lcd = { width: 320, height: 240 }; // the robot's screen; replaced by what commands.json says

const el = (id) => document.getElementById(id);

function node(tag, className = "", text = "") {
  const made = document.createElement(tag);
  made.className = className;
  made.textContent = text;
  return made;
}

// Only touch the page when the words change, so screen readers are not told
// the same status again every time it is checked.
function setText(target, text) {
  if (target.textContent !== text) target.textContent = text;
}

// Ask the server something. Resolves with its JSON answer; rejects with an
// Error whose message is a sentence that can be shown as it is.
async function api(path, body) {
  const options =
    body === undefined
      ? {}
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("Can't reach the Salus app. Is it still running?");
  }
  const answer = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(answer?.detail ?? `The app answered with an error (${response.status}).`);
  }
  return answer;
}

// ---- connection ----

function showStatus() {
  const state = status ? status.state : "offline";
  el("status").dataset.state = state;
  setText(el("status-text"), STATUS_TEXT[state]);
  setText(el("status-port"), state === "connected" ? status.port : "");

  el("fake-notice").hidden = status?.mode !== "fake";

  let trouble = "";
  if (!status) {
    trouble = "This page can't reach the Salus app. Start it again and the page will reconnect by itself.";
  } else if (state !== "connected") {
    trouble = status.detail;
  }
  setText(el("notice"), trouble);
  el("notice").dataset.state = state;
  el("notice").hidden = !trouble;

  // One button: let go of the board (or stop looking for it), or take it back.
  el("link-button").hidden = !status;
  setText(el("link-button"), LINK_BUTTON_TEXT[state] ?? "");
  refreshButtons();
}

async function watchStatus() {
  for (;;) {
    try {
      status = await api("/api/status");
    } catch {
      status = null;
    }
    showStatus();
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}

el("link-button").addEventListener("click", async () => {
  const button = el("link-button");
  button.disabled = true;
  try {
    status = await api(status?.state === "released" ? "/api/connect" : "/api/disconnect", {});
  } catch {
    // The next status check will show what really happened.
  }
  button.disabled = false;
  showStatus();
});

// A button that sends to the board only works while a board is connected, its
// last request has finished, and what it would send is valid.
function refreshButtons() {
  const connected = status?.state === "connected";
  for (const button of document.querySelectorAll("[data-needs-board]")) {
    button.disabled = !connected || "busy" in button.dataset || "blocked" in button.dataset;
  }
}

// ---- sending ----

function show(result, kind, text) {
  result.dataset.kind = kind;
  result.textContent = text;
}

// Make one request on behalf of `button`, and say how it went in `result`.
async function send(button, result, request) {
  button.dataset.busy = "";
  refreshButtons();
  show(result, "busy", "Sending…");
  try {
    await request();
    // An acknowledgement means the firmware ran the command, not that anyone saw
    // or heard the result: a screen that is not plugged in acknowledges too.
    show(result, "ok", status?.mode === "fake" ? "Acknowledged by the fake board." : "Acknowledged by the board.");
  } catch (error) {
    show(result, "error", error.message);
  }
  delete button.dataset.busy;
  refreshButtons();
}

// ---- commands, built from commands.json ----

// The input for a command's value, and how to read the value back out of it.
// A command that takes no value gets no input.
function valueField(command) {
  if (command.value === "text") {
    const input = node("input");
    input.type = "text";
    input.spellcheck = false;
    input.value = command.example ?? "";
    return { input, read: () => input.value };
  }
  if (command.value === "number") {
    const input = node("input");
    input.type = "number";
    if (command.min !== undefined) input.min = command.min;
    if (command.max !== undefined) input.max = command.max;
    input.step = command.step ?? "any";
    input.value = command.example ?? "";
    const read = () => {
      if (Number.isNaN(input.valueAsNumber)) throw new Error("Type a number first.");
      return input.valueAsNumber;
    };
    return { input, read };
  }
  if (command.value === "json") {
    const input = node("textarea");
    input.rows = 3;
    input.spellcheck = false;
    input.value = command.example === undefined ? "" : JSON.stringify(command.example, null, 2);
    const read = () => {
      try {
        return JSON.parse(input.value);
      } catch {
        throw new Error("That is not valid JSON.");
      }
    };
    return { input, read };
  }
  return null;
}

function commandCard(command) {
  const form = node("form", "command");
  const heading = node("div", "command-heading");
  heading.append(node("h3", "", command.label), node("code", "command-key", command.key));
  form.append(heading);
  if (command.description) form.append(node("p", "hint", command.description));

  const field = valueField(command);
  const button = node("button", "button", "Send");
  button.type = "submit";
  button.dataset.needsBoard = "";
  const row = node("div", "row");
  if (field) {
    field.input.setAttribute("aria-label", `Value for ${command.label}`);
    row.append(field.input);
  }
  row.append(button);
  const result = node("p", "result");
  result.setAttribute("role", "status");
  form.append(row, result);

  // What the last send reported no longer describes what is in the box.
  field?.input.addEventListener("input", () => show(result, "", ""));

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (button.disabled) return;
    let value = null;
    try {
      if (field) value = field.read();
    } catch (error) {
      show(result, "error", error.message);
      return;
    }
    send(button, result, () => api("/api/command", { key: command.key, value }));
  });
  return form;
}

async function loadCommands() {
  const list = el("commands");
  let contract;
  try {
    contract = await api("/api/commands");
  } catch (error) {
    list.replaceChildren(node("p", "problem", error.message));
    return;
  }
  lcd = contract.screen;
  // A command with its own editor (the face) is not repeated in this list.
  const cards = contract.commands.filter((command) => !command.editor).map(commandCard);
  list.replaceChildren(...cards);
  if (!cards.length) list.append(node("p", "hint", "commands.json lists no commands yet."));
}

// ---- the face editor ----

function refreshFace() {
  const face = checkFace(el("face-text").value, lcd);
  const colour = rgb565(el("face-colour").value);
  drawFace(el("preview"), face, lcd, rgb565ToCss(colour));

  el("face-problem").textContent = face.problem ?? "";
  el("face-problem").hidden = !face.problem;

  let size = "";
  if (face.empty) {
    size = "Type a face below to see it here.";
  } else if (!face.problem) {
    const wide = `${face.cols} ${face.cols === 1 ? "character" : "characters"} wide`;
    const tall = `${face.rows} ${face.rows === 1 ? "line" : "lines"}`;
    size = `${wide}, ${tall}, drawn at text size ${face.size}`;
  }
  el("face-size").textContent = size;
  el("face-colour-value").textContent = `Sent as ${colour}, the screen's 16-bit version of this colour`;

  const button = el("face-send");
  if (face.empty || face.problem) button.dataset.blocked = "";
  else delete button.dataset.blocked;
  refreshButtons();
}

for (const preset of PRESETS) {
  // A one-line face is its own label; a taller one goes by its name.
  const chip = node("button", "chip", preset.text.includes("\n") ? preset.name : preset.text);
  chip.type = "button";
  chip.title = preset.name;
  chip.setAttribute("aria-label", `${preset.name} face`);
  chip.addEventListener("click", () => {
    el("face-text").value = preset.text;
    faceEdited();
  });
  el("presets").append(chip);
}

// After an edit the preview shows a face the robot has not been sent yet, so
// "acknowledged" would no longer be true of what is on the page.
function faceEdited() {
  show(el("face-result"), "", "");
  refreshFace();
}

el("face-text").addEventListener("input", faceEdited);
el("face-colour").addEventListener("input", faceEdited);

el("face-text").addEventListener("keydown", (event) => {
  // Enter makes a new line of art; Ctrl+Enter (Cmd+Enter on a Mac) sends it.
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) el("face-form").requestSubmit();
});

el("face-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = el("face-send");
  if (button.disabled) return;
  send(button, el("face-result"), () =>
    api("/api/face", { text: el("face-text").value, colour: el("face-colour").value }),
  );
});

// ---- start ----

refreshFace();
watchStatus();
loadCommands().then(() => {
  const preview = el("preview");
  preview.width = lcd.width * ZOOM;
  preview.height = lcd.height * ZOOM;
  refreshFace();
});
