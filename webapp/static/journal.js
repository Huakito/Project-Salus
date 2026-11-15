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

el("journal-chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = el("journal-chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  message("person", text);
  el("journal-chat-status").textContent = "Salus is thinking…";
  try {
    const reply = await api("/api/journal/chat", { message: text });
    message("salus", reply.response);
    if (reply.follow_up) message("salus", reply.follow_up);
    await speak([reply.response, reply.follow_up].filter(Boolean).join(" "));
    el("journal-chat-status").textContent = reply.calendar_event ? "Calendar updated." : "";
  } catch (error) { el("journal-chat-status").textContent = error.message; }
});

el("journal-mic").addEventListener("click", () => {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) { el("journal-chat-status").textContent = "Speech recognition is not available in this browser."; return; }
  const recognition = new Recognition();
  recognition.lang = "en-GB";
  recognition.interimResults = false;
  recognition.onstart = () => { el("journal-chat-status").textContent = "Listening…"; };
  recognition.onresult = (event) => {
    el("journal-chat-input").value = event.results[0][0].transcript;
    el("journal-chat-form").requestSubmit();
  };
  recognition.onerror = () => { el("journal-chat-status").textContent = "I didn’t catch that."; };
  recognition.start();
});

loadEntries();
