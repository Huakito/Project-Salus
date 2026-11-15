const el = (id) => document.getElementById(id);

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const answer = await response.json();
  if (!response.ok) throw new Error(answer.detail || "Something went wrong.");
  return answer;
}

function renderEntry(entry) {
  const article = document.createElement("article");
  article.className = "journal-entry-card";
  const date = new Date(entry.recorded_at);
  const time = document.createElement("time");
  time.textContent = date.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  const body = document.createElement("p");
  body.textContent = entry.body;
  article.append(time, body);
  return article;
}

async function loadEntries() {
  const data = await api("/api/journal");
  el("journal-entries").replaceChildren(...data.entries.map(renderEntry));
}

function message(who, text) {
  const bubble = document.createElement("p");
  bubble.className = `journal-message ${who}`;
  bubble.textContent = text;
  el("journal-messages").append(bubble);
  bubble.scrollIntoView({ block: "nearest" });
}

async function speak(text) {
  try {
    const response = await fetch("/api/voice", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
    if (!response.ok) return;
    const audio = new Audio(URL.createObjectURL(await response.blob()));
    await audio.play();
    await new Promise((resolve) => { audio.onended = resolve; audio.onerror = resolve; });
  } catch { /* Text remains visible if audio is unavailable. */ }
}

el("journal-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = el("journal-entry").value.trim();
  if (!body) return;
  const data = await api("/api/journal", { body });
  el("journal-entry").value = "";
  el("journal-save-status").textContent = "Saved locally.";
  el("journal-entries").prepend(renderEntry(data.entry));
});

let liveChat = false;
let recognition = null;

async function sendMessage(text) {
  if (!text) return;
  message("person", text);
  el("journal-chat-status").textContent = "Salus is thinking…";
  try {
    const reply = await api("/api/journal/chat", { message: text });
    message("salus", reply.response);
    if (reply.follow_up) message("salus", reply.follow_up);
    await speak([reply.response, reply.follow_up].filter(Boolean).join(" "));
    el("journal-chat-status").textContent = reply.calendar_event ? "Calendar updated." : "";
    if (reply.calendar_event) loadCalendar();
  } catch (error) { el("journal-chat-status").textContent = error.message; }
  if (liveChat) startListening();
}

el("journal-chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = el("journal-chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  await sendMessage(text);
});

function startListening() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) { el("journal-chat-status").textContent = "Speech recognition is not available in this browser."; return; }
  recognition?.abort();
  recognition = new Recognition();
  recognition.lang = "en-GB";
  recognition.interimResults = false;
  recognition.onstart = () => { el("journal-chat-status").textContent = "Listening…"; };
  recognition.onresult = (event) => {
    const transcript = event.results[0][0].transcript;
    el("journal-chat-input").value = "";
    sendMessage(transcript);
  };
  recognition.onerror = (event) => {
    if (liveChat && event.error !== "aborted") {
      el("journal-chat-status").textContent = "I didn’t catch that. Listening again…";
      setTimeout(startListening, 500);
    }
  };
  recognition.start();
}

el("journal-mic").addEventListener("click", () => {
  liveChat = !liveChat;
  el("journal-mic").textContent = liveChat ? "Stop live chat" : "Start live chat";
  if (liveChat) startListening();
  else {
    recognition?.abort();
    el("journal-chat-status").textContent = "Live chat stopped.";
  }
});

function renderCalendarEvent(event) {
  const row = document.createElement("div");
  row.className = "calendar-event";
  const date = new Date(event.starts_at);
  const time = document.createElement("time");
  time.textContent = date.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
  const title = document.createElement("span");
  title.textContent = event.title;
  row.append(time, title);
  return row;
}

async function loadCalendar() {
  const data = await api("/api/calendar/events");
  el("journal-calendar-events").replaceChildren(...data.events.slice(0, 8).map(renderCalendarEvent));
  el("google-calendar-status").textContent = data.connected ? "Connected to Google Calendar." : (data.configured ? "Using demo events until you connect Google." : "Google OAuth credentials are needed. Demo events are shown for now.");
  el("google-calendar-connect").textContent = data.connected ? "Disconnect Google" : "Connect Google Calendar";
  el("google-calendar-connect").dataset.connected = String(data.connected);
}

el("google-calendar-connect").addEventListener("click", async () => {
  if (el("google-calendar-connect").dataset.connected === "true") {
    await api("/api/calendar/google/disconnect", {});
    loadCalendar();
  } else {
    window.location.href = "/api/calendar/google/connect";
  }
});

loadEntries();
loadCalendar();
