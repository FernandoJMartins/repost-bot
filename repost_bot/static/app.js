"use strict";

// ---------------------------------------------------------------- utilidades
const $ = (sel) => document.querySelector(sel);

// Cria elementos sem innerHTML: títulos de chats vêm do Telegram e não são confiáveis.
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(path, data, method) {
  const opts = { method: method || (data === undefined ? "GET" : "POST"), headers: { "X-Requested-With": "fetch" } };
  if (data !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(data);
  }
  const res = await fetch(path, opts);
  let json = {};
  try { json = await res.json(); } catch (_) { /* resposta vazia */ }
  if (res.status === 401) { showAuth(); throw new Error("Sessão expirada"); }
  if (!res.ok || json.ok === false) throw new Error(json.error || `Erro ${res.status}`);
  return json.data;
}

let toastTimer;
function toast(msg, bad) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = "toast"; }, 3500);
}

async function busy(btn, fn) {
  if (btn) btn.disabled = true;
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { if (btn) btn.disabled = false; }
}

function fmtTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("pt-BR");
}

const TYPE_LABEL = { channel: "Canal", supergroup: "Supergrupo", group: "Grupo", private: "Privado" };
const STATUS_LABEL = {
  creator: ["dono", "ok"], administrator: ["admin", "ok"], member: ["membro", ""],
  restricted: ["restrito", "warn"], left: ["fora do chat", "bad"], kicked: ["removido", "bad"],
  migrado: ["migrado p/ supergrupo", "bad"], unknown: ["status desconhecido", "warn"],
};
const STATE_LABEL = {
  sem_token: "Sem token", conectando: "Conectando", descoberta: "Aguardando configuração",
  ativo: "Ativo", erro: "Erro",
};

// ---------------------------------------------------------------- auth
async function boot() {
  try {
    const st = await api("/api/auth/state");
    if (st.logged_in) return showApp();
    $("#auth-hint").classList.toggle("hidden", !st.default_password);
    showAuth();
  } catch (e) {
    toast(e.message, true);
  }
}

function showAuth() {
  stopPolling();
  $("#app").classList.add("hidden");
  $("#auth").classList.remove("hidden");
  $("#auth-pass").focus();
}

$("#auth-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#auth-error").textContent = "";
  try {
    await api("/api/auth/login", { user: $("#auth-user").value, password: $("#auth-pass").value });
    $("#auth-pass").value = "";
    showApp();
  } catch (e) {
    $("#auth-error").textContent = e.message;
  }
});

$("#btn-logout").addEventListener("click", async () => {
  await api("/api/auth/logout", {}).catch(() => {});
  showAuth();
});

// ---------------------------------------------------------------- app
let S = null;          // último estado
let pollTimer = null;
let logTimer = null;
let logSince = 0;
let settingsDirty = false;
let queueStatus = "failed";

function showApp() {
  $("#auth").classList.add("hidden");
  $("#app").classList.remove("hidden");
  refresh(true);
  startPolling();
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(() => refresh(false), 3000);
  logTimer = setInterval(loadLogs, 3000);
  loadLogs();
}
function stopPolling() {
  clearInterval(pollTimer);
  clearInterval(logTimer);
}

async function refresh(full) {
  try {
    const st = await api("/api/state");
    render(st, full);
  } catch (_) { /* erros de rede: tenta de novo no próximo ciclo */ }
}

function render(st, full) {
  const first = S === null;
  S = st;
  renderHeader(st);
  renderAlerts(st);
  renderFlow(st);
  renderStats(st);
  renderToken(st);
  renderChats(st);
  renderAdmin(st);
  renderAccount(st);
  renderTopics(st);
  renderImport(st);
  if (first || (full && !settingsDirty)) renderSettings(st);
  if (first || full) loadQueue();
  $("#backfill-card").classList.toggle("hidden", !st.gap_supported);
}

function chatName(id) {
  if (!id) return null;
  const c = (S.chats || []).find((x) => x.chat_id === id);
  return c ? (c.title || c.username || String(id)) : String(id);
}

