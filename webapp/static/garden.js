const el = (id) => document.getElementById(id);
let state = null;
let selectedId = null;
let seenEvents = 0;
let careStarted = 0;
let careCleanup = () => {};
const EARLY_STAGE_SPRITES = [0, 8, 13];
const GROWTH_SHEET_POSITIONS = ["0%", "50%", "100%"];
const POTTED_PLANT_COUNT = 31;
const PLANT_SPOTS = [
  [22.5,15.5], [32,15.5], [41.5,15.5], [22.5,28.5], [32,28.5], [41.5,28.5],
  [67.5,15.5], [77,15.5], [86.5,15.5], [67.5,28.5], [77,28.5], [86.5,28.5],
  [22.5,71.5], [32,71.5], [41.5,71.5], [22.5,84.5], [32,84.5], [41.5,84.5],
  [67.5,71.5], [77,71.5], [86.5,71.5], [67.5,84.5], [77,84.5], [86.5,84.5],
];
const player = {x:50, y:82, direction:0, frame:0};
const robot = {x:46, y:86};
const heldKeys = new Set();
let lastWalkFrame = 0;
let lastKnobValue = null;
let speechFaceTimer = null;
let movingPlant = false;
let deleteConfirmId = null;

async function request(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.detail || `Request failed (${response.status})`);
  return result;
}

function sprite(node, index) {
  node.classList.remove("potted-plant-sprite");
  node.classList.remove("growth-sheet-sprite");
  node.style.removeProperty("background-image");
  node.style.removeProperty("background-size");
  node.style.removeProperty("background-position");
  node.classList.add("plant-sprite");
  node.style.setProperty("--sprite", index);
}

function growthSheetSprite(node, plant) {
  node.classList.remove("potted-plant-sprite");
  node.classList.remove("plant-sprite");
  node.style.removeProperty("--sprite");
  node.classList.add("growth-sheet-sprite");
  node.style.backgroundImage = `url("${plant.growth_asset}")`;
  node.style.backgroundSize = "300% 100%";
  node.style.backgroundPosition = `${GROWTH_SHEET_POSITIONS[plant.visual_stage] ?? "100%"} center`;
}

function pottedPlantSprite(node, plant) {
  const fallback = Number.isInteger(plant.plot_slot) ? plant.plot_slot + 1 : 1;
  const identity = Number(plant.id) > 0 ? Number(plant.id) : fallback;
  const assetNumber = ((identity - 1) % POTTED_PLANT_COUNT) + 1;
  node.classList.remove("plant-sprite", "growth-sheet-sprite");
  node.style.removeProperty("--sprite");
  node.classList.add("potted-plant-sprite");
  node.style.backgroundImage = `url("assets/potted-plants/plant_${String(assetNumber).padStart(2, "0")}.png")`;
  node.style.backgroundSize = "contain";
  node.style.backgroundPosition = "center bottom";
}

function plantSprite(node, plant) {
  pottedPlantSprite(node, plant);
}

function stageSprite(plant) {
  return plant.growth < 3 ? EARLY_STAGE_SPRITES[plant.growth] : plant.sprite;
}

function selectedPlant() {
  return state?.plants.find((plant) => plant.id === selectedId) ?? null;
}

function screenText(text) {
  return String(text).replace(/[^\x20-\x7e\n]/g, "").split("\n").map((line) => line.slice(0, 26)).slice(0, 10).join("\n");
}

async function robotCommand(key, value = null) {
  try { await request("/api/command", {key, value}); } catch { /* The garden also works without the robot. */ }
}

function gardenHardwareCue({face, colour = "#00ff00", melody = null, lights = null}) {
  const jobs = [request("/api/face", {text: screenText(face), colour}).catch(() => {})];
  if (melody) jobs.push(robotCommand("spk_play_rtttl", melody));
  if (lights) jobs.push(robotCommand("led_animation", lights));
  return Promise.allSettled(jobs);
}

