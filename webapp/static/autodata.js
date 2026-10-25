/* Synthetic lookup UI. The server is the authority for time and acceptance. */
function CarbitAutodata({ api, canWrite, current, onWorkUpdated, toast }) {
  const panel = document.querySelector("#autodata-panel");
  const retained = new Map();
  const closed = new Set();
  const searchContexts = new Map();
  const inFlight = { lookup: false, accept: false };
  const reads = new Set();
  let work = null, selectionEpoch = 0, panelEpoch = 0;
  let cardGeneration = 0, checkGeneration = 0, historyGeneration = 0, reloadGeneration = 0;
  let writer = false, locked = false, enabled = null, card = null, lookupId = null;
  let loggingOut = false, logoutGeneration = 0;
  let check = null, timer = null, stale = false;
  let form, status, error, result, retries, actions, lookupList, decisionList;
  let lookupNext = null, decisionNext = null, lookups = [], decisions = [];
  const explanation = "Hyväksyntä tallentaa haun työajan tiedusteluun. Se ei ole työaikasuunnitelman hyväksyntä, laskutushyväksyntä eikä tarjous.";
  const messages = {
    VEHICLE_NOT_FOUND: "Ajoneuvoa ei löytynyt. Tarkenna hakua tai kirjaa käsin.",
    WORK_NOT_FOUND: "Työtä ei löytynyt. Tarkenna hakua tai kirjaa käsin.",
    TIME_MISSING: "Lähteessä ei ollut työaikaa.",
    UNPARSEABLE: "Lähteen aikaa ei voitu tulkita.",
    LOGIN_REQUIRED: "Autodata vaatii kirjautumisen (IT-tuki).",
    BLOCKED: "Haku pysäytettiin.", SITE_CHANGED: "Lähteen sisältöä ei voitu käyttää.",
    TIMEOUT: "Haku aikakatkaistiin.", ERROR: "Haku epäonnistui.",
    EXPIRED: "Vanhentunut. Tee uusi haku.",
    INVALID_LOOKUP_TIME: "Haun työaika ei kelpaa. Tee uusi haku.",
    STALE_LOOKUP: "Tiedustelu on muuttunut haun jälkeen. Hakua ei voi enää hyväksyä.",
    ACCEPTED: "Haku on jo hyväksytty.", REDACTED: "Haun nimiöt ja lähdeteksti on poistettu."
  };
  const rejectionMessages = {
    EMPTY: "lähteen aikateksti on tyhjä", TOO_LONG: "lähteen aikateksti on liian pitkä",
    CLOCK_FORMAT: "kellonaika ei ole työaika", RANGE: "aikaväli ei ole yksi työaika",
    MISSING_UNIT: "yksikkö puuttuu", UNKNOWN_UNIT: "yksikköä ei tunnistettu",
    AMBIGUOUS_SEPARATOR: "desimaalierotin on epäselvä", NOT_POSITIVE: "työajan on oltava positiivinen",
    MALFORMED: "aikamuoto ei kelpaa"
  };

  function node(tag, text, className) {
    const value = document.createElement(tag);
    if (text !== undefined) { value.textContent = text; value.dir = "auto"; }
    if (className) value.className = className;
    return value;
  }
  function button(text, action, parent, id) {
    const value = node("button", text, "secondary");
    value.type = "button";
    if (id) value.id = id;
    value.addEventListener("click", action);
    parent.append(value);
    return value;
  }
  function date(value) {
    return new Intl.DateTimeFormat("fi-FI", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
  }
  function hours(value) { return typeof value === "string" ? value.replace(".", ",") : "?"; }
  function cancelTimer() { window.clearTimeout(timer); timer = null; }
  const blocked = () => locked || loggingOut;
  function authGuard() {
    const generation = logoutGeneration;
    return () => !loggingOut && generation === logoutGeneration;
  }
  async function read(path) {
    if (blocked()) throw new Error("Autodata-operaatiot odottavat kirjautumistilan vahvistusta.");
    const controller = new AbortController();
    reads.add(controller);
    try { return await api(path, { signal: controller.signal, autodataGuard: authGuard() }); }
    finally { reads.delete(controller); }
  }
  function cancelReads() {
    panelEpoch += 1;
    for (const controller of reads) controller.abort();
    reads.clear();
  }
  function beginLogout() {
    if (!CarbitAuth.active() || loggingOut) return;
    loggingOut = true; logoutGeneration += 1;
    cancelTimer(); cancelReads(); syncButtons();
  }
  // Freeze this module before logout revokes the session. Writes finish, but
  // their old generation cannot lock auth or consume a retained command.
  document.querySelector("#logout-button").addEventListener("click", beginLogout, { capture: true });
  function commands(id = work?.id) { return retained.get(id) || {}; }
  function keep(command) {
    const entry = commands(command.inquiry_id);
    retained.set(command.inquiry_id, { ...entry, [command.kind]: command });
  }
  function release(command) {
    if (commands(command.inquiry_id)[command.kind] !== command) return;
    const entry = { ...commands(command.inquiry_id) };
    delete entry[command.kind];
    if (Object.keys(entry).length) retained.set(command.inquiry_id, entry);
    else retained.delete(command.inquiry_id);
  }
  function token(op, extra = {}) {
    return { op, inquiry_id: work?.id, selectionEpoch, panelEpoch, ...extra };
  }
  function matches(t) {
    return !blocked() && work?.id === t.inquiry_id && current().work?.id === t.inquiry_id
      && selectionEpoch === t.selectionEpoch && current().selectionEpoch === t.selectionEpoch
      && panelEpoch === t.panelEpoch
      && (t.cardGeneration === undefined || cardGeneration === t.cardGeneration)
      && (t.checkGeneration === undefined || checkGeneration === t.checkGeneration)
      && (t.historyGeneration === undefined || historyGeneration === t.historyGeneration)
      && (t.reloadGeneration === undefined || reloadGeneration === t.reloadGeneration)
      && (t.lookup_id === undefined || lookupId === t.lookup_id);
  }
  function notify(text, failed = false) {
    if (!status || blocked()) return;
    error.textContent = failed ? text : "";
    error.hidden = !failed;
    status.textContent = failed ? "" : text;
    (failed ? error : status).focus();
  }
  function authError(problem) {
    if (problem.status === 403 && problem.code !== "PERMISSION_DENIED" && CarbitAuth.active()) {
      CarbitAuth.lock("denied", "Käyttöoikeus tai suojaus ei kelpaa. Tarkista kirjautumistila.");
    }
    return problem.status === 401 || (problem.status === 403 && problem.code !== "PERMISSION_DENIED");
  }
  function clearCard() {
    cancelTimer(); cardGeneration += 1; checkGeneration += 1;
    card = null; lookupId = null; check = null;
    if (result?.contains(document.activeElement) && !locked) status?.focus();
    if (result) result.replaceChildren();
  }
  function removeAcceptance() {
    const accept = panel.querySelector("#autodata-accept");
    const label = panel.querySelector("#autodata-confirm")?.closest("label");
    const focused = accept === document.activeElement || label?.contains(document.activeElement);
    accept?.remove(); label?.remove();
    if (focused && !locked) status.focus();
  }
  function syncButtons() {
    panel.inert = loggingOut;
    if (!form) return;
    form.hidden = !writer || enabled !== true || closed.has(work?.id);
    const busy = inFlight.lookup || inFlight.accept;
    form.setAttribute("aria-busy", String(busy));
    Array.from(form.elements).forEach(control => {
      control.disabled = blocked() || busy || stale || (control.type === "submit" && Boolean(commands().lookup));
    });
    const accept = panel.querySelector("#autodata-accept");
    const confirmation = panel.querySelector("#autodata-confirm");
    if (accept) accept.disabled = blocked() || busy || !canWrite() || !confirmation?.checked
      || Boolean(commands().accept) || enabled !== true
      || check?.acceptability !== "ACCEPTABLE" || check?.enabled !== true;
    if (confirmation) confirmation.disabled = blocked() || busy;
    if (enabled === false || !writer) removeAcceptance();
    panel.querySelectorAll("[data-autodata-retry]").forEach(value => {
      const command = commands()[value.dataset.autodataRetry];
      value.disabled = blocked() || busy || !canWrite() || !command || Date.now() < (command.retryAt || 0);
    });
    panel.querySelectorAll("[data-autodata-new]").forEach(value => { value.disabled = blocked() || busy || stale; });
    panel.querySelectorAll("#autodata-history button, #autodata-check, #autodata-reload").forEach(value => {
      value.disabled = blocked();
    });
    const next = panel.querySelector("#autodata-continue");
    if (next) next.disabled = blocked() || busy || !canWrite() || Boolean(commands().lookup);
  }
  function renderRetries() {
    if (!retries) return;
    cancelTimer();
    const wasFocused = retries.contains(document.activeElement);
    retries.replaceChildren();
    for (const kind of ["lookup", "accept"]) {
      const command = commands()[kind];
      if (!command || !writer) continue;
      if (kind === "accept" && lookupId && lookupId !== command.lookup_id) continue;
      if (kind === "accept" && !card) retries.append(node("p", "Edellisen hyväksynnän tulos jäi vahvistamatta"));
      const text = kind === "accept" ? "Lähetä sama hyväksyntä uudelleen"
        : command.retryAt || command.reason === "LOOKUP_BUSY" || command.reason === "LOOKUP_RATE_LIMITED"
          ? "Yritä samaa hakua uudelleen" : "Lähetä sama haku uudelleen";
      const retry = button(text, () => send(command), retries);
      retry.dataset.autodataRetry = kind;
      if (command.retryAt && Date.now() < command.retryAt) {
        retries.append(node("p", `Uusi haku mahdollinen noin klo ${new Intl.DateTimeFormat("fi-FI", { hour: "2-digit", minute: "2-digit" }).format(command.retryAt)}`));
        const t = token("retry-timer", { cardGeneration });
        timer = window.setTimeout(() => { if (matches(t)) syncButtons(); }, command.retryAt - Date.now() + 10);
      }
    }
    syncButtons();
    if (wasFocused && !locked) status.focus();
  }
  function newSearch() {
    if (stale || blocked() || inFlight.lookup || inFlight.accept) return;
    clearCard(); actions.replaceChildren();
    notify("Tarkenna hakua tai tee uusi haku.");
    renderRetries(); syncButtons();
    if (!form.hidden) form.elements.work_query.focus();
  }
  function conflict(code) {
    actions.replaceChildren();
    if (["STALE_VIEW", "STALE_LOOKUP"].includes(code)) {
      stale = true;
      button("Lataa tiedustelu uudelleen", reloadWork, actions, "autodata-reload");
    }
    if (["STALE_LOOKUP", "EXPIRED", "INVALID_LOOKUP_TIME", "REQUEST_REDACTED"].includes(code)) {
      const fresh = button("Tee uusi haku", newSearch, actions);
      fresh.dataset.autodataNew = "true";
    }
    syncButtons();
  }
  async function reloadWork() {
    if (blocked() || !work) return;
    const t = token("reload", { reloadGeneration: ++reloadGeneration });
    try {
      const value = await read(`/api/works/${work.id}`);
      if (!matches(t)) return;
      onWorkUpdated(value); stale = false; syncButtons();
      notify("Tiedustelu ladattiin. Lähettämättömät manuaaliset kentät säilyivät.");
    } catch (problem) {
      if (!matches(t)) return;
      if (!authError(problem)) notify(problem.message, true);
    }
  }
  function command(kind, path, data, id = null) {
    return Object.freeze({ kind, inquiry_id: work.id, lookup_id: id, path,
      body: JSON.stringify(data) });
  }
  function lookupCommand(selections = {}, context = null) {
    let facts = context?.vehicle_facts, query = context?.work_query;
    if (!context) {
      const fields = Object.fromEntries(new FormData(form));
      facts = {};
      for (const name of ["vin", "make", "model", "model_year", "engine_code", "fuel", "power_kw", "displacement_cc"]) {
        const raw = fields[name].trim();
        facts[name] = !raw ? null : ["model_year", "power_kw", "displacement_cc"].includes(name) ? Number(raw) : raw;
      }
      query = fields.work_query.trim();
    }
    return command("lookup", `/api/works/${work.id}/autodata/lookups`, {
      request_id: crypto.randomUUID(), expected_version: work.version,
      vehicle_facts: facts, work_query: query,
      vehicle_selection: null, operation_selection: null, variant_selection: null, ...selections
    });
  }
  function validResponse(value, cmd) {
    if (!value || typeof value !== "object" || typeof value.replayed !== "boolean") return false;
    if (cmd.kind === "lookup") return typeof value.lookup?.lookup_id === "string"
      && value.lookup.lookup_id.length > 0 && value.lookup.inquiry_id === cmd.inquiry_id
      && typeof value.lookup.outcome === "string";
    return typeof value.decision?.decision_id === "string" && value.decision.decision_id.length > 0
      && value.decision.inquiry_id === cmd.inquiry_id && value.decision.lookup_id === cmd.lookup_id
      && value.work?.id === cmd.inquiry_id && Number.isSafeInteger(value.work.version)
      && Array.isArray(value.work.events);
  }
  async function send(cmd) {
    if (blocked() || !canWrite() || inFlight[cmd.kind] || !work || cmd.inquiry_id !== work.id) return;
    if (commands()[cmd.kind] && commands()[cmd.kind] !== cmd) return;
    inFlight[cmd.kind] = true;
    keep(cmd);
    if (cmd.kind === "lookup") clearCard();
    const t = token(cmd.kind, { cardGeneration, ...(cmd.kind === "accept" ? { lookup_id: lookupId } : {}) });
    notify(cmd.kind === "lookup" ? "Haetaan…" : "Hyväksytään…");
    renderRetries(); syncButtons();
    let value, confirmed = false;
    try {
      // Transport classification is deliberately separate from rendering.
      try {
        value = await api(cmd.path, { method: "POST", body: cmd.body, autodataGuard: authGuard() });
        if (!validResponse(value, cmd)) throw Object.assign(new Error("Vastausta ei voitu vahvistaa."), { outcome: "unconfirmed" });
        confirmed = true;
      } catch (problem) {
        const retainedHere = commands(cmd.inquiry_id)[cmd.kind] === cmd;
        const uncertain = problem.outcome === "unconfirmed" || (problem.status >= 500
          && problem.code !== "AUTODATA_DISABLED");
        const retryable = uncertain || problem.outcome === "unsent" || problem.status === 401
          || (problem.status === 403 && problem.code !== "PERMISSION_DENIED")
          || ["LOOKUP_BUSY", "LOOKUP_RATE_LIMITED"].includes(problem.code);
        if (retainedHere && !retryable) release(cmd);
        if (!matches(t)) return;
        if (authError(problem)) return;
        if (problem.code === "AUTODATA_DISABLED") enabled = false;
        if (problem.code === "AUTODATA_CLOSED") closed.add(cmd.inquiry_id);
        if (retryable && retainedHere) {
          let retryAt = 0;
          if (problem.code === "LOOKUP_RATE_LIMITED" && /^[0-9]+$/.test(problem.retryAfter || "")) {
            const seconds = Number(problem.retryAfter);
            if (Number.isSafeInteger(seconds) && seconds >= 1 && seconds <= 86400) retryAt = Date.now() + seconds * 1000;
          }
          // Immutable body survives; metadata does not change the command.
          const updated = Object.freeze({ ...cmd, reason: uncertain ? "UNCERTAIN" : problem.code, retryAt });
          keep(updated);
        }
        if (panel.querySelector("#autodata-confirm")) panel.querySelector("#autodata-confirm").checked = false;
        conflict(problem.code);
        notify(uncertain ? "Vastausta ei voitu vahvistaa. Haku tai hyväksyntä on voinut tallentua."
          : problem.code === "STALE_VIEW" ? "Tiedustelu on muuttunut toisaalla."
            : problem.code === "AUTODATA_DISABLED" ? "Autodata-haku ei ole käytössä. Kirjaa työaika käsin."
              : problem.code === "AUTODATA_CLOSED" ? "Tiedustelun Autodata-aineisto on suljettu"
                : messages[problem.code] || problem.message, true);
        if (cmd.kind === "accept" && card) checkCard();
        if (["STALE_LOOKUP", "ALREADY_ACCEPTED"].includes(problem.code)) loadHistory();
        return;
      }
      // This is a confirmed result even when the visible inquiry has changed.
      release(cmd);
      if (cmd.kind === "lookup" && value.lookup.result?.candidates?.length) {
        const original = JSON.parse(cmd.body);
        searchContexts.set(value.lookup.lookup_id, Object.freeze({ inquiry_id: cmd.inquiry_id,
          work_query: original.work_query, vehicle_facts: Object.freeze(original.vehicle_facts) }));
      }
      if (!matches(t)) return;
      if (cmd.kind === "lookup") {
        showCard(value.lookup);
        if (value.replayed) notify("Haku oli jo tallennettu – näytetään tallennettu tulos");
      } else {
        onWorkUpdated(value.work, value.decision);
        notify(`${value.replayed ? "Hyväksyntä oli jo tallennettu" : "Autodata-haun työaika hyväksyttiin tiedusteluun."} ${explanation}`);
        toast(value.replayed ? "Hyväksyntä oli jo tallennettu" : "Autodata-haun työaika hyväksyttiin tiedusteluun.");
        if (card) checkCard();
      }
      loadHistory();
    } catch (problem) {
      // A programming/render error after confirmation must never offer replay.
      if (!confirmed) release(cmd);
      if (matches({ ...t, cardGeneration: undefined, lookup_id: undefined })) {
        notify("Näkymän päivitys epäonnistui. Lataa tiedustelu uudelleen.", true);
      }
    } finally {
      inFlight[cmd.kind] = false;
      if (work) { renderRetries(); syncButtons(); }
    }
  }

  function heading(text) {
    const value = node("h4", text);
    value.tabIndex = -1; result.append(value); return value;
  }
  function renderChoice(resolved, label) {
    if (!resolved) return;
    const identity = resolved.choice.identity;
    result.append(node("p", `${label}: ${identity.selection_path.join(" › ")}${resolved.resolved_by === "ONLY_CANDIDATE" ? " · ainoa vaihtoehto – tarkista" : ""}`));
    for (const detail of resolved.choice.details || []) result.append(node("p", detail));
    if (identity.source_id) result.append(node("p", `Lähteen tunniste: ${identity.source_id}`));
  }
  function renderCard() {
    result.replaceChildren();
    if (!card) return;
    const h = heading(card.outcome === "FOUND" ? "Haun työaika" : "Hakutulos");
    const choices = card.result?.candidates;
    if (choices?.length && ["VEHICLE_AMBIGUOUS", "WORK_AMBIGUOUS", "VARIANT_AMBIGUOUS", "SELECTION_INVALID", "SELECTION_NOT_UNIQUE"].includes(card.outcome)) {
      const level = card.result.level;
      const remembered = searchContexts.get(card.lookup_id);
      const context = remembered?.inquiry_id === work.id ? remembered : null;
      const fieldset = node("fieldset");
      fieldset.append(node("legend", { VEHICLE: "Valitse ajoneuvo", OPERATION: "Valitse työ", VARIANT: "Valitse variantti" }[level] || "Valitse vaihtoehto"));
      if (card.outcome.startsWith("SELECTION_")) fieldset.append(node("p", "Aiempi valinta ei kelpaa tai ei ole yksikäsitteinen. Tarkista vaihtoehdot."));
      choices.forEach((choice, index) => {
        const label = node("label", undefined, "autodata-choice");
        const radio = node("input"); radio.type = "radio"; radio.name = "autodata-choice"; radio.value = String(index);
        radio.disabled = !writer || !context || blocked();
        label.append(radio, node("span", [choice.identity.selection_path.join(" › "), ...(choice.details || []), choice.identity.source_id || ""].filter(Boolean).join(" · ")));
        radio.addEventListener("change", () => {
          if (!context || blocked()) return;
          const old = commands().lookup; if (old) release(old);
          check = null;
          const confirmation = panel.querySelector("#autodata-confirm"); if (confirmation) confirmation.checked = false;
          renderRetries(); syncButtons();
        });
        fieldset.append(label);
      });
      result.append(fieldset);
      if (!context) {
        result.append(node("p", "Alkuperäiset hakutiedot eivät ole käytettävissä. Tulos on vain lukua varten. Tee uusi haku."));
        if (writer) {
          const fresh = button("Tee uusi haku", newSearch, result);
          fresh.dataset.autodataNew = "true";
        }
      }
      if (writer && context) button("Jatka valinnalla", () => {
        if (inFlight.lookup || blocked() || !canWrite()) return;
        const radio = fieldset.querySelector("input:checked");
        if (!radio) { notify("Valitse vaihtoehto.", true); return; }
        const selections = {};
        for (const name of ["vehicle", "operation", "variant"]) {
          selections[`${name}_selection`] = card.result[name]?.choice.identity || null;
        }
        selections[`${level.toLowerCase()}_selection`] = choices[Number(radio.value)].identity;
        send(lookupCommand(selections, context));
      }, result, "autodata-continue");
    } else if (card.outcome === "FOUND") {
      if (card.redacted) result.append(node("p", messages.REDACTED, "autodata-badge"));
      else {
        for (const [name, label] of [["vehicle", "Ajoneuvo"], ["operation", "Työ"], ["variant", "Variantti"]]) renderChoice(card.result?.[name], label);
        result.append(node("p", `Lähteen teksti: ${card.raw_time_text || ""}`));
      }
      result.append(node("p", `Normalisoitu aika: ${card.time_text || "?"} · Työaika: ${hours(card.hours)} h`));
      result.append(node("p", `Lähde: ${card.adapter_name} ${card.adapter_version} · Haettu ${date(card.retrieved_at)} · Voimassa ${date(card.expires_at)}`));
      if (card.requested_by_name) result.append(node("p", `Hakija: ${card.requested_by_name}`));
      if (card.synthetic) result.append(node("p", "SYNTEETTINEN – ei Autodata-aineistoa", "autodata-badge"));
      const checked = node("p", "Tarkistetaan hyväksyttävyyttä…"); checked.id = "autodata-check-status"; result.append(checked);
      button("Tarkista uudelleen", checkCard, result, "autodata-check");
      result.append(node("p", explanation, "notice"));
      if (writer && enabled === true && !card.redacted) {
        const label = node("label", undefined, "autodata-choice");
        const box = node("input"); box.type = "checkbox"; box.id = "autodata-confirm";
        label.append(box, node("span", "Tarkistin ajoneuvon, operaation, variantin ja ajan"));
        result.append(label);
        box.addEventListener("change", syncButtons);
        button("Hyväksy työaika tiedusteluun", () => {
          if (!box.checked || check?.acceptability !== "ACCEPTABLE" || check?.enabled !== true
              || inFlight.accept || blocked() || commands().accept) return;
          send(command("accept", `/api/works/${work.id}/autodata/lookups/${card.lookup_id}/accept`, {
            request_id: crypto.randomUUID(), expected_version: work.version,
            confirmation: "VEHICLE_OPERATION_VARIANT_TIME"
          }, card.lookup_id));
        }, result, "autodata-accept");
      }
    } else {
      result.append(node("p", messages[card.outcome] || "Hakua ei voitu ratkaista."));
      if (card.outcome === "UNPARSEABLE") {
        result.append(node("p", rejectionMessages[card.rejection_code] || "Ajan esitysmuoto tai yksikkö ei kelpaa."));
        if (card.result?.rejected_time_text !== null) result.append(node("p", `Tulkitsematon lähdeteksti: ${card.result?.rejected_time_text || ""}`));
      }
      result.append(node("p", "Voit kirjata työajan käsin alla"));
    }
    renderRetries(); syncButtons(); h.focus();
  }
  function showCard(value) {
    clearCard(); card = value; lookupId = value.lookup_id;
    actions.replaceChildren();
    renderCard();
    if (value.outcome === "FOUND") checkCard();
  }
  function listShape(value) {
    return value && Array.isArray(value.items) && (value.next === null || typeof value.next === "string")
      && typeof value.autodata_enabled === "boolean" && value.items.every(row => row && typeof row.lookup_id === "string" && typeof row.acceptability === "string");
  }
  async function checkCard() {
    if (!card || card.outcome !== "FOUND" || blocked()) return;
    check = null;
    const t = token("check", { lookup_id: lookupId, cardGeneration, checkGeneration: ++checkGeneration });
    const line = panel.querySelector("#autodata-check-status");
    if (line) line.textContent = "Tarkistetaan hyväksyttävyyttä…";
    syncButtons();
    const cursors = new Set();
    let cursor = null;
    try {
      for (let page = 0; page < 20; page += 1) {
        const value = await read(`/api/works/${t.inquiry_id}/autodata/lookups${cursor ? `?before=${encodeURIComponent(cursor)}` : ""}`);
        if (!matches(t)) return;
        if (!listShape(value)) throw new Error("invalid lookup list");
        const row = value.items.find(item => item.lookup_id === t.lookup_id);
        if (row) {
          check = { acceptability: row.acceptability, enabled: value.autodata_enabled };
          enabled = value.autodata_enabled;
          if (line) line.textContent = messages[row.acceptability] || (row.acceptability === "ACCEPTABLE" ? "Hyväksyttävissä" : row.acceptability);
          if (["STALE_LOOKUP", "EXPIRED", "INVALID_LOOKUP_TIME"].includes(row.acceptability)) conflict(row.acceptability);
          if (row.acceptability !== "ACCEPTABLE") removeAcceptance();
          if (enabled === false) {
            // Remove controls without replacing the source card or its text.
            removeAcceptance();
            notify("Autodata-haku ei ole käytössä. Kirjaa työaika käsin.");
          }
          syncButtons(); return;
        }
        if (value.next === null) {
          if (line) line.textContent = "Haun tilaa ei löytynyt. Haku on voinut poistua.";
          return;
        }
        if (!value.next || cursors.has(value.next)) throw new Error("non-advancing cursor");
        cursors.add(value.next); cursor = value.next;
      }
      throw new Error("page limit");
    } catch (problem) {
      if (!matches(t)) return;
      if (authError(problem)) return;
      if (line) line.textContent = "Hyväksyttävyyttä ei voitu tarkistaa.";
      if (problem.code === "PERMISSION_DENIED") notify("Ei oikeutta.", true);
    } finally { if (matches(t)) syncButtons(); }
  }
  async function openCard(id) {
    if (blocked() || !work) return;
    clearCard(); lookupId = id;
    const t = token("open-card", { lookup_id: id, cardGeneration });
    renderRetries();
    try {
      const value = await read(`/api/works/${work.id}/autodata/lookups/${id}`);
      if (!matches(t)) return;
      if (value.lookup_id !== id || value.inquiry_id !== work.id) throw new Error("Vastaus ei kuulu hakuun.");
      showCard(value);
    } catch (problem) {
      if (!matches(t)) return;
      if (authError(problem)) return;
      clearCard();
      notify(problem.status === 404 ? "Hakua ei löytynyt. Tiedustelun Autodata-aineisto on voitu sulkea tai poistaa." : problem.message, problem.status !== 404);
      renderRetries();
    }
  }
  function drawHistory() {
    const focused = lookupList.contains(document.activeElement) || decisionList.contains(document.activeElement);
    lookupList.replaceChildren(node("h4", "Haut"));
    if (!lookups.length) lookupList.append(node("p", "Ei Autodata-hakuja."));
    for (const row of lookups) {
      const entry = node("div", undefined, "autodata-history-row");
      entry.append(node("span", `${row.outcome} · ${date(row.retrieved_at)}${row.redacted ? " · nimiöt poistettu" : ""}`));
      const open = button("Avaa", () => openCard(row.lookup_id), entry);
      open.dataset.lookupId = row.lookup_id;
      lookupList.append(entry);
    }
    if (lookupNext) button("Näytä vanhemmat haut", () => loadHistory("lookups"), lookupList);
    decisionList.replaceChildren(node("h4", "Päätökset"));
    for (const row of decisions) {
      decisionList.append(node("p", `Autodata, hyväksyjä ${row.decided_by_name || "tuntematon"}, ${date(row.decided_at)} · ${hours(row.hours)} h${row.redacted ? " · nimiöt poistettu" : ""}`));
    }
    if (decisionNext) button("Näytä vanhemmat päätökset", () => loadHistory("decisions"), decisionList);
    if (focused && !locked) status.focus();
  }
  async function loadHistory(more = null) {
    if (blocked() || !work) return;
    const t = token("history", { historyGeneration: ++historyGeneration });
    try {
      const kinds = more ? [more] : ["lookups", "decisions"];
      const values = await Promise.all(kinds.map(kind => {
        const cursor = more ? kind === "lookups" ? lookupNext : decisionNext : null;
        return read(`/api/works/${t.inquiry_id}/autodata/${kind}${cursor ? `?before=${encodeURIComponent(cursor)}` : ""}`);
      }));
      if (!matches(t)) return;
      values.forEach((value, index) => {
        const kind = kinds[index];
        if (!Array.isArray(value?.items) || !(value.next === null || typeof value.next === "string")) throw new Error("Historiaa ei voitu lukea.");
        if (kind === "lookups") {
          if (!listShape(value)) throw new Error("Historiaa ei voitu lukea.");
          lookups = more ? [...lookups, ...value.items] : value.items;
          lookupNext = value.next; enabled = value.autodata_enabled;
        } else { decisions = more ? [...decisions, ...value.items] : value.items; decisionNext = value.next; }
      });
      drawHistory(); renderRetries(); syncButtons();
      if (!enabled) notify("Autodata-haku ei ole käytössä. Kirjaa työaika käsin.");
    } catch (problem) {
      if (matches(t) && !authError(problem)) notify(problem.code === "PERMISSION_DENIED" ? "Ei oikeutta." : problem.message, true);
    }
  }

  function build() {
    panel.replaceChildren();
    const title = node("h3", "Autodata-haku"); title.id = "autodata-title"; title.tabIndex = -1;
    status = node("p", "", "notice"); status.id = "autodata-status";
    status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite"); status.tabIndex = -1;
    error = node("p", "", "form-error"); error.id = "autodata-error";
    error.setAttribute("role", "alert"); error.tabIndex = -1; error.hidden = true;
    retries = node("div", undefined, "autodata-actions"); retries.id = "autodata-retries";
    actions = node("div", undefined, "autodata-actions"); actions.id = "autodata-actions";
    form = node("form", undefined, "form-grid"); form.id = "autodata-form";
    const registration = node("p", `Rekisterinumero: ${work.registration}`);
    form.append(registration);
    for (const [name, label, max, min, ceiling] of [
      ["make", "Merkki", 100], ["model", "Malli", 100], ["model_year", "Vuosi", null, 1900, 2100],
      ["engine_code", "Moottorikoodi", 40], ["fuel", "Polttoaine", 40], ["power_kw", "Teho (kW)", null, 1, 2000],
      ["displacement_cc", "Tilavuus (cm³)", null, 1, 20000], ["vin", "VIN", 17], ["work_query", "Hakusana", 200]
    ]) {
      const wrapper = node("label", label); const input = node("input"); input.name = name;
      if (max) input.maxLength = max;
      if (min) { input.type = "number"; input.min = String(min); input.max = String(ceiling); input.step = "1"; }
      if (["vin", "work_query"].includes(name)) input.autocomplete = "off";
      if (name === "work_query") input.required = true;
      wrapper.append(input); form.append(wrapper);
    }
    const submit = node("button", "Hae Autodatasta", "primary"); submit.type = "submit"; form.append(submit);
    form.addEventListener("input", () => {
      if (blocked()) return;
      clearCard(); actions.replaceChildren();
      const cmd = commands().lookup; if (cmd) release(cmd);
      renderRetries(); syncButtons();
    });
    form.addEventListener("submit", event => {
      event.preventDefault();
      if (!canWrite() || blocked() || stale || inFlight.lookup || commands().lookup || enabled !== true || closed.has(work.id)) return;
      send(lookupCommand());
    });
    result = node("div"); result.id = "autodata-result";
    const history = node("section"); history.id = "autodata-history";
    history.append(node("h4", "Autodata-historia"));
    button("Päivitä historia", () => loadHistory(), history, "autodata-refresh");
    lookupList = node("div"); lookupList.id = "autodata-lookups";
    decisionList = node("div"); decisionList.id = "autodata-decisions";
    history.append(lookupList, decisionList);
    panel.append(title, status, error, retries, actions, form, result, history);
  }
  return {
    open(value, epoch) {
      panelEpoch += 1; clearCard(); work = value; selectionEpoch = epoch;
      enabled = null; stale = false; lookups = []; decisions = []; lookupNext = null; decisionNext = null;
      if (!work) { panel.replaceChildren(); form = null; retries = null; return; }
      build(); renderRetries(); syncButtons(); loadHistory();
      if (closed.has(work.id)) notify("Tiedustelun Autodata-aineisto on suljettu");
      if (commands().accept) openCard(commands().accept.lookup_id);
    },
    render(value) {
      if (work?.id === value.id) {
        if (work.version !== value.version) reloadGeneration += 1;
        work = value; syncButtons();
      }
    },
    lock() { locked = true; panelEpoch += 1; cancelTimer(); syncButtons(); },
    discard() {
      cancelReads(); clearCard(); retained.clear(); closed.clear(); searchContexts.clear();
      work = null; lookups = []; decisions = []; enabled = null;
      panel.replaceChildren(); form = null; status = null; error = null; retries = null;
      actions = null; result = null; lookupList = null; decisionList = null;
    },
    confirm(value, sameOwner) {
      writer = value; locked = false; loggingOut = false;
      panel.inert = false;
      selectionEpoch = current().selectionEpoch;
      if (sameOwner && work) {
        renderRetries(); syncButtons();
        if (card?.outcome === "FOUND") checkCard();
      }
    }
  };
}
