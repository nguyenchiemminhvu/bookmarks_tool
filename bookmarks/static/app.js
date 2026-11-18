"use strict";

const STATUS_LABELS = {
  ok: "OK", auth: "Login required", broken: "Broken", unreachable: "Unreachable",
  error: "Error", skipped: "Skipped", unchecked: "Unchecked",
};
const STATUS_RANK = { broken: 0, unreachable: 1, error: 2, auth: 3, unchecked: 4, skipped: 5, ok: 6 };
const SAFE_LINK = /^(https?|ftp|file):/i;
const AUTH_LABELS = { bearer: "Bearer token", basic: "Basic", header: "Header" };

const state = {
  links: [],
  tags: new Set(),        // selected tag filters, combined with AND
  untagged: false,
  query: "",
  status: "",
  sort: "newest",
  tagFilter: "",
  selected: new Set(),
  visible: [],
  lastClicked: -1,        // index in `visible` for shift-click range selection
  check: { running: false, total: 0, done: 0 },
  pollTimer: 0,
  editingId: null,
  credentials: [],
  editingCredId: null,
};

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "checked") node.checked = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child != null && child !== false) node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error((data && data.error) || `${response.status} ${response.statusText}`);
  return data;
}

const statusOf = (link) => link.check_status || "unchecked";
const fmtDate = (secs) => (secs ? new Date(secs * 1000).toISOString().slice(0, 10) : "");
const parseTags = (text) => text.toLowerCase().split(/[\s,]+/).filter(Boolean);
const allTags = () => new Set(state.links.flatMap((l) => l.tags));

function hostOf(url) {
  try { return new URL(url).hostname.toLowerCase(); } catch { return ""; }
}

// Mirrors credentials.host_matches on the server.
function hostMatches(pattern, host) {
  return pattern.startsWith("*.") ? host.endsWith(pattern.slice(1)) : host === pattern;
}

function indexLink(link) {
  link.hay = [link.title, link.url, link.notes, ...link.tags].join(" ").toLowerCase();
  return link;
}

// ---- data ----------------------------------------------------------------

async function loadLinks() {
  state.links = (await api("GET", "/api/links")).map(indexLink);
  const known = allTags();
  for (const tag of [...state.tags]) if (!known.has(tag)) state.tags.delete(tag);
  render();
}

function replaceLink(link) {
  const index = state.links.findIndex((l) => l.id === link.id);
  if (index >= 0) state.links[index] = indexLink(link);
}

function computeVisible() {
  const tokens = state.query.toLowerCase().split(/\s+/).filter(Boolean);
  const tags = [...state.tags];
  const newest = (a, b) => b.created_at - a.created_at || b.id - a.id;
  const comparators = {
    newest,
    oldest: (a, b) => -newest(a, b),
    title: (a, b) => (a.title || a.url).localeCompare(b.title || b.url, undefined, { sensitivity: "base" }),
    updated: (a, b) => b.updated_at - a.updated_at || newest(a, b),
    status: (a, b) => STATUS_RANK[statusOf(a)] - STATUS_RANK[statusOf(b)] || newest(a, b),
  };
  return state.links
    .filter((l) =>
      (!state.untagged || l.tags.length === 0) &&
      tags.every((t) => l.tags.includes(t)) &&
      (!state.status || statusOf(l) === state.status) &&
      tokens.every((t) => l.hay.includes(t)))
    .sort(comparators[state.sort]);
}

// ---- rendering -----------------------------------------------------------

function render() {
  state.visible = computeVisible();
  const visibleIds = new Set(state.visible.map((l) => l.id));
  for (const id of [...state.selected]) if (!visibleIds.has(id)) state.selected.delete(id);
  state.lastClicked = -1;
  renderRows();
  renderTagList();
  renderFilterBar();
  renderBulkBar();
  renderCheckButton();
}

function renderRows() {
  const fragment = document.createDocumentFragment();
  state.visible.forEach((link, index) => fragment.append(linkRow(link, index)));
  $("rows").replaceChildren(fragment);
  const empty = $("empty");
  if (state.visible.length) {
    empty.classList.add("hidden");
  } else {
    empty.textContent = state.links.length
      ? "No links match the current filters."
      : "No links yet. Click \u201cAdd link\u201d, or \u201cImport\u201d a bookmarks HTML file exported from your browser.";
    empty.classList.remove("hidden");
  }
}