function stopSpeechFace(restFace = "(^_^)") {
  if (speechFaceTimer) window.clearInterval(speechFaceTimer);
  speechFaceTimer = null;
  gardenHardwareCue({face:restFace});
}

function speakAsRobot(text, restFace = "(^_^)") {
  speechSynthesis?.cancel();
  if (!("speechSynthesis" in window)) return;
  if (speechFaceTimer) window.clearInterval(speechFaceTimer);
  let mouthOpen = false;
  const draw = () => {
    mouthOpen = !mouthOpen;
    gardenHardwareCue({face:mouthOpen ? "(^o^)" : "(^-^)", colour:"#58e39a"});
  };
  draw();
  speechFaceTimer = window.setInterval(draw, 520);
  const line = new SpeechSynthesisUtterance(text);
  line.rate = .92;
  line.onend = () => stopSpeechFace(restFace);
  line.onerror = () => stopSpeechFace(restFace);
  speechSynthesis.speak(line);
}

function render() {
  el("garden-day").textContent = state.today;
  const plot = el("garden-plot");
  plot.replaceChildren(...state.plants.map((plant, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "garden-plant world-plant";
    button.classList.toggle("selected", plant.id === selectedId);
    button.classList.toggle("due", plant.needs_care);
    button.dataset.id = plant.id;
    const slot = Number.isInteger(plant.plot_slot) ? plant.plot_slot : index % PLANT_SPOTS.length;
    const spot = PLANT_SPOTS[slot];
    button.style.left = `${spot[0]}%`; button.style.top = `${spot[1]}%`;
    button.title = plant.needs_care ? `${plant.nickname} is ready for care` : `${plant.nickname} is settled for today`;
    const picture = document.createElement("span");
    picture.className = "world-crop";
    pottedPlantSprite(picture, plant);
    picture.classList.toggle("resting", plant.needs_care && plant.care_count > 0);
    const label = document.createElement("strong");
    label.textContent = plant.nickname;
    const status = document.createElement("span");
    status.textContent = plant.needs_care ? "ready" : "settled";
    button.append(picture, label, status);
    button.addEventListener("click", () => approachPlant(plant.id));
    return button;
  }));

  renderPlacementGrid();
  renderSlotOptions();
  renderHistory();

  const plant = selectedPlant();
  el("plant-empty").hidden = Boolean(plant);
  el("plant-details").hidden = !plant;
  if (!plant) {
    movingPlant = false;
    el("garden-world").classList.remove("arranging");
    return;
  }
  plantSprite(el("plant-large"), plant);
  el("plant-name").textContent = `${plant.nickname} · ${plant.name}`;
  el("plant-about").textContent = plant.about;
  el("plant-state").textContent = plant.needs_care
    ? `${plant.care_label} when you have a moment. Missing it only pauses growth.`
    : `Settled for today · ${plant.stage_name} · stage ${plant.visual_stage + 1} / ${plant.stage_count}`;
  el("plant-care").textContent = plant.needs_care ? plant.care_label : "Already cared for today";
  el("plant-care").disabled = !plant.needs_care;
  if (el("plant-move")) el("plant-move").textContent = movingPlant ? "Cancel moving" : "Move on bed";
  if (el("plant-delete")) {
    el("plant-delete").textContent = deleteConfirmId === plant.id ? "Confirm remove" : "Remove plant";
    el("plant-delete").classList.toggle("confirming", deleteConfirmId === plant.id);
  }
  if (el("placement-hint")) el("placement-hint").hidden = !movingPlant;
}

function slotName(index) {
  return `Bed ${Math.floor(index / 6) + 1} · position ${(index % 6) + 1}`;
}

