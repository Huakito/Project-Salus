import { FilesetResolver, PoseLandmarker } from "./vendor/mediapipe/vision_bundle.mjs";

const el = (id) => document.getElementById(id);
const HOLD_MS = 1600;
const TRANSITION_SECONDS = 3;
const POSES_PER_RUN = 5;
const CONNECTIONS = [
  [11, 12], [11, 13], [13, 15], [12, 14], [14, 16],
  [11, 23], [12, 24], [23, 24], [23, 25], [24, 26], [25, 27], [26, 28],
];

const POSES = [
  {
    key: "wings", name: "Open wings", cue: "Let both arms open comfortably to the sides", face: "(^_^)",
    modes: ["standing", "seated"],
    matches: (p, s) => level(p[15], p[11], s * 0.65) && level(p[16], p[12], s * 0.65) && Math.abs(p[15].x - p[16].x) > s * 2.8,
  },
  {
    key: "reach", name: "Reach high", cue: "Take both hands overhead without forcing the stretch", face: "\\(^o^)/",
    modes: ["standing", "seated"], matches: (p) => p[15].y < p[0].y && p[16].y < p[0].y,
  },
  {
    key: "together", name: "Hands together", cue: "Bring your hands together in front of you", face: "(._.)",
    modes: ["standing", "seated"], matches: (p, s) => distance(p[15], p[16]) < s * 0.7 && Math.max(p[15].y, p[16].y) < p[23].y,
  },
  {
    key: "left_reach", name: "One hand high", cue: "Lift the hand on the left of the screen", face: "(^_^)/",
    modes: ["standing", "seated"], matches: (p) => p[16].y < p[0].y && p[15].y > p[11].y,
  },
  {
    key: "right_reach", name: "Other hand high", cue: "Lift the hand on the right of the screen", face: "\\(^_^)",
    modes: ["standing", "seated"], matches: (p) => p[15].y < p[0].y && p[16].y > p[12].y,
  },
  {
    key: "goalposts", name: "Goalposts", cue: "Bend both elbows and point your hands upward", face: "[o_o]",
    modes: ["standing", "seated"],
    matches: (p, s) => level(p[13], p[11], s * 0.8) && level(p[14], p[12], s * 0.8) && p[15].y < p[13].y && p[16].y < p[14].y,
  },
  {
    key: "hands_shoulders", name: "Hands to shoulders", cue: "Rest each hand close to its shoulder", face: "(o_o)",
    modes: ["standing", "seated"],
    matches: (p, s) => distance(p[15], p[11]) < s * 0.9 && distance(p[16], p[12]) < s * 0.9,
  },
  {
    key: "hands_hips", name: "Hands to hips", cue: "Place both hands near your hips", face: "(^.^)",
    modes: ["standing", "seated"],
    matches: (p, s) => distance(p[15], p[23]) < s * 1.05 && distance(p[16], p[24]) < s * 1.05,
  },
  {
    key: "crossed", name: "Comfortable cross", cue: "Bring each hand toward the opposite shoulder", face: "(-_-)",
    modes: ["standing", "seated"],
    matches: (p, s) => distance(p[15], p[12]) < s * 1.05 && distance(p[16], p[11]) < s * 1.05,
  },
  {
    key: "diamond", name: "Diamond", cue: "Bring your hands together above your head", face: "/^_^\\",
    modes: ["standing", "seated"],
    matches: (p, s) => distance(p[15], p[16]) < s * 0.85 && p[15].y < p[0].y && p[16].y < p[0].y,
  },
  {
    key: "left_side", name: "Long line left", cue: "Open one arm toward the left of the screen", face: "<(^_^)",
    modes: ["standing", "seated"],
    matches: (p, s) => level(p[16], p[12], s * 0.65) && p[16].x < p[12].x - s * 1.1 && p[15].y > p[11].y,
  },
  {
    key: "right_side", name: "Long line right", cue: "Open one arm toward the right of the screen", face: "(^_)>",
    modes: ["standing", "seated"],
    matches: (p, s) => level(p[15], p[11], s * 0.65) && p[15].x > p[11].x + s * 1.1 && p[16].y > p[12].y,
  },
  {
    key: "wide_stance", name: "Wide stance", cue: "Place your feet a comfortable distance apart", face: "(^_^)",
    modes: ["standing"], required: [27, 28],
    matches: (p, s) => Math.abs(p[27].x - p[28].x) > s * 2.1,
  },
  {
    key: "left_balance", name: "Light balance", cue: "Lift the knee on the left of the screen a little", face: "(^_~)",
    modes: ["standing"], required: [25, 26],
    matches: (p, s) => p[26].y < p[25].y - s * 0.7,
  },
  {
    key: "right_balance", name: "Switch balance", cue: "Lift the other knee a little", face: "(~_^)",
    modes: ["standing"], required: [25, 26],
    matches: (p, s) => p[25].y < p[26].y - s * 0.7,
  },
];

