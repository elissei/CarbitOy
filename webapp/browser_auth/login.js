/* Public UI code only. Credentials/proofs are transient memory; cookies stay HttpOnly. */
window.CarbitAuth = (() => {
  let phase = "checking", user = null, owner = null, proof = null;
  let busy = false, epoch = 0, timer = null, idle = 0, absolute = 0, callbacks = {};
  const panel = document.querySelector("#auth-panel");
  panel.innerHTML = `<section class="auth-card" aria-labelledby="auth-title">
    <h1 id="auth-title" tabindex="-1">Carbit – kirjaudu</h1>
    <p id="auth-message" role="status" aria-live="polite"></p>
    <form id="password-form">
      <label>Käyttäjätunnus<input name="username" autocomplete="username" required maxlength="64" autocapitalize="none" spellcheck="false"></label>
      <label>Salasana<input name="password" type="password" autocomplete="current-password" required maxlength="256"></label>
      <button type="submit">Kirjaudu</button>
    </form>
    <form id="mfa-form" hidden>
      <label>Vahvistustapa<select id="mfa-method"><option value="totp_code">Tunnistussovellus</option><option value="recovery_code">Kertakäyttöinen palautuskoodi</option></select></label>
      <label>Vahvistuskoodi<input name="totp_code" autocomplete="one-time-code" inputmode="numeric" pattern="[0-9]{6}" minlength="6" maxlength="6" required></label>
      <button type="submit">Vahvista kirjautuminen</button>
    </form>
    <button id="auth-check" type="button" hidden>Tarkista kirjautumistila</button>
    <p class="auth-help">Käytä henkilökohtaista tunnustasi ja valmiiksi rekisteröityä tunnistussovellusta. Tunnuksen ja MFA:n käyttöönotto tehdään ylläpitäjän hallitussa menettelyssä.</p>
  </section>`;
  const passwordForm = panel.querySelector("#password-form");
  const mfaForm = panel.querySelector("#mfa-form");
  const check = panel.querySelector("#auth-check");
  const message = panel.querySelector("#auth-message");
  const mfaMethod = panel.querySelector("#mfa-method");
  function configureMfa() {
    const input = mfaForm.querySelector("input");
    const recovery = mfaMethod.value === "recovery_code";
    input.value = "";
    input.name = recovery ? "recovery_code" : "totp_code";
    input.inputMode = recovery ? "text" : "numeric";
    input.minLength = recovery ? 1 : 6;
    input.maxLength = recovery ? 40 : 6;
    if (recovery) input.removeAttribute("pattern"); else input.pattern = "[0-9]{6}";
  }
  function clearMfa() { mfaForm.reset(); configureMfa(); }
  mfaMethod.addEventListener("change", configureMfa);
  const titles = { checking: "Tarkistetaan kirjautumista", unauthenticated: "Carbit – kirjaudu",
    mfa: "Vahvista kirjautuminen", authenticated: "Kirjautunut", expired: "Istunto on päättynyt",
    denied: "Käyttöoikeus ei riitä", uncertain: "Kirjautumistila on tarkistettava" };

  function render(text = "", focus = false) {
    panel.hidden = phase === "authenticated";
    panel.dataset.state = phase;
    panel.setAttribute("aria-busy", String(busy));
    panel.querySelector("#auth-title").textContent = titles[phase];
    message.textContent = text;
    passwordForm.hidden = phase !== "unauthenticated";
    mfaForm.hidden = phase !== "mfa";
    check.hidden = !["expired", "denied", "uncertain"].includes(phase);
    panel.querySelectorAll("input, select, button").forEach(control => { control.disabled = busy; });
    if (focus && !panel.hidden) panel.querySelector("#auth-title").focus();
  }

  function lock(next = "expired", text = "Kirjaudu uudelleen. Tallentamattomat luonnokset säilyvät tämän sivun muistissa.") {
    epoch += 1;
    phase = next;
    user = null;
    proof = null;
    clearTimeout(timer);
    passwordForm.elements.password.value = "";
    clearMfa();
    callbacks.lock?.();
    render(text, true);
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(() => lock(), Math.max(0, Math.min(idle, absolute) - Date.now()));
  }

  function active() {
    if (phase !== "authenticated") return false;
    if (Date.now() >= Math.min(idle, absolute)) { lock(); return false; }
    return true;
  }

  function activity(requestStartedAt) {
    // A response's arrival time can be later than the server's idle refresh.
    // Use the request start as a conservative UI estimate, never as authority.
    if (active()) { idle = Math.min(absolute, requestStartedAt + 1800000); schedule(); }
  }

  function validateProof(body) {
    const fields = typeof body.csrf_token === "string" ? body.csrf_token.split(".") : [];
    const issued = Number(fields[3]), expires = Number(fields[4]);
    if (!["preauth", "mfa", "session"].includes(body.state) || fields.length !== 7
        || fields[0] !== "v1" || fields[2] !== body.state || body.csrf_token.length > 512
        || !Number.isSafeInteger(issued) || !Number.isSafeInteger(expires)
        || expires !== body.expires_at || expires <= issued || expires - issued > 600
        || expires * 1000 <= Date.now() + 1000) throw new Error("invalid contract");
    return body;
  }

  async function request(operation, data, csrf) {
    const generation = epoch;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    const headers = data === undefined ? {} : { "Content-Type": "application/json", "X-CSRF-Token": csrf };
    if (operation === "csrf") headers["X-Carbit-Auth"] = "bootstrap";
    try {
      const response = await fetch("/api/auth/" + operation, { method: data === undefined ? "GET" : "POST",
        credentials: "same-origin", cache: "no-store", redirect: "error", headers, signal: controller.signal,
        ...(data === undefined ? {} : { body: JSON.stringify(data) }) });
      if (generation !== epoch) throw new Error("authentication state changed");
      if (!response.ok) { const problem = new Error("authentication rejected"); problem.status = response.status; throw problem; }
      if (response.status !== (operation === "logout" ? 204 : 200)) throw new Error("invalid authentication response");
      const body = response.status === 204 ? null : await response.json();
      if (generation !== epoch) throw new Error("authentication state changed");
      return body;
    } finally { clearTimeout(timeout); }
  }

  async function confirm(sessionProof = null) {
    const body = await request("session");
    if (!body.user || typeof body.user.user_id !== "string"
        || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(body.user.user_id)
        || typeof body.user.display_name !== "string" || body.user.display_name.length > 200
        || !["OWNER", "SERVICE_MANAGER", "IT_ADMIN"].includes(body.user.role)
        || !Number.isSafeInteger(body.absolute_expires_at) || !Number.isSafeInteger(body.idle_expires_at)
        || body.idle_expires_at > body.absolute_expires_at || body.idle_expires_at * 1000 <= Date.now()) {
      throw new Error("invalid session contract");
    }
    const sameOwner = owner === body.user.user_id;
    if (owner && !sameOwner) callbacks.discard?.();
    owner = body.user.user_id;
    user = body.user;
    idle = body.idle_expires_at * 1000;
    absolute = body.absolute_expires_at * 1000;
    phase = "authenticated";
    epoch += 1;
    passwordForm.reset(); clearMfa(); proof = null;
    if (sessionProof) callbacks.cacheProof?.(sessionProof);
    render(); schedule();
    if (document.body.dataset.page === "login") window.location.replace("/");
    else await callbacks.confirm?.(user, sameOwner);
  }

  async function bootstrap() {
    proof = validateProof(await request("csrf"));
    if (proof.state === "session") { await confirm(proof); return; }
    phase = proof.state === "mfa" ? "mfa" : "unauthenticated";
    render("", true);
    if (phase === "mfa") {
      clearTimeout(timer);
      timer = setTimeout(() => lock("expired", "Vahvistus on vanhentunut. Tarkista kirjautumistila ennen uutta yritystä."), proof.expires_at * 1000 - Date.now());
    }
  }

  function failure(problem, operation) {
    const status = problem.status;
    if (status === 429) {
      lock("uncertain", "Liian monta yritystä. Odota vähintään viisi minuuttia ennen uutta yritystä.");
    } else if (status === 401 && ["login", "mfa"].includes(operation)) {
      render("Kirjautuminen tai vahvistus epäonnistui. Tarkista tiedot; tarvittaessa ota yhteyttä ylläpitäjään.");
    } else {
      lock("uncertain", status === 503 ? "Kirjautumispalvelu ei ole käytettävissä. Yritä myöhemmin."
        : "Vastausta ei voitu vahvistaa. Tarkista kirjautumistila ennen uutta yritystä. Mitään pyyntöä ei lähetetä uudelleen automaattisesti.");
    }
  }

  async function run(operation, action) {
    if (busy) return;
    busy = true; render(message.textContent);
    try { await action(); } catch (problem) { failure(problem, operation); }
    finally { busy = false; render(message.textContent); }
  }

  passwordForm.addEventListener("submit", event => {
    event.preventDefault();
    if (busy || phase !== "unauthenticated") return;
    const data = { username: passwordForm.elements.username.value, password: passwordForm.elements.password.value };
    passwordForm.elements.password.value = "";
    run("login", async () => {
      if (!proof || proof.expires_at * 1000 <= Date.now() + 1000) await bootstrap();
      if (phase !== "unauthenticated" || proof.state !== "preauth") return;
      proof = validateProof(await request("login", data, proof.csrf_token));
      if (proof.state !== "mfa") throw new Error("invalid login contract");
      phase = "mfa"; render("", true);
      clearTimeout(timer);
      timer = setTimeout(() => lock("expired", "Vahvistus on vanhentunut. Tarkista kirjautumistila."), proof.expires_at * 1000 - Date.now());
    });
  });

  mfaForm.addEventListener("submit", event => {
    event.preventDefault();
    if (busy || phase !== "mfa") return;
    const field = mfaMethod.value;
    const code = mfaForm.querySelector("input").value;
    clearMfa();
    run("mfa", async () => {
      if (!proof || proof.expires_at * 1000 <= Date.now() + 1000) { lock(); return; }
      const body = await request("mfa", { [field]: code }, proof.csrf_token);
      if (body?.state !== "session") throw new Error("invalid MFA contract");
      await confirm();
    });
  });
  check.addEventListener("click", () => run("check", bootstrap));

  async function logout() {
    if (busy || !active()) return;
    await run("logout", async () => {
      const token = callbacks.sessionProof ? await callbacks.sessionProof() : validateProof(await request("csrf")).csrf_token;
      await request("logout", {}, token);
      callbacks.discard?.(); owner = null;
      lock("expired", "Olet kirjautunut ulos. Tarkista kirjautumistila aloittaaksesi uuden kirjautumisen.");
    });
  }

  async function start(options = {}) {
    callbacks = options;
    callbacks.lock?.(); render();
    await run("start", document.body.dataset.page === "login" ? bootstrap : confirm);
  }

  // Notifications carry no identity or secret and only lock other tabs. They
  // never authorize a user or claim a server-side logout.
  const channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("carbit-auth-change") : null;
  channel?.addEventListener("message", () => lock("uncertain", "Kirjautumistila muuttui toisessa välilehdessä. Tarkista tila."));
  passwordForm.addEventListener("submit", () => channel?.postMessage("check"));
  mfaForm.addEventListener("submit", () => channel?.postMessage("check"));
  document.querySelector("#logout-button")?.addEventListener("click", () => { channel?.postMessage("check"); logout(); });
  window.addEventListener("pagehide", () => { passwordForm.elements.password.value = ""; clearMfa(); });
  window.addEventListener("pageshow", event => { if (event.persisted) lock("uncertain", "Tarkista kirjautumistila ennen jatkamista."); });
  if (document.body.dataset.page === "login") start();
  return { start, lock, active, activity, epoch: () => epoch,
    canWrite: () => active() && ["OWNER", "SERVICE_MANAGER"].includes(user?.role) };
})();