function renderPlacementGrid() {
  if (!el("garden-slots")) return;
  const occupants = new Map(state.plants.map((plant) => [plant.plot_slot, plant]));
  const selected = selectedPlant();
  el("garden-world").classList.toggle("arranging", movingPlant);
  el("garden-slots").replaceChildren(...PLANT_SPOTS.map((spot, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "garden-slot";
    button.style.left = `${spot[0]}%`; button.style.top = `${spot[1]}%`;
    const occupant = occupants.get(index);
    button.disabled = selected?.plot_slot === index;
    button.setAttribute("aria-label", occupant
      ? `Swap with ${occupant.nickname} in ${slotName(index)}`
      : `Move plant to ${slotName(index)}`);
    button.addEventListener("click", () => moveSelectedPlant(index));
    return button;
  }));
}

function renderSlotOptions() {
  const select = el("plant-slot");
  if (!select) return;
  const previous = Number(select.value);
  const occupied = new Set(state.plants.map((plant) => plant.plot_slot));
  const available = PLANT_SPOTS.map((_, index) => index).filter((index) => !occupied.has(index));
  select.replaceChildren(...available.map((index) => {
    const option = document.createElement("option");
    option.value = index;
    option.textContent = slotName(index);
    return option;
  }));
  if (available.includes(previous)) select.value = previous;
  if (!available.length) {
    select.append(Object.assign(document.createElement("option"), {textContent:"All positions are in use"}));
  }
  el("plant-submit").disabled = !available.length;
}

async function moveSelectedPlant(plotSlot) {
  const plant = selectedPlant();
  if (!movingPlant || !plant) return;
  try {
    await request(`/api/garden/plants/${plant.id}/move`, {plot_slot: plotSlot});
    movingPlant = false;
    el("garden-result").textContent = `${plant.nickname} moved to ${slotName(plotSlot).toLowerCase()}.`;
    await loadGarden();
  } catch (error) {
    el("garden-result").textContent = error.message;
  }
}

async function deleteSelectedPlant() {
  const plant = selectedPlant();
  if (!plant) return;
  if (deleteConfirmId !== plant.id) {
    deleteConfirmId = plant.id;
    el("garden-result").textContent = `Press confirm to remove ${plant.nickname}.`;
    render();
    return;
  }
  try {
    await request(`/api/garden/plants/${plant.id}/delete`, {});
    selectedId = null;
    deleteConfirmId = null;
    movingPlant = false;
    await loadGarden();
  } catch (error) {
    el("garden-result").textContent = error.message;
  }
}

function renderHistory() {
  const rows = state.history ?? [];
  el("garden-history").replaceChildren(...(rows.length ? rows.map((item) => {
    const row = document.createElement("article");
    const title = document.createElement("strong");
    title.textContent = `${item.nickname} · ${item.activity}`;
    const detail = document.createElement("span");
    detail.textContent = `${item.day} · ${item.duration_seconds}s`;
    const note = document.createElement("p"); note.textContent = item.note;
    row.append(title, detail, note);
    return row;
  }) : [Object.assign(document.createElement("p"), {textContent:"Care activities will appear here."})]));
}

async function selectPlant(id) {
  if (selectedId !== Number(id)) {
    movingPlant = false;
    deleteConfirmId = null;
  }
  selectedId = Number(id);
  render();
  const plant = selectedPlant();
  if (plant) gardenHardwareCue({face: `${plant.nickname}\n${plant.needs_care ? plant.care_label : "settled today"}`});
}

function sayAsRobot(text) {
  el("robot-dialogue-text").textContent = text;
  el("robot-dialogue").hidden = false;
  speakAsRobot(text);
  robotCommand("led_animation", {r:48,g:170,b:126});
}

function approachPlant(id) {
  selectPlant(id);
  const plant = state.plants.find((item) => item.id === id);
  sayAsRobot(plant.needs_care ? `${plant.nickname} is ready. ${plant.care_label}.` : `${plant.nickname} is settled for today.`);
}

