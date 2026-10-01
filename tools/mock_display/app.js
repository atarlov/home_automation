(() => {
  const linesEl = document.getElementById("lines");
  const modeEl = document.getElementById("mode");
  const panel = document.getElementById("panel");
  const buttons = document.getElementById("buttons");
  const btnA = document.getElementById("btnA");
  const btnB = document.getElementById("btnB");
  const toast = document.getElementById("toast");
  const mqttEl = document.getElementById("mqtt");
  const labEl = document.getElementById("lab");
  const geom = document.getElementById("geom");
  const product = document.getElementById("product");

  let proposalId = null;

  function showToast(msg) {
    toast.hidden = false;
    toast.textContent = msg;
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      toast.hidden = true;
    }, 2500);
  }

  function renderDisplay(state) {
    if (!state) return;
    if (state.panel_w && state.panel_h) {
      panel.style.width = `${state.panel_w}px`;
      panel.style.height = `${state.panel_h}px`;
      geom.textContent = `${state.panel_w}×${state.panel_h}`;
    }
    if (state.product) product.textContent = state.product;
    modeEl.textContent = state.mode;
    panel.classList.toggle("offline", state.mode === "offline");
    linesEl.innerHTML = "";
    (state.lines || []).forEach((text) => {
      const div = document.createElement("div");
      div.className = "line";
      div.textContent = text;
      linesEl.appendChild(div);
    });
    const showBtns = state.mode === "proposal" && state.proposal_id;
    buttons.hidden = !showBtns;
    proposalId = showBtns ? state.proposal_id : null;
    if (showBtns) {
      btnA.textContent = state.button_a || "Yes";
      btnB.textContent = state.button_b || "No";
    }
  }

  async function refresh() {
    try {
      const res = await fetch("/mock/state");
      const data = await res.json();
      renderDisplay(data.display);
      mqttEl.textContent = data.mqtt_ok
        ? "MQTT: connected (live topics)"
        : `MQTT: offline — ${data.mqtt_error || "start Mosquitto for live decide"}`;
      labEl.textContent = JSON.stringify(
        { clients: data.sources.clients, shellies: data.sources.shellies },
        null,
        2
      );
    } catch (err) {
      mqttEl.textContent = `poll failed: ${err}`;
    }
  }

  async function decide(decision) {
    if (!proposalId) return;
    try {
      const res = await fetch("/mock/decision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: proposalId, decision }),
      });
      const body = await res.json();
      if (!res.ok) {
        showToast(body.error || "decide failed");
        return;
      }
      showToast(`${decision} signed & published`);
    } catch (err) {
      showToast(String(err));
    }
  }

  btnA.addEventListener("click", () => decide("approve"));
  btnB.addEventListener("click", () => decide("deny"));

  document.querySelectorAll("[data-scenario]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.getAttribute("data-scenario");
      await fetch(`/mock/scenario/${name}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      showToast(`scenario: ${name}`);
      refresh();
    });
  });

  refresh();
  setInterval(refresh, 1000);
})();