const CHECK_IN_QUESTIONS = [
  "What pace feels right from here?",
  "What is one small thing that went okay today?",
  "What would make the rest of today feel a little easier?",
  "Where would you like your attention to be for the next few minutes?",
  "Is there something you would like to set down for the next minute?",
];

let landmarker;
let stream;
let frameRequest;
let recognition;
let running = false;
let phase = "idle";
let poseIndex = 0;
let holdStarted = null;
let sessionStarted = null;
let lastVideoTime = -1;
let lastPoints = null;
let completed = [];
let skipped = [];
let selectedPoses = [];
let checkIns = [];
let checkInAfter = null;
let activeQuestion = "";
let safetyPaused = false;
let trackingSamples = 0;
let trackingHits = 0;
let flowToken = 0;

function distance(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }
function level(a, b, tolerance) { return Math.abs(a.y - b.y) < tolerance; }
function delay(ms) { return new Promise((resolve) => window.setTimeout(resolve, ms)); }

function shuffle(items) {
  const copy = [...items];
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const target = Math.floor(Math.random() * (index + 1));
    [copy[index], copy[target]] = [copy[target], copy[index]];
  }
  return copy;
}

async function api(path, body) {
  const response = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const answer = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(answer.detail || `Request failed (${response.status})`);
  return answer;
}

async function setRobotFace(text, colour = "#1a6964") {
  try { await api("/api/face", { text, colour }); } catch { /* The game also works without the robot. */ }
}

function say(text) {
  if (!el("game-voice").checked || !("speechSynthesis" in window)) return;
  speechSynthesis.cancel();
  const message = new SpeechSynthesisUtterance(text);
  message.rate = 0.9;
  message.pitch = 1;
  speechSynthesis.speak(message);
}

async function loadTracker() {
  if (landmarker) return;
  el("camera-status").textContent = "Loading movement tracker…";
  const vision = await FilesetResolver.forVisionTasks("./vendor/mediapipe/wasm");
  const options = {
    baseOptions: { modelAssetPath: "./models/pose_landmarker_lite.task", delegate: "GPU" },
    runningMode: "VIDEO", numPoses: 1,
    minPoseDetectionConfidence: 0.55, minPosePresenceConfidence: 0.55, minTrackingConfidence: 0.55,
  };
  try { landmarker = await PoseLandmarker.createFromOptions(vision, options); }
  catch { options.baseOptions.delegate = "CPU"; landmarker = await PoseLandmarker.createFromOptions(vision, options); }
}

function availablePoses() {
  const mode = document.querySelector('input[name="movement-mode"]:checked').value;
  return POSES.filter((pose) => pose.modes.includes(mode));
}

function renderPoseLibrary() {
  el("pose-library-list").replaceChildren(...availablePoses().map((pose) => {
    const item = document.createElement("span");
    item.textContent = pose.name;
    return item;
  }));
}

function renderProgress() {
  el("game-progress").replaceChildren(...selectedPoses.map((pose, index) => {
    const dot = document.createElement("span");
    dot.className = "progress-dot";
    dot.title = pose.name;
    if (index < poseIndex) dot.dataset.state = skipped.includes(pose.key) ? "skipped" : "done";
    else if (index === poseIndex && running) dot.dataset.state = phase === "pose" ? "current" : "waiting";
    return dot;
  }));
}

