const state = {
  works: [], listLoaded: false, selected: null, selectionEpoch: 0, listEpoch: 0,
  labourPending: false, createPending: false, creationKey: null
};
const list = document.querySelector("#work-list");
const count = document.querySelector("#work-count");
const detail = document.querySelector("#work-detail");
const empty = document.querySelector("#empty-state");
const dialog = document.querySelector("#new-work-dialog");
const newForm = document.querySelector("#new-work-form");
const labourForm = document.querySelector("#labour-form");
const inquiryDrafts = CarbitInquiryDrafts({
  api, form: newForm, dialog, workBusy: () => state.createPending
});
const autodata = CarbitAutodata({
  api, canWrite: () => CarbitAuth.canWrite(),
  current: () => ({ work: state.selected, selectionEpoch: state.selectionEpoch }),
  onWorkUpdated, toast: showToast
});
const moneyFormatter = new Intl.NumberFormat("fi-FI", {
  style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2
});
const formatMoney = (value) => moneyFormatter.format(value);
const formatDate = (value) => new Intl.DateTimeFormat("fi-FI", {
  dateStyle: "short", timeStyle: "short"
}).format(new Date(value));

const fieldNames = {
  registration: "Rekisterinumero", customer_description: "Asiakkaan alkuperäinen työkuvaus",
  actor: "Kirjaaja", recorded_by: "Kirjaaja", hours: "Työaika",
  work_description: "Autodatan työn kuvaus", autodata_reference: "Autodata-viite",
  version: "Työn versio", idempotency_key: "Lähetysavain"
};

function formatApiError(detail) {
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail)) return "Pyyntö epäonnistui. Yritä uudelleen.";
  return detail.map((error) => {
    const field = fieldNames[error.loc?.at(-1)] || "Pyyntö";
    let message;
    switch (error.type) {
      case "missing": message = "Kenttä on pakollinen."; break;
      case "string_too_short": message = "Kenttä ei voi olla tyhjä."; break;
      case "string_too_long": message = `Enintään ${error.ctx?.max_length} merkkiä.`; break;
      case "greater_than": message = `Arvon on oltava suurempi kuin ${error.ctx?.gt}.`; break;
      case "greater_than_equal": message = `Arvon on oltava vähintään ${error.ctx?.ge}.`; break;
      case "decimal_parsing":
      case "decimal_type":
      case "finite_number": message = "Anna kelvollinen, äärellinen numero."; break;
      case "int_parsing":
      case "int_type": message = "Anna kokonaisluku."; break;
      case "uuid_parsing": message = "Lähetysavain on virheellinen. Avaa uusi tiedustelu."; break;
      default:
        message = error.msg?.includes("Kenttä ei voi olla tyhjä")
          ? "Kenttä ei voi olla tyhjä." : "Tarkista kentän arvo.";
    }
    return `${field}: ${message}`;
  }).join("\n");
}

let sessionCsrf = null;
let csrfRefresh = null;
let csrfGeneration = 0;
let draftDialogOpen = false;

function lockBusiness() {
  forgetSessionCsrf();
  inquiryDrafts.lock();
  autodata.lock();
  state.selectionEpoch += 1;
  state.listEpoch += 1;
  draftDialogOpen ||= dialog.open;
  dialog.close();
  document.querySelector("#business-shell").hidden = true;
  document.querySelector("#business-shell").inert = true;
}

function discardBusiness() {
  forgetSessionCsrf();
  state.works = []; state.listLoaded = false; state.selected = null; state.creationKey = null;
  state.selectionEpoch += 1; state.listEpoch += 1;
  draftDialogOpen = false;
  newForm.reset(); labourForm.reset();
  inquiryDrafts.discard();
  autodata.discard();
  delete labourForm.dataset.dirtyWork;
  document.querySelector("#labour-notice").textContent = "";
  list.replaceChildren(); count.textContent = "";
  for (const id of ["detail-registration", "detail-description", "detail-created", "price-summary", "event-list", "session-user", "toast", "new-work-error", "labour-error"]) {
    document.getElementById(id).replaceChildren();
  }
  labourForm.dataset.workId = ""; labourForm.dataset.version = "";
  detail.classList.add("hidden"); empty.classList.remove("hidden");
}

