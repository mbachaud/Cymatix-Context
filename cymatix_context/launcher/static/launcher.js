/*
 * Cymatix Launcher dashboard reactivity.
 *
 * Polls server-rendered panels, wires lifecycle actions, and preserves
 * selected dashboard tabs across refreshes.
 */

(function () {
  "use strict";

  const panels = document.getElementById("panels");
  if (!panels) return;

  const pollUrl = panels.dataset.pollUrl || "/api/state/panels";
  const pollIntervalMs = parseInt(panels.dataset.pollIntervalMs || "2000", 10);
  const domParser = new DOMParser();
  const tabStorageKey = "cymatix-dashboard-tab";
  const agentTabStorageKey = "cymatix-dashboard-agent-tab";
  const agentOpenStorageKey = "cymatix-dashboard-agent-open";
  // Set when a click asks for "start"/"stop"; cleared once the state arrives.
  let pendingAction = null;
  let pendingDeadlineMs = 0;
  // The folded-away switchboard settings; the 2 s panel swap would reset it.
  let switchboardOffOpen = false;

  // Native-shell actions (Diagnostics "Open folder") show only with the bridge.
  if (window.cymatix) document.body.classList.add("has-bridge");
  const pipelineDevStorageKey = "cymatix-pipeline-dev-view";

  // One poll drives everything. These bound that poll; they add no
  // second loop, no second interval and no extra endpoint.
  const fetchDeadlineMs = Math.max(5000, pollIntervalMs * 4);

  let pollTimer = null;
  let inFlight = false;
  let controlsInFlight = false;
  let panelsRenderedAt = monotonicMs();

  function monotonicMs() {
    return typeof performance !== "undefined" && typeof performance.now === "function"
      ? performance.now()
      : Date.now();
  }

  /* Host status observation aging.
     The server serialises how long the observation it rendered stays
     fresh (`data-fresh-for-s`). Once that much local time has passed the
     card is marked expired so the styling stops implying "now". This
     reads two numbers and sets one attribute: no readiness is decided
     here, and nothing is fetched. */
  function markObservationAge() {
    const card = panels.querySelector("[data-host-status]");
    if (!card) return;
    const freshForS = parseFloat(card.dataset.freshForS);
    if (!isFinite(freshForS)) {
      delete card.dataset.observation;
      return;
    }
    const elapsedS = (monotonicMs() - panelsRenderedAt) / 1000;
    if (elapsedS >= freshForS) {
      card.dataset.observation = "expired";
    } else {
      delete card.dataset.observation;
    }
  }

  /* Every poll fetch runs under a deadline that covers the body read as
     well as the response, and the timer is always cleared. A hung
     response aborts instead of holding the last card current forever. */
  async function fetchBounded(url, headers, readBody) {
    const controller =
      typeof AbortController === "function" ? new AbortController() : null;
    const timer =
      controller === null ? null : setTimeout(() => controller.abort(), fetchDeadlineMs);
    try {
      const options = controller === null
        ? { headers: headers }
        : { headers: headers, signal: controller.signal };
      const resp = await fetch(url, options);
      if (!resp.ok) return null;
      return await readBody(resp);
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }

  function setActiveTab(tab) {
    const nextTab = tab || "overview";
    panels.dataset.activeTab = nextTab;
    document.querySelectorAll("[data-tab]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.tab === nextTab);
    });
    try {
      window.localStorage.setItem(tabStorageKey, nextTab);
    } catch (err) {
      // Storage is optional.
    }
  }

  function restoreActiveTab() {
    try {
      const saved = window.localStorage.getItem(tabStorageKey);
      if (saved) {
        setActiveTab(saved);
        return;
      }
    } catch (err) {
      // Ignore storage failures.
    }
    setActiveTab(panels.dataset.activeTab || "overview");
  }

  function setAgentTab(tab) {
    const nextTab = tab || "active";
    document.querySelectorAll("[data-agent-panel]").forEach((panel) => {
      panel.querySelectorAll("[data-agent-tab]").forEach((btn) => {
        const active = btn.dataset.agentTab === nextTab;
        btn.classList.toggle("is-active", active);
        btn.setAttribute("aria-selected", active ? "true" : "false");
      });
      panel.querySelectorAll("[data-agent-view]").forEach((view) => {
        view.hidden = view.dataset.agentView !== nextTab;
      });
    });
    try {
      window.localStorage.setItem(agentTabStorageKey, nextTab);
    } catch (err) {
      // Storage is optional.
    }
  }

  function restoreAgentTab() {
    try {
      setAgentTab(window.localStorage.getItem(agentTabStorageKey) || "active");
      return;
    } catch (err) {
      // Ignore storage failures.
    }
    setAgentTab("active");
  }

  function restoreAgentOpenState() {
    let open = true;
    try {
      const saved = window.localStorage.getItem(agentOpenStorageKey);
      if (saved === "false") open = false;
    } catch (err) {
      // Ignore storage failures.
    }
    document.querySelectorAll("[data-agent-panel]").forEach((panel) => {
      panel.open = open;
    });
  }

  function swapPanelsHtml(htmlString) {
    const activeTab = panels.dataset.activeTab || "overview";
    const doc = domParser.parseFromString(htmlString, "text/html");
    const newNodes = Array.from(doc.body.childNodes);
    panels.replaceChildren(...newNodes);
    panels.dataset.activeTab = activeTab;
    panelsRenderedAt = monotonicMs();
    restoreAgentOpenState();
    restoreAgentTab();
    restorePipelineDevView();
    restoreSwitchboardOff();
    markObservationAge();
  }

  function restoreSwitchboardOff() {
    document.querySelectorAll("[data-keep-open='switchboard-off']").forEach((el) => {
      el.open = switchboardOffOpen;
    });
  }

  /* ── Pipeline panel: dev-view toggle (default ON) ──────────────── */

  function readPipelineDevView() {
    try {
      const saved = window.localStorage.getItem(pipelineDevStorageKey);
      if (saved === "off") return "off";
    } catch (err) {
      // Storage optional — fall through to default.
    }
    return "on";
  }

  function applyPipelineDevView(state) {
    const next = state === "off" ? "off" : "on";
    document.querySelectorAll("[data-pipeline-panel]").forEach((panel) => {
      panel.dataset.dev = next;
      const checkbox = panel.querySelector("[data-pipeline-dev-toggle]");
      if (checkbox instanceof HTMLInputElement) {
        checkbox.checked = next === "on";
      }
    });
    try {
      window.localStorage.setItem(pipelineDevStorageKey, next);
    } catch (err) {
      // Ignore storage failures.
    }
  }

  function restorePipelineDevView() {
    applyPipelineDevView(readPipelineDevView());
  }

  async function fetchPanels() {
    if (inFlight) return;
    inFlight = true;
    try {
      const html = await fetchBounded(
        pollUrl, { Accept: "text/html" }, (resp) => resp.text(),
      );
      if (html === null) {
        panels.dataset.stale = "true";
        return;
      }
      swapPanelsHtml(html);
      delete panels.dataset.stale;
    } catch (err) {
      // Network failure, or this fetch hitting its deadline and
      // aborting. Either way the page below is no longer live, and
      // launcher.css says so in words.
      panels.dataset.stale = "true";
    } finally {
      inFlight = false;
      markObservationAge();
    }
  }

  async function refreshControls() {
    if (controlsInFlight) return;
    controlsInFlight = true;
    try {
      const state = await fetchBounded(
        "/api/state", { Accept: "application/json" }, (resp) => resp.json(),
      );
      if (state === null) return;
      const running = state?.cymatix?.running === true;

      const statusDot = document.querySelector(".status-dot");
      if (statusDot) {
        statusDot.classList.toggle("status-dot--running", running);
        statusDot.classList.toggle("status-dot--stopped", !running);
      }

      const statusLabel = document.querySelector(".status-label");
      if (statusLabel) {
        if (running) {
          statusLabel.textContent =
            "Running / pid " + state.cymatix.pid + " / port " + state.cymatix.port;
        } else {
          statusLabel.textContent = "Stopped";
        }
      }

      const startPending = state?.cymatix?.start_pending === true;
      // A click's "Starting…/Stopping…" holds until its POST resolves (see
      // sendControl); the deadline is a backstop for a request that never does.
      if (pendingAction !== null && monotonicMs() > pendingDeadlineMs) {
        pendingAction = null;
      }
      const btnRestart = document.querySelector('[data-action="restart"]');
      if (btnRestart) btnRestart.disabled = !running || pendingAction !== null;
      renderToggle(running, startPending);
    } catch (err) {
      // The next poll will retry.
    } finally {
      controlsInFlight = false;
    }
  }

  /* The single Start/Stop control: its label always names the next action. */
  function renderToggle(running, startPending) {
    const btn = document.querySelector('[data-action="toggle"]');
    if (!btn) return;
    const label = btn.querySelector("[data-toggle-label]") || btn;
    // startPending only relabels: the supervisor flag is sticky, so it must
    // never disable the one control that can stop a hung start.
    const starting = pendingAction === "start" || (running && startPending);
    btn.dataset.running = running ? "true" : "false";
    btn.dataset.pending = (pendingAction !== null || starting) ? "true" : "false";
    btn.disabled = pendingAction !== null;
    if (pendingAction === "stop") label.textContent = "Stopping…";
    else if (starting) label.textContent = "Starting…";
    else label.textContent = running ? "Stop" : "Start";
  }

  function startPolling() {
    if (pollTimer !== null) return;
    markObservationAge();
    fetchPanels();
    refreshControls();
    pollTimer = setInterval(() => {
      // Age the card on every tick, so an observation still expires
      // visibly while the fetches are failing.
      markObservationAge();
      fetchPanels();
      refreshControls();
    }, pollIntervalMs);
  }

  function stopPolling() {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function sendControl(action) {
    if (action === "start" || action === "stop") {
      pendingAction = action;
      pendingDeadlineMs = monotonicMs() + 90000;
      renderToggle(action === "stop", false);
    }
    const btn = document.querySelector('[data-action="' + action + '"]');
    if (btn) btn.disabled = true;

    try {
      const resp = await fetch("/api/control/" + action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        pendingAction = null;
        alert(action + " failed: " + (body.error || resp.statusText));
      }
    } catch (err) {
      pendingAction = null;
      alert(action + " failed: " + err);
    } finally {
      pendingAction = null;
      setTimeout(() => {
        fetchPanels();
        refreshControls();
      }, 500);
    }
  }

  document.addEventListener("click", function (evt) {
    const target = evt.target;
    if (!(target instanceof HTMLElement)) return;

    const tabButton = target.closest("[data-tab]");
    if (tabButton instanceof HTMLElement) {
      setActiveTab(tabButton.dataset.tab || "overview");
      return;
    }

    const agentTabButton = target.closest("[data-agent-tab]");
    if (agentTabButton instanceof HTMLElement) {
      setAgentTab(agentTabButton.dataset.agentTab || "active");
      return;
    }

    const actionButton = target.closest("[data-action]");
    if (!(actionButton instanceof HTMLElement)) return;
    const action = actionButton.dataset.action;
    if (action === "toggle") {
      if (pendingAction !== null) return;
      sendControl(actionButton.dataset.running === "true" ? "stop" : "start");
      return;
    }
    if (action === "restart") {
      sendControl(action);
      return;
    }
    if (action === "desktop-open-logs") {
      // The rail's own button is handled by the rail's listener.
      if (actionButton.closest("[data-desktop-rail]")) return;
      if (window.cymatix && window.cymatix.openLogs) {
        window.cymatix.openLogs().catch((err) => window.alert("Could not open the folder: " + err));
      }
      return;
    }
    if (action === "copy-path") {
      const text = actionButton.dataset.copyText || "";
      const label = actionButton.textContent;
      if (text && navigator.clipboard) {
        navigator.clipboard.writeText(text).then(() => {
          actionButton.textContent = "Copied";
          setTimeout(() => { actionButton.textContent = label; }, 1500);
        }).catch(() => {});
      }
      return;
    }
    if (action === "lane-start" || action === "lane-stop" || action === "lane-restart" ||
        action === "lane-snapshot") {
      const lane = actionButton.dataset.lane;
      if (!lane) return;
      if (action === "lane-snapshot" && !window.confirm(
          "Replace lane '" + lane + "' with a fresh copy of its source store?\n\n" +
          "Anything written to this lane since its last snapshot is discarded.")) {
        return;
      }
      const verb = action.slice("lane-".length);
      postGenome("/api/control/lanes/" + encodeURIComponent(lane) + "/" + verb,
        {}, actionButton);
      return;
    }
    if (action === "store-sync" || action === "store-freeze" ||
        action === "store-add-folder" || action === "store-remove-folder") {
      handleStoreAction(action, actionButton);
      return;
    }
    if (action === "obs-enable" || action === "obs-disable") {
      // Enabling starts the sidecar, then restarts the backend so it exports.
      if (action === "obs-disable" && !window.confirm(
          "Stop observability?\n\nCymatix will restart so it stops exporting.")) {
        return;
      }
      postGenome("/api/control/observability/" + action.slice("obs-".length),
        {}, actionButton);
      return;
    }
    if (action === "genome-select") {
      const path = actionButton.dataset.genomePath;
      if (!path) return;
      if (!window.confirm("Switch the active genome to:\n\n" + path +
          "\n\nCymatix will restart so the new genome can be loaded.")) {
        return;
      }
      postGenome("/api/genome/select", { path: path }, actionButton);
    }
  });

  async function postGenome(url, payload, btn) {
    if (btn) btn.disabled = true;
    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok || body.ok === false) {
        alert("Genome action failed: " + (body.error || resp.statusText));
      }
    } catch (err) {
      alert("Genome action failed: " + err);
    } finally {
      if (btn) btn.disabled = false;
      setTimeout(() => {
        fetchPanels();
        refreshControls();
      }, 750);
    }
  }

  /* Per-store settings: saved beside the .db by POST /api/genome/settings.
     Changing the active store restarts the server, so that asks first. */
  function storeRow(btn) {
    return btn.closest("[data-store-path]");
  }

  function storeRoots(row) {
    try {
      const roots = JSON.parse(row.dataset.storeRoots || "[]");
      return Array.isArray(roots) ? roots : [];
    } catch (err) {
      return [];
    }
  }

  async function handleStoreAction(action, btn) {
    const row = storeRow(btn);
    if (!row) return;
    const path = row.dataset.storePath;
    const isActive = row.dataset.storeActive === "true";
    const roots = storeRoots(row);
    let payload = null;
    let verb = "";

    if (action === "store-freeze") {
      const freeze = row.dataset.storeFrozen !== "true";
      payload = { frozen: freeze };
      verb = freeze ? "Freeze" : "Unfreeze";
    } else if (action === "store-sync") {
      const on = row.dataset.storeSync !== "true";
      // Send the folders too: a store still following cymatix.toml adopts
      // them, so switching on or off never loses the list.
      payload = { sync_enabled: on, sync_roots: roots };
      verb = on ? "Turn on auto-sync for" : "Turn off auto-sync for";
    } else if (action === "store-remove-folder") {
      const folder = btn.dataset.folder;
      payload = { sync_roots: roots.filter((r) => r !== folder) };
      verb = "Stop watching a folder in";
    } else if (action === "store-add-folder") {
      let folder = null;
      if (window.cymatix && window.cymatix.pickFolder) {
        try {
          folder = await window.cymatix.pickFolder();
        } catch (err) {
          window.alert("Could not open the folder picker: " + err);
          return;
        }
      } else {
        folder = window.prompt("Folder to keep in sync (full path):");
      }
      if (!folder) return;
      payload = { sync_enabled: true, sync_roots: roots.concat([folder]) };
      verb = "Watch a new folder in";
    }
    if (!payload) return;

    if (isActive && !window.confirm(
        verb + " the active knowledge store?\n\nCymatix will restart to apply it.")) {
      return;
    }
    payload.path = path;
    postGenome("/api/genome/settings", payload, btn);
  }

  document.addEventListener("submit", function (evt) {
    const form = evt.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.matches("[data-genome-create]")) return;
    evt.preventDefault();
    const input = form.querySelector('input[name="path"]');
    const path = input instanceof HTMLInputElement ? input.value.trim() : "";
    if (!path) return;
    if (!window.confirm("Create a new genome at:\n\n" + path +
        "\n\nand switch cymatix to it?")) {
      return;
    }
    postGenome("/api/genome/create", { path: path },
               form.querySelector("button"));
  });

  document.addEventListener("toggle", function (evt) {
    const target = evt.target;
    if (!(target instanceof HTMLDetailsElement)) return;
    if (target.matches("[data-keep-open='switchboard-off']")) {
      switchboardOffOpen = target.open;
      return;
    }
    if (!target.matches("[data-agent-panel]")) return;
    try {
      window.localStorage.setItem(agentOpenStorageKey, target.open ? "true" : "false");
    } catch (err) {
      // Storage is optional.
    }
  }, true);

  document.addEventListener("change", function (evt) {
    const target = evt.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (!target.matches("[data-pipeline-dev-toggle]")) return;
    applyPipelineDevView(target.checked ? "on" : "off");
  });

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stopPolling();
    } else {
      startPolling();
    }
  });

  /* ── First-boot db-selection modal (v0.7.0) ────────────────────── */

  const dbModal = document.querySelector("[data-db-modal]");

  async function populateDbModal() {
    if (!dbModal || dbModal.hidden) return;
    const list = dbModal.querySelector("[data-db-modal-list]");
    if (!list) return;
    // Already showing real entries — don't re-render under the user's
    // cursor; the placeholder has no buttons, so this only skips
    // repopulation once a fetch has succeeded (#308).
    if (list.querySelector("button")) return;
    try {
      const resp = await fetch("/api/genomes");
      const body = await resp.json();
      list.replaceChildren();
      (body.genomes || []).forEach((g) => {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.className = "btn btn--mini";
        btn.textContent = "Select";
        btn.addEventListener("click", () => {
          postGenome("/api/genome/select", { path: g.path }, btn);
        });
        const label = document.createElement("span");
        label.className = "path-value";
        label.textContent = g.path + "  (" + (g.total_genes ?? "?") + " genes)";
        li.appendChild(btn);
        li.appendChild(label);
        list.appendChild(li);
      });
      if (!list.children.length) {
        const li = document.createElement("li");
        li.className = "muted";
        li.textContent = "No existing genomes found — create one below.";
        list.appendChild(li);
      }
    } catch (err) {
      // pollDbModal retries on the next tick
    }
  }

  async function maybeDismissDbModal() {
    if (!dbModal || dbModal.hidden) return;
    try {
      const resp = await fetch("/api/state");
      const state = await resp.json();
      if (state?.cymatix?.running || state?.needs_db_selection === false) {
        dbModal.hidden = true;
      }
    } catch (err) {
      // keep showing
    }
  }

  function pollDbModal() {
    maybeDismissDbModal();
    // #308: population used to run only once at page load — a single
    // failed /api/genomes fetch left a dead "Scanning…" placeholder
    // with no Select buttons. Retry until entries render or the modal
    // dismisses.
    populateDbModal();
  }

  if (dbModal && !dbModal.hidden) {
    const closeBtn = dbModal.querySelector("[data-db-modal-close]");
    if (closeBtn) {
      closeBtn.addEventListener("click", () => {
        dbModal.hidden = true;
      });
    }
    populateDbModal();
    setInterval(pollDbModal, 2000);
  }

  restoreActiveTab();
  restoreAgentOpenState();
  restoreAgentTab();
  restorePipelineDevView();
  startPolling();
})();

