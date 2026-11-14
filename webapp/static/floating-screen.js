import {checkFace, drawFace} from "./face.js";

const windowNode = document.getElementById("floating-screen");
const handle = document.getElementById("floating-screen-handle");
const body = document.getElementById("floating-screen-body");
const toggle = document.getElementById("floating-screen-toggle");
const canvas = document.getElementById("floating-screen-canvas");
const status = document.getElementById("floating-screen-status");
const screen = {width:160, height:80};
const positionKey = "salus-floating-screen-position";
const collapsedKey = "salus-floating-screen-collapsed";
let drag = null;
let lastFace = "";

function clampPosition(x, y) {
  const width = windowNode.offsetWidth || 280;
  const height = windowNode.offsetHeight || 170;
  return {
    x: Math.max(8, Math.min(window.innerWidth - width - 8, x)),
    y: Math.max(8, Math.min(window.innerHeight - height - 8, y)),
  };
}

function place(x, y, save = true) {
  const next = clampPosition(x, y);
  windowNode.style.left = `${next.x}px`;
  windowNode.style.top = `${next.y}px`;
  if (save) localStorage.setItem(positionKey, JSON.stringify(next));
}

function initialPosition() {
  try {
    const saved = JSON.parse(localStorage.getItem(positionKey));
    if (Number.isFinite(saved?.x) && Number.isFinite(saved?.y)) return saved;
  } catch { /* Use the default corner. */ }
  return {x:window.innerWidth - 300, y:window.innerHeight - 190};
}

function setCollapsed(collapsed) {
  body.hidden = collapsed;
  toggle.textContent = collapsed ? "+" : "−";
  toggle.setAttribute("aria-label", collapsed ? "Expand robot face window" : "Minimise robot face window");
  localStorage.setItem(collapsedKey, String(collapsed));
  const current = windowNode.getBoundingClientRect();
  place(current.left, current.top);
}

handle.addEventListener("pointerdown", (event) => {
  if (event.target.closest("button")) return;
  const box = windowNode.getBoundingClientRect();
  drag = {dx:event.clientX - box.left, dy:event.clientY - box.top};
  handle.setPointerCapture(event.pointerId);
  windowNode.classList.add("dragging");
});

handle.addEventListener("pointermove", (event) => {
  if (!drag) return;
  place(event.clientX - drag.dx, event.clientY - drag.dy, false);
});

function endDrag(event) {
  if (!drag) return;
  drag = null;
  windowNode.classList.remove("dragging");
  if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
  const box = windowNode.getBoundingClientRect();
  place(box.left, box.top);
}

handle.addEventListener("pointerup", endDrag);
handle.addEventListener("pointercancel", endDrag);
handle.addEventListener("keydown", (event) => {
  const movement = {ArrowLeft:[-1,0], ArrowRight:[1,0], ArrowUp:[0,-1], ArrowDown:[0,1]}[event.key];
  if (!movement) return;
  event.preventDefault();
  const box = windowNode.getBoundingClientRect();
  const amount = event.shiftKey ? 25 : 8;
  place(box.left + movement[0] * amount, box.top + movement[1] * amount);
});
toggle.addEventListener("click", () => setCollapsed(!body.hidden));
window.addEventListener("resize", () => {
  const box = windowNode.getBoundingClientRect();
  place(box.left, box.top);
});

async function refreshFace() {
  try {
    const response = await fetch("/api/screen-state", {cache:"no-store"});
    if (!response.ok) throw new Error();
    const state = await response.json();
    const faceKey = `${state.text}|${state.colour}`;
    if (faceKey !== lastFace) {
      drawFace(canvas, checkFace(state.text || "(^_^)", screen), screen, state.colour || "#00ff00");
      lastFace = faceKey;
    }
    status.textContent = "Live";
  } catch {
    status.textContent = "Waiting for app";
  }
}

const start = initialPosition();
place(start.x, start.y, false);
setCollapsed(localStorage.getItem(collapsedKey) === "true");
refreshFace();
window.setInterval(refreshFace, 300);
