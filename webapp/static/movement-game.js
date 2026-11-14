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
    key: "wave", name: "Wave hello", cue: "Raise either hand and give Salus a small wave", face: "(^_^)/",
    motion: "wave",
  },
  {
    key: "both_up", name: "Both hands up", cue: "Lift both hands above your shoulders", face: "\\(^o^)/",
    matches: (p) => p[15].y < p[11].y && p[16].y < p[12].y,
  },
  {
    key: "together", name: "Hands together", cue: "Bring your hands together in front of you", face: "(._.)",
    matches: (p, s) => distance(p[15], p[16]) < s * 0.7,
  },
  {
    key: "left_up", name: "Left screen hand", cue: "Lift the hand on the left of the screen", face: "(^_^)/",
    matches: (p) => p[16].y < p[12].y && p[15].y > p[11].y,
  },
  {
    key: "right_up", name: "Right screen hand", cue: "Lift the hand on the right of the screen", face: "\\(^_^)",
    matches: (p) => p[15].y < p[11].y && p[16].y > p[12].y,
  },
  {
    key: "hands_shoulders", name: "Touch shoulders", cue: "Bring each hand close to its shoulder", face: "(o_o)",
    matches: (p, s) => distance(p[15], p[11]) < s * 0.9 && distance(p[16], p[12]) < s * 0.9,
  },
  {
    key: "left_cross", name: "Cross one hand", cue: "Move the hand on the left of the screen to the opposite shoulder", face: "(^.-)",
    matches: (p, s) => distance(p[16], p[12]) < s * 0.9,
  },
  {
    key: "right_cross", name: "Cross the other hand", cue: "Move the hand on the right of the screen to the opposite shoulder", face: "(-.^)",
    matches: (p, s) => distance(p[15], p[11]) < s * 0.9,
  },
  {
    key: "crossed", name: "Cross both hands", cue: "Bring both hands toward the opposite shoulders", face: "(-_-)",
    matches: (p, s) => distance(p[15], p[12]) < s * 1.05 && distance(p[16], p[11]) < s * 1.05,
  },
  {
    key: "frame_face", name: "Frame your face", cue: "Bring both hands up beside your face", face: "\\(o_o)/",
    required: [7, 8],
    matches: (p, s) => distance(p[15], p[7]) < s * 1.1 && distance(p[16], p[8]) < s * 1.1,
  },
  {
    key: "left_side", name: "Hand out left", cue: "Move one hand toward the left edge of the screen", face: "<(^_^)",
    matches: (p, s) => level(p[16], p[12], s * 0.65) && p[16].x < p[12].x - s * 1.1 && p[15].y > p[11].y,
  },
  {
    key: "right_side", name: "Hand out right", cue: "Move one hand toward the right edge of the screen", face: "(^_)>",
    matches: (p, s) => level(p[15], p[11], s * 0.65) && p[15].x > p[11].x + s * 1.1 && p[16].y > p[12].y,
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
let waveSamples = [];
let recognitionAttempts = 0;
let recognitionHandled = false;

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

function speak(text, force = false) {
  return new Promise((resolve) => {
    if ((!force && !el("game-voice").checked) || !("speechSynthesis" in window)) {
      resolve();
      return;
    }
    speechSynthesis.cancel();
    const message = new SpeechSynthesisUtterance(text);
    message.rate = 0.9;
    message.pitch = 1;
    message.onend = resolve;
    message.onerror = resolve;
    speechSynthesis.speak(message);
  });
}

function say(text) { void speak(text); }

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
  return POSES;
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
  waveSamples = [];
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

async function transitionToNextPose(message = "Take a moment before the next signal") {
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
  return [0, 11, 12, 13, 14, 15, 16, ...(pose?.required ?? [])].every((index) => visible(points[index]));
}

function waveMatches(points, shoulderWidth) {
  const wrist = points[15].y < points[11].y ? points[15] : points[16].y < points[12].y ? points[16] : null;
  if (!wrist) {
    waveSamples = [];
    return false;
  }
  const now = performance.now();
  waveSamples.push({ at: now, x: wrist.x });
  waveSamples = waveSamples.filter((sample) => now - sample.at < 1400);
  const positions = waveSamples.map((sample) => sample.x);
  return positions.length >= 5 && Math.max(...positions) - Math.min(...positions) > shoulderWidth * 0.75;
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
    el("camera-status").textContent = "Keep your hands and shoulders in view";
    return;
  }
  const shoulderWidth = distance(points[11], points[12]);
  const matched = pose.motion === "wave" ? waveMatches(points, shoulderWidth) : pose.matches(points, shoulderWidth);
  if (shoulderWidth < 0.04 || !matched) {
    holdStarted = null;
    el("hold-progress").style.width = "0%";
    el("camera-status").textContent = "Take your time finding the shape";
    return;
  }
  if (pose.motion === "wave") {
    el("hold-progress").style.width = "100%";
    completePose();
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

async function beginCheckIn(pose) {
  if (!running) return;
  phase = "checkin";
  recognitionAttempts = 0;
  recognitionHandled = false;
  activeQuestion = CHECK_IN_QUESTIONS[Math.floor(Math.random() * CHECK_IN_QUESTIONS.length)];
  el("pose-card").hidden = true;
  el("checkin-question").textContent = activeQuestion;
  el("checkin-transcript").textContent = "";
  el("checkin-transcript").hidden = true;
  el("checkin-status").textContent = "Salus is asking…";
  el("checkin-reply").hidden = true;
  el("checkin-card").hidden = false;
  el("camera-status").textContent = `${pose.name} complete`;
  setRobotFace("(o_o)");
  await speak(activeQuestion, true);
  if (running && phase === "checkin") listenForCheckIn();
}

function stopRecognition() {
  try { recognition?.stop(); } catch { /* It may already be stopped. */ }
  recognition = null;
}

function listenForCheckIn() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    moveOnWithoutAnswer("Speech recognition is not available here, so we’ll keep going.");
    return;
  }
  stopRecognition();
  recognitionAttempts += 1;
  recognitionHandled = false;
  recognition = new Recognition();
  recognition.lang = navigator.language || "en-US";
  recognition.interimResults = true;
  recognition.continuous = false;
  recognition.onstart = () => {
    el("checkin-status").textContent = "Listening — answer aloud";
  };
  recognition.onresult = (event) => {
    const answer = Array.from(event.results).map((result) => result[0].transcript).join(" ").trim();
    el("checkin-transcript").textContent = `“${answer}”`;
    el("checkin-transcript").hidden = !answer;
    if (event.results[event.results.length - 1].isFinal && answer && !recognitionHandled) {
      recognitionHandled = true;
      sendCheckIn(answer);
    }
  };
  recognition.onerror = (event) => {
    if (recognitionHandled) return;
    recognitionHandled = true;
    if (event.error === "not-allowed" || event.error === "service-not-allowed") {
      moveOnWithoutAnswer("Microphone access was not available, so we’ll keep going.");
    } else {
      retryOrMoveOn();
    }
  };
  recognition.onend = () => {
    if (!recognitionHandled && phase === "checkin") {
      recognitionHandled = true;
      retryOrMoveOn();
    }
  };
  recognition.start();
}