function showPose() {
  const pose = selectedPoses[poseIndex];
  if (!pose) return finishGame();
  phase = "pose";
  holdStarted = null;
  el("hold-progress").style.width = "0%";
  el("pose-count").textContent = `${poseIndex + 1} / ${selectedPoses.length}`;
  el("pose-name").textContent = pose.name;
  el("pose-cue").textContent = pose.cue;
  el("pose-card").hidden = false;
  el("camera-status").textContent = "Find a comfortable version of the shape";
  renderProgress();
  say(`${pose.name}. ${pose.cue}.`);
  setRobotFace(pose.face);
}

async function transitionToNextPose(message = "Take a moment before the next shape") {
  if (!running) return;
  if (poseIndex >= selectedPoses.length) return finishGame();
  phase = "transition";
  holdStarted = null;
  el("pose-card").hidden = true;
  el("checkin-card").hidden = true;
  const token = flowToken;
  setRobotFace("(-_-)");
  for (let count = TRANSITION_SECONDS; count > 0; count -= 1) {
    if (!running || token !== flowToken) return;
    el("camera-status").textContent = `${message} · ${count}`;
    await delay(900);
  }
  if (running && token === flowToken) showPose();
}

function visible(point) { return (point.visibility ?? 1) > 0.55; }

function confidentPose(points, pose) {
  return [0, 11, 12, 15, 16, 23, 24, ...(pose?.required ?? [])].every((index) => visible(points[index]));
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
  context.globalAlpha = 0.72;
  context.drawImage(video, 0, 0, canvas.width, canvas.height);
  context.globalAlpha = 1;
  if (points) {
    context.strokeStyle = "#83d5c9";
    context.lineWidth = Math.max(3, canvas.width / 220);
    context.lineCap = "round";
    for (const [a, b] of CONNECTIONS) {
      if (!visible(points[a]) || !visible(points[b])) continue;
      context.beginPath();
      context.moveTo(points[a].x * canvas.width, points[a].y * canvas.height);
      context.lineTo(points[b].x * canvas.width, points[b].y * canvas.height);
      context.stroke();
    }
    context.fillStyle = "#f8fffd";
    for (const index of [0, 11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28]) {
      if (!visible(points[index])) continue;
      context.beginPath();
      context.arc(points[index].x * canvas.width, points[index].y * canvas.height, context.lineWidth * 1.2, 0, Math.PI * 2);
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
    if (lastPoints && confidentPose(lastPoints, selectedPoses[poseIndex])) trackingHits += 1;
  }
  drawCamera(lastPoints);
  if (phase === "pose") updatePose(lastPoints);
  frameRequest = requestAnimationFrame(detectFrame);
}

function updatePose(points) {
  const pose = selectedPoses[poseIndex];
  if (!pose || !points || !confidentPose(points, pose)) {
    holdStarted = null;
    el("hold-progress").style.width = "0%";
    el("camera-status").textContent = pose?.required?.length ? "Move back until your whole body is visible" : "Move back until your upper body is visible";
    return;
  }
  const shoulderWidth = distance(points[11], points[12]);
  if (shoulderWidth < 0.04 || !pose.matches(points, shoulderWidth)) {
    holdStarted = null;
    el("hold-progress").style.width = "0%";
    el("camera-status").textContent = "Take your time finding the shape";
    return;
  }
  el("camera-status").textContent = "That’s it — stay there comfortably";
  if (holdStarted === null) holdStarted = performance.now();
  const held = performance.now() - holdStarted;
  el("hold-progress").style.width = `${Math.min(100, held / HOLD_MS * 100)}%`;
  if (held >= HOLD_MS) completePose();
}

function completePose() {
  if (phase !== "pose") return;
  phase = "transition";
  const finishedIndex = poseIndex;
  const pose = selectedPoses[poseIndex];
  completed.push(pose.key);
  poseIndex += 1;
  holdStarted = null;
  renderProgress();
  say("Got it. Take a moment.");
  setRobotFace("(^o^)");
  if (checkInAfter === finishedIndex) window.setTimeout(() => beginCheckIn(pose), 1100);
  else window.setTimeout(() => transitionToNextPose(), 900);
}

function beginCheckIn(pose) {
  if (!running) return;
  phase = "checkin";
  activeQuestion = CHECK_IN_QUESTIONS[Math.floor(Math.random() * CHECK_IN_QUESTIONS.length)];
  el("pose-card").hidden = true;
  el("checkin-question").textContent = activeQuestion;
  el("checkin-answer").value = "";
  el("checkin-status").textContent = "";
  el("checkin-reply").hidden = true;
  el("checkin-send").disabled = false;
  el("checkin-listen").disabled = false;
  el("checkin-continue").textContent = "Continue";
  el("checkin-card").hidden = false;
  el("camera-status").textContent = `${pose.name} complete`;
  setRobotFace("(o_o)");
  say(activeQuestion);
}

function stopRecognition() {
  try { recognition?.stop(); } catch { /* It may already be stopped. */ }
  recognition = null;
}

function listenForCheckIn() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    el("checkin-status").textContent = "Speech input is not available here. You can type instead.";
    return;
  }
  stopRecognition();
  recognition = new Recognition();
  recognition.lang = navigator.language || "en-US";
  recognition.interimResults = true;
  recognition.continuous = false;
  recognition.onstart = () => {
    el("checkin-listen").disabled = true;
    el("checkin-status").textContent = "Listening…";
  };
  recognition.onresult = (event) => {
    el("checkin-answer").value = Array.from(event.results).map((result) => result[0].transcript).join(" ");
    el("checkin-status").textContent = event.results[event.results.length - 1].isFinal
      ? "Transcript ready. Check it, then press Respond." : "Listening…";
  };
  recognition.onerror = (event) => {
    el("checkin-status").textContent = event.error === "not-allowed"
      ? "Microphone access was not allowed. You can type instead."
      : "I couldn’t catch that. You can try again or type instead.";
  };
  recognition.onend = () => { el("checkin-listen").disabled = false; };
  recognition.start();
}

