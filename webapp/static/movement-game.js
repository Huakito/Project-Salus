import { FilesetResolver, PoseLandmarker } from "./vendor/mediapipe/vision_bundle.mjs";

const el = (id) => document.getElementById(id);
const HOLD_MS = 1200;
const CONNECTIONS = [
  [11, 12], [11, 13], [13, 15], [12, 14], [14, 16],
  [11, 23], [12, 24], [23, 24], [23, 25], [24, 26], [25, 27], [26, 28],
];

const POSES = [
  {
    key: "wings",
    name: "Open wings",
    cue: "Stretch both arms out to the sides",
    face: "(^_^)",
    matches: (p, s) =>
      Math.abs(p[15].y - p[11].y) < s * 0.65 && Math.abs(p[16].y - p[12].y) < s * 0.65 &&
      Math.abs(p[15].x - p[16].x) > s * 2.8,
  },
  {
    key: "reach",
    name: "Reach high",
    cue: "Take both hands comfortably overhead",
    face: "\\(^o^)/",
    matches: (p) => p[15].y < p[0].y && p[16].y < p[0].y,
  },
  {
    key: "together",
    name: "Hands together",
    cue: "Bring your hands together in front of you",
    face: "(._.)",
    matches: (p, s) => distance(p[15], p[16]) < s * 0.7 && Math.max(p[15].y, p[16].y) < p[23].y,
  },
  {
    key: "left_reach",
    name: "One side",
    cue: "Lift the hand on the left of the screen",
    face: "(^_^)/",
    matches: (p) => p[16].y < p[0].y && p[15].y > p[11].y,
  },
  {
    key: "right_reach",
    name: "Other side",
    cue: "Switch hands",
    face: "\\(^_^)",
    matches: (p) => p[15].y < p[0].y && p[16].y > p[12].y,
  },
];

let landmarker;
let stream;
let frameRequest;
let running = false;
let poseIndex = 0;
let holdStarted = null;
let sessionStarted = null;
let lastVideoTime = -1;
let lastPoints = null;
let completed = [];
let skipped = [];
let trackingSamples = 0;
let trackingHits = 0;

function distance(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

async function api(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const answer = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(answer.detail || `Request failed (${response.status})`);
  return answer;
}

async function setRobotFace(text, colour = "#00ff88") {
  try {
    await api("/api/face", { text, colour });
  } catch {
    // The camera game remains playable when the physical robot is disconnected.
  }
}

function say(text) {
  if (!el("game-voice").checked || !("speechSynthesis" in window)) return;
  speechSynthesis.cancel();
  const message = new SpeechSynthesisUtterance(text);
  message.rate = 0.96;
  message.pitch = 1.05;
  speechSynthesis.speak(message);
}

async function loadTracker() {
  if (landmarker) return;
  el("camera-status").textContent = "Loading movement tracker…";
  const vision = await FilesetResolver.forVisionTasks("./vendor/mediapipe/wasm");
  const options = {
    baseOptions: { modelAssetPath: "./models/pose_landmarker_lite.task", delegate: "GPU" },
    runningMode: "VIDEO",
    numPoses: 1,
    minPoseDetectionConfidence: 0.55,
    minPosePresenceConfidence: 0.55,
    minTrackingConfidence: 0.55,
  };
  try {
    landmarker = await PoseLandmarker.createFromOptions(vision, options);
  } catch {
    options.baseOptions.delegate = "CPU";
    landmarker = await PoseLandmarker.createFromOptions(vision, options);
  }
}

function renderProgress() {
  el("game-progress").replaceChildren(...POSES.map((pose, index) => {
    const dot = document.createElement("span");
    dot.className = "progress-dot";
    dot.title = pose.name;
    if (index < poseIndex) dot.dataset.state = skipped.includes(pose.key) ? "skipped" : "done";
    else if (index === poseIndex && running) dot.dataset.state = "current";
    return dot;
  }));
}

function showPose() {
  const pose = POSES[poseIndex];
  if (!pose) return finishGame();
  holdStarted = null;
  el("hold-progress").style.width = "0%";
  el("pose-count").textContent = `${poseIndex + 1} / ${POSES.length}`;
  el("pose-name").textContent = pose.name;
  el("pose-cue").textContent = pose.cue;
  el("pose-card").hidden = false;
  renderProgress();
  say(`${pose.name}. ${pose.cue}.`);
  setRobotFace(pose.face, "#00ffaa");
}

function visible(landmark) {
  return (landmark.visibility ?? 1) > 0.55;
}

function confidentPose(points) {
  return [0, 11, 12, 15, 16, 23, 24].every((index) => visible(points[index]));
}

function drawCamera(points) {
  const video = el("game-video");
  const canvas = el("game-canvas");
  const context = canvas.getContext("2d");
  if (canvas.width !== video.videoWidth || canvas.height !== video.videoHeight) {
    canvas.width = video.videoWidth || 960;
    canvas.height = video.videoHeight || 720;
  }
  context.save();
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.translate(canvas.width, 0);
  context.scale(-1, 1);
  context.globalAlpha = 0.58;
  context.drawImage(video, 0, 0, canvas.width, canvas.height);
  context.globalAlpha = 1;
  if (points) {
    context.strokeStyle = "#74f7ba";
    context.lineWidth = Math.max(3, canvas.width / 220);
    context.lineCap = "round";
    for (const [a, b] of CONNECTIONS) {
      if (!visible(points[a]) || !visible(points[b])) continue;
      context.beginPath();
      context.moveTo(points[a].x * canvas.width, points[a].y * canvas.height);
      context.lineTo(points[b].x * canvas.width, points[b].y * canvas.height);
      context.stroke();
    }
    context.fillStyle = "#f5fff9";
    for (const index of [0, 11, 12, 13, 14, 15, 16, 23, 24]) {
      if (!visible(points[index])) continue;
      context.beginPath();
      context.arc(points[index].x * canvas.width, points[index].y * canvas.height, context.lineWidth * 1.25, 0, Math.PI * 2);
      context.fill();
    }
  }
  context.restore();
}

function detectFrame() {
  if (!running) return;
  const video = el("game-video");
  if (video.readyState >= 2 && video.currentTime !== lastVideoTime) {
    lastVideoTime = video.currentTime;
    const result = landmarker.detectForVideo(video, performance.now());
    lastPoints = result.landmarks?.[0] ?? null;
    trackingSamples += 1;
    if (lastPoints && confidentPose(lastPoints)) trackingHits += 1;
  }
  drawCamera(lastPoints);
  updatePose(lastPoints);
  frameRequest = requestAnimationFrame(detectFrame);
}

function updatePose(points) {
  if (!points || !confidentPose(points)) {
    holdStarted = null;
    el("hold-progress").style.width = "0%";
    el("camera-status").textContent = "Move back until your upper body is visible";
    return;
  }
  el("camera-status").textContent = "Movement found";
  const shoulderWidth = distance(points[11], points[12]);
  if (shoulderWidth < 0.04 || !POSES[poseIndex].matches(points, shoulderWidth)) {
    holdStarted = null;
    el("hold-progress").style.width = "0%";
    return;
  }
  if (holdStarted === null) holdStarted = performance.now();
  const held = performance.now() - holdStarted;
  el("hold-progress").style.width = `${Math.min(100, held / HOLD_MS * 100)}%`;
  if (held >= HOLD_MS) completePose();
}

function completePose() {
  const pose = POSES[poseIndex];
  completed.push(pose.key);
  poseIndex += 1;
  holdStarted = null;
  say("Got it.");
  setRobotFace("(^o^)", "#65ff9a");
  window.setTimeout(showPose, 600);
}

async function startGame() {
  el("game-problem").hidden = true;
  el("game-summary").hidden = true;
  el("game-start").disabled = true;
  try {
    await loadTracker();
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: { ideal: 960 }, height: { ideal: 720 } },
      audio: false,
    });
    const video = el("game-video");
    video.srcObject = stream;
    await video.play();
  } catch (error) {
    el("game-start").disabled = false;
    el("game-problem").textContent = error.name === "NotAllowedError"
      ? "Camera access was not allowed. You can change that in the browser's camera settings."
      : `The camera could not start: ${error.message}`;
    el("game-problem").hidden = false;
    return;
  }

  running = true;
  poseIndex = 0;
  completed = [];
  skipped = [];
  trackingSamples = 0;
  trackingHits = 0;
  lastPoints = null;
  sessionStarted = Date.now();
  el("camera-empty").hidden = true;
  el("camera-shell").dataset.state = "playing";
  el("game-skip").disabled = false;
  el("game-stop").disabled = false;
  showPose();
  detectFrame();
}