/*
 * Desktop app rail (?embedded=1). Native actions go through the desktop
 * app's preload bridge (window.cymatix); without it (a plain browser on an
 * embedded URL) the rail explains itself and does nothing.
 */
(function () {
  "use strict";

  const rail = document.querySelector("[data-desktop-rail]");
  if (!rail) return;
  const status = rail.querySelector("[data-desktop-status]");
  const bridge = window.cymatix;
  const RAIL_KEY = "cymatix.desktopRail.collapsed";

  function say(msg) {
    if (status) status.textContent = msg;
  }

  try {
    if (window.localStorage.getItem(RAIL_KEY) === "1") {
      document.body.classList.add("rail-collapsed");
    }
  } catch (_err) { /* storage unavailable: rail starts open */ }

  if (!bridge) {
    say("Open this page in the Cymatix desktop app to use these actions.");
    rail.querySelectorAll("button[data-action^='desktop-']:not([data-action='desktop-rail-toggle']), input")
      .forEach((el) => { el.disabled = true; });
  } else {
    bridge.getLaunchAtLogin().then((on) => {
      const box = rail.querySelector("[data-action='desktop-login-toggle']");
      if (box) box.checked = Boolean(on);
    }).catch(() => {});
  }

  async function run(label, fn) {
    say(label + "…");
    try {
      const result = await fn();
      say(result && result.message ? result.message : label + ": done.");
    } catch (err) {
      say(label + " failed: " + (err && err.message ? err.message : String(err)));
    }
  }

  rail.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-action]") : null;
    if (!target) return;
    const action = target.getAttribute("data-action");
    if (action === "desktop-rail-toggle") {
      const collapsed = document.body.classList.toggle("rail-collapsed");
      try { window.localStorage.setItem(RAIL_KEY, collapsed ? "1" : "0"); } catch (_err) { /* ignore */ }
      return;
    }
    if (!bridge) return;
    if (action === "desktop-mcp-install") {
      const host = rail.querySelector("[data-desktop-mcp-host]").value;
      const lane = rail.querySelector("[data-desktop-mcp-lane]").value;
      run("Writing " + host + " MCP config for lane " + lane,
        () => bridge.installMcp(host, lane));
    } else if (action === "desktop-copy-diagnostics") {
      run("Copying diagnostics", () => bridge.copyDiagnostics());
    } else if (action === "desktop-open-logs") {
      run("Opening logs", () => bridge.openLogs());
    }
  });

  rail.addEventListener("change", (event) => {
    const box = event.target;
    if (!bridge || !(box instanceof HTMLInputElement)) return;
    if (box.getAttribute("data-action") === "desktop-login-toggle") {
      run(box.checked ? "Enabling start at login" : "Disabling start at login",
        () => bridge.setLaunchAtLogin(box.checked));
    }
  });
})();
