// The page: shows whether the board is connected, sends it commands, and edits
// the face on its screen. Everything goes through the server's /api endpoints.

import { checkFace, drawFace, drawVolume, rgb565, rgb565ToCss } from "./face.js";

const POLL_MS = 1500; // how often to ask whether the board is still connected
const PREVIEW_WIDTH = 960; // about how many pixels wide to draw the preview, so small text stays sharp
const PORT_GAP = 30; // pixels between the sockets where cables leave the console's side
const SVG = "http://www.w3.org/2000/svg";

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
let lcd = { width: 160, height: 80 }; // the robot's screen; replaced by what commands.json says

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
  el("console").dataset.board = state; // the light on the console
  setText(el("status-text"), STATUS_TEXT[state]);
  setText(el("status-port"), state === "connected" ? status.port : "");

  el("fake-notice").hidden = status?.mode !== "fake";

  // The robot says when its microphone is muted (a push of its knob); the
  // robot's own screen does not show it, so this notice is the only sign.
  const muted = state === "connected" && status.mic_muted === true;
  setText(el("mic-note"), muted ? "Mic muted" : "");
  el("mic-note").hidden = !muted;

  followRobotVolume();

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
  scheduleWires(); // a module's cable shows when it is sending
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
  heading.append(node("h2", "", command.label), node("code", "command-key", command.key));
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

  // The cable to this module lights up while the module is being used.
  for (const type of ["mouseenter", "mouseleave", "focusin", "focusout"]) {
    form.addEventListener(type, scheduleWires);
  }

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
  const note = el("commands-note");
  let contract;
  try {
    contract = await api("/api/commands");
  } catch (error) {
    note.className = "problem stage-note";
    note.textContent = error.message;
    return;
  }
  lcd = contract.screen;
  // Plain commands each get a module built from the file. A command with its
  // own editor (the face, the volume slider, the lights) has a module of its
  // own in index.html, shown only if the file still lists that command.
  const listed = (key) => contract.commands.some((command) => command.key === key);
  const cards = contract.commands.filter((command) => !command.editor && command.show !== false).map(commandCard);
  el("command-list").replaceChildren(...cards);
  el("volume-module").hidden = !listed("spk_set_volume");
  el("lights-module").hidden = !listed("led_colour");
  note.className = "hint stage-note";
  note.textContent = "Each module wired to the console sends commands listed in commands.json.";
  for (const card of cards) sizes.observe(card);
  scheduleWires();
}

// ---- volume: a slider that looks like, and follows, the robot's volume screen ----

const VOLUME_SETTLE_MS = 2500; // after the slider is moved, ignore what the robot reports for this long

let volumeTouchedAt = 0; // when the slider was last moved here
let volumeSending = false;
let volumeWanted = null; // the newest position not yet sent

function showVolume() {
  const percent = Number(el("volume-slider").value);
  drawVolume(el("volume-screen"), lcd, percent);
  el("volume-slider").setAttribute("aria-valuetext", `${percent}%`);
}

// Send the slider's position. While one send is on its way further movement
// only updates what is wanted, so a drag sends a few positions, not hundreds,
// and always ends on the last one.
async function sendVolume() {
  if (volumeSending) return;
  const result = el("volume-result");
  volumeSending = true;
  result.dataset.busy = ""; // lights this module's cable
  scheduleWires();
  try {
    while (volumeWanted !== null) {
      const percent = volumeWanted;
      volumeWanted = null;
      await api("/api/command", { key: "spk_set_volume", value: percent / 100 });
      show(result, "ok", `Set to ${percent}%.`);
    }
  } catch (error) {
    volumeWanted = null;
    show(result, "error", error.message);
  }
  volumeSending = false;
  delete result.dataset.busy;
  scheduleWires();
}

el("volume-slider").addEventListener("input", () => {
  volumeTouchedAt = Date.now();
  volumeWanted = Number(el("volume-slider").value);
  showVolume();
  sendVolume();
});