function renderHeader(st) {
  $("#bot-name").textContent = st.bot ? `@${st.bot.username} · id ${st.bot.id}` : "bot não conectado";
  const pill = $("#state-pill");
  const paused = st.paused && st.state === "ativo";
  pill.textContent = paused ? "Pausado" : (STATE_LABEL[st.state] || st.state);
  pill.className = "pill " + (paused ? "pausado" : st.state);
  const b = $("#btn-pause");
  b.textContent = st.paused ? "Retomar" : "Pausar";
  b.classList.toggle("primary", st.paused);
  b.disabled = !st.config.source_chat_id;
}

function renderAlerts(st) {
  const box = $("#alerts");
  box.replaceChildren();
  if (st.error) {
    const a = h("div", { class: "alert bad" }, h("b", {}, "Atenção: "), st.error);
    if (/webhook/i.test(st.error)) {
      a.append(h("div", {}, h("button", { class: "btn", onclick: (e) => busy(e.target, async () => {
        render(await api("/api/webhook/delete", {}), true); toast("Webhook removido");
      }) }, "Remover webhook")));
    } else if (st.state === "erro") {
      a.append(h("div", {}, h("button", { class: "btn", onclick: (e) => busy(e.target, async () => {
        render(await api("/api/restart", {}), true); toast("Reconectando…");
      }) }, "Tentar de novo")));
    }
    box.append(a);
  }
  if (st.default_password) {
    box.append(h("div", { class: "alert warn" }, "Você está usando a senha padrão (admin). ",
      h("button", { class: "btn small", onclick: () => {
        $("#account").open = true;
        $("#account").scrollIntoView({ behavior: "smooth" });
        $("#pw-current").focus();
      } }, "Trocar senha")));
  }
  if (st.paused && st.pause_reason) {
    box.append(h("div", { class: "alert warn" }, h("b", {}, "Pausado: "), st.pause_reason));
  }
  for (const w of st.warnings || []) box.append(h("div", { class: "alert warn" }, w));
  if (!st.error && st.missing && st.missing.length) {
    box.append(h("div", { class: "alert info" }, "Falta configurar: ", st.missing.join(", "), "."));
  }
}

function renderFlow(st) {
  const fill = (el, id, empty) => {
    el.replaceChildren(id ? h("span", {}, chatName(id), h("span", { class: "id" }, id)) : empty);
  };
  fill($("#flow-src"), st.config.source_chat_id, "não definida");
  fill($("#flow-dst"), st.config.dest_chat_id, "não definido");
}

function renderStats(st) {
  const box = $("#stats");
  if (!st.stats) { box.replaceChildren(); box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const c = st.stats.counts;
  const tile = (v, k, cls) => h("div", { class: "stat " + (cls || "") }, h("div", { class: "v" }, v), h("div", { class: "k" }, k));
  box.replaceChildren(
    tile(c.done, "copiadas"),
    tile(st.stats.copied_24h, "últimas 24 h"),
    tile(c.pending + c.sending, "na fila"),
    tile(c.failed, "falhas", c.failed ? "bad" : ""),
    tile(c.uncertain, "incertas", c.uncertain ? "warn" : ""),
    tile(c.skipped, "ignoradas"),
    tile(st.stats.last_copied_at ? new Date(st.stats.last_copied_at * 1000).toLocaleTimeString("pt-BR") : "—", "última cópia"),
  );
}

function renderToken(st) {
  $("#token-current").textContent = st.token.set ? `salvo: ${st.token.masked}` : "nenhum token salvo";
  document.querySelector("#step-token .step").classList.toggle("done", !!st.bot);
}

$("#token-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const btn = ev.submitter;
  busy(btn, async () => {
    const st = await api("/api/token", { token: $("#token-input").value.trim() });
    $("#token-input").value = "";
    render(st, true);
    toast(`Conectado como @${st.bot ? st.bot.username : "bot"}`);
  });
});