function stopCamera() {
  running = false;
  if (frameRequest) cancelAnimationFrame(frameRequest);
  stream?.getTracks().forEach((track) => track.stop());
  stream = null;
  speechSynthesis?.cancel();
  el("game-start").disabled = false;
  el("game-skip").disabled = true;
  el("game-stop").disabled = true;
  el("pose-card").hidden = true;
  el("camera-shell").dataset.state = "idle";
  el("camera-status").textContent = "Camera off";
}

function finishGame() {
  const duration = Math.max(1, Math.round((Date.now() - sessionStarted) / 1000));
  stopCamera();
  renderProgress();
  el("summary-copy").textContent = `${completed.length} shapes found in ${duration} seconds${skipped.length ? `, ${skipped.length} skipped` : ""}.`;
  el("game-summary").dataset.duration = duration;
  el("game-summary").hidden = false;
  say(`Run complete. ${completed.length} shapes found.`);
  setRobotFace("(^_^)", "#00ff88");
}

function skipPose() {
  if (!running) return;
  skipped.push(POSES[poseIndex].key);
  poseIndex += 1;
  showPose();
}

async function saveSession() {
  const selected = document.querySelector('input[name="game-feeling"]:checked');
  const payload = {
    game: "salus_says",
    mode: document.querySelector('input[name="movement-mode"]:checked').value,
    duration_seconds: Number(el("game-summary").dataset.duration),
    movements_offered: POSES.map((pose) => pose.key),
    movements_completed: completed,
    movements_skipped: skipped,
    tracking_quality: trackingSamples ? trackingHits / trackingSamples : 0,
    feeling: selected?.value ?? null,
  };
  const result = el("game-save-result");
  try {
    const answer = await api("/api/movement-sessions", payload);
    result.dataset.kind = "ok";
    result.textContent = answer.feedback;
    el("game-save").disabled = true;
    say(answer.feedback);
  } catch (error) {
    result.dataset.kind = "error";
    result.textContent = error.message;
  }
}

for (const tab of document.querySelectorAll("[data-view]")) {
  tab.addEventListener("click", () => {
    const play = tab.dataset.view === "play";
    el("play-view").hidden = !play;
    el("stage").hidden = play;
    el("commands-note").hidden = play;
    for (const item of document.querySelectorAll("[data-view]")) {
      const active = item === tab;
      item.classList.toggle("active", active);
      item.setAttribute("aria-pressed", String(active));
    }
    if (!play && running) stopCamera();
  });
}

el("game-start").addEventListener("click", startGame);
el("game-skip").addEventListener("click", skipPose);
el("game-stop").addEventListener("click", finishGame);
el("game-save").addEventListener("click", saveSession);

renderProgress();