function updateActors() {
  const character = el("game-character");
  character.style.left = `${player.x}%`; character.style.top = `${player.y}%`;
  character.style.backgroundPosition = `${-player.frame * 64}px ${-player.direction * 96}px`;
  robot.x += (player.x - 5 - robot.x) * .055; robot.y += (player.y + 3 - robot.y) * .055;
  el("robot-follower").style.left = `${robot.x}%`; el("robot-follower").style.top = `${robot.y}%`;
}

function walkLoop(now) {
  let dx = 0, dy = 0;
  if (heldKeys.has("ArrowLeft") || heldKeys.has("a")) { dx = -1; player.direction = 2; }
  if (heldKeys.has("ArrowRight") || heldKeys.has("d")) { dx = 1; player.direction = 3; }
  if (heldKeys.has("ArrowUp") || heldKeys.has("w")) { dy = -1; player.direction = 1; }
  if (heldKeys.has("ArrowDown") || heldKeys.has("s")) { dy = 1; player.direction = 0; }
  if (dx || dy) {
    player.x = Math.max(5, Math.min(94, player.x + dx * .55));
    player.y = Math.max(12, Math.min(90, player.y + dy * .75));
    if (now - lastWalkFrame > 125) { player.frame = (player.frame + 1) % 4; lastWalkFrame = now; }
  } else player.frame = 0;
  updateActors(); requestAnimationFrame(walkLoop);
}

async function loadGarden() {
  state = await request("/api/garden");
  if (selectedId && !state.plants.some((plant) => plant.id === selectedId)) selectedId = null;
  render();
  if (!el("seed-list").children.length) renderSeeds();
}

function renderSeeds() {
  el("seed-list").replaceChildren(...state.catalog.map((plant, index) => {
    const label = document.createElement("label");
    label.className = "seed-card";
    const input = document.createElement("input");
    input.type = "radio"; input.name = "seed"; input.value = plant.species; input.checked = index === 0;
    const picture = document.createElement("span");
    if (plant.growth_asset) growthSheetSprite(picture, {...plant, visual_stage: 2});
    else sprite(picture, plant.sprite);
    const name = document.createElement("strong"); name.textContent = plant.name;
    const care = document.createElement("small"); care.textContent = plant.care_label;
    label.append(input, picture, name, care);
    return label;
  }));
}

async function completeCare(activity, score = 1) {
  const plant = selectedPlant();
  if (!plant) return;
  careCleanup();
  el("care-game").hidden = true;
  try {
    const result = await request(`/api/garden/plants/${plant.id}/care`, {
      activity, duration_seconds: Math.max(1, Math.round((Date.now() - careStarted) / 1000)), score,
    });
    el("garden-result").textContent = result.changed ? `${plant.nickname} grew a little.` : `${plant.nickname} is already settled.`;
    await gardenHardwareCue({
      face:`${plant.nickname}\n(^_^)\nthank you`,
      melody:"grow:d=8,o=5,b=150:c,e,g,4c6",
      lights:{r:90,g:220,b:130},
    });
    await loadGarden();
  } catch (error) { el("garden-result").textContent = error.message; }
}

function careButton(text, className = "") {
  const button = document.createElement("button");
  button.type = "button"; button.className = `care-control ${className}`; button.textContent = text;
  return button;
}

function startCareGame() {
  const plant = selectedPlant();
  if (!plant || !plant.needs_care) return;
  careStarted = Date.now();
  careCleanup(); careCleanup = () => {};
  el("care-game").hidden = false;
  el("care-game-title").textContent = `${plant.care_label} for ${plant.nickname}`;
  el("care-game-status").textContent = "";
  el("care-progress").style.width = "0%";
  const stage = el("care-stage"); stage.replaceChildren();
  const activity = plant.care;
  if (activity === "water") buildWater(stage, plant);
  else if (activity === "mist") buildMist(stage, plant);
  else if (activity === "turn") buildTurn(stage, plant);
  else if (activity === "sun") buildSun(stage, plant);
  else buildCheck(stage, plant);
}