async function confirmBusiness(user, sameOwner) {
  const writer = ["OWNER", "SERVICE_MANAGER"].includes(user.role);
  document.querySelector("#session-user").textContent = `${user.display_name}${writer ? "" : " · Vain luku"}`;
  document.querySelector("#new-work-button").hidden = !writer;
  document.querySelectorAll("[data-open-new]").forEach(button => { button.hidden = !writer; });
  Array.from(labourForm.elements).forEach(control => { control.disabled = !writer; });
  labourForm.querySelector("button[type=submit]").disabled = !writer || state.labourPending;
  inquiryDrafts.render();
  autodata.confirm(writer, sameOwner);
  const shell = document.querySelector("#business-shell");
  shell.hidden = false; shell.inert = false;
  if (sameOwner) {
    // Do not renderDetail: it would overwrite unsent labour fields and version.
    if (draftDialogOpen && writer) dialog.showModal();
    draftDialogOpen = false;
    if (!state.listLoaded) await loadWorks();
    return;
  }
  await loadWorks();
}

function forgetSessionCsrf() {
  sessionCsrf = null;
  csrfGeneration += 1;
}

function cacheSessionProof(body, generation = csrfGeneration) {
  const fields = typeof body.csrf_token === "string" ? body.csrf_token.split(".") : [];
  const issued = Number(fields[3]);
  const expires = Number(fields[4]);
  const remaining = Math.min(expires * 1000 - Date.now(), (expires - issued) * 1000) - 5000;
  if (generation !== csrfGeneration || body.state !== "session" || fields.length !== 7
      || fields[0] !== "v1" || fields[2] !== "session" || body.csrf_token.length > 512
      || !Number.isSafeInteger(issued) || !Number.isSafeInteger(expires)
      || expires !== body.expires_at || expires - issued > 600 || remaining <= 0) {
    throw new Error("Kirjautuminen on vanhentunut. Kirjaudu uudelleen.");
  }
  sessionCsrf = { token: body.csrf_token, expires: expires * 1000 - 5000,
    until: performance.now() + remaining };
  return sessionCsrf.token;
}

async function sessionProof() {
  if (sessionCsrf && performance.now() < sessionCsrf.until
      && Date.now() < sessionCsrf.expires) return sessionCsrf.token;
  if (!csrfRefresh) {
    const generation = csrfGeneration;
    csrfRefresh = (async () => {
      const response = await fetch("/api/auth/csrf", {
        credentials: "same-origin", cache: "no-store",
        headers: { "X-Carbit-Auth": "bootstrap" }
      });
      const body = await response.json().catch(() => ({}));
      if (generation !== csrfGeneration) throw new Error("Kirjautumistila muuttui. Tarkista tila.");
      if (!response.ok) {
        if (response.status === 401) CarbitAuth.lock();
        const problem = new Error(response.status === 429
          ? CarbitAuth.rateLimitMessage(response.headers.get("Retry-After"))
          : "Kirjautumisen suojausta ei voitu vahvistaa. Tarkista kirjautumistila tai yritä myöhemmin.");
        problem.status = response.status; problem.retryAfter = response.headers.get("Retry-After");
        throw problem;
      }
      if (body.state !== "session") {
        CarbitAuth.lock();
        throw new Error("Kirjautuminen on vanhentunut. Kirjaudu uudelleen.");
      }
      return cacheSessionProof(body, generation);
    })().finally(() => { csrfRefresh = null; });
  }
  return csrfRefresh;
}

