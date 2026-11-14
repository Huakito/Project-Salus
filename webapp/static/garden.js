const el = (id) => document.getElementById(id);
let state = null;
let selectedId = null;
let seenEvents = 0;

async function request(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.detail || `Request failed (${response.status})`);
  return result;
}

function sprite(node, index) {
  node.classList.add("plant-sprite");
  node.style.setProperty("--sprite", index);
}

function selectedPlant() {
  return state?.plants.find((plant) => plant.id === selectedId) ?? null;
}

function render() {
  el("garden-day").textContent = state.today;
  const plot = el("garden-plot");
  plot.replaceChildren(...state.plants.map((plant) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "garden-plant";
    button.classList.toggle("selected", plant.id === selectedId);
    button.classList.toggle("due", plant.needs_care);
    button.dataset.id = plant.id;
    button.title = plant.needs_care ? `${plant.nickname} is ready for care` : `${plant.nickname} is settled for today`;
    const picture = document.createElement("span");
    sprite(picture, plant.sprite);
    picture.style.setProperty("--growth", Math.max(.48, .48 + plant.growth * .104));
    const label = document.createElement("strong");
    label.textContent = plant.nickname;
    const status = document.createElement("span");
    status.textContent = plant.needs_care ? "ready" : "settled";
    button.append(picture, label, status);
    button.addEventListener("click", () => selectPlant(plant.id));
    return button;
  }));

  const plant = selectedPlant();
  el("plant-empty").hidden = Boolean(plant);
  el("plant-details").hidden = !plant;
  if (!plant) return;
  sprite(el("plant-large"), plant.sprite);
  el("plant-name").textContent = `${plant.nickname} · ${plant.name}`;
  el("plant-about").textContent = plant.about;
  el("plant-state").textContent = plant.needs_care
    ? `${plant.care_label} when you have a moment. Missing it only pauses growth.`
    : `Settled for today · growth ${plant.growth} / 5`;
  el("plant-care").textContent = plant.needs_care ? plant.care_label : "Already cared for today";
  el("plant-care").disabled = !plant.needs_care;
}

async function selectPlant(id) {
  selectedId = Number(id);
  render();
  const plant = selectedPlant();
  if (plant) request("/api/face", {text: `${plant.nickname}\n${plant.needs_care ? plant.care_label : "settled today"}`, colour: "#00ff00"}).catch(() => {});
}

async function loadGarden() {
  state = await request("/api/garden");
  if (!selectedId || !state.plants.some((plant) => plant.id === selectedId)) selectedId = state.plants[0]?.id ?? null;
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
    sprite(picture, plant.sprite);
    const name = document.createElement("strong"); name.textContent = plant.name;
    const care = document.createElement("small"); care.textContent = plant.care_label;
    label.append(input, picture, name, care);
    return label;
  }));
}

async function careForSelected() {
  const plant = selectedPlant();
  if (!plant) return;
  try {
    const result = await request(`/api/garden/plants/${plant.id}/care`, {});
    el("garden-result").textContent = result.changed ? `${plant.nickname} grew a little.` : `${plant.nickname} is already settled.`;
    await Promise.allSettled([
      request("/api/face", {text: `${plant.nickname}\n(^_^)\nthank you`, colour: "#00ff00"}),
      request("/api/command", {key:"spk_play_rtttl", value:"grow:d=8,o=5,b=150:c,e,g,4c6"}),
    ]);
    await loadGarden();
  } catch (error) { el("garden-result").textContent = error.message; }
}

function tellAboutSelected() {
  const plant = selectedPlant();
  if (!plant) return;
  speechSynthesis?.cancel();
  if ("speechSynthesis" in window) speechSynthesis.speak(new SpeechSynthesisUtterance(`${plant.name}. ${plant.about}`));
  request("/api/face", {text:`${plant.name}\n${plant.care_label}`, colour:"#00ff00"}).catch(() => {});
}

el("plant-care").addEventListener("click", careForSelected);
el("plant-tell").addEventListener("click", tellAboutSelected);
el("seed-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const species = new FormData(event.currentTarget).get("seed");
  const result = await request("/api/garden/plants", {species, nickname: el("plant-nickname").value});
  el("plant-nickname").value = "";
  selectedId = result.plant.id;
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
      if (event.type !== "button" || event.value !== 1) continue;
      if (event.key === "yes_btn") careForSelected();
      if (event.key === "no_btn" && state?.plants.length) {
        const index = state.plants.findIndex((plant) => plant.id === selectedId);
        selectPlant(state.plants[(index + 1) % state.plants.length].id);
      }
    }
    seenEvents = events.length;
  } catch { /* Garden remains usable without the robot. */ }
}, 1000);

loadGarden().catch((error) => { el("garden-plot").textContent = error.message; });