function linkRow(link, index) {
  const selected = state.selected.has(link.id);
  return el("tr", { class: selected ? "selected" : null },
    el("td", { class: "sel" },
      el("input", { type: "checkbox", checked: selected, onclick: (e) => onRowCheckbox(e, index) })),
    el("td", { class: "link" },
      el("a", {
        class: "title",
        href: SAFE_LINK.test(link.url) ? link.url : null,
        target: "_blank",
        rel: "noopener noreferrer",
        title: link.title || link.url,
      }, link.title || link.url),
      el("div", { class: "url", title: link.url }, link.url),
      link.notes ? el("div", { class: "notes" }, link.notes) : null),
    el("td", { class: "tags" },
      link.tags.map((tag) => el("button", {
        type: "button",
        class: state.tags.has(tag) ? "tag active" : "tag",
        onclick: () => toggleTag(tag),
      }, tag))),
    el("td", { class: "status" }, statusBadge(link)),
    el("td", { class: "date" }, fmtDate(link.created_at)),
    el("td", { class: "actions" },
      el("button", { type: "button", onclick: () => openEditor(link) }, "Edit"),
      " ",
      el("button", { type: "button", onclick: (e) => checkOne(link, e.currentTarget) }, "Check"),
      " ",
      el("button", { type: "button", class: "danger", onclick: () => deleteOne(link) }, "Delete")));
}

function statusBadge(link) {
  const status = statusOf(link);
  const details = [];
  if (link.check_code) details.push(`HTTP ${link.check_code}`);
  if (link.check_error) details.push(link.check_error);
  if (link.checked_at) details.push(`checked ${new Date(link.checked_at * 1000).toLocaleString()}`);
  const fixable = status === "auth" && SAFE_LINK.test(link.url);
  if (fixable) details.push("Click to add a credential for this host");
  return el("button", {
    type: "button",
    class: `badge s-${status}${fixable ? " clickable" : ""}`,
    title: details.join("\n") || null,
    onclick: fixable ? () => openCredentials(hostOf(link.url)) : null,
  }, STATUS_LABELS[status]);
}

function renderTagList() {
  const counts = new Map();
  let untagged = 0;
  for (const link of state.visible) {
    if (!link.tags.length) untagged += 1;
    for (const tag of link.tags) counts.set(tag, (counts.get(tag) || 0) + 1);
  }
  const filter = state.tagFilter.trim().toLowerCase();
  const items = [];
  if ((untagged || state.untagged) && (!filter || "untagged".includes(filter))) {
    items.push(tagItem(null, untagged));
  }
  for (const tag of [...counts.keys()].sort()) {
    if (!filter || tag.includes(filter)) items.push(tagItem(tag, counts.get(tag)));
  }
  $("tag-list").replaceChildren(...items);
  $("tag-total").textContent = `Tags (${allTags().size})`;
}

function tagItem(tag, count) {
  const active = tag === null ? state.untagged : state.tags.has(tag);
  return el("li", { class: active ? "active" : null },
    el("button", {
      type: "button",
      class: tag === null ? "tag-name special" : "tag-name",
      title: tag === null ? "Links without tags" : tag,
      onclick: () => (tag === null ? toggleUntagged() : toggleTag(tag)),
    }, tag === null ? "(untagged)" : tag),
    el("span", { class: "count" }, count),
    tag === null ? null : el("span", { class: "tag-tools" },
      el("button", { type: "button", class: "icon-btn", title: "Rename or merge this tag", onclick: () => renameTag(tag) }, "rename"),
      el("button", { type: "button", class: "icon-btn", title: "Remove this tag from all links", onclick: () => deleteTag(tag) }, "delete")));
}

function renderFilterBar() {
  const total = state.links.length;
  const shown = state.visible.length;
  $("result-count").textContent = shown === total ? `${total} links` : `${shown} of ${total} links`;
  const chips = [...state.tags].sort().map((tag) =>
    el("button", { type: "button", class: "tag active", title: "Remove filter", onclick: () => toggleTag(tag) },
      tag, el("span", { class: "chip-x" }, "\u00d7")));
  if (state.untagged) {
    chips.push(el("button", { type: "button", class: "tag active", onclick: toggleUntagged },
      "(untagged)", el("span", { class: "chip-x" }, "\u00d7")));
  }
  if (chips.length || state.query || state.status) {
    chips.push(el("button", { type: "button", class: "link-btn", onclick: clearFilters }, "Clear filters"));
  }
  $("active-filters").replaceChildren(...chips);
}