async function api(path, options = {}) {
  // Mark only transport stages. Rendering and local programming errors remain
  // ordinary errors; Autodata must not describe them as uncertain writes.
  const mark = (problem, outcome) => Object.assign(problem, { outcome });
  // Only Autodata supplies this guard. Other callers retain their auth policy.
  const { autodataGuard, ...requestOptions } = options;
  const checkContext = (outcome) => {
    if (autodataGuard && !autodataGuard()) {
      throw mark(new Error("Autodata-operaation kirjautumiskonteksti muuttui."), outcome);
    }
  };
  let authEpoch, requestStartedAt, headers;
  try {
    checkContext("unsent");
    if (!CarbitAuth.active()) throw new Error("Kirjautuminen on vanhentunut. Kirjaudu uudelleen.");
    authEpoch = CarbitAuth.epoch();
    requestStartedAt = Date.now();
    const method = (options.method || "GET").toUpperCase();
    headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
      if (!CarbitAuth.canWrite()) throw new Error("Käyttöoikeus ei riitä. Näkymä on vain lukua varten.");
      headers["X-CSRF-Token"] = await sessionProof();
    }
    checkContext("unsent");
    if (authEpoch !== CarbitAuth.epoch() || !CarbitAuth.active()) throw new Error("Kirjautumistila muuttui. Tarkista tila.");
  } catch (problem) { throw mark(problem, "unsent"); }

  let response;
  try {
    response = await fetch(path, { ...requestOptions, credentials: "same-origin", headers });
  } catch (problem) { throw mark(problem, "unconfirmed"); }
  const checkAbort = () => {
    if (options.signal?.aborted) throw mark(new DOMException("Pyyntö peruutettiin.", "AbortError"), "unconfirmed");
  };
  const checkEpoch = () => {
    checkContext("unconfirmed");
    checkAbort();
    if (authEpoch !== CarbitAuth.epoch() || !CarbitAuth.active()) {
      throw mark(new Error("Kirjautumistila muuttui. Tarkista tila."), "unconfirmed");
    }
  };
  checkEpoch();
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) forgetSessionCsrf();
    const body = await response.json().catch(() => ({}));
    checkContext("unconfirmed");
    checkAbort();
    if (authEpoch !== CarbitAuth.epoch()) {
      throw mark(new Error("Kirjautumistila muuttui. Tarkista tila."), "unconfirmed");
    }
    if (response.status === 401) CarbitAuth.lock();
    if (response.status === 403 && body?.detail === "Permission denied") {
      CarbitAuth.lock("denied", "Käyttöoikeus ei riitä. Tarkista nykyinen kirjautumistila.");
    }
    const problem = new Error(formatApiError(body?.detail));
    problem.status = response.status;
    if (body && typeof body === "object" && !Array.isArray(body)
        && typeof body.code === "string" && /^[A-Z_]{1,40}$/.test(body.code)) problem.code = body.code;
    problem.retryAfter = response.headers.get("Retry-After");
    throw mark(problem, "http");
  }
  let body;
  try { body = await response.json(); }
  catch (problem) { throw mark(problem, "unconfirmed"); }
  checkEpoch();
  CarbitAuth.activity(requestStartedAt);
  return body;
}

let toastTimer;
function showToast(message) {
  const toast = document.querySelector("#toast");
  window.clearTimeout(toastTimer);
  toast.textContent = message;
  toast.classList.remove("hidden");
  toastTimer = window.setTimeout(() => toast.classList.add("hidden"), 4200);
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
    button.innerHTML = '<strong></strong><span class="description"></span><span class="work-meta"><span>Kesken</span><span></span></span>';
    button.querySelector("strong").textContent = work.registration;
    button.querySelector(".description").textContent = work.customer_description;
    button.querySelector(".work-meta span:last-child").textContent = formatDate(work.updated_at);
    button.addEventListener("click", () => openWork(work.id));
    return button;
  }));
}

function labourConflict(work) {
  const notice = document.querySelector("#labour-notice");
  notice.textContent = `Lomake on tehty versiolle ${labourForm.dataset.version || "?"}; tiedustelu on nyt versiossa ${work.version} (${work.labour_hours || "?"} h, ${work.labour_source || "käsin"}). Tallennusta ei lähetetä. Palauta tallennettuun ja kirjaa uudelleen.`;
}

function fillLabourForm(work) {
  if (labourForm.dataset.dirtyWork === work.id) {
    if (Number(labourForm.dataset.version) !== work.version) labourConflict(work);
    return;
  }
  labourForm.dataset.workId = work.id;
  labourForm.dataset.version = String(work.version);
  const manual = work.labour_source !== "AUTODATA";
  labourForm.elements.hours.value = manual ? work.labour_hours || "" : "";
  labourForm.elements.work_description.value = manual ? work.labour_work_description || "" : "";
  labourForm.elements.autodata_reference.value = manual ? work.autodata_reference || "" : "";
  document.querySelector("#labour-notice").textContent = manual ? "" : "Manuaalinen kirjaus korvaa nykyisen Autodata-ajan";
}