// The robot reports its volume, so turning its knob moves the slider too. Not
// while the slider is being used here: the report could be a moment out of date.
function followRobotVolume() {
  const heard = status?.state === "connected" ? status.volume : null;
  if (typeof heard !== "number" || volumeSending || Date.now() - volumeTouchedAt < VOLUME_SETTLE_MS) return;
  const slider = el("volume-slider");
  if (Number(slider.value) === heard) return;
  slider.value = heard;
  show(el("volume-result"), "", "");
  showVolume();
}

// ---- lights: the robot's 3x3 grid ----

const EDGE = [0, 1, 2, 5, 8, 7, 6, 3]; // clockwise round the grid from the top left
const TAIL = [1, 90 / 255, 25 / 255]; // how bright the dot and the two lights behind it are
let gridRun = 0; // goes up each time the grid is repainted from scratch, which ends an animation

function ledColour() {
  const hex = el("led-colour").value;
  return { r: parseInt(hex.slice(1, 3), 16), g: parseInt(hex.slice(3, 5), 16), b: parseInt(hex.slice(5, 7), 16) };
}

// Colour the page's copy of the grid: `levels` is how bright each of the nine lights is, 0 to 1.
function paintGrid(levels) {
  const { r, g, b } = ledColour();
  [...el("led-grid").children].forEach((light, i) => {
    const level = levels[i];
    light.style.backgroundColor = `rgb(${Math.round(r * level)}, ${Math.round(g * level)}, ${Math.round(b * level)})`;
  });
}

function showGridColour() {
  gridRun++;
  paintGrid(new Array(9).fill(1));
}

// The robot's animation, played here at the same time: the same timings as
// animateGrid() in firmware/project.yaml.
function playGridAnimation() {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const run = ++gridRun;
  const started = performance.now();
  const frame = (now) => {
    if (run !== gridRun) return; // something else has repainted the grid
    const t = now - started;
    const levels = new Array(9).fill(0);
    if (t < 1920) {
      const head = Math.floor(t / 80) % 8;
      TAIL.forEach((level, behind) => (levels[EDGE[(head - behind + 8) % 8]] = level));
    } else if (t < 3120) {
      levels.fill(0.5 - 0.5 * Math.cos(((t - 1920) / 600) * 2 * Math.PI));
    } else {
      showGridColour();
      return;
    }
    paintGrid(levels);
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

el("led-colour").addEventListener("input", () => {
  show(el("led-result"), "", "");
  showGridColour();
});

el("lights-module").addEventListener("submit", (event) => {
  event.preventDefault();
  if (el("led-set").disabled) return;
  send(el("led-set"), el("led-result"), () => api("/api/command", { key: "led_colour", value: ledColour() }));
});

el("led-play").addEventListener("click", () => {
  send(el("led-play"), el("led-result"), async () => {
    await api("/api/command", { key: "led_animation", value: ledColour() });
    playGridAnimation();
  });
});

el("led-meter").addEventListener("click", () => {
  send(el("led-meter"), el("led-result"), () => api("/api/command", { key: "led_meter", value: null }));
});

for (const module of [el("volume-module"), el("lights-module")]) {
  for (const type of ["mouseenter", "mouseleave", "focusin", "focusout"]) {
    module.addEventListener(type, scheduleWires);
  }
}

// ---- the cables between the console and the modules ----

function svgNode(tag, attributes) {
  const made = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes)) made.setAttribute(name, value);
  return made;
}

