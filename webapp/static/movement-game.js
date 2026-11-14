import { FilesetResolver, GestureRecognizer } from "./vendor/mediapipe/vision_bundle.mjs";

const el = (id) => document.getElementById(id);
const HOLD_MS = 1600;
const TRANSITION_SECONDS = 3;
const POSES_PER_RUN = 5;
const CONNECTIONS = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],[10,11],[11,12],[9,13],[13,14],[14,15],[15,16],[13,17],[17,18],[18,19],[19,20],[0,17]];

const POSES = [
  {key:"Open_Palm", name:"Open palm", cue:"Show one open hand to the camera", face:"(^_^)/"},
  {key:"Closed_Fist", name:"Make a fist", cue:"Close one hand into a comfortable fist", face:"(._.)"},
  {key:"Pointing_Up", name:"Point up", cue:"Hold up one index finger", face:"(o_o)^"},
  {key:"Thumb_Up", name:"Thumbs up", cue:"Show one thumbs-up", face:"(^_^)b"},
  {key:"Victory", name:"Two fingers", cue:"Show a two-finger victory sign", face:"(^_^)v"},
  {key:"ILoveYou", name:"Three-finger sign", cue:"Show thumb, index finger and little finger", face:"\\m/(^_^)"},
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
let activeQuestionId = null;
let safetyPaused = false;
let trackingSamples = 0;
let trackingHits = 0;
let flowToken = 0;
let waveSamples = [];
let recognitionAttempts = 0;
let recognitionHandled = false;
let speechFaceTimer = null;
let voiceController = null;
let voiceAudio = null;
let voiceObjectUrl = null;
let voiceFinish = null;

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

async function getJson(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

async function setRobotFace(text, colour = "#1a6964") {
  try { await api("/api/face", { text, colour }); } catch { /* The game also works without the robot. */ }
}

async function robotCommand(key, value = null) {
  try { await api("/api/command", {key, value}); } catch { /* Optional physical feedback. */ }
}

function robotSuccessCue(face = "(^o^)") {
  return Promise.allSettled([
    setRobotFace(face, "#58e39a"),
    robotCommand("spk_play_rtttl", "ok:d=16,o=6,b=170:c,e,g"),
    robotCommand("led_animation", {r:72,g:220,b:142}),
  ]);
}

function stopSpeechFace(restFace = "(^_^)") {
  if (speechFaceTimer) window.clearInterval(speechFaceTimer);
  speechFaceTimer = null;
  setRobotFace(restFace);
}

function startSpeechFace() {
  if (speechFaceTimer) window.clearInterval(speechFaceTimer);
  let mouthOpen = false;
  const draw = () => {
    mouthOpen = !mouthOpen;
    setRobotFace(mouthOpen ? "(^o^)" : "(^-^)", "#58e39a");
  };
  draw();
  speechFaceTimer = window.setInterval(draw, 520);
}

function cancelSpeechPlayback() {
  voiceController?.abort();
  voiceController = null;
  voiceAudio?.pause();
  voiceAudio = null;
  if (voiceObjectUrl) URL.revokeObjectURL(voiceObjectUrl);
  voiceObjectUrl = null;
  if (voiceFinish) {
    const finish = voiceFinish;
    voiceFinish = null;
    finish();
  }
  speechSynthesis?.cancel();
}

function browserSpeak(text, restFace) {
  return new Promise((resolve) => {
    if (!("speechSynthesis" in window)) {
      resolve();
      return;
    }
    const message = new SpeechSynthesisUtterance(text);
    message.rate = 0.9;
    message.pitch = 1;
    message.onstart = startSpeechFace;
    message.onend = () => { stopSpeechFace(restFace); resolve(); };
    message.onerror = () => { stopSpeechFace(restFace); resolve(); };
    speechSynthesis.speak(message);
  });
}

async function elevenLabsSpeak(text, restFace) {
  const controller = new AbortController();
  voiceController = controller;
  const response = await fetch("/api/voice", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({text}),
    signal: controller.signal,
  });
  if (!response.ok) throw new Error("Cloud voice unavailable");
  const blob = await response.blob();
  if (controller.signal.aborted) return;
  voiceObjectUrl = URL.createObjectURL(blob);
  const audio = new Audio(voiceObjectUrl);
  voiceAudio = audio;
  await new Promise((resolve, reject) => {
    let settled = false;
    const finish = (failed = false) => {
      if (settled) return;
      settled = true;
      if (voiceAudio === audio) voiceAudio = null;
      if (voiceController === controller) voiceController = null;
      if (voiceObjectUrl) URL.revokeObjectURL(voiceObjectUrl);
      voiceObjectUrl = null;
      voiceFinish = null;
      stopSpeechFace(restFace);
      if (failed) reject(new Error("Cloud voice playback failed"));
      else resolve();
    };
    voiceFinish = () => finish(false);
    audio.onended = () => finish(false);
    audio.onerror = () => finish(true);
    startSpeechFace();
    el("game-voice-provider").textContent = "Playing ElevenLabs voice…";
    audio.play().catch(() => finish(true));
  });
}

async function speak(text, force = false, restFace = "(^_^)" ) {
  if (!force && !el("game-voice").checked) return "off";
  cancelSpeechPlayback();
  try {
    await elevenLabsSpeak(text, restFace);
    el("game-voice-provider").textContent = "ElevenLabs voice played";
    return "elevenlabs";
  } catch (error) {
    if (error.name === "AbortError") return;
    console.warn("ElevenLabs voice failed; using browser voice.", error);
    await browserSpeak(text, restFace);
    el("game-voice-provider").textContent = "Browser voice fallback";
    return "browser";
  }
}

function say(text) { void speak(text); }

async function testVoice() {
  const button = el("game-test-voice");
  button.disabled = true;
  el("game-voice-provider").textContent = "Loading ElevenLabs voice…";
  await speak("Hello. This is the Salus movement voice.", true);
  button.disabled = false;
}

async function loadVoiceStatus() {
  try {
    const status = await getJson("/api/voice/status");
    el("game-voice-provider").textContent = status.configured
      ? "ElevenLabs voice configured"
      : "Browser voice fallback";
  } catch {
    el("game-voice-provider").textContent = "Voice status unavailable";
  }
}

async function loadTracker() {
  if (landmarker) return;
  el("camera-status").textContent = "Loading movement tracker…";
  const vision = await FilesetResolver.forVisionTasks("./vendor/mediapipe/wasm");
  const options = {baseOptions:{modelAssetPath:"./models/gesture_recognizer.task", delegate:"GPU"}, runningMode:"VIDEO", numHands:2,
    minHandDetectionConfidence:0.45, minHandPresenceConfidence:0.45, minTrackingConfidence:0.45};
  try { landmarker = await GestureRecognizer.createFromOptions(vision, options); }
  catch { options.baseOptions.delegate = "CPU"; landmarker = await GestureRecognizer.createFromOptions(vision, options); }
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

function drawCamera(hands) {
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
  for (const points of hands ?? []) {
    context.strokeStyle = "#83d5c9";
    context.lineWidth = Math.max(3, canvas.width / 220);
    context.lineCap = "round";
    for (const [a, b] of CONNECTIONS) {
      context.beginPath();
      context.moveTo(points[a].x * canvas.width, points[a].y * canvas.height);
      context.lineTo(points[b].x * canvas.width, points[b].y * canvas.height);
      context.stroke();
    }
    context.fillStyle = "#f8fffd";
    for (let index = 0; index < points.length; index += 1) {
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
    const result = landmarker.recognizeForVideo(video, performance.now());
    lastPoints = result.landmarks ?? [];
    const labels = (result.gestures ?? []).map((items) => items[0]).filter((item) => item && item.score >= 0.55).map((item) => item.categoryName);
    lastPoints.labels = labels;
    trackingSamples += 1;
    if (lastPoints.length) trackingHits += 1;
  }
  drawCamera(lastPoints);
  if (phase === "pose") updatePose(lastPoints);
  frameRequest = requestAnimationFrame(detectFrame);
}

function updatePose(points) {
  const pose = selectedPoses[poseIndex];
  if (!pose || !points?.length) {
    holdStarted = null;
    el("hold-progress").style.width = "0%";
    el("camera-status").textContent = "Bring one hand into the camera view";
    return;
  }
  if (!points.labels?.includes(pose.key)) {
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
  robotSuccessCue();
  if (checkInAfter === finishedIndex) window.setTimeout(() => beginCheckIn(pose), 1100);
  else window.setTimeout(() => transitionToNextPose(), 900);
}

async function beginCheckIn(pose) {
  if (!running) return;
  phase = "checkin";
  recognitionAttempts = 0;
  recognitionHandled = false;
  try {
    const picked = await getJson("/api/check-in/question");
    activeQuestion = picked.text;
    activeQuestionId = picked.id;
  } catch {
    activeQuestion = "What pace feels right from here?";
    activeQuestionId = null;
  }
  el("pose-card").hidden = true;
  el("checkin-question").textContent = activeQuestion;
  el("checkin-transcript").textContent = "";
  el("checkin-transcript").hidden = true;
  el("checkin-status").textContent = "Salus is asking…";
  el("checkin-reply").hidden = true;
  el("checkin-card").hidden = false;
  el("camera-status").textContent = `${pose.name} complete`;
  setRobotFace("(o_o)");
  await speak(activeQuestion, true, "(o_o)");
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
    const reply = await api("/api/check-in", { question_id: activeQuestionId, question: activeQuestion, answer });
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
  cancelSpeechPlayback();
  stopSpeechFace("(^_^)");
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
  robotSuccessCue("(^_^)");
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

const viewTabs = [...document.querySelectorAll("[data-view]")];

function activateView(tab, moveFocus = false) {
  const view = tab.dataset.view;
  el("play-view").hidden = view !== "play";
  el("garden-view").hidden = view !== "garden";
  el("stage").hidden = view !== "workshop";
  el("commands-note").hidden = view !== "workshop";
  for (const item of viewTabs) {
    const active = item === tab;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
    item.tabIndex = active ? 0 : -1;
  }
  if (moveFocus) tab.focus();
  if (view === "garden") el("garden-world").focus({preventScroll:true});
  if (view !== "play" && running) stopCamera();
}

for (const [index, tab] of viewTabs.entries()) {
  tab.addEventListener("click", () => activateView(tab));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? viewTabs.length - 1
      : (index + (event.key === "ArrowRight" ? 1 : -1) + viewTabs.length) % viewTabs.length;
    activateView(viewTabs[next], true);
  });
}

el("game-start").addEventListener("click", startGame);
el("game-test-voice").addEventListener("click", testVoice);
el("game-skip").addEventListener("click", skipPose);
el("game-stop").addEventListener("click", finishGame);
el("game-save").addEventListener("click", saveSession);
for (const id of ["game-voice", "game-checkins"]) {
  el(id).addEventListener("change", () => api("/api/settings", {
    spoken_guidance: el("game-voice").checked,
    spoken_check_ins: el("game-checkins").checked,
  }).catch(() => {}));
}
getJson("/api/settings").then((settings) => {
  el("game-voice").checked = settings.spoken_guidance;
  el("game-checkins").checked = settings.spoken_check_ins;
}).catch(() => {});
loadVoiceStatus();
renderPoseLibrary();
renderProgress();