function onWorkUpdated(work, decision = null) {
  // Called only for a confirmed response to this tab's retained accept command,
  // or an explicit GET refresh (without a decision).
  const raw = labourForm.dataset.version;
  const formVersion = Number(raw);
  const resulting = decision?.resulting_work_version;
  if (labourForm.dataset.dirtyWork === work.id && decision) {
    if (typeof raw === "string" && /^[1-9][0-9]*$/.test(raw)
        && Number.isSafeInteger(formVersion) && formVersion >= 1
        && Number.isSafeInteger(resulting) && resulting >= 2
        && Number.isSafeInteger(work.version)
        && formVersion === resulting - 1 && work.version === resulting) {
      labourForm.dataset.version = String(resulting);
      document.querySelector("#labour-notice").textContent = "Autodata-aika hyväksyttiin. Lähettämättömät arvosi säilyivät. Tallennus korvaa hyväksytyn Autodata-ajan.";
    } else labourConflict(work);
  }
  state.selected = work;
  renderDetail();
  renderList();
}

labourForm.addEventListener("input", () => {
  if (state.selected) labourForm.dataset.dirtyWork = state.selected.id;
});
document.querySelector("#labour-reset").addEventListener("click", () => {
  if (!state.selected || !CarbitAuth.canWrite()) return;
  delete labourForm.dataset.dirtyWork;
  fillLabourForm(state.selected);
});

function renderDetail() {
  const work = state.selected;
  empty.classList.toggle("hidden", Boolean(work));
  detail.classList.toggle("hidden", !work);
  document.querySelector("#labour-error").classList.add("hidden");
  if (!work) {
    labourForm.dataset.workId = "";
    labourForm.dataset.version = "";
    delete labourForm.dataset.dirtyWork;
    document.querySelector("#labour-notice").textContent = "";
  }
  labourForm.querySelector("button[type=submit]").disabled = state.labourPending || !CarbitAuth.canWrite();
  if (!work) return;
  document.querySelector("#detail-registration").textContent = work.registration;
  document.querySelector("#detail-description").textContent = work.customer_description;
  document.querySelector("#detail-created").textContent = formatDate(work.created_at);
  fillLabourForm(work);
  autodata.render(work);
  const price = document.querySelector("#price-summary");
  price.classList.toggle("hidden", !work.total_price);
  if (work.total_price) {
    price.replaceChildren(...[
      ["Työkustannus", work.total_price], ["Veroton", work.net_price], ["ALV 25,5 %", work.vat_price]
    ].map(([label, value]) => {
      const item = document.createElement("span");
      item.textContent = label;
      const amount = document.createElement("strong");
      amount.textContent = formatMoney(value);
      item.append(amount);
      return item;
    }));
  }
  document.querySelector("#event-list").replaceChildren(...work.events.map((event) => {
    const item = document.createElement("li");
    item.innerHTML = "<strong></strong><span></span>";
    item.querySelector("strong").textContent = event.event_type === "TIEDUSTELU_LUOTU"
      ? "Tiedustelu luotu" : event.event_type === "AUTODATA_HAKU_HYVAKSYTTY"
        ? "Autodata-haun työaika hyväksytty" : "Autodata-työaika kirjattu";
    const actor = event.details?.actor_attribution;
    const verified = actor?.version === 1 && actor.provenance === "authenticated_session"
      && actor.user_id === event.actor && typeof actor.display_name === "string";
    const label = verified ? `${actor.display_name} · ${actor.user_id} (kirjautunut käyttäjä)`
      : `${event.actor} (vanha itse ilmoitettu kirjaaja)`;
    item.querySelector("span").textContent = `${formatDate(event.occurred_at)} · ${label}`;
    return item;
  }));
}

async function loadWorks() {
  const epoch = ++state.listEpoch;
  const works = await api("/api/works");
  if (epoch !== state.listEpoch) return;
  state.works = works;
  state.listLoaded = true;
  renderList();
}