async function sendCheckIn() {
  const answer = el("checkin-answer").value.trim();
  if (!answer) {
    el("checkin-status").textContent = "Add a short answer first, or skip this check-in.";
    return;
  }
  stopRecognition();
  el("checkin-send").disabled = true;
  el("checkin-listen").disabled = true;
  el("checkin-status").textContent = "Putting that into words…";
  try {
    const reply = await api("/api/check-in", { question: activeQuestion, answer });
    checkIns.push({
      question: activeQuestion, answer, response: reply.response,
      response_source: reply.response_source, safety_pause: reply.safety_pause,
    });
    safetyPaused = reply.safety_pause;
    el("checkin-reply-text").textContent = reply.response;
    el("checkin-reply").hidden = false;
    el("checkin-status").textContent = "";
    if (safetyPaused) {
      pauseCameraForSafety();
      el("checkin-continue").textContent = "End run";
    }
    say(reply.response);
  } catch (error) {
    el("checkin-status").textContent = error.message;
    el("checkin-send").disabled = false;
    el("checkin-listen").disabled = false;
  }
}

function continueAfterCheckIn() {
  el("checkin-card").hidden = true;
  if (safetyPaused) finishGame();
  else transitionToNextPose("Next shape when you’re ready");
}

function skipCheckIn() {
  stopRecognition();
  el("checkin-card").hidden = true;
  transitionToNextPose("Next shape when you’re ready");
}