// ---------------------------------------------------------------- chats
function renderChats(st) {
  const box = $("#chats");
  const links = $("#add-links");
  links.replaceChildren();
  if (st.links && st.links.channel) {
    links.append(
      h("a", { class: "btn", href: st.links.channel, target: "_blank", rel: "noopener" }, "+ Adicionar bot a um canal"),
      h("a", { class: "btn", href: st.links.group, target: "_blank", rel: "noopener" }, "+ Adicionar bot a um grupo"),
    );
  }
  document.querySelector("#step-chats .step").classList.toggle("done", !!(st.config.source_chat_id && st.config.dest_chat_id));
  $("#btn-refresh").disabled = !st.bot;
  $("#add-form button").disabled = !st.bot;

  if (!st.bot) {
    box.replaceChildren(h("div", { class: "empty" }, "Salve um token válido para o bot começar a descobrir chats."));
    return;
  }
  if (!st.chats.length) {
    box.replaceChildren(h("div", { class: "empty" },
      "Nenhum chat ainda. Adicione o bot ao canal/grupo pelos botões acima (ou remova e adicione de novo), ",
      "poste algo no chat, ou encaminhe uma mensagem do canal para o bot no privado."));
    return;
  }
  const src = st.config.source_chat_id, dst = st.config.dest_chat_id;
  box.replaceChildren(...st.chats.map((c) => {
    const [stLabel, stCls] = STATUS_LABEL[c.bot_status] || [c.bot_status || "status desconhecido", "warn"];
    const gone = ["left", "kicked", "migrado"].includes(c.bot_status);
    const isSrc = c.chat_id === src, isDst = c.chat_id === dst;
    const meta = h("div", { class: "chat-meta" },
      h("span", { class: "chip" }, TYPE_LABEL[c.type] || c.type || "?"),
      h("span", { class: "chip " + stCls }, "bot: " + stLabel),
      c.can_post === 1 ? h("span", { class: "chip ok" }, "pode postar") : null,
      c.can_post === 0 && !gone ? h("span", { class: "chip warn" }, "não pode postar") : null,
      c.protected === 1 ? h("span", { class: "chip bad" }, "conteúdo protegido") : null,
      c.username ? h("span", { class: "chip" }, "@" + c.username) : null,
      h("span", { class: "chip mono", title: "Clique para copiar", onclick: () => {
        navigator.clipboard && navigator.clipboard.writeText(String(c.chat_id)).then(() => toast("ID copiado"));
      } }, c.chat_id),
    );
    const choose = (field, label, active) => h("button", {
      class: "btn small" + (active ? " on" : ""),
      disabled: active || null,
      onclick: (e) => busy(e.target, async () => {
        const other = field === "source_chat_id" ? dst : src;
        const payload = { [field]: c.chat_id };
        if (other === c.chat_id) payload[field === "source_chat_id" ? "dest_chat_id" : "source_chat_id"] = null;
        render(await api("/api/settings", payload), true);
        toast(`${label} definida: ${c.title || c.chat_id}`);
      }),
    }, active ? `✓ ${label}` : label);
    return h("div", { class: "chat" + (isSrc || isDst ? " selected" : "") + (gone ? " gone" : "") },
      h("div", {}, h("div", { class: "chat-title" }, c.title || "(sem título)"), meta),
      h("div", { class: "chat-actions" },
        choose("source_chat_id", "Origem", isSrc),
        choose("dest_chat_id", "Destino", isDst),
        !isSrc && !isDst ? h("button", { class: "btn small ghost", title: "Remover da lista",
          onclick: (e) => busy(e.target, async () => {
            render(await api("/api/chats/forget", { chat_id: c.chat_id }), true);
          }) }, "✕") : null,
      ));
  }));
}

$("#btn-refresh").addEventListener("click", (e) => busy(e.target, async () => {
  const st = await api("/api/chats/refresh", {});
  render(st, false);
  toast(`${st.refreshed} chat(s) verificados`);
}));

$("#add-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy(ev.submitter, async () => {
    const st = await api("/api/chats/add", { ref: $("#add-input").value });
    $("#add-input").value = "";
    render(st, false);
    toast(`Encontrado: ${st.chat.title || st.chat.id}`);
  });
});

