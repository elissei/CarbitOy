"use strict";

// Public STATIC fiction only. Do not replace this fixture with internal data
// until server authentication and authorized read endpoints exist. No API,
// trusted reviewer context, browser persistence or approval writes here.
(() => {
  const basePlan = {
    revision_id: "demo-revision-2", revision: 2, content_id: "b".repeat(64),
    rules_version: "3B-v1", coverage_status: "READY", human_review_required: true,
    active_decision_id: "demo-decision-3",
    works: [
      { work_item_id: "demo-shocks", work_description: "Etuiskunvaimentimien vaihto, molemmat puolet",
        operation_identifier: "SHOCK_ABSORBER_REPLACEMENT", autodata_operation_id: "DEMO-AD-SHOCK-FRONT-PAIR",
        autodata_reference: "Keksitty parivaihdon viite", original_hours: "2.55" },
      { work_item_id: "demo-brakes", work_description: "Etujarrujen vaihto",
        operation_identifier: "BRAKE_REPLACEMENT", autodata_operation_id: "DEMO-AD-BRAKE-FRONT",
        autodata_reference: "Keksitty jarrutyön viite", original_hours: "1.60" }
    ],
    // Append order mirrors stored decision history. These identities are
    // fictional DISPLAY data, never an authenticated approver/context.
    decisions: [
      { decision_id: "demo-decision-1", revision_id: "demo-revision-1", content_id: "a".repeat(64),
        rules_version: "3B-v1", status: "APPROVED_ADJUSTED", original_total_hours: "4.10",
        final_billable_hours: "3.50", reason: "Keksitty esimerkkiperustelu aiemman suunnitelman ajanmuutokselle.",
        reviewer_id: "demo-employee-a", reviewer_name: "Esimerkkimekaanikko A (keksitty)", reviewed_at: "2026-07-01T09:00:00Z" },
      { decision_id: "demo-decision-2", revision_id: "demo-revision-2", content_id: "b".repeat(64),
        rules_version: "3B-v1", status: "APPROVED_UNCHANGED", original_total_hours: "4.15",
        final_billable_hours: "4.15", reason: null,
        reviewer_id: "demo-employee-a", reviewer_name: "Esimerkkimekaanikko A (keksitty)", reviewed_at: "2026-07-02T08:00:00Z" },
      { decision_id: "demo-decision-3", revision_id: "demo-revision-2", content_id: "b".repeat(64),
        rules_version: "3B-v1", status: "APPROVED_ADJUSTED", original_total_hours: "4.15",
        final_billable_hours: "3.60", reason: "Keksitty esimerkkiperustelu: mekaanikon tarkistama lopullinen työaika.",
        reviewer_id: "demo-employee-b", reviewer_name: "Esimerkkimekaanikko B (keksitty)", reviewed_at: "2026-07-02T10:30:00Z" }
    ]
  };
  const pendingPlan = { ...basePlan, revision_id: "demo-revision-3", revision: 3,
    content_id: "c".repeat(64), active_decision_id: null };
  const fixtures = {
    reviewed: basePlan,
    pending: pendingPlan,
    incomplete: { ...pendingPlan, coverage_status: "REVIEW_REQUIRED",
      works: [basePlan.works[0], { ...basePlan.works[1], original_hours: null }] },
    "rules-changed": { ...basePlan, rules_version: "DEMO-next-rules" },
    single: { ...pendingPlan, human_review_required: false, works: [basePlan.works[0]], decisions: [] }
  };
  function freezeFixture(value) {
    Object.values(value).forEach((child) => {
      if (child && typeof child === "object") freezeFixture(child);
    });
    return Object.freeze(value);
  }
  freezeFixture(fixtures);

  const select = document.querySelector("#demo-scenario");
  const form = document.querySelector("#review-preview-form");
  const fields = document.querySelector("#adjustment-fields");
  const error = document.querySelector("#preview-error");
  const result = document.querySelector("#preview-result");
  const groupFormatter = new Intl.NumberFormat("fi-FI", { maximumFractionDigits: 0 });
  const dateFormatter = new Intl.DateTimeFormat("fi-FI", { dateStyle: "short", timeStyle: "short", timeZone: "UTC" });
  let plan;
  let originalSum;

  function parseHours(raw) {
    const text = raw.trim().replace(",", ".");
    if (text.length > 64 || !/^\d+(\.\d+)?$/.test(text)) {
      throw new Error("Anna lopulliseksi ajaksi vähintään 0 tuntia kelvollisena desimaalilukuna.");
    }
    const [whole, fraction = ""] = text.split(".");
    return { units: BigInt(whole + fraction), scale: fraction.length };
  }

  function sumOriginals(works) {
    if (works.some((work) => work.original_hours === null)) return null;
    const values = works.map((work) => parseHours(work.original_hours));
    const scale = Math.max(...values.map((value) => value.scale));
    const units = values.reduce((sum, value) => sum + value.units * 10n ** BigInt(scale - value.scale), 0n);
    return { units, scale };
  }

  function hoursText(value) {
    const scale = Math.max(2, value.scale);
    const digits = (value.units * 10n ** BigInt(scale - value.scale)).toString().padStart(scale + 1, "0");
    return `${digits.slice(0, -scale)},${digits.slice(-scale)} h`;
  }

  function moneyText(cents) {
    return `${groupFormatter.format(cents / 100n)},${(cents % 100n).toString().padStart(2, "0")}\u00a0€`;
  }

  function roundedQuotient(numerator, denominator) {
    const whole = numerator / denominator;
    return whole + (2n * (numerator % denominator) >= denominator ? 1n : 0n);
  }

  function previewPrice(hours) {
    // Decimal text -> integer arithmetic, never binary-float money. This is
    // ONLY a display preview of today's 90 EUR/h inclusive 25.5% VAT policy.
    // Authoritative pricing stays in ApprovedLabourPricingService, which
    // calls LabourPlanService.require_approved_final_hours server-side.
    const gross = roundedQuotient(hours.units * 9000n, 10n ** BigInt(hours.scale));
    const net = roundedQuotient(gross * 200n, 251n);
    return { gross, net, vat: gross - net };
  }

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function decisionState(decision) {
    // Presentation of fictional identities only, not an approval gate.
    if (decision.revision_id !== plan.revision_id || decision.content_id !== plan.content_id) {
      return ["stale", "Vanhentunut: suunnitelman sisältö muuttunut"];
    }
    if (decision.rules_version !== plan.rules_version) {
      return ["stale", "Vanhentunut: sääntöversio muuttunut"];
    }
    if (plan.coverage_status !== "READY") return ["stale", "Vanhentunut: suunnitelma keskeneräinen"];
    if (decision.decision_id === plan.active_decision_id) return ["current", "Voimassa oleva esimerkkipäätös"];
    return ["previous", "Aiempi päätös: korvattu uudemmalla päätöksellä"];
  }

  function renderOriginals() {
    document.querySelector("#plan-identity").textContent = `Esimerkkisuunnitelma · Revisio ${plan.revision} · Säännöt ${plan.rules_version}`;
    document.querySelector("#original-work-rows").replaceChildren(...plan.works.map((work) => {
      const row = node("tr");
      row.dataset.workId = work.work_item_id;
      const description = node("td", work.work_description);
      description.append(node("small", `${work.work_item_id} · ${work.autodata_reference}`));
      const operation = node("td");
      operation.append(node("code", work.autodata_operation_id), node("small", work.operation_identifier));
      row.append(description, operation, node("td", work.original_hours === null ? "Puuttuu" : hoursText(parseHours(work.original_hours))));
      return row;
    }));
    document.querySelector("#original-hours-total").textContent = originalSum ? hoursText(originalSum) : "Ei laskettavissa";
    document.querySelector("#review-requirement").textContent = plan.human_review_required
      ? "Usean erillisen työn suunnitelma vaatii ihmisen tarkistuksen. Alkuperäiset ajat summataan ilman automaattisia vähennyksiä."
      : "Yksi korjaustyö. Lopullinen hinnoittelu edellyttää voimassa olevaa ihmisen hyväksyntää.";
    const incomplete = originalSum === null || plan.coverage_status !== "READY";
    const coverageNotice = document.querySelector("#coverage-notice");
    coverageNotice.classList.toggle("hidden", !incomplete);
    coverageNotice.textContent = incomplete ? "Suunnitelma on keskeneräinen: alkuperäinen työaika puuttuu. Hyväksyntää ei voi esikatsella; puuttuvaa aikaa ei arvioida." : "";
    document.querySelector("#preview-controls").disabled = incomplete;
  }

  function renderHistory() {
    const history = document.querySelector("#demo-decision-history");
    history.replaceChildren(...[...plan.decisions].reverse().map((decision) => {
      const [state, label] = decisionState(decision);
      const item = node("li");
      item.dataset.state = state;
      item.dataset.decisionId = decision.decision_id;
      const sums = node("dl", undefined, "review-totals");
      [["Alkuperäinen summa", decision.original_total_hours], ["Lopullinen aika", decision.final_billable_hours]].forEach(([title, value]) => {
        const pair = node("div");
        pair.append(node("dt", title), node("dd", hoursText(parseHours(value))));
        sums.append(pair);
      });
      item.append(node("span", label, "decision-state"),
        node("h3", decision.status === "APPROVED_UNCHANGED" ? "Hyväksyntä sellaisenaan" : "Perusteltu ajanmuutos"),
        sums, node("p", `Perustelu: ${decision.reason || "Alkuperäinen summa hyväksytty sellaisenaan."}`,
          decision.status === "APPROVED_ADJUSTED" ? "adjustment-reason" : undefined),
        node("p", `Hyväksyjä: ${decision.reviewer_name} · ${decision.reviewer_id}`, "decision-meta"),
        node("p", `${dateFormatter.format(new Date(decision.reviewed_at))} UTC · ${decision.revision_id} · ${decision.rules_version}`, "decision-meta"));
      return item;
    }));
    document.querySelector("#empty-decision-history").classList.toggle("hidden", plan.decisions.length !== 0);
    const current = plan.decisions.find((decision) => decisionState(decision)[0] === "current");
    document.querySelector("#current-review-status").textContent = current
      ? "Voimassa oleva hyväksyntä keksityssä esimerkkidatassa."
      : "Ei voimassa olevaa hyväksyntää. Aiempi tai keskeneräinen päätös ei kelpaa lopulliseen hinnoitteluun.";
    document.querySelector("#current-review-details").classList.toggle("hidden", !current);
    document.querySelector("#current-original-hours").textContent = current ? hoursText(parseHours(current.original_total_hours)) : "";
    document.querySelector("#current-final-hours").textContent = current ? hoursText(parseHours(current.final_billable_hours)) : "";
    document.querySelector("#current-reviewer").textContent = current ? current.reviewer_name : "";
  }

  function clearPreview() {
    result.classList.add("hidden");
    error.classList.add("hidden");
    form.elements.final_hours.removeAttribute("aria-invalid");
    form.elements.reason.removeAttribute("aria-invalid");
  }

  function updateChoice() {
    const adjusted = form.elements.decision.value === "adjusted";
    fields.classList.toggle("hidden", !adjusted);
    form.elements.final_hours.disabled = !adjusted;
    form.elements.reason.disabled = !adjusted;
    form.elements.final_hours.required = adjusted;
    form.elements.reason.required = adjusted;
    clearPreview();
  }

  function loadScenario() {
    plan = fixtures[select.value];
    originalSum = sumOriginals(plan.works);
    form.reset();
    form.elements.final_hours.value = originalSum ? hoursText(originalSum).replace(" h", "") : "";
    updateChoice();
    renderOriginals();
    renderHistory();
  }

  form.addEventListener("input", clearPreview);
  form.addEventListener("change", updateChoice);
  select.addEventListener("change", loadScenario);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    clearPreview();
    if (originalSum === null || plan.coverage_status !== "READY") return;
    const adjusted = form.elements.decision.value === "adjusted";
    let hours = originalSum;
    if (adjusted) {
      try {
        hours = parseHours(form.elements.final_hours.value);
      } catch (problem) {
        form.elements.final_hours.setAttribute("aria-invalid", "true");
        error.textContent = problem.message;
        error.classList.remove("hidden");
        form.elements.final_hours.focus();
        return;
      }
      if (!form.elements.reason.value.trim()) {
        form.elements.reason.setAttribute("aria-invalid", "true");
        error.textContent = "Ajanmuutos vaatii perustelun, myös silloin kun lopullinen aika on 0,00 h.";
        error.classList.remove("hidden");
        form.elements.reason.focus();
        return;
      }
    }
    const price = previewPrice(hours);
    document.querySelector("#preview-choice").textContent = adjusted
      ? "Esikatselu: perusteltu ajanmuutos · Ei tallennettu"
      : "Esikatselu: hyväksyntä sellaisenaan · Ei tallennettu";
    document.querySelector("#preview-hours").textContent = hoursText(hours);
    document.querySelector("#preview-gross").textContent = moneyText(price.gross);
    document.querySelector("#preview-net").textContent = moneyText(price.net);
    document.querySelector("#preview-vat").textContent = moneyText(price.vat);
    const reasonSummary = document.querySelector("#preview-reason-summary");
    reasonSummary.classList.toggle("adjustment-reason", adjusted);
    reasonSummary.textContent = adjusted
      ? `Esikatselun perustelu: ${form.elements.reason.value.trim()}` : "Alkuperäiset työajat säilyvät muuttumattomina.";
    result.classList.remove("hidden");
  });
  loadScenario();
})();