function retryOrMoveOn() {
  if (recognitionAttempts < 2) {
    el("checkin-status").textContent = "I didn’t catch that. Listening once more…";
    window.setTimeout(listenForCheckIn, 1200);
  } else {
    moveOnWithoutAnswer("I didn’t catch that, so we’ll keep going.");
  }
}

async function moveOnWithoutAnswer(message) {
  if (phase !== "checkin") return;
  recognitionHandled = true;
  stopRecognition();
  el("checkin-status").textContent = message;
  await speak(message, true);
  await delay(1200);
  if (running && phase === "checkin") {
    el("checkin-card").hidden = true;
    transitionToNextPose("Next signal when you’re ready");
  }
}

async function sendCheckIn(answer) {
  stopRecognition();
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
    }
    await speak(reply.response, true);
    await delay(1400);
    el("checkin-card").hidden = true;
    if (safetyPaused) finishGame();
    else if (running) transitionToNextPose("Next signal when you’re ready");
  } catch (error) {
    moveOnWithoutAnswer("I couldn’t make a reply just now, so we’ll keep going.");
  }
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
  const signalNoun = completed.length === 1 ? "hand signal" : "hand signals";
  el("summary-copy").textContent = `${completed.length} ${signalNoun} completed in ${duration} seconds${skipped.length ? `, ${skipped.length} skipped` : ""}.`;
  el("game-summary").dataset.duration = duration;
  el("game-summary").hidden = false;
  say(`Run complete. ${completed.length} ${signalNoun} completed.`);
  setRobotFace("(^_^)");
}

function skipPose() {
  if (!running || phase !== "pose") return;
  phase = "transition";
  skipped.push(selectedPoses[poseIndex].key);
  poseIndex += 1;
  renderProgress();
  transitionToNextPose("No problem. Next signal");
}

async function saveSession() {
  const selected = document.querySelector('input[name="game-feeling"]:checked');
  const payload = {
    game: "hand_signals",
    mode: "hands",
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

el("game-start").addEventListener("click", startGame);
el("game-skip").addEventListener("click", skipPose);
el("game-stop").addEventListener("click", finishGame);
el("game-save").addEventListener("click", saveSession);
renderPoseLibrary();
renderProgress();
