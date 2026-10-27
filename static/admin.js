const tokenKey = "ragkb_token";
let token = localStorage.getItem(tokenKey) || "";
let me = null;
let departments = [];
let knowledgeBases = [];

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const logoutButton = document.querySelector("#logout");

function detailMessage(body) {
  if (!body) return "请求失败";
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) return body.detail.map((item) => item.msg).join("；");
  return "请求失败";
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  let body;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.json);
  } else if (options.body !== undefined) {
    body = options.body;
  }
  const response = await fetch(path, { method: options.method || "GET", headers, body });
  if (response.status === 401) {
    logout();
    throw new Error("登录已失效");
  }
  if (response.status === 204) return null;
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(detailMessage(payload));
  return payload;
}

function logout() {
  token = "";
  me = null;
  localStorage.removeItem(tokenKey);
  loginBox.classList.remove("hidden");
  appBox.classList.add("hidden");
  logoutButton.classList.add("hidden");
}

function showApp() {
  loginBox.classList.add("hidden");
  appBox.classList.remove("hidden");
  logoutButton.classList.remove("hidden");
  document.querySelector("#who").textContent = `${me.username} · ${me.role}`;
  document.querySelector('[data-tab="users"]').classList.toggle("hidden", me.role !== "admin");
}

document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.querySelector("#login-error");
  error.textContent = "";
  try {
    const result = await api("/api/v1/auth/login", {
      method: "POST",
      json: {
        username: document.querySelector("#username").value.trim(),
        password: document.querySelector("#password").value,
      },
    });
    token = result.access_token;
    localStorage.setItem(tokenKey, token);
    me = result.user;
    showApp();
    await refreshAll();
  } catch (err) {
    error.textContent = err.message;
  }
});

logoutButton.onclick = logout;

document.querySelectorAll(".tabs button").forEach((button) => {
  button.onclick = () => {
    document.querySelectorAll(".tabs button").forEach((item) => {
      item.setAttribute("aria-pressed", item === button ? "true" : "false");
    });
    for (const name of ["kb", "docs", "users"]) {
      document.querySelector(`#tab-${name}`).classList.toggle("hidden", name !== button.dataset.tab);
    }
  };
});

function renderDepartments() {
  const box = document.querySelector("#dept-checks");
  box.innerHTML = "";
  for (const department of departments) {
    const label = document.createElement("label");
    label.className = "row";
    label.innerHTML = `<input type="checkbox" value="${department.id}"> <span></span>`;
    label.querySelector("span").textContent = `${department.name} (${department.code})`;
    box.append(label);
  }
}

function renderKnowledgeBases() {
  const box = document.querySelector("#kb-table");
  box.innerHTML = "<h2>已有知识库</h2>";
  if (!knowledgeBases.length) {
    box.insertAdjacentHTML("beforeend", "<p class='muted'>还没有知识库。</p>");
  }
  for (const kb of knowledgeBases) {
    const item = document.createElement("article");
    item.className = "doc-item";
    const dept = kb.departments.map((item) => item.name).join("、");
    item.innerHTML = "<strong></strong><small></small>";
    item.querySelector("strong").textContent = kb.name;
    item.querySelector("small").textContent = `${kb.visibility}${dept ? " · " + dept : ""}`;
    const remove = document.createElement("button");
    remove.className = "danger";
    remove.type = "button";
    remove.textContent = "删除";
    remove.onclick = async () => {
      if (!confirm(`删除知识库「${kb.name}」？`)) return;
      await api(`/api/v1/knowledge-bases/${kb.id}`, { method: "DELETE" });
      await refreshAll();
    };
    item.append(remove);
    box.append(item);
  }
  const select = document.querySelector("#doc-kb");
  select.innerHTML = "";
  for (const kb of knowledgeBases) {
    const option = document.createElement("option");
    option.value = kb.id;
    option.textContent = kb.name;
    select.append(option);
  }
}