function renderBulkBar() {
  const count = state.selected.size;
  $("bulkbar").classList.toggle("hidden", count === 0);
  $("bulk-count").textContent = `${count} selected`;
  $("bulk-check").disabled = state.check.running;
  const all = $("select-all");
  all.checked = count > 0 && count === state.visible.length;
  all.indeterminate = count > 0 && count < state.visible.length;
}

function renderCheckButton() {
  const button = $("btn-check");
  if (state.check.running) {
    button.disabled = true;
    button.textContent = `Checking ${state.check.done}/${state.check.total}\u2026`;
    return;
  }
  const count = state.visible.length;
  button.disabled = count === 0;
  button.textContent = count === state.links.length ? `Check all (${count})` : `Check filtered (${count})`;
}

// ---- filters and selection -------------------------------------------------

function toggleTag(tag) {
  if (state.tags.has(tag)) state.tags.delete(tag);
  else {
    state.tags.add(tag);
    state.untagged = false;
  }
  render();
}

function toggleUntagged() {
  state.untagged = !state.untagged;
  if (state.untagged) state.tags.clear();
  render();
}

function clearFilters() {
  state.tags.clear();
  state.untagged = false;
  state.query = $("search").value = "";
  state.status = $("status-filter").value = "";
  render();
}

function onRowCheckbox(event, index) {
  const checked = event.currentTarget.checked;
  const range = event.shiftKey && state.lastClicked >= 0
    ? state.visible.slice(Math.min(index, state.lastClicked), Math.max(index, state.lastClicked) + 1)
    : [state.visible[index]];
  for (const link of range) checked ? state.selected.add(link.id) : state.selected.delete(link.id);
  state.lastClicked = index;
  const rows = $("rows").children;
  state.visible.forEach((link, i) => {
    const on = state.selected.has(link.id);
    rows[i].classList.toggle("selected", on);
    rows[i].querySelector("input").checked = on;
  });
  renderBulkBar();
}

// ---- link actions ----------------------------------------------------------

async function checkOne(link, button) {
  button.disabled = true;
  button.textContent = "Checking\u2026";
  try {
    replaceLink(await api("POST", `/api/links/${link.id}/check`, {}));
  } catch (err) {
    alert(err.message);
  }
  render();
}

async function deleteOne(link) {
  if (!confirm(`Delete \u201c${link.title || link.url}\u201d?`)) return;
  try {
    await api("DELETE", `/api/links/${link.id}`);
  } catch (err) {
    alert(err.message);
  }
  await loadLinks();
}

async function bulk(extra) {
  try {
    await api("POST", "/api/bulk", { ids: [...state.selected], ...extra });
  } catch (err) {
    alert(err.message);
  }
  await loadLinks();
}

async function renameTag(tag) {
  const target = prompt(`Rename tag \u201c${tag}\u201d to:\n(Using an existing tag name merges the two tags.)`, tag);
  if (target === null || !target.trim() || target.trim() === tag) return;
  try {
    const { name } = await api("POST", "/api/tags/rename", { from: tag, to: target });
    if (state.tags.delete(tag)) state.tags.add(name);
  } catch (err) {
    alert(err.message);
  }
  await loadLinks();
}

async function deleteTag(tag) {
  const count = state.links.filter((l) => l.tags.includes(tag)).length;
  if (!confirm(`Remove tag \u201c${tag}\u201d from ${count} link(s)? The links themselves are kept.`)) return;
  try {
    await api("DELETE", `/api/tags/${encodeURIComponent(tag)}`);
  } catch (err) {
    alert(err.message);
  }
  await loadLinks();
}

async function startCheck(ids) {
  try {
    state.check = await api("POST", "/api/check", { ids });
  } catch (err) {
    alert(err.message);
  }
  pollCheck();
}

async function pollCheck() {
  clearTimeout(state.pollTimer);
  try {
    state.check = await api("GET", "/api/check");
    await loadLinks();
  } catch (err) {
    console.error(err);
  }
  if (state.check.running) state.pollTimer = setTimeout(pollCheck, 1500);
}

// ---- link editor -----------------------------------------------------------

