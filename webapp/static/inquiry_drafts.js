/* Manual WORK_INQUIRY editing only. Payloads and retry envelopes stay in memory. */
function CarbitInquiryDrafts({ api, form, dialog, workBusy }) {
  const fields = ["registration", "customer_description"];
  const panel = document.querySelector("#inquiry-drafts");
  const list = document.querySelector("#draft-list");
  const listMessage = document.querySelector("#draft-list-message");
  const refresh = document.querySelector("#draft-list-refresh");
  const message = document.querySelector("#draft-message");
  const expiry = document.querySelector("#draft-expiry");
  const save = document.querySelector("#draft-save");
  const retry = document.querySelector("#draft-retry");
  const reload = document.querySelector("#draft-reload");
  const discardButton = document.querySelector("#draft-discard");
  const attributionLine = document.querySelector("#draft-attribution");
  const submit = form.querySelector("button[type=submit]");
  const text = () => JSON.stringify(fields.map(name => form.elements[name].value));
  let snapshot = null, baseline = text(), operation = null;
  let writing = false, reading = false, blocked = false, epoch = 0, listEpoch = 0;
  let notice = "", error = false;
  const dirty = () => text() !== baseline;
  const busy = () => writing || reading;
  const canSubmit = () => !snapshot && !operation && !blocked && !busy();

  function render() {
    const writer = CarbitAuth.canWrite();
    const discarding = operation?.kind === "DISCARD";
    panel.hidden = !writer;
    // An unresolved discard may still end the draft: no edits that it would silently drop.
    fields.forEach(name => { form.elements[name].disabled = !writer || reading || workBusy() || discarding; });
    save.disabled = !writer || busy() || workBusy() || !!operation || blocked;
    retry.hidden = !operation || busy();
    retry.disabled = !writer || workBusy();
    retry.textContent = discarding ? "Uusi sama hylkäys" : "Uusi sama tallennus";
    discardButton.hidden = !snapshot;
    discardButton.disabled = !writer || busy() || workBusy() || !!operation;
    reload.hidden = !snapshot || !blocked;
    reload.disabled = !writer || busy() || workBusy();
    submit.disabled = !writer || workBusy() || !canSubmit();
    message.dataset.error = String(error);
    message.textContent = notice || (dirty() ? "Tallentamattomia muutoksia. Tallenna luonnos käsin."
      : "Luonnos tallentuu vain Tallenna luonnos -painikkeella.");
    if (notice && dirty() && !busy()) message.textContent += " Lomakkeella on tallentamattomia muutoksia.";
    attributionLine.hidden = !snapshot;
    attributionLine.textContent = snapshot ? attribution(snapshot) : "";
    expiry.textContent = snapshot
      ? `Tallennetun sisällön säilytysaika päättyy ${new Date(snapshot.expires_at * 1000).toLocaleString("fi-FI")}. Luonnosta ei voi vielä muuntaa tiedusteluksi.`
      : "Tallennettu sisältö säilyy seitsemän päivää viimeisestä sisältömuutoksesta.";
  }

  // Current names at read time, shown as text only. A missing name never blocks the draft.
  function person(name, mine) {
    return `${typeof name === "string" && name.trim() ? name : "Nimi ei saatavilla"}${mine === true ? " (sinä)" : ""}`;
  }

  function attribution(value) {
    const editor = value.last_editor_known === true
      ? person(value.last_edited_by_name, value.last_edited_by_me) : "ei tiedossa";
    return `Luonut: ${person(value.created_by_name, value.created_by_me)} · Viimeksi muokannut: ${editor}`;
  }

  function announce(value, failed = false) { notice = value; error = failed; render(); }

  function abandon() {
    return !(dirty() || operation || blocked) || window.confirm(
      "Lomakkeella voi olla tallentamattomia muutoksia tai epäselvä tallennus. Haluatko korvata lomakkeen sisällön? Palvelimelle jo tallentunut luonnos säilyy."
    );
  }

  function clearEditor() {
    epoch += 1;
    snapshot = null; operation = null; writing = false; reading = false; blocked = false;
    form.reset(); baseline = text(); notice = ""; error = false;
    render();
  }

  function validSnapshot(value) {
    if (value?.draft_type !== "WORK_INQUIRY" || value.state !== "ACTIVE"
        || value.payload_schema_version !== 1 || !Number.isSafeInteger(value.revision)
        || value.revision < 1 || !Number.isFinite(value.expires_at)
        || !value.payload || typeof value.payload !== "object") {
      throw new Error("Luonnoksen vastausta ei voitu vahvistaa.");
    }
    return value;
  }

  function payload() {
    const value = {}, original = snapshot?.payload, shown = JSON.parse(baseline);
    fields.forEach((name, index) => {
      const input = form.elements[name].value;
      // Compare with the text shown at open/last save, not the server string: the browser may
      // normalize CR/LF. An unchanged field keeps its server value, also missing/null.
      if (original && input === shown[index]) {
        if (Object.hasOwn(original, name)) value[name] = original[name];
      } else value[name] = input;
    });
    return value;
  }

  function samePayload(a, b) {
    const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])];
    return keys.every(key => Object.hasOwn(a, key) === Object.hasOwn(b, key) && a[key] === b[key]);
  }

  async function open(id) {
    if (!CarbitAuth.canWrite() || workBusy() || busy() || !abandon()) return;
    const current = ++epoch;
    reading = true;
    announce("Avataan luonnosta…");
    // A stalled read must not lock the editor. An aborted response never reaches this code.
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const value = validSnapshot(await api(`/api/drafts/${id}`, { signal: controller.signal }));
      if (current !== epoch) return;
      snapshot = value; operation = null; blocked = false;
      fields.forEach(name => { form.elements[name].value = value.payload[name] ?? ""; });
      baseline = text();
      document.querySelector("#new-work-error").classList.add("hidden");
      if (!dialog.open) dialog.showModal();
      announce("Palvelimen luonnos avattu. Muutokset tallennetaan käsin.");
    } catch (problem) {
      if (current === epoch) {
        announce(controller.signal.aborted
          ? "Luonnoksen avaus keskeytettiin, koska palvelin ei vastannut 15 sekunnissa. Lomakkeen sisältö säilyi. Voit jatkaa tai yrittää avausta uudelleen."
          : readError(problem), true);
        if (!dialog.open) listMessage.textContent = notice;
      }
    } finally {
      window.clearTimeout(timeout);
      if (current === epoch) { reading = false; render(); }
    }
  }

  function readError(problem) {
    if (problem.status === 503) return "Luonnospalvelu ei ole käytössä tai on tilapäisesti poissa käytöstä. Lomakkeen sisältö säilyi.";
    if (problem.status === 404) return "Luonnos ei ole saatavilla. Se on voinut vanhentua tai tulla hylätyksi. Lomakkeen sisältö säilyi.";
    return "Luonnosta ei voitu lukea. Lomakkeen sisältö säilyi. Tarkista kirjautumistila tai yritä myöhemmin.";
  }

  async function loadList() {
    if (!CarbitAuth.canWrite()) return;
    const current = ++listEpoch;
    refresh.disabled = true;
    listMessage.textContent = "Haetaan korjaamon luonnoksia…";
    // Same bound as open: a stalled list must not keep the refresh button locked.
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const values = await api("/api/drafts?limit=100", { signal: controller.signal });
      if (current !== listEpoch) return;
      list.replaceChildren();
      for (const value of values.filter(row => row.draft_type === "WORK_INQUIRY")) {
        const button = document.createElement("button");
        button.type = "button"; button.className = "secondary draft-row";
        button.dataset.draftId = value.draft_id;
        button.textContent = `${value.payload.registration || "Keskeneräinen rekisterinumero"} · ${value.payload.customer_description || "Kuvaus kesken"}\n${attribution(value)}`;
        button.addEventListener("click", () => open(value.draft_id));
        list.append(button);
      }
      listMessage.textContent = values.length === 100
        ? "Lista on rajattu palvelimen 100 luonnokseen, myös työaikaluonnokset lasketaan rajaan. Kaikki tiedusteluluonnokset eivät välttämättä näy."
        : list.childElementCount ? "Valitse luonnos jatkaaksesi. Vain käsin tallennettu versio säilyy." : "Ei korjaamon aktiivisia tiedusteluluonnoksia.";
    } catch (problem) {
      if (current === listEpoch) {
        listMessage.textContent = controller.signal.aborted
          ? "Luonnosluettelon haku keskeytettiin, koska palvelin ei vastannut 15 sekunnissa. Lomakkeen sisältö säilyi. Yritä hakua uudelleen."
          : readError(problem);
      }
    } finally {
      window.clearTimeout(timeout);
      if (current === listEpoch) refresh.disabled = false;
    }
  }

  async function persist(op) {
    if (!CarbitAuth.canWrite() || busy() || workBusy()) return;
    // Receipt retention can be as short as one day. Never reuse an old unknown CREATE indefinitely.
    if (Date.now() - op.started >= 86400000) {
      blocked = true; operation = null;
      announce("Epäselvä tallennus on liian vanha uusittavaksi. Tarkista palvelimen luonnoslista ennen jatkamista.", true);
      return;
    }
    const current = ++epoch;
    operation = op; writing = true;
    announce("Tallennetaan luonnosta…");
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const ack = await api(op.path, { method: op.method, body: op.body, signal: controller.signal });
      if (current !== epoch) return;
      if (ack?.accepted !== true || ack.draft_type !== "WORK_INQUIRY" || !ack.draft_id) {
        throw new Error("invalid draft receipt");
      }
      if (ack.termination_reason) {
        operation = null; blocked = true;
        announce("Tallennuksen kuitti löytyi, mutta luonnos on jo päättynyt. Sisältöä ei palautettu palvelimelle.", true);
        return;
      }
      // A receipt acknowledges a historical operation, not the current editable revision.
      const value = validSnapshot(await api(`/api/drafts/${ack.draft_id}`, { signal: controller.signal }));
      if (current !== epoch) return;
      snapshot = value; operation = null;
      if (!samePayload(value.payload, op.payload)) {
        blocked = true;
        announce("Palvelimella on toinen sisältöversio. Oma tekstisi säilyi. Avaa palvelimen versio ennen seuraavaa tallennusta.", true);
        return;
      }
      baseline = op.text; blocked = false;
      announce("Luonnos tallennettu.");
    } catch (problem) {
      if (current !== epoch) return;
      if (problem.status === 409 || problem.status === 404) {
        operation = null; blocked = true;
        announce(problem.status === 409
          ? "Versioristiriita: palvelimen sisältöä ei ylikirjoitettu. Oma tekstisi säilyi. Avaa palvelimen versio jatkaaksesi."
          : readError(problem), true);
      } else if ([400, 413, 415, 422].includes(problem.status)) {
        operation = null;
        announce("Luonnosta ei hyväksytty. Tarkista kentät; lomakkeen sisältö säilyi.", true);
      } else {
        announce(`${problem.status === 503 ? "Luonnospalvelu ei ole käytössä tai on tilapäisesti poissa käytöstä. " : ""}Tallennuksen tulos jäi epäselväksi. Sisältö säilyi. Uusi sama tallennus -painike lähettää alkuperäisen pyynnön; myöhemmät muokkaukset säilyvät lomakkeella.`, true);
      }
    } finally {
      window.clearTimeout(timeout);
      if (current === epoch) { writing = false; render(); }
    }
  }

  // Server discard of the shared workshop draft. Separate from the local forget on user change.
  async function discardOnServer(op) {
    if (!CarbitAuth.canWrite() || busy() || workBusy()) return;
    if (Date.now() - op.started >= 86400000) {
      blocked = true; operation = null;
      announce("Epäselvä hylkäys on liian vanha uusittavaksi. Tarkista palvelimen luonnoslista ennen jatkamista.", true);
      return;
    }
    const current = ++epoch;
    operation = op; writing = true;
    announce("Hylätään luonnosta…");
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const ack = await api(op.path, { method: "POST", body: op.body, signal: controller.signal });
      if (current !== epoch) return;
      if (ack?.accepted !== true || ack.draft_id !== op.draftId || ack.state !== "DISCARDED"
          || ack.termination_reason !== "DISCARDED") {
        throw new Error("invalid discard receipt");
      }
      // An ended draft no longer opens: no read-back as after a save. A list request
      // started before the discard could still show the row, so it is invalidated.
      listEpoch += 1; refresh.disabled = false;
      clearEditor();
      list.querySelector(`[data-draft-id="${CSS.escape(op.draftId)}"]`)?.remove();
      listMessage.textContent = "Luonnos hylätty. Se ei enää näy korjaamon luonnoksissa.";
      announce("Luonnos hylätty.");
    } catch (problem) {
      if (current !== epoch) return;
      if ([403, 404, 409].includes(problem.status)) {
        operation = null; blocked = true;
        announce(problem.status === 409
          ? "Versioristiriita: luonnosta ei hylätty, koska palvelimella on uudempi versio. Lomakkeen sisältö säilyi. Avaa palvelimen versio ennen uutta päätöstä."
          : problem.status === 404
            ? "Luonnos ei ole enää saatavilla. Se on voinut jo vanhentua tai tulla hylätyksi. Lomakkeen sisältö säilyi."
            : "Sinulla ei ole oikeutta hylätä tätä luonnosta. Lomakkeen sisältö säilyi.", true);
      } else if ([400, 413, 415, 422].includes(problem.status)) {
        operation = null;
        announce("Hylkäyspyyntöä ei hyväksytty. Lomakkeen sisältö säilyi.", true);
      } else {
        announce(`${problem.status === 503 ? "Luonnospalvelu ei ole käytössä tai on tilapäisesti poissa käytöstä. " : ""}Hylkäyksen tulos jäi epäselväksi. Lomakkeen sisältö säilyi. Uusi sama hylkäys -painike lähettää alkuperäisen pyynnön.`, true);
      }
    } finally {
      window.clearTimeout(timeout);
      if (current === epoch) { writing = false; render(); }
    }
  }

  save.addEventListener("click", () => {
    if (operation || blocked || busy() || workBusy() || !CarbitAuth.canWrite()) return;
    const value = payload();
    const body = { request_id: crypto.randomUUID(), payload: value };
    if (snapshot) body.expected_revision = snapshot.revision;
    else body.draft_type = "WORK_INQUIRY";
    persist({ kind: "SAVE", path: snapshot ? `/api/drafts/${snapshot.draft_id}` : "/api/drafts",
      method: snapshot ? "PUT" : "POST", body: JSON.stringify(body), payload: value, text: text(), started: Date.now() });
  });
  discardButton.addEventListener("click", () => {
    if (!snapshot || operation || busy() || workBusy() || !CarbitAuth.canWrite()) return;
    if (!window.confirm(
      "Hylätäänkö korjaamon yhteinen luonnos? Luonnos poistuu kaikilta korjaamon käyttäjiltä, eikä sitä voi palauttaa. Lomakkeen tallentamattomat muutokset menetetään."
    )) return;
    discardOnServer({ kind: "DISCARD", draftId: snapshot.draft_id, path: `/api/drafts/${snapshot.draft_id}/discard`,
      body: JSON.stringify({ request_id: crypto.randomUUID(), expected_revision: snapshot.revision }), started: Date.now() });
  });
  retry.addEventListener("click", () => {
    if (operation?.kind === "DISCARD") discardOnServer(operation);
    else if (operation) persist(operation);
  });
  reload.addEventListener("click", () => { if (snapshot) open(snapshot.draft_id); });
  refresh.addEventListener("click", loadList);
  fields.forEach(name => form.elements[name].addEventListener("input", () => {
    if (!operation && !blocked && !busy()) { notice = ""; error = false; }
    render();
  }));

  return {
    busy, canSubmit, render,
    startNew() { if (busy() || !abandon()) return false; clearEditor(); return true; },
    submitted: clearEditor,
    lock() {
      epoch += 1; listEpoch += 1; writing = false; reading = false; refresh.disabled = false;
      if (operation) {
        notice = operation.kind === "DISCARD"
          ? "Hylkäyksen tulos jäi epäselväksi. Tarkista kirjautumistila ja uusi tarvittaessa sama hylkäys."
          : "Tallennuksen tulos jäi epäselväksi. Tarkista kirjautumistila ja uusi tarvittaessa sama tallennus.";
        error = true;
      }
      render();
    },
    // Local forget on user change: never a server discard. Clears any unknown operation.
    discard() {
      listEpoch += 1; list.replaceChildren(); listMessage.textContent = "";
      clearEditor();
    }
  };
}
