const state = { works: [], selected: null };
const list = document.querySelector("#work-list");
const count = document.querySelector("#work-count");
const detail = document.querySelector("#work-detail");
const empty = document.querySelector("#empty-state");
const dialog = document.querySelector("#new-work-dialog");
const newForm = document.querySelector("#new-work-form");
const labourForm = document.querySelector("#labour-form");

const formatDate = (value) => new Intl.DateTimeFormat("fi-FI", {
  dateStyle: "short", timeStyle: "short"
}).format(new Date(value));

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "Pyyntö epäonnistui");
  }
  return response.json();
}

function showToast(message) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  window.setTimeout(() => toast.classList.add("hidden"), 4200);
}

function renderList() {
  count.textContent = `${state.works.length} kpl`;
  if (!state.works.length) {
    list.innerHTML = '<p class="empty-list">Ei tallennettuja tiedusteluja.</p>';
    return;
  }
  list.replaceChildren(...state.works.map((work) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `work-row${state.selected?.id === work.id ? " active" : ""}`;
    button.innerHTML = `<strong></strong><span class="description"></span><span class="work-meta"><span>Kesken</span><span></span></span>`;
    button.querySelector("strong").textContent = work.registration;
    button.querySelector(".description").textContent = work.customer_description;
    button.querySelector(".work-meta span:last-child").textContent = formatDate(work.updated_at);
    button.addEventListener("click", () => openWork(work.id));
    return button;
  }));
}

function renderDetail() {
  const work = state.selected;
  empty.classList.toggle("hidden", Boolean(work));
  detail.classList.toggle("hidden", !work);
  if (!work) return;
  document.querySelector("#detail-registration").textContent = work.registration;
  document.querySelector("#detail-description").textContent = work.customer_description;
  document.querySelector("#detail-created").textContent = formatDate(work.created_at);
  labourForm.elements.hours.value = work.labour_hours || "";
  labourForm.elements.work_description.value = work.labour_work_description || "";
  labourForm.elements.recorded_by.value = work.labour_recorded_by || "";
  labourForm.elements.autodata_reference.value = work.autodata_reference || "";
  const price = document.querySelector("#price-summary");
  price.classList.toggle("hidden", !work.total_price);
  if (work.total_price) {
    price.innerHTML = `<span>Työkustannus<strong>${work.total_price} €</strong></span><span>Veroton<strong>${work.net_price} €</strong></span><span>ALV 25,5 %<strong>${work.vat_price} €</strong></span>`;
  }
  const events = document.querySelector("#event-list");
  events.replaceChildren(...work.events.map((event) => {
    const item = document.createElement("li");
    const title = event.event_type === "TIEDUSTELU_LUOTU" ? "Tiedustelu luotu" : "Autodata-työaika kirjattu";
    item.innerHTML = `<strong></strong><span></span>`;
    item.querySelector("strong").textContent = title;
    item.querySelector("span").textContent = `${formatDate(event.occurred_at)} · ${event.actor}`;
    return item;
  }));
}

async function loadWorks() {
  state.works = await api("/api/works");
  renderList();
}

async function openWork(id) {
  state.selected = await api(`/api/works/${id}`);
  renderList();
  renderDetail();
}

function openNewDialog() {
  document.querySelector("#new-work-error").classList.add("hidden");
  dialog.showModal();
  window.setTimeout(() => newForm.elements.registration.focus(), 0);
}

document.querySelector("#new-work-button").addEventListener("click", openNewDialog);
document.querySelectorAll("[data-open-new]").forEach((button) => button.addEventListener("click", openNewDialog));
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => dialog.close()));

newForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.querySelector("#new-work-error");
  const data = Object.fromEntries(new FormData(newForm));
  try {
    const created = await api("/api/works", { method: "POST", body: JSON.stringify(data) });
    dialog.close();
    newForm.reset();
    await loadWorks();
    await openWork(created.id);
    showToast("Tiedustelu tallennettiin.");
  } catch (problem) {
    error.textContent = problem.message;
    error.classList.remove("hidden");
  }
});

labourForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(labourForm));
  data.version = state.selected.version;
  try {
    state.selected = await api(`/api/works/${state.selected.id}/labour`, {
      method: "PUT", body: JSON.stringify(data)
    });
    await loadWorks();
    renderDetail();
    showToast("Työaika ja kustannus tallennettiin.");
  } catch (problem) {
    showToast(problem.message);
  }
});

loadWorks().catch((problem) => showToast(problem.message));
