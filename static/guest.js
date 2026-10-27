const state = { kbs: [], kbId: "", sessionId: "" };

const kbList = document.querySelector("#kb-list");
const messages = document.querySelector("#messages");
const errorBox = document.querySelector("#error");
const form = document.querySelector("#ask-form");
const questionInput = document.querySelector("#question");

function showError(error) {
  errorBox.textContent = error ? error.message || String(error) : "";
}

function renderKbs() {
  kbList.innerHTML = "";
  const all = document.createElement("button");
  all.type = "button";
  all.className = "kb-item" + (state.kbId ? "" : " active");
  all.textContent = "全部可访问知识库";
  all.onclick = () => {
    state.kbId = "";
    state.sessionId = "";
    renderKbs();
  };
  kbList.append(all);
  if (!state.kbs.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "还没有公开知识库。管理员可以在管理端创建。";
    kbList.append(empty);
    return;
  }
  for (const kb of state.kbs) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "kb-item" + (state.kbId === kb.id ? " active" : "");
    button.innerHTML = `<strong></strong><small></small>`;
    button.querySelector("strong").textContent = kb.name;
    button.querySelector("small").textContent = kb.description || "公开知识库";
    button.onclick = () => {
      state.kbId = kb.id;
      state.sessionId = "";
      renderKbs();
    };
    kbList.append(button);
  }
}

function addMessage(role, text) {
  const box = document.createElement("article");
  box.className = `msg ${role}`;
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  box.append(paragraph);
  messages.append(box);
  messages.scrollTop = messages.scrollHeight;
  return box;
}

function addCitations(parent, citations) {
  if (!citations?.length) return;
  const wrap = document.createElement("div");
  wrap.className = "cites";
  citations.forEach((item, index) => {
    const card = document.createElement("div");
    card.className = "cite";
    if (item.kind === "faq") {
      card.textContent = `依据：FAQ「${item.question}」`;
    } else {
      card.textContent = `[${index + 1}] ${item.filename} #${item.chunk_index}  ${item.excerpt || ""}`;
    }
    wrap.append(card);
  });
  parent.append(wrap);
}

async function readSse(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop();
    for (const part of parts) {
      let event = "message";
      let data = "";
      for (const line of part.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) onEvent(event, JSON.parse(data));
    }
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = questionInput.value.trim();
  if (!question) return;
  showError(null);
  questionInput.value = "";
  addMessage("user", question);
  const assistant = addMessage("assistant", "");
  const paragraph = assistant.querySelector("p");
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const response = await fetch("/api/v1/qa/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        knowledge_base_id: state.kbId || null,
        session_id: state.sessionId || null,
      }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(typeof body.detail === "string" ? body.detail : "提问失败");
    }
    await readSse(response, (name, data) => {
      if (name === "session") state.sessionId = data.session_id;
      if (name === "chunk") paragraph.textContent += data.text;
      if (name === "citations") addCitations(assistant, data.citations);
      if (name === "error") showError(new Error(data.message));
    });
  } catch (error) {
    showError(error);
    if (!paragraph.textContent) paragraph.textContent = "没有得到回答。";
  } finally {
    button.disabled = false;
  }
});

async function boot() {
  try {
    const health = await fetch("/api/v1/health").then((response) => response.json());
    document.querySelector("#mode").textContent =
      health.llm === "remote" ? "已连接大模型" : "摘录回答";
    state.kbs = await fetch("/api/v1/knowledge-bases").then((response) => response.json());
    renderKbs();
  } catch (error) {
    showError(error);
  }
}

boot();