const editorField = (name) => $("editor-form").elements.namedItem(name);

function openEditor(link, prefill = {}) {
  state.editingId = link ? link.id : null;
  $("editor-heading").textContent = link ? "Edit link" : "Add link";
  editorField("url").value = link ? link.url : prefill.url || "";
  editorField("title").value = link ? link.title : prefill.title || "";
  editorField("tags").value = (link ? link.tags : [...state.tags]).join(" ");
  editorField("notes").value = link ? link.notes : "";
  $("editor-error").textContent = "";
  $("tag-suggestions").replaceChildren();
  updateDupWarning();
  const dialog = $("editor");
  if (!dialog.open) dialog.showModal();
  editorField(link || prefill.url ? "tags" : "url").focus();
}

function updateDupWarning() {
  const url = editorField("url").value.trim();
  const other = url && state.links.find((l) => l.url === url && l.id !== state.editingId);
  const warning = $("dup-warning");
  warning.classList.toggle("hidden", !other);
  if (other) {
    warning.replaceChildren(`Already saved as \u201c${other.title || other.url}\u201d. `,
      el("button", { type: "button", class: "link-btn", onclick: () => openEditor(other) }, "Edit that link"));
  }
}

async function fetchTitle() {
  const url = editorField("url").value.trim();
  if (!url) return;
  const button = $("btn-fetch-title");
  button.disabled = true;
  button.textContent = "Fetching\u2026";
  try {
    const { title } = await api("GET", `/api/title?url=${encodeURIComponent(url)}`);
    if (title) editorField("title").value = title;
    else $("editor-error").textContent = "Could not read a title from that page.";
  } catch (err) {
    $("editor-error").textContent = err.message;
  } finally {
    button.disabled = false;
    button.textContent = "Fetch title";
  }
}

function updateTagSuggestions() {
  const input = editorField("tags");
  const parts = input.value.split(/[\s,]+/);
  const last = parts[parts.length - 1].toLowerCase();
  const box = $("tag-suggestions");
  if (!last) {
    box.replaceChildren();
    return;
  }
  const used = new Set(parts.slice(0, -1));
  const matches = [...allTags()]
    .filter((t) => t.includes(last) && t !== last && !used.has(t))
    .sort((a, b) => b.startsWith(last) - a.startsWith(last) || a.localeCompare(b))
    .slice(0, 12);
  box.replaceChildren(...matches.map((tag) => el("button", {
    type: "button",
    class: "tag",
    onclick: () => {
      parts[parts.length - 1] = tag;
      input.value = parts.join(" ") + " ";
      input.focus();
      box.replaceChildren();
    },
  }, tag)));
}

async function saveLink(event) {
  event.preventDefault();
  const body = {
    url: editorField("url").value.trim(),
    title: editorField("title").value.trim(),
    notes: editorField("notes").value.trim(),
    tags: parseTags(editorField("tags").value),
  };
  try {
    if (state.editingId == null) await api("POST", "/api/links", body);
    else await api("PUT", `/api/links/${state.editingId}`, body);
    $("editor").close();
    await loadLinks();
  } catch (err) {
    $("editor-error").textContent = err.message;
  }
}

// ---- credentials -------------------------------------------------------------

const credField = (name) => $("cred-form").elements.namedItem(name);

function credMessage(text, success = false) {
  const node = $("cred-message");
  node.textContent = text;
  node.classList.toggle("success", success);
}

async function openCredentials(host = "") {
  try {
    state.credentials = await api("GET", "/api/credentials");
  } catch (err) {
    alert(err.message);
    return;
  }
  renderCredentials();
  const existing = host && state.credentials.find((c) => hostMatches(c.host, host));
  editCredential(existing || null, host);
  const dialog = $("creds");
  if (!dialog.open) dialog.showModal();
}

function renderCredentials() {
  $("cred-rows").replaceChildren(...state.credentials.map((cred) => el("tr", {},
    el("td", {}, cred.label),
    el("td", {}, el("code", {}, cred.host)),
    el("td", {}, AUTH_LABELS[cred.type],
      cred.type === "basic" ? ` (${cred.username})` : "",
      cred.type === "header" ? ` (${cred.header})` : ""),
    el("td", {},
      el("button", { type: "button", onclick: () => testCredential(cred) }, "Test"), " ",
      el("button", { type: "button", onclick: () => editCredential(cred) }, "Edit"), " ",
      el("button", { type: "button", class: "danger", onclick: () => deleteCredential(cred) }, "Delete")))));
  $("cred-empty").classList.toggle("hidden", state.credentials.length > 0);
}