async function startGame() {
  el("game-problem").hidden = true;
  el("game-summary").hidden = true;
  el("game-save-result").textContent = "";
  el("game-save").disabled = false;
  el("game-start").disabled = true;
  try {
    await loadTracker();
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: { ideal: 960 }, height: { ideal: 720 } }, audio: false,
    });
    const video = el("game-video");
    video.srcObject = stream;
    await video.play();
  } catch (error) {
    el("game-start").disabled = false;
    el("camera-status").textContent = "Camera off";
    el("game-problem").textContent = error.name === "NotAllowedError"
      ? "Camera access was not allowed. You can change that in the browser’s camera settings."
      : `The camera could not start: ${error.message}`;
    el("game-problem").hidden = false;
    return;
  }

  selectedPoses = shuffle(availablePoses()).slice(0, POSES_PER_RUN);
  checkInAfter = el("game-checkins").checked ? Math.floor(Math.random() * (selectedPoses.length - 1)) : null;
  running = true;
  phase = "transition";
  flowToken += 1;
  poseIndex = 0;
  completed = [];
  skipped = [];
  checkIns = [];
  safetyPaused = false;
  trackingSamples = 0;
  trackingHits = 0;
  lastPoints = null;
  sessionStarted = Date.now();
  el("camera-empty").hidden = true;
  el("camera-shell").dataset.state = "playing";
  el("game-skip").disabled = false;
  el("game-stop").disabled = false;
  renderProgress();
  detectFrame();
  say("We’ll start slowly.");
  transitionToNextPose("Starting gently");
}

function pauseCameraForSafety() {
  running = false;
  flowToken += 1;
  if (frameRequest) cancelAnimationFrame(frameRequest);
  stream?.getTracks().forEach((track) => track.stop());
  stream = null;
  el("game-skip").disabled = true;
  el("game-stop").disabled = true;
  el("camera-status").textContent = "Run paused";
}

function stopCamera() {
  running = false;
  phase = "idle";
  flowToken += 1;
  if (frameRequest) cancelAnimationFrame(frameRequest);
  stream?.getTracks().forEach((track) => track.stop());
  stream = null;
  stopRecognition();
  speechSynthesis?.cancel();
  el("game-start").disabled = false;
  el("game-skip").disabled = true;
  el("game-stop").disabled = true;
  el("pose-card").hidden = true;
  el("checkin-card").hidden = true;
  el("camera-shell").dataset.state = "idle";
  el("camera-status").textContent = "Camera off";
}

function finishGame() {
  const duration = Math.max(1, Math.round((Date.now() - sessionStarted) / 1000));
  stopCamera();
  renderProgress();
  el("summary-copy").textContent = `${completed.length} shapes completed in ${duration} seconds${skipped.length ? `, ${skipped.length} skipped` : ""}.`;
  el("game-summary").dataset.duration = duration;
  el("game-summary").hidden = false;
  say(`Run complete. ${completed.length} shapes completed.`);
  setRobotFace("(^_^)");
}

function skipPose() {
  if (!running || phase !== "pose") return;
  phase = "transition";
  skipped.push(selectedPoses[poseIndex].key);
  poseIndex += 1;
  renderProgress();
  transitionToNextPose("No problem. Next shape");
}

async function saveSession() {
  const selected = document.querySelector('input[name="game-feeling"]:checked');
  const payload = {
    game: "salus_says",
    mode: document.querySelector('input[name="movement-mode"]:checked').value,
    duration_seconds: Number(el("game-summary").dataset.duration),
    movements_offered: selectedPoses.map((pose) => pose.key),
    movements_completed: completed,
    movements_skipped: skipped,
    tracking_quality: trackingSamples ? trackingHits / trackingSamples : 0,
    feeling: selected?.value ?? null,
    check_ins: checkIns,
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

for (const input of document.querySelectorAll('input[name="movement-mode"]')) input.addEventListener("change", renderPoseLibrary);
el("game-start").addEventListener("click", startGame);
el("game-skip").addEventListener("click", skipPose);
el("game-stop").addEventListener("click", finishGame);
el("game-save").addEventListener("click", saveSession);
el("checkin-listen").addEventListener("click", listenForCheckIn);
el("checkin-send").addEventListener("click", sendCheckIn);
el("checkin-skip").addEventListener("click", skipCheckIn);
el("checkin-continue").addEventListener("click", continueAfterCheckIn);

renderPoseLibrary();
renderProgress();