// ---------------------------------------------------------------- admin
function renderAdmin(st) {
  const admin = st.config.admin_user_id;
  $("#admin-current").textContent = admin ? `ID atual: ${admin}` : "não definido";
  document.querySelector("#step-admin .step").classList.toggle("done", !!admin);
  const links = $("#admin-links");
  links.replaceChildren();
  if (st.links && st.links.private) {
    links.append(h("a", { class: "btn", href: st.links.private, target: "_blank", rel: "noopener" }, "Abrir conversa com o bot"));
  }
  if (admin) {
    links.append(h("button", { class: "btn ghost", onclick: (e) => busy(e.target, async () => {
      render(await api("/api/settings", { admin_user_id: null }), true);
    }) }, "Remover admin"));
  }
  const box = $("#users");
  if (!st.users.length) {
    box.replaceChildren(h("div", { class: "empty" }, "Ninguém falou com o bot no privado ainda."));
    return;
  }
  box.replaceChildren(...st.users.map((u) => {
    const name = [u.first_name, u.last_name].filter(Boolean).join(" ") || "(sem nome)";
    const me = u.user_id === admin;
    return h("div", { class: "user" },
      h("div", {}, h("b", {}, name), " ", h("span", { class: "muted small" },
        (u.username ? "@" + u.username + " · " : "") + "id " + u.user_id + " · " + fmtTime(u.last_seen))),
      h("button", { class: "btn small" + (me ? " on" : ""), disabled: me || null,
        onclick: (e) => busy(e.target, async () => {
          render(await api("/api/settings", { admin_user_id: u.user_id }), true);
          toast("Administrador definido");
        }) }, me ? "✓ Admin" : "Sou eu"));
  }));
}

// ---------------------------------------------------------------- conta de usuário
const ACC_LABEL = {
  desconectada: "não conectada", aguardando_codigo: "aguardando código",
  aguardando_senha: "aguardando senha de 2 etapas", conectada: "conectada",
};

function renderAccount(st) {
  const a = st.account;
  $("#account-current").textContent = a.state === "conectada"
    ? `conectada: ${a.name || ""} ${a.phone ? "(" + a.phone + ")" : ""}` : ACC_LABEL[a.state];
  document.querySelector("#step-account .step").classList.toggle("done", a.state === "conectada");
  $("#acc-form").classList.toggle("hidden", a.state === "conectada");
  $("#acc-code-form").classList.toggle("hidden", a.state !== "aguardando_codigo");
  $("#acc-pass-form").classList.toggle("hidden", a.state !== "aguardando_senha");
  $("#acc-connected").classList.toggle("hidden", a.state !== "conectada");
  if (a.api_id && !$("#acc-api-id").value && document.activeElement !== $("#acc-api-id")) $("#acc-api-id").value = a.api_id;
  $("#acc-api-hash").placeholder = a.has_api_hash ? "api_hash (já salvo)" : "api_hash";
}

$("#acc-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy(ev.submitter, async () => {
    render(await api("/api/account/send_code", {
      api_id: $("#acc-api-id").value, api_hash: $("#acc-api-hash").value, phone: $("#acc-phone").value,
    }), false);
    $("#acc-api-hash").value = "";
    toast("Código enviado para o seu Telegram");
    $("#acc-code").focus();
  });
});

$("#acc-code-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy(ev.submitter, async () => {
    const st = await api("/api/account/code", { code: $("#acc-code").value });
    $("#acc-code").value = "";
    render(st, false);
    if (st.account.state === "aguardando_senha") { toast("Digite a senha de duas etapas"); $("#acc-pass").focus(); }
    else toast("Conta conectada");
  });
});

$("#acc-pass-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy(ev.submitter, async () => {
    const st = await api("/api/account/password", { password: $("#acc-pass").value });
    $("#acc-pass").value = "";
    render(st, false);
    toast("Conta conectada");
  });
});

$("#btn-acc-logout").addEventListener("click", (e) => busy(e.target, async () => {
  if (!confirm("Desconectar a conta e apagar a sessão salva?")) return;
  render(await api("/api/account/logout", {}), false);
  toast("Conta desconectada");
}));

// ---------------------------------------------------------------- tópicos
const colorHex = (c) => (c || c === 0) ? "#" + Number(c).toString(16).padStart(6, "0") : null;