function editCredential(cred, host = "") {
  state.editingCredId = cred ? cred.id : null;
  $("cred-heading").textContent = cred ? `Edit credential \u201c${cred.label}\u201d` : "Add credential";
  credField("label").value = cred ? cred.label : "";
  credField("host").value = cred ? cred.host : host;
  credField("type").value = cred ? cred.type : "bearer";
  credField("username").value = cred ? cred.username : "";
  credField("header").value = cred ? cred.header : "";
  credField("secret").value = "";
  credField("secret").placeholder = cred ? "Leave empty to keep the saved secret" : "";
  updateAuthFields();
  credMessage("");
  credField(cred ? "secret" : host ? "label" : "host").focus();
}

function updateAuthFields() {
  const type = credField("type").value;
  for (const label of $("cred-form").querySelectorAll("[data-auth]")) {
    label.classList.toggle("hidden", label.dataset.auth !== type);
  }
}

async function saveCredential(event) {
  event.preventDefault();
  const body = {
    label: credField("label").value,
    host: credField("host").value,
    type: credField("type").value,
    username: credField("username").value,
    header: credField("header").value,
    secret: credField("secret").value,
  };
  try {
    const saved = state.editingCredId
      ? await api("PUT", `/api/credentials/${state.editingCredId}`, body)
      : await api("POST", "/api/credentials", body);
    state.credentials = await api("GET", "/api/credentials");
    renderCredentials();
    editCredential(saved);
    credMessage(`Saved \u201c${saved.label}\u201d. Use Test to verify it, then re-check the affected links.`, true);
  } catch (err) {
    credMessage(err.message);
  }
}

async function deleteCredential(cred) {
  if (!confirm(`Delete credential \u201c${cred.label}\u201d for ${cred.host}?`)) return;
  try {
    await api("DELETE", `/api/credentials/${cred.id}`);
    state.credentials = await api("GET", "/api/credentials");
    renderCredentials();
    editCredential(null);
  } catch (err) {
    credMessage(err.message);
  }
}

async function testCredential(cred) {
  const sample = state.links.find((l) => hostMatches(cred.host, hostOf(l.url)));
  const fallback = cred.host.startsWith("*.") ? "" : `https://${cred.host}/`;
  const url = prompt(`URL to test with \u201c${cred.label}\u201d:`, sample ? sample.url : fallback);
  if (!url) return;
  credMessage("Testing\u2026");
  try {
    const r = await api("POST", "/api/credentials/test", { url });
    const used = r.credential ? `using \u201c${r.credential.label}\u201d` : "without any credential (no host match)";
    const detail = [r.code ? `HTTP ${r.code}` : "", r.error || ""].filter(Boolean).join(", ");
    credMessage(`${STATUS_LABELS[r.status]} ${used}${detail ? ` \u2014 ${detail}` : ""}`, r.status === "ok");
  } catch (err) {
    credMessage(err.message);
  }
}

// ---- wiring ----------------------------------------------------------------

function handleAddParams() {
  const params = new URLSearchParams(location.search);
  if (!params.has("add")) return;
  history.replaceState(null, "", location.pathname);
  const url = params.get("add");
  const existing = state.links.find((l) => l.url === url);
  if (existing) openEditor(existing);
  else openEditor(null, { url, title: params.get("title") || "" });
}

