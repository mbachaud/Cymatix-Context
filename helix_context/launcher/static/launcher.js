/*
 * Helix Launcher dashboard reactivity.
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
  const tabStorageKey = "helix-dashboard-tab";
  const agentTabStorageKey = "helix-dashboard-agent-tab";
  const agentOpenStorageKey = "helix-dashboard-agent-open";
  const pipelineDevStorageKey = "helix-pipeline-dev-view";

  let pollTimer = null;
  let inFlight = false;

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
    restoreAgentOpenState();
    restoreAgentTab();
    restorePipelineDevView();
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
      const resp = await fetch(pollUrl, { headers: { Accept: "text/html" } });
      if (!resp.ok) {
        panels.dataset.stale = "true";
        return;
      }
      const html = await resp.text();
      swapPanelsHtml(html);
      delete panels.dataset.stale;
    } catch (err) {
      panels.dataset.stale = "true";
    } finally {
      inFlight = false;
    }
  }

  async function refreshControls() {
    try {
      const resp = await fetch("/api/state", { headers: { Accept: "application/json" } });
      if (!resp.ok) return;
      const state = await resp.json();
      const running = state?.helix?.running === true;

      const statusDot = document.querySelector(".status-dot");
      if (statusDot) {
        statusDot.classList.toggle("status-dot--running", running);
        statusDot.classList.toggle("status-dot--stopped", !running);
      }

      const statusLabel = document.querySelector(".status-label");
      if (statusLabel) {
        if (running) {
          statusLabel.textContent =
            "Running / pid " + state.helix.pid + " / port " + state.helix.port;
        } else {
          statusLabel.textContent = "Stopped";
        }
      }

      const btnStart = document.querySelector('[data-action="start"]');
      const btnRestart = document.querySelector('[data-action="restart"]');
      const btnStop = document.querySelector('[data-action="stop"]');
      if (btnStart) btnStart.disabled = running;
      if (btnRestart) btnRestart.disabled = !running;
      if (btnStop) btnStop.disabled = !running;
    } catch (err) {
      // The next poll will retry.
    }
  }

  function startPolling() {
    if (pollTimer !== null) return;
    fetchPanels();
    refreshControls();
    pollTimer = setInterval(() => {
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
    const btn = document.querySelector('[data-action="' + action + '"]');
    if (btn) btn.disabled = true;

    try {
      const resp = await fetch("/api/control/" + action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        alert(action + " failed: " + (body.error || resp.statusText));
      }
    } catch (err) {
      alert(action + " failed: " + err);
    } finally {
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
    if (action === "start" || action === "stop" || action === "restart") {
      sendControl(action);
      return;
    }
    if (action === "bench-start" || action === "bench-stop") {
      const ep = action === "bench-start"
        ? "/api/control/bench/start" : "/api/control/bench/stop";
      postGenome(ep, {}, actionButton);
      return;
    }
    if (action === "genome-select") {
      const path = actionButton.dataset.genomePath;
      if (!path) return;
      if (!window.confirm("Switch the active genome to:\n\n" + path +
          "\n\nHelix will restart so the new genome can be loaded.")) {
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

  document.addEventListener("submit", function (evt) {
    const form = evt.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.matches("[data-genome-create]")) return;
    evt.preventDefault();
    const input = form.querySelector('input[name="path"]');
    const path = input instanceof HTMLInputElement ? input.value.trim() : "";
    if (!path) return;
    if (!window.confirm("Create a new genome at:\n\n" + path +
        "\n\nand switch helix to it?")) {
      return;
    }
    postGenome("/api/genome/create", { path: path },
               form.querySelector("button"));
  });

  document.addEventListener("toggle", function (evt) {
    const target = evt.target;
    if (!(target instanceof HTMLDetailsElement)) return;
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
      // next poll retries
    }
  }

  async function maybeDismissDbModal() {
    if (!dbModal || dbModal.hidden) return;
    try {
      const resp = await fetch("/api/state");
      const state = await resp.json();
      if (state?.helix?.running || state?.needs_db_selection === false) {
        dbModal.hidden = true;
      }
    } catch (err) {
      // keep showing
    }
  }

  if (dbModal && !dbModal.hidden) {
    populateDbModal();
    setInterval(maybeDismissDbModal, 2000);
  }

  restoreActiveTab();
  restoreAgentOpenState();
  restoreAgentTab();
  restorePipelineDevView();
  startPolling();
})();