function renderTopics(st) {
  const card = $("#topics-card");
  card.classList.toggle("hidden", !(st.config.source_chat_id && (st.is_forum || st.topics.length)));
  $("#auto-topics").checked = !!st.config.auto_create_topics;
  $("#btn-topics-fetch").disabled = st.account.state !== "conectada";
  $("#btn-topics-fetch").title = st.account.state !== "conectada" ? "Conecte sua conta para ler a lista de tópicos" : "";
  const box = $("#topics");
  if (box.contains(document.activeElement)) return; // não atrapalha quem está escolhendo
  if (!st.topics.length) {
    box.replaceChildren(h("div", { class: "empty" },
      "Nenhum tópico conhecido ainda. Conecte sua conta e clique em 'Ler tópicos', ou aguarde mensagens novas."));
    return;
  }
  const destOpts = st.dest_topics.map((t) => [t.thread_id, t.title || `Tópico #${t.thread_id}`]);
  box.replaceChildren(...st.topics.map((t) => {
    const sel = h("select", { "aria-label": "Destino do tópico" },
      h("option", { value: "auto", selected: t.mode === "auto" || null }, "Criar automaticamente"),
      h("option", { value: "general", selected: t.mode === "general" || null }, "Tópico Geral"),
      ...destOpts.map(([id, title]) => h("option", {
        value: "topic:" + id, selected: (t.mode === "topic" && t.dest_thread_id === id) || null,
      }, "→ " + title)),
      t.mode === "topic" && !destOpts.some(([id]) => id === t.dest_thread_id)
        ? h("option", { value: "topic:" + t.dest_thread_id, selected: true }, `→ Tópico #${t.dest_thread_id}`) : null,
    );
    sel.addEventListener("change", () => busy(sel, async () => {
      const [mode, id] = sel.value.split(":");
      render(await api("/api/topics/map", { source_thread_id: t.thread_id, mode, dest_thread_id: id ? Number(id) : null }), false);
      sel.blur();
      toast("Mapeamento salvo");
    }));
    const dot = h("span", { class: "dot" });
    const c = colorHex(t.icon_color);
    if (c) dot.style.background = c;
    return h("div", { class: "topic" },
      h("div", {}, h("div", { class: "topic-name" }, dot, t.title || `Tópico #${t.thread_id}`),
        h("div", { class: "chat-meta" }, h("span", { class: "chip mono" }, "#" + t.thread_id),
          t.closed ? h("span", { class: "chip warn" }, "fechado") : null,
          t.created_by_bot ? h("span", { class: "chip ok" }, "criado pelo bot") : null)),
      sel);
  }));
}

$("#btn-topics-fetch").addEventListener("click", (e) => busy(e.target, async () => {
  render(await api("/api/topics/fetch", {}), false);
  toast(`${S.topics.length} tópico(s) na origem`);
}));

$("#auto-topics").addEventListener("change", (e) => busy(e.target, async () => {
  render(await api("/api/settings", { auto_create_topics: e.target.checked }), false);
}));

// ---------------------------------------------------------------- importação
function renderImport(st) {
  const j = st.import;
  const connected = st.account.state === "conectada";
  const ready = connected && st.config.source_chat_id && st.config.dest_chat_id;
  $("#btn-import").disabled = !ready || j.running;
  $("#btn-import").textContent = j.finished_at && !j.error ? "Importar de novo (só o que falta)" : "Importar histórico";
  $("#btn-import-stop").classList.toggle("hidden", !j.running);
  const box = $("#import-status");
  const c = st.stats ? st.stats.counts : null;
  const parts = [];
  if (!connected) parts.push(h("div", {}, "Conecte sua conta no passo 4 para importar."));
  if (j.running) parts.push(h("div", {}, `Lendo histórico… ${j.read} mensagens lidas (até o ID ${j.last_id ?? "—"}), ${j.queued} na fila.`));
  else if (j.finished_at) parts.push(h("div", {}, j.error ? `Leitura falhou: ${j.error}` :
    `Leitura concluída: ${j.read} mensagens lidas, ${j.queued} enfileiradas, ${j.skipped} não copiáveis.`));
  if (c && (j.running || j.finished_at)) {
    const total = c.done + c.pending + c.sending + c.failed + c.uncertain;
    const pct = total ? Math.round((c.done / total) * 100) : 0;
    const left = c.pending + c.sending;
    const mins = Math.ceil(left / (st.config.dest_messages_per_minute || 20));
    const bar = h("div", { class: "progress" }, h("div", {}));
    bar.firstChild.style.width = pct + "%";
    parts.push(h("div", {}, `Cópia: ${c.done} de ${total} (${pct}%)` + (left ? ` · faltam ~${mins >= 60 ? Math.floor(mins / 60) + "h" + String(mins % 60).padStart(2, "0") : mins + " min"}` : "")), bar);
  }
  box.replaceChildren(...parts);
}