function buildWater(stage, plant) {
  el("care-game-copy").textContent = "Hold the watering can until the soil is evenly damp.";
  const button = careButton("Hold to pour", "watering-can"); stage.append(button);
  let amount = 0; let timer = null;
  const stop = () => { if (timer) clearInterval(timer); timer = null; };
  const start = () => { if (timer) return; timer = setInterval(() => {
    amount += 2; el("care-progress").style.width = `${amount}%`; button.style.setProperty("--pour", amount / 100);
    if (amount >= 100) { stop(); completeCare("water"); }
  }, 45); };
  button.addEventListener("pointerdown", start); button.addEventListener("pointerup", stop); button.addEventListener("pointerleave", stop);
  button.addEventListener("click", () => { if (amount >= 100) return; amount = Math.min(100, amount + 20); el("care-progress").style.width = `${amount}%`; if (amount >= 100) completeCare("water"); });
  button.addEventListener("keydown", (event) => { if (event.code === "Space") { event.preventDefault(); start(); } });
  button.addEventListener("keyup", stop); careCleanup = stop;
}

function buildMist(stage) {
  el("care-game-copy").textContent = "Mist each dry patch. The drops will show where you have reached.";
  let hits = 0;
  for (let index = 0; index < 6; index += 1) {
    const target = careButton("", "mist-target"); target.setAttribute("aria-label", `Mist patch ${index + 1}`);
    target.style.setProperty("--x", `${12 + (index * 31) % 78}%`); target.style.setProperty("--y", `${18 + (index * 23) % 65}%`);
    target.addEventListener("click", () => { if (target.classList.contains("wet")) return; target.classList.add("wet"); hits += 1; el("care-progress").style.width = `${hits / 6 * 100}%`; if (hits === 6) completeCare("mist"); });
    stage.append(target);
  }
}

function buildTurn(stage) {
  el("care-game-copy").textContent = "Turn the pot slowly so another side can face the light.";
  const pot = document.createElement("div"); pot.className = "turn-pot";
  const slider = document.createElement("input"); slider.type = "range"; slider.min = "0"; slider.max = "100"; slider.value = "0"; slider.setAttribute("aria-label", "Turn the pot");
  slider.addEventListener("input", () => { const value = Number(slider.value); pot.style.transform = `rotateY(${value * 1.8}deg)`; el("care-progress").style.width = `${value}%`; if (value >= 96) completeCare("turn"); });
  stage.append(pot, slider);
}

function buildSun(stage, plant) {
  el("care-game-copy").textContent = "Choose a place in the greenhouse. You can try again if the light is not right.";
  const correct = plant.species === "lavender" ? "sunny shelf" : "bright bench";
  for (const [index, place] of ["shaded corner", "bright bench", "sunny shelf"].entries()) {
    const button = careButton(place, "sun-place");
    button.classList.toggle("robot-choice", index === 0);
    button.addEventListener("click", () => { if (place === correct) { el("care-progress").style.width = "100%"; completeCare("sun"); } else el("care-game-status").textContent = "That spot is not quite right for this plant. Try another."; });
    stage.append(button);
  }
}

function robotYes() {
  if (el("care-game").hidden) { startCareGame(); return; }
  const stage = el("care-stage");
  const target = stage.querySelector(".robot-choice, .mist-target:not(.wet), .soil-check:not([data-done]), .watering-can");
  if (target) { target.click(); return; }
  const slider = stage.querySelector('input[type="range"]');
  if (slider) { slider.value = Math.min(100, Number(slider.value) + 25); slider.dispatchEvent(new Event("input")); }
}

function robotNo() {
  if (el("care-game").hidden) {
    if (!state?.plants.length) return;
    const index = state.plants.findIndex((plant) => plant.id === selectedId);
    selectPlant(state.plants[(index + 1) % state.plants.length].id);
    return;
  }
  const choices = [...el("care-stage").querySelectorAll(".sun-place")];
  if (choices.length) {
    const index = choices.findIndex((choice) => choice.classList.contains("robot-choice"));
    choices.forEach((choice) => choice.classList.remove("robot-choice"));
    choices[(index + 1) % choices.length].classList.add("robot-choice");
  } else {
    careCleanup(); el("care-game").hidden = true;
  }
}

