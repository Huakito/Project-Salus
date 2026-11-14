import {checkFace, drawFace, drawVolume} from "./face.js";

const canvas = document.getElementById("live-screen");
const status = document.getElementById("screen-status");
const screen = {width:160, height:80};
let lastUpdate = "";

async function refresh() {
  try {
    const response = await fetch("/api/screen-state", {cache:"no-store"});
    if (!response.ok) throw new Error(`status ${response.status}`);
    const state = await response.json();
    if (state.updated_at !== lastUpdate) {
      if (state.kind === "volume") drawVolume(canvas, screen, Number(state.volume) || 0);
      else drawFace(canvas, checkFace(state.text || "(^_^)", screen), screen, state.colour || "#00ff00");
      lastUpdate = state.updated_at;
    }
    status.textContent = state.kind === "volume" ? `Volume ${state.volume}%` : "Following the main app";
  } catch {
    status.textContent = "Waiting for the Salus app…";
  }
}

refresh();
window.setInterval(refresh, 300);