async function renderDocuments() {
  const kbId = document.querySelector("#doc-kb").value;
  const box = document.querySelector("#doc-list");
  box.innerHTML = "<h2>文档</h2>";
  if (!kbId) return;
  const docs = await api(`/api/v1/knowledge-bases/${kbId}/documents`);
  if (!docs.length) box.insertAdjacentHTML("beforeend", "<p class='muted'>这个库还没有文档。</p>");
  for (const doc of docs) {
    const item = document.createElement("article");
    item.className = "doc-item";
    item.innerHTML = "<strong></strong><small></small>";
    item.querySelector("strong").textContent = doc.filename;
    item.querySelector("small").textContent =
      doc.status === "ready" ? `${doc.chunk_count} 个分段` : `${doc.status} ${doc.error || ""}`;
    box.append(item);
  }
}

function renderUsers(users) {
  const box = document.querySelector("#user-table");
  box.innerHTML = "<h2>用户</h2>";
  const table = document.createElement("table");
  table.innerHTML = "<tr><th>用户</th><th>角色</th><th>部门</th><th></th></tr>";
  for (const user of users) {
    const row = document.createElement("tr");
    const deptOptions = [`<option value="">无</option>`]
      .concat(departments.map((department) => {
        const selected = department.id === user.department_id ? "selected" : "";
        return `<option value="${department.id}" ${selected}></option>`;
      }))
      .join("");
    row.innerHTML = `<td></td><td><select data-role><option value="member">member</option><option value="admin">admin</option></select></td><td><select data-dept>${deptOptions}</select></td><td><button class="ghost" type="button">保存</button></td>`;
    row.querySelector("td").textContent = user.username;
    row.querySelector("[data-role]").value = user.role;
    const deptSelect = row.querySelector("[data-dept]");
    [...deptSelect.options].forEach((option) => {
      const department = departments.find((item) => item.id === option.value);
      if (department) option.textContent = department.name;
    });
    row.querySelector("button").onclick = async () => {
      await api(`/api/v1/users/${user.id}`, {
        method: "PATCH",
        json: {
          role: row.querySelector("[data-role]").value,
          department_id: deptSelect.value || null,
          clear_department: !deptSelect.value,
        },
      });
      await refreshAll();
    };
    table.append(row);
  }
  box.append(table);
}

async function refreshAll() {
  departments = await api("/api/v1/departments");
  knowledgeBases = await api("/api/v1/knowledge-bases");
  const users = me.role === "admin" ? await api("/api/v1/users") : [];
  renderDepartments();
  renderKnowledgeBases();
  await renderDocuments();
  if (me.role === "admin") renderUsers(users);
  document.querySelector("#tab-users").querySelector("form").classList.toggle("hidden", me.role !== "admin");
}

document.querySelector("#kb-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.querySelector("#kb-error");
  error.textContent = "";
  const form = event.currentTarget;
  const departmentIds = [...form.querySelectorAll("input[type=checkbox]:checked")].map((item) => item.value);
  try {
    await api("/api/v1/knowledge-bases", {
      method: "POST",
      json: {
        name: form.name.value,
        description: form.description.value,
        visibility: form.visibility.value,
        department_ids: departmentIds,
      },
    });
    form.reset();
    await refreshAll();
  } catch (err) {
    error.textContent = err.message;
  }
});

document.querySelector("#doc-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.querySelector("#doc-error");
  error.textContent = "";
  const file = document.querySelector("#doc-file").files[0];
  const kbId = document.querySelector("#doc-kb").value;
  if (!file || !kbId) {
    error.textContent = "请选择知识库和文件";
    return;
  }
  const body = new FormData();
  body.append("file", file);
  try {
    await api(`/api/v1/knowledge-bases/${kbId}/documents`, { method: "POST", body });
    document.querySelector("#doc-file").value = "";
    await renderDocuments();
  } catch (err) {
    error.textContent = err.message;
  }
});

document.querySelector("#doc-kb").addEventListener("change", () => {
  renderDocuments().catch((error) => {
    document.querySelector("#doc-error").textContent = error.message;
  });
});

document.querySelector("#dept-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.querySelector("#dept-error");
  error.textContent = "";
  const form = event.currentTarget;
  try {
    await api("/api/v1/departments", {
      method: "POST",
      json: { code: form.code.value, name: form.name.value },
    });
    form.reset();
    await refreshAll();
  } catch (err) {
    error.textContent = err.message;
  }
});

async function boot() {
  if (!token) return;
  try {
    me = await api("/api/v1/auth/me");
    showApp();
    await refreshAll();
  } catch (_error) {
    logout();
  }
}

boot();