$("#btn-import").addEventListener("click", (e) => busy(e.target, async () => {
  render(await api("/api/import/start", {}), false);
  toast("Importação iniciada");
}));

$("#btn-import-stop").addEventListener("click", (e) => busy(e.target, async () => {
  render(await api("/api/import/stop", {}), false);
}));

// ---------------------------------------------------------------- pausa
$("#btn-pause").addEventListener("click", (e) => busy(e.target, async () => {
  render(await api(S && S.paused ? "/api/resume" : "/api/pause", {}), false);
}));

// ---------------------------------------------------------------- fila
const QUEUE_HINT = {
  failed: "Mensagens que falharam após todas as tentativas. Você pode recolocá-las na fila.",
  uncertain: "O envio pode ter chegado ao destino (queda ou timeout no meio da chamada). Confira o destino antes de reenviar para não duplicar.",
  pending: "Próximas mensagens a copiar, em ordem.",
};

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
  queueStatus = t.dataset.status;
  loadQueue();
}));

async function loadQueue() {
  $("#queue-hint").textContent = QUEUE_HINT[queueStatus];
  const box = $("#queue");
  const actions = $("#queue-actions");
  actions.replaceChildren();
  if (!S || !S.config.source_chat_id) {
    box.replaceChildren(h("div", { class: "empty" }, "Defina a origem primeiro."));
    return;
  }
  let rows = [];
  try { rows = await api("/api/queue?status=" + queueStatus); } catch (e) { toast(e.message, true); return; }
  if (!rows.length) {
    box.replaceChildren(h("div", { class: "empty" }, "Nada aqui. 👍"));
    return;
  }
  const canRequeue = queueStatus === "failed" || queueStatus === "uncertain";
  const label = queueStatus === "failed" ? "Tentar de novo" : "Reenviar";
  box.replaceChildren(h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "ID origem"), h("th", {}, "Tipo"), h("th", {}, "Tentativas"),
      h("th", {}, "Erro"), h("th", {}, ""))),
    h("tbody", {}, ...rows.map((r) => h("tr", {},
      h("td", {}, r.source_message_id),
      h("td", {}, r.kind),
      h("td", {}, r.attempts),
      h("td", { class: "err" }, r.last_error || ""),
      h("td", {}, h("div", { class: "chat-actions" },
        canRequeue ? h("button", { class: "btn small", onclick: (e) => busy(e.target, async () => {
          await api("/api/queue/requeue", { status: queueStatus, id: r.source_message_id });
          loadQueue(); refresh(false);
        }) }, label) : null,
        h("button", { class: "btn small ghost", onclick: (e) => busy(e.target, async () => {
          await api("/api/queue/skip", { id: r.source_message_id });
          loadQueue(); refresh(false);
        }) }, "Ignorar"))),
    ))))));
  if (canRequeue) {
    actions.append(h("button", { class: "btn", onclick: (e) => busy(e.target, async () => {
      if (queueStatus === "uncertain" && !confirm("Reenviar todas as incertas? Se alguma já chegou ao destino, ela ficará duplicada.")) return;
      const r = await api("/api/queue/requeue", { status: queueStatus, id: "all" });
      toast(`${r.count} mensagem(ns) recolocada(s) na fila`);
      loadQueue(); refresh(false);
    }) }, `${label} todas`));
  }
}