// Draw one cable from the console to every module, wherever the layout has put
// them: out of the console's sides on a wide screen, and down from one module
// to the next on a narrow one.
function drawWires() {
  const stage = el("stage");
  const origin = stage.getBoundingClientRect();
  const box = (element) => {
    const r = element.getBoundingClientRect();
    const [left, right, top, bottom] = [r.left - origin.left, r.right - origin.left, r.top - origin.top, r.bottom - origin.top];
    return { left, right, top, bottom, midX: (left + right) / 2, midY: (top + bottom) / 2 };
  };
  const hub = box(el("console"));
  const modules = [...stage.querySelectorAll(".command")]
    .filter((card) => !card.hidden)
    .map((card) => ({ card, at: box(card) }))
    .sort((a, b) => a.at.top - b.at.top);

  const wires = [];
  const wire = (card, path, [x0, y0], [x1, y1]) => {
    const group = svgNode("g", { class: "wire" });
    const sending = card.querySelector("[data-busy]") !== null;
    group.classList.toggle("busy", sending);
    group.classList.toggle("live", sending || card.matches(":hover, :focus-within"));
    group.append(
      svgNode("path", { d: path }),
      svgNode("circle", { cx: x0, cy: y0, r: 4 }),
      svgNode("circle", { cx: x1, cy: y1, r: 4 }),
    );
    wires.push(group);
  };

  // Cables from one side of the console leave from sockets spaced around its
  // middle, and curve to the middle of each module's near edge.
  const fan = (group, hubX, edge) => {
    group.forEach(({ card, at }, i) => {
      const y0 = hub.midY + (i - (group.length - 1) / 2) * PORT_GAP;
      const [x1, y1] = [at[edge], at.midY];
      const bend = (x1 - hubX) / 2;
      wire(card, `M${hubX} ${y0} C${hubX + bend} ${y0} ${x1 - bend} ${y1} ${x1} ${y1}`, [hubX, y0], [x1, y1]);
    });
  };
  fan(modules.filter(({ at }) => at.right <= hub.left), hub.left, "right");
  fan(modules.filter(({ at }) => at.left >= hub.right), hub.right, "left");

  // Modules stacked under the console are chained: each hangs from the one above.
  let above = hub;
  for (const { card, at } of modules.filter(({ at }) => at.right > hub.left && at.left < hub.right)) {
    wire(card, `M${at.midX} ${above.bottom} V${at.top}`, [at.midX, above.bottom], [at.midX, at.top]);
    above = at;
  }

  el("wires").replaceChildren(...wires);
}

// Redraw once before the next repaint, however many things asked for it.
let wiresDue = false;
function scheduleWires() {
  if (wiresDue) return;
  wiresDue = true;
  requestAnimationFrame(() => {
    wiresDue = false;
    drawWires();
  });
}

// The cables follow the boxes, so they are redrawn whenever a box changes size:
// the window is resized, a message appears, a text box is dragged larger.
const sizes = new ResizeObserver(scheduleWires);
sizes.observe(el("stage"));
sizes.observe(el("console"));
sizes.observe(el("volume-module"));
sizes.observe(el("lights-module"));

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
  el("face-colour-value").textContent = `Colour sent as ${colour}, the screen's 16-bit version`;

  // Name the face if it is one of the ready-made ones.
  const preset = PRESETS.find((made) => made.text === el("face-text").value);
  setText(el("face-name"), preset ? preset.name : "Your own");

  const button = el("face-send");
  if (face.empty || face.problem) button.dataset.blocked = "";
  else delete button.dataset.blocked;
  refreshButtons();
}

// The rocker steps through the ready-made faces, round and round. From a face
// of your own it starts at the first (or, going back, the last).
function stepPreset(step) {
  const now = PRESETS.findIndex((made) => made.text === el("face-text").value);
  const next = now === -1 ? (step > 0 ? 0 : PRESETS.length - 1) : (now + step + PRESETS.length) % PRESETS.length;
  el("face-text").value = PRESETS[next].text;
  faceEdited();
}

el("face-prev").addEventListener("click", () => stepPreset(-1));
el("face-next").addEventListener("click", () => stepPreset(1));

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
showVolume();
showGridColour();
watchStatus();
loadCommands().then(() => {
  // Both pictures of the robot's screen take its real shape. A whole number of
  // canvas pixels for each screen pixel, so every font pixel is a clean square.
  const zoom = Math.max(1, Math.round(PREVIEW_WIDTH / lcd.width));
  for (const canvas of [el("preview"), el("volume-screen")]) {
    canvas.width = lcd.width * zoom;
    canvas.height = lcd.height * zoom;
  }
  refreshFace();
  showVolume();
});