function wire() {
  $("search").addEventListener("input", (e) => { state.query = e.target.value; render(); });
  $("search").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.target.value = state.query = ""; render(); }
  });
  $("status-filter").addEventListener("change", (e) => { state.status = e.target.value; render(); });
  $("sort").addEventListener("change", (e) => { state.sort = e.target.value; render(); });
  $("tag-filter").addEventListener("input", (e) => { state.tagFilter = e.target.value; renderTagList(); });

  $("btn-add").addEventListener("click", () => openEditor(null));
  $("btn-check").addEventListener("click", () =>
    startCheck(state.visible.length === state.links.length ? null : state.visible.map((l) => l.id)));
  $("btn-creds").addEventListener("click", () => openCredentials());
  $("btn-import").addEventListener("click", () => $("import-file").click());
  $("import-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    try {
      const content = await file.text();
      const isBackup = file.name.toLowerCase().endsWith(".json");
      const r = await api("POST", "/api/import", isBackup ? { backup: JSON.parse(content) } : { html: content });
      alert(`Imported ${r.added} new links. ${r.merged} already existed and received the ${isBackup ? "backup" : "folder"} tags. ${r.skipped} skipped.`);
    } catch (err) {
      alert(`Import failed: ${err.message}`);
    }
    await loadLinks();
  });

  $("btn-export").addEventListener("click", () => {
    $("export-error").textContent = "";
    $("export-dialog").showModal();
  });
  $("export-cancel").addEventListener("click", () => $("export-dialog").close());
  $("export-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.target.elements;
    const urls = [];
    if (form.json.checked) urls.push("/api/export");
    if (form.chrome.checked) urls.push("/api/export?format=chrome");
    if (!urls.length) {
      $("export-error").textContent = "Pick at least one format.";
      return;
    }
    $("export-dialog").close();
    // Staggered so the browser treats them as separate downloads rather than blocking the second.
    urls.forEach((url, i) => setTimeout(() => {
      const a = el("a", { href: url, download: "" });
      document.body.append(a);
      a.click();
      a.remove();
    }, i * 400));
  });

  $("select-all").addEventListener("change", (e) => {
    if (e.target.checked) state.visible.forEach((l) => state.selected.add(l.id));
    else state.selected.clear();
    renderRows();
    renderBulkBar();
  });
  $("bulk-clear").addEventListener("click", () => { state.selected.clear(); renderRows(); renderBulkBar(); });
  $("bulk-check").addEventListener("click", () => startCheck([...state.selected]));
  $("bulk-add-tags").addEventListener("click", () => {
    const text = prompt(`Tags to add to ${state.selected.size} link(s), separated by spaces:`);
    if (text && parseTags(text).length) bulk({ add_tags: parseTags(text) });
  });
  $("bulk-remove-tags").addEventListener("click", () => {
    const present = [...new Set(state.links.filter((l) => state.selected.has(l.id)).flatMap((l) => l.tags))].sort();
    const text = prompt(`Tags to remove from ${state.selected.size} link(s), separated by spaces.\nTags on the selection: ${present.join(" ") || "(none)"}`);
    if (text && parseTags(text).length) bulk({ remove_tags: parseTags(text) });
  });
  $("bulk-delete").addEventListener("click", () => {
    if (confirm(`Delete ${state.selected.size} link(s)? This cannot be undone.`)) bulk({ delete: true });
  });

  $("editor-form").addEventListener("submit", saveLink);
  $("btn-cancel").addEventListener("click", () => $("editor").close());
  $("btn-fetch-title").addEventListener("click", fetchTitle);
  editorField("url").addEventListener("input", updateDupWarning);
  editorField("url").addEventListener("change", () => { if (!editorField("title").value.trim()) fetchTitle(); });
  editorField("tags").addEventListener("input", updateTagSuggestions);

  $("cred-form").addEventListener("submit", saveCredential);
  $("cred-new").addEventListener("click", () => editCredential(null));
  $("cred-close").addEventListener("click", () => $("creds").close());
  credField("type").addEventListener("change", updateAuthFields);

  const bookmarklet = $("bookmarklet");
  bookmarklet.href = `javascript:void(window.open(${JSON.stringify(`${location.origin}/?add=`)}` +
    `+encodeURIComponent(location.href)+"&title="+encodeURIComponent(document.title)))`;
  bookmarklet.addEventListener("click", (e) => {
    e.preventDefault();
    alert("Drag this link to your browser's bookmarks bar. Then click it on any page to save that page here.");
  });

  document.addEventListener("keydown", (e) => {
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
    if (e.key === "/" && !typing && !document.querySelector("dialog[open]")) {
      e.preventDefault();
      $("search").focus();
    }
  });
}

async function init() {
  wire();
  try {
    state.check = await api("GET", "/api/check");
    await loadLinks();
  } catch (err) {
    $("empty").textContent = `Cannot reach the server: ${err.message}`;
    $("empty").classList.remove("hidden");
    return;
  }
  if (state.check.running) state.pollTimer = setTimeout(pollCheck, 1500);
  handleAddParams();
}

init();