$("#backfill-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy(ev.submitter, async () => {
    const r = await api("/api/backfill", { first: $("#bf-first").value, last: $("#bf-last").value });
    toast(`${r.count} ID(s) enfileirados (~${r.minutes} min)`);
    refresh(false);
  });
});

// ---------------------------------------------------------------- avançado
const FIELDS = [
  ["dest_messages_per_minute", "Mensagens por minuto no destino", "number", "Limite oficial: até 20/min em grupos, ~1/s por chat. Máx. 60."],
  ["album_settle_seconds", "Espera para juntar álbuns (s)", "number", "Tempo para receber todas as fotos de um álbum."],
  ["auto_create_topics", "Criar tópicos automaticamente", "bool", "Cria no destino o tópico com o mesmo nome quando não houver mapeamento."],
  ["gap_recovery", "Recuperar lacunas de IDs", "bool", "Copia por ID mensagens perdidas (canal/supergrupo)."],
  ["gap_auto_max", "Tamanho máximo de lacuna automática", "number", "Lacunas maiores exigem backfill manual."],
  ["gap_settle_seconds", "Espera antes de recuperar lacuna (s)", "number", ""],
  ["backfill_max", "Máximo de IDs por backfill", "number", ""],
  ["max_attempts", "Tentativas antes de marcar falha", "number", ""],
  ["uncertain_policy", "Entrega incerta", "select:hold=Reter (nunca duplica)|resend=Reenviar (pode duplicar)", ""],
  ["poll_timeout", "Long polling (s)", "number", ""],
  ["request_timeout", "Timeout das requisições (s)", "number", ""],
  ["heartbeat_minutes", "Intervalo do heartbeat no log (min)", "number", ""],
];

function renderSettings(st) {
  const form = $("#settings-form");
  form.replaceChildren(...FIELDS.map(([name, label, type, help]) => {
    const v = st.config[name];
    let input;
    if (type === "bool") {
      input = h("select", { name }, h("option", { value: "true", selected: v || null }, "Sim"),
        h("option", { value: "false", selected: !v || null }, "Não"));
    } else if (type.startsWith("select:")) {
      input = h("select", { name }, ...type.slice(7).split("|").map((o) => {
        const [val, txt] = o.split("=");
        return h("option", { value: val, selected: v === val || null }, txt);
      }));
    } else {
      input = h("input", { name, type: "number", step: "any", value: v });
    }
    input.addEventListener("input", () => { settingsDirty = true; });
    return h("label", {}, label, input, help ? h("span", { class: "help" }, help) : null);
  }));
  settingsDirty = false;
}

$("#btn-save-settings").addEventListener("click", (e) => busy(e.target, async () => {
  const data = {};
  for (const el of $("#settings-form").elements) data[el.name] = el.value;
  const st = await api("/api/settings", data);
  settingsDirty = false;
  render(st, true);
  toast("Configurações salvas");
}));

$("#btn-restart").addEventListener("click", (e) => busy(e.target, async () => {
  render(await api("/api/restart", {}), true);
  toast("Bot reconectado");
}));

// ---------------------------------------------------------------- log
async function loadLogs() {
  let lines;
  try { lines = await api("/api/logs?since=" + logSince); } catch (_) { return; }
  if (!lines.length) return;
  const pre = $("#log");
  for (const l of lines) {
    const time = new Date(l.t * 1000).toLocaleTimeString("pt-BR");
    pre.append(h("div", { class: l.level }, `${time} ${l.msg}`));
    logSince = l.n;
  }
  while (pre.childElementCount > 500) pre.firstChild.remove();
  if ($("#log-follow").checked) pre.scrollTop = pre.scrollHeight;
}

// ---------------------------------------------------------------- conta
$("#pw-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy(ev.submitter, async () => {
    await api("/api/auth/password", { user: $("#pw-user").value, current: $("#pw-current").value, new: $("#pw-new").value });
    $("#pw-user").value = $("#pw-current").value = $("#pw-new").value = "";
    toast("Login atualizado");
    refresh(false);
  });
});

boot();