function buildCheck(stage) {
  el("care-game-copy").textContent = "Check all three areas of soil and notice how they feel.";
  let checked = 0;
  ["dry", "damp", "cool"].forEach((feel, index) => {
    const button = careButton("check", "soil-check");
    button.addEventListener("click", () => { if (button.dataset.done) return; button.dataset.done = "yes"; button.textContent = feel; checked += 1; el("care-progress").style.width = `${checked / 3 * 100}%`; if (checked === 3) window.setTimeout(() => completeCare("check"), 350); });
    stage.append(button);
  });
}

function tellAboutSelected() {
  const plant = selectedPlant();
  if (!plant) return;
  speakAsRobot(`${plant.name}. ${plant.about}`, `${plant.name}\n${plant.care_label}`);
}

el("plant-care").addEventListener("click", startCareGame);
el("plant-move")?.addEventListener("click", () => {
  movingPlant = !movingPlant;
  deleteConfirmId = null;
  render();
});
el("plant-delete")?.addEventListener("click", deleteSelectedPlant);
el("care-cancel").addEventListener("click", () => { careCleanup(); el("care-game").hidden = true; });
el("dialogue-close").addEventListener("click", () => { el("robot-dialogue").hidden = true; speechSynthesis?.cancel(); stopSpeechFace(); });
window.addEventListener("keydown", (event) => {
  if (el("garden-view").hidden || !["ArrowLeft","ArrowRight","ArrowUp","ArrowDown","w","a","s","d"].includes(event.key)) return;
  el("game-help")?.classList.add("dismissed");
  event.preventDefault(); heldKeys.add(event.key);
});
window.addEventListener("keyup", (event) => heldKeys.delete(event.key));
el("plant-tell").addEventListener("click", tellAboutSelected);
el("seed-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const species = new FormData(event.currentTarget).get("seed");
  const slot = el("plant-slot");
  if (slot && !slot.value) return;
  const payload = {species, nickname: el("plant-nickname").value};
  if (slot) payload.plot_slot = Number(slot.value);
  const result = await request("/api/garden/plants", payload);
  el("plant-nickname").value = "";
  selectedId = result.plant.id;
  movingPlant = false;
  await loadGarden();
});
for (const button of document.querySelectorAll("[data-admin]")) button.addEventListener("click", async () => {
  state = await request("/api/garden/admin", {action: button.dataset.admin, days: 1});
  render();
});

window.setInterval(async () => {
  if (el("garden-view").hidden) return;
  try {
    const events = await request("/api/events");
    if (events.length < seenEvents) seenEvents = 0;
    for (const event of events.slice(seenEvents)) {
      if (event.key === "knob" && Number.isFinite(Number(event.value))) {
        const next = Number(event.value);
        if (lastKnobValue !== null && next !== lastKnobValue && state.plants.length) {
          const step = next > lastKnobValue ? 1 : -1;
          const index = state.plants.findIndex((plant) => plant.id === selectedId);
          const nextIndex = index < 0 ? (step > 0 ? 0 : state.plants.length - 1) : (index + step + state.plants.length) % state.plants.length;
          selectPlant(state.plants[nextIndex].id);
        }
        lastKnobValue = next;
        continue;
      }
      if (event.type !== "button" || event.value !== 1) continue;
      if (["btn", "knob_btn", "yes_btn"].includes(event.key)) robotYes();
      if (event.key === "no_btn") robotNo();
    }
    seenEvents = events.length;
  } catch { /* Garden remains usable without the robot. */ }
}, 1000);

loadGarden().catch((error) => { el("garden-plot").textContent = error.message; });
el("garden-tools").append(el("seed-shelf"), el("admin-tools"), el("garden-journal"));
requestAnimationFrame(walkLoop);
