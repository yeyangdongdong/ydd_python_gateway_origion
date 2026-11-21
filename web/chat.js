"use strict";

const TOKEN_KEY = "ydd_gateway_token";
const SESSION_KEY = "ydd_gateway_session";

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  sessionId: localStorage.getItem(SESSION_KEY) || "",
  busy: false,
};

const el = {
  sessionLabel: document.getElementById("session-label"),
  newSession: document.getElementById("new-session"),
  token: document.getElementById("token"),
  saveToken: document.getElementById("save-token"),
  authNote: document.getElementById("auth-note"),
  messages: document.getElementById("messages"),
  composer: document.getElementById("composer"),
  input: document.getElementById("input"),
  send: document.getElementById("send"),
  status: document.getElementById("status"),
};

function setStatus(text, kind) {
  el.status.textContent = text;
  el.status.dataset.kind = kind || "";
}

function errorMessage(payload, status) {
  return (payload && payload.error && payload.error.message) || `请求失败（HTTP ${status}）`;
}

/** 统一的 HTTP 调用：带上 token，失败时抛出带 status 的 Error。 */
async function request(path, options) {
  const settings = options || {};
  const headers = { "Content-Type": "application/json" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { method: settings.method || "GET",
                                        headers, body: settings.body });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(errorMessage(payload, response.status));
    error.status = response.status;
    throw error;
  }
  return payload;
}

/** 追加一条纯文本气泡；不使用 innerHTML，避免把模型输出当 HTML 执行。 */
function addBubble(role, text) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  bubble.textContent = text;
  el.messages.append(bubble);
  el.messages.scrollTop = el.messages.scrollHeight;
  return bubble;
}

function appendText(bubble, text) {
  bubble.textContent += text;
  el.messages.scrollTop = el.messages.scrollHeight;
}

/** 开始提问后，把“新会话已就绪”这类提示气泡清掉，保持对话区干净。 */
function clearHints() {
  for (const hint of el.messages.querySelectorAll(".bubble.hint")) hint.remove();
}

function showSession() {
  el.sessionLabel.textContent = `会话：${state.sessionId}`;
}

/** 复用已保存的会话；服务端数据被清理过（404）时自动新建一个。 */
async function ensureSession() {
  if (state.sessionId) {
    try {
      await request(`/sessions/${state.sessionId}`);
      showSession();
      return state.sessionId;
    } catch (error) {
      if (error.status !== 404) throw error;
      state.sessionId = "";
      localStorage.removeItem(SESSION_KEY);
    }
  }
  const session = await request("/sessions", {
    method: "POST",
    body: JSON.stringify({ title: "浏览器会话" }),
  });
  state.sessionId = session.id;
  localStorage.setItem(SESSION_KEY, session.id);
  showSession();
  return state.sessionId;
}

async function loadHistory() {
  el.messages.textContent = "";
  const rows = await request(`/sessions/${state.sessionId}/messages?limit=200`);
  if (!rows.length) {
    addBubble("hint", "新会话已就绪，输入问题开始对话。");
    return;
  }
  for (const row of rows) addBubble(row.role, row.content);
}

/**
 * 一次提问 = 一张新票据 + 一条新 WebSocket 连接。
 * 票据只能用一次，所以每条消息都重新取票；连接成功后立即发送内容。
 */
async function ask(content) {
  const ticket = await request(`/sessions/${state.sessionId}/ticket`, { method: "POST" });
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const url = `${scheme}://${location.host}/sessions/${state.sessionId}`
            + `/stream?ticket=${encodeURIComponent(ticket.ticket)}`;

  return new Promise((resolve, reject) => {
    const socket = new WebSocket(url);
    const bubble = addBubble("assistant", "");
    let reply = "";
    let settled = false;

    function finish(action, value) {
      if (settled) return;
      settled = true;
      action(value);
    }

    socket.onopen = () => socket.send(JSON.stringify({ content }));

    socket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === "start") {
        setStatus("正在生成…", "busy");
        // 服务端可能为了适配上下文丢掉早期对话，告诉用户一声，免得以为模型失忆。
        if (data.context && data.context.trimmed_messages > 0) {
          const notice = document.createElement("div");
          notice.className = "bubble hint";
          notice.textContent =
            `已省略最早的 ${data.context.trimmed_messages} 条消息，以适配模型上下文。`;
          bubble.before(notice);
        }
      } else if (data.type === "delta") {
        reply += data.content;
        appendText(bubble, data.content);
      } else if (data.type === "done") {
        // 重放成功结果时没有 delta，直接把完整回答放进去。
        if (!reply) appendText(bubble, data.reply || "");
        setStatus(data.replayed ? "这一问之前已经回答过，直接复用了结果" : "完成", "ok");
        socket.close();
        finish(resolve);
      } else if (data.type === "error") {
        appendText(bubble, data.error.message);
        bubble.classList.add("error");
        setStatus("生成失败", "error");
        socket.close();
        finish(reject, new Error(data.error.message));
      }
    };

    socket.onerror = () => finish(reject, new Error("连接失败，请检查服务是否仍在运行。"));

    socket.onclose = () => {
      // 没有收到 done 就断开，说明这一轮没有完整结果，不能当成成功。
      finish(reject, new Error("连接在回答完成前关闭，本轮未完成。"));
    };
  });
}

async function start() {
  el.token.value = state.token;
  try {
    const health = await fetch("/health").then((response) => response.json());
    el.authNote.textContent = health.auth_required
      ? "服务端已启用 token 校验，请填写后保存。"
      : "服务端未设置 token，本机可直接使用。";
    await ensureSession();
    await loadHistory();
    setStatus("就绪", "ok");
  } catch (error) {
    if (error.status === 401) {
      setStatus("token 不正确，请在上方填写后保存。", "error");
      el.token.focus();
    } else {
      setStatus(error.message, "error");
    }
  }
}

el.composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const content = el.input.value.trim();
  if (!content || state.busy) return;
  state.busy = true;
  el.send.disabled = true;
  el.input.value = "";
  clearHints();
  addBubble("user", content);
  try {
    await ask(content);
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    state.busy = false;
    el.send.disabled = false;
    el.input.focus();
  }
});

el.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    el.composer.requestSubmit();
  }
});

el.saveToken.addEventListener("click", () => {
  state.token = el.token.value.trim();
  localStorage.setItem(TOKEN_KEY, state.token);
  start();
});

el.newSession.addEventListener("click", () => {
  state.sessionId = "";
  localStorage.removeItem(SESSION_KEY);
  el.messages.textContent = "";
  start();
});

start();