async function openWork(id) {
  const epoch = ++state.selectionEpoch;
  state.selected = null;
  autodata.open(null, epoch);
  renderList();
  renderDetail();
  try {
    const work = await api(`/api/works/${id}`);
    if (epoch !== state.selectionEpoch) return;
    state.selected = work;
    autodata.open(work, epoch);
    renderList();
    renderDetail();
  } catch (problem) {
    if (epoch === state.selectionEpoch) showToast(problem.message);
  }
}

function openNewDialog() {
  if (state.createPending || !CarbitAuth.canWrite()) return;
  if (!inquiryDrafts.startNew()) return;
  state.creationKey = crypto.randomUUID();
  document.querySelector("#new-work-error").classList.add("hidden");
  dialog.showModal();
  newForm.elements.registration.focus();
}

document.querySelector("#new-work-button").addEventListener("click", openNewDialog);
document.querySelectorAll("[data-open-new]").forEach((button) => button.addEventListener("click", openNewDialog));
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => {
  if (!state.createPending && !inquiryDrafts.busy()) dialog.close();
}));
dialog.addEventListener("cancel", (event) => {
  if (state.createPending || inquiryDrafts.busy()) event.preventDefault();
});

newForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.createPending || !CarbitAuth.canWrite() || !inquiryDrafts.canSubmit()) return;
  const error = document.querySelector("#new-work-error");
  error.classList.add("hidden");
  const data = Object.fromEntries(new FormData(newForm));
  data.idempotency_key = state.creationKey;
  const epoch = state.selectionEpoch;
  state.createPending = true;
  Array.from(newForm.elements).forEach((control) => { control.disabled = true; });
  try {
    const created = await api("/api/works", { method: "POST", body: JSON.stringify(data) });
    dialog.close();
    state.creationKey = null;
    newForm.reset();
    inquiryDrafts.submitted();
    showToast("Tiedustelu tallennettiin.");
    if (epoch === state.selectionEpoch) await openWork(created.id);
    loadWorks().catch(() => showToast("Tiedustelu tallennettiin, mutta työlistan päivitys epäonnistui."));
  } catch (problem) {
    error.textContent = problem.message;
    error.classList.remove("hidden");
  } finally {
    state.createPending = false;
    Array.from(newForm.elements).forEach((control) => { control.disabled = false; });
    inquiryDrafts.render();
  }
});

labourForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const work = state.selected;
  if (!CarbitAuth.canWrite() || state.labourPending || !work || labourForm.dataset.workId !== work.id) return;
  if (Number(labourForm.dataset.version) !== work.version) {
    labourConflict(work);
    return;
  }
  const epoch = state.selectionEpoch;
  const data = Object.fromEntries(new FormData(labourForm));
  data.version = work.version;
  const error = document.querySelector("#labour-error");
  error.classList.add("hidden");
  state.labourPending = true;
  labourForm.querySelector("button[type=submit]").disabled = true;
  try {
    const saved = await api(`/api/works/${work.id}/labour`, {
      method: "PUT", body: JSON.stringify(data)
    });
    if (epoch === state.selectionEpoch && state.selected?.id === work.id
        && Number.isSafeInteger(saved.version) && Number.isSafeInteger(state.selected.version)
        && saved.version >= state.selected.version) {
      state.selected = saved;
      delete labourForm.dataset.dirtyWork;
      renderDetail();
      showToast("Työaika ja kustannus tallennettiin.");
    }
    loadWorks().catch(() => showToast("Työaika tallennettiin, mutta työlistan päivitys epäonnistui."));
  } catch (problem) {
    if (epoch === state.selectionEpoch && state.selected?.id === work.id) {
      error.textContent = problem.message;
      error.classList.remove("hidden");
    }
  } finally {
    state.labourPending = false;
    labourForm.querySelector("button[type=submit]").disabled = !CarbitAuth.canWrite();
  }
});

CarbitAuth.start({ lock: lockBusiness, discard: discardBusiness, confirm: confirmBusiness, sessionProof, cacheProof: cacheSessionProof });
