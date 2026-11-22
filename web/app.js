const thread = document.getElementById("thread");
const logEl = document.getElementById("log");
const weatherEl = document.getElementById("weather");
const callsignsEl = document.getElementById("callsigns");
const clockEl = document.getElementById("clock");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");

const params = new URLSearchParams(location.search);
let sessionId = params.get("session_id") || localStorage.getItem("duty_session") || "";
let busy = false;

function addBubble(role, text) {
  const div = document.createElement("div");
  div.className = "bubble " + role;
  if (role === "duty") {
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = "Дежурный";
    div.appendChild(who);
  }
  const body = document.createElement("div");
  body.className = "txt";
  body.textContent = text;
  div.appendChild(body);
  thread.appendChild(div);
  thread.scrollTop = thread.scrollHeight;
  return body;
}

function addTool(name, status) {
  const div = document.createElement("div");
  div.className = "tool" + (status === "start" ? " on" : "");
  div.textContent = status === "start" ? "→ " + name : "✓ " + name;
  thread.appendChild(div);
  thread.scrollTop = thread.scrollHeight;
}

function renderLog(entries) {
  if (!entries || !entries.length) {
    logEl.innerHTML = '<div class="empty">Пока пусто. Дежурный пишет сюда сам.</div>';
    return;
  }
  logEl.innerHTML = "";
  for (const e of entries) {
    const row = document.createElement("div");
    row.className = "log-row";
    row.innerHTML =
      '<span class="t">' +
      (e.time || "") +
      "</span> <span class=\"k\">" +
      (e.kind || "") +
      "</span> " +
      (e.text || "");
    logEl.appendChild(row);
  }
  logEl.scrollTop = logEl.scrollHeight;
}

function renderCallsigns(rows) {
  callsignsEl.innerHTML = "";
  for (const row of rows || []) {
    const d = document.createElement("div");
    d.className = "callsign";
    d.innerHTML = "<b>" + row.name + "</b><br><span>" + row.about + "</span>";
    callsignsEl.appendChild(d);
  }
}

async function loadStatus() {
  const q = sessionId ? "?session_id=" + encodeURIComponent(sessionId) : "";
  const st = await fetch("/api/status" + q).then((r) => r.json());
  sessionId = st.session_id;
  localStorage.setItem("duty_session", sessionId);
  clockEl.textContent = st.clock;
  weatherEl.textContent = st.weather || "нет запроса";
  renderCallsigns(st.callsigns);
  renderLog(st.log);
  if ((!thread.children.length) && st.transcript && st.transcript.length) {
    for (const m of st.transcript) {
      addBubble(m.role === "user" ? "user" : "duty", m.text);
    }
  }
}

async function send(text) {
  if (busy) return;
  text = (text || input.value || "").trim();
  if (!text) return;
  busy = true;
  sendBtn.disabled = true;
  input.value = "";
  addBubble("user", text);
  const dutyBody = addBubble("duty", "…");
  let acc = "";
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: sessionId }),
    });
    const sid = res.headers.get("X-Session-Id");
    if (sid) {
      sessionId = sid;
      localStorage.setItem("duty_session", sid);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const chunks = buf.split("\n\n");
      buf = chunks.pop();
      for (const chunk of chunks) {
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        let ev;
        try {
          ev = JSON.parse(line.slice(6));
        } catch {
          continue;
        }
        if (ev.type === "token") {
          acc += ev.text;
          dutyBody.textContent = acc;
          thread.scrollTop = thread.scrollHeight;
        } else if (ev.type === "tool") {
          const n = ev.name || "";
          if (n.startsWith("call-")) continue;
          addTool(n, ev.status);
        } else if (ev.type === "log") {
          const st = await fetch(
            "/api/status?session_id=" + encodeURIComponent(sessionId)
          ).then((r) => r.json());
          renderLog(st.log);
        } else if (ev.type === "board" && ev.weather) {
          weatherEl.textContent = ev.weather;
        } else if (ev.type === "error") {
          acc = acc || "сбой: " + ev.message;
          dutyBody.textContent = acc;
        } else if (ev.type === "done" && !acc && ev.final) {
          dutyBody.textContent = ev.final;
        }
      }
    }
    if (!dutyBody.textContent || dutyBody.textContent === "…") {
      dutyBody.textContent = acc || "тишина";
    }
  } catch (err) {
    dutyBody.textContent = "связь с пультом пропала: " + err;
  }
  busy = false;
  sendBtn.disabled = false;
  input.focus();
}

document.getElementById("send").onclick = () => send();
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter") send();
});
document.getElementById("reset").onclick = async () => {
  await fetch("/api/reset?session_id=" + encodeURIComponent(sessionId), {
    method: "POST",
  });
  thread.innerHTML = "";
  renderLog([]);
  weatherEl.textContent = "нет запроса";
  input.focus();
};
document.querySelectorAll(".hints button").forEach((b) => {
  b.onclick = () => send(b.getAttribute("data-q"));
});

loadStatus().catch((err) => {
  weatherEl.textContent = "пульт ещё не поднялся: " + err;
});
