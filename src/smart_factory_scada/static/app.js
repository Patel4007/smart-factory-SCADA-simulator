const state = {
  snapshot: null,
  source: null,
  socket: null,
  channels: [],
  channelId: "server_sent_events",
  connectionStatus: "disconnected",
  connectionMessage: "Loading available stream channels...",
};

const uploadWidgetMarkup = `
  <form id="upload-form" class="upload-widget upload-widget--inline">
    <div class="stream-widget__header">
      <div>
        <p class="eyebrow">Real Data Intake</p>
        <h3 class="stream-widget__title">Upload Trace</h3>
      </div>
    </div>
    <label class="stream-widget__field">
      <span class="chart__label">Target Machine</span>
      <select id="upload-machine-select" class="stream-select"></select>
    </label>
    <div class="upload-grid">
      <label class="stream-widget__field">
        <span class="chart__label">Replay Role</span>
        <select id="upload-role-select" class="stream-select">
          <option value="normal">Normal Replay</option>
          <option value="fault">Fault Replay</option>
        </select>
      </label>
      <label class="stream-widget__field">
        <span class="chart__label">Sample Rate (Hz)</span>
        <input id="upload-sample-rate" class="control-input" type="number" min="1" step="1" value="2000" />
      </label>
    </div>
    <label class="stream-widget__field">
      <span class="chart__label">Trace File</span>
      <input id="upload-file-input" class="file-input" type="file" accept=".h5,.hdf5,.csv,.txt" />
    </label>
    <div class="button-row button-row--stream">
      <button id="upload-submit-button" type="submit" class="button--upload">Upload Data</button>
    </div>
    <div id="upload-feedback" class="feedback">
      Load a \`.h5\`, \`.hdf5\`, \`.csv\`, or \`.txt\` vibration trace and bind it to a machine replay profile.
    </div>
    <div class="upload-library">
      <p class="eyebrow">Active Uploads</p>
      <div id="upload-binding-list" class="stack"></div>
    </div>
  </form>
`;

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

function number(value, suffix = "") {
  return `${fmt.format(value ?? 0)}${suffix}`;
}

function stateClass(value) {
  return `state--${String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
}

function severityClass(value) {
  return `severity--${String(value || "info").toLowerCase()}`;
}

function setFeedback(message, error = false) {
  const node = document.getElementById("command-feedback");
  node.textContent = message;
  node.style.color = error ? "var(--red)" : "var(--muted)";
}

function setUploadFeedback(message, error = false) {
  const node = document.getElementById("upload-feedback");
  if (!node) {
    return;
  }
  node.textContent = message;
  node.style.color = error ? "var(--red)" : "var(--muted)";
}

function ensureUploadInterface() {
  if (document.getElementById("upload-form")) {
    return;
  }
  const streamWidget = document.querySelector(".stream-widget");
  if (!streamWidget || !streamWidget.parentElement) {
    return;
  }
  streamWidget.insertAdjacentHTML("afterend", uploadWidgetMarkup);
}

function activeChannel() {
  return state.channels.find((channel) => channel.channel_id === state.channelId) || state.channels[0] || null;
}

function websocketUrl(endpoint) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${endpoint}`;
}

function setStreamStatus(status, message) {
  state.connectionStatus = status;
  state.connectionMessage = message;
  renderStreamControls();
}

function renderStreamControls() {
  const select = document.getElementById("stream-channel-select");
  const detail = document.getElementById("stream-channel-detail");
  const badge = document.getElementById("stream-status-badge");
  const connectButton = document.getElementById("stream-connect-button");
  const disconnectButton = document.getElementById("stream-disconnect-button");

  select.innerHTML = state.channels
    .map(
      (channel) => `
        <option value="${channel.channel_id}">
          ${channel.label} · ${channel.protocol.toUpperCase()}
        </option>
      `
    )
    .join("");
  if (state.channelId) {
    select.value = state.channelId;
  }

  const channel = activeChannel();
  const statusLabel = {
    connected: "Connected",
    connecting: "Connecting",
    disconnected: "Disconnected",
  }[state.connectionStatus] || "Disconnected";

  badge.textContent = statusLabel;
  badge.className = `stream-badge stream-badge--${state.connectionStatus}`;

  if (channel) {
    detail.textContent = `${channel.description} ${state.connectionMessage}`.trim();
  } else {
    detail.textContent = state.connectionMessage;
  }

  connectButton.disabled = !channel;
  disconnectButton.disabled = state.connectionStatus === "disconnected";
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || "Request failed");
  }
  return body;
}

function render(snapshot) {
  state.snapshot = snapshot;
  document.getElementById("line-state").innerHTML = `<span class="machine__pill ${stateClass(snapshot.metrics.line_state)}">${snapshot.metrics.line_state}</span>`;
  document.getElementById("transport-mode").innerHTML = `
    <span class="transport-pill ${snapshot.kafka_enabled ? "transport-pill--kafka" : "transport-pill--fallback"}">
      ${snapshot.transport}
    </span>
  `;
  document.getElementById("heartbeat").textContent = new Date(snapshot.generated_at).toLocaleTimeString();
  document.getElementById("plant-meta").textContent = `${snapshot.site_name} · ${snapshot.line_name} · ${snapshot.data_source}`;

  renderKpis(snapshot);
  renderScenarios(snapshot);
  renderUploadPanel(snapshot);
  renderMachines(snapshot);
  renderCharts(snapshot);
  renderAlarms(snapshot);
  renderEvents(snapshot);
}

function renderKpis(snapshot) {
  const metrics = snapshot.metrics;
  const cards = [
    ["Throughput", `${number(metrics.throughput_uph)} UPH`, `Target ${number(metrics.target_throughput_uph)} UPH`],
    ["Downtime", `${number(metrics.downtime_pct, "%")}`, `${number(metrics.availability_pct, "%")} availability`],
    ["OEE", `${number(metrics.oee, "%")}`, `${number(metrics.performance_pct, "%")} performance`],
    ["Health", `${number(metrics.overall_health, "%")}`, `${metrics.active_alarm_count} active alarms`],
    ["Energy", `${number(metrics.energy_kw)} kW`, `${number(metrics.buffer_utilization_pct, "%")} buffer fill`],
  ];
  document.getElementById("kpi-grid").innerHTML = cards
    .map(
      ([label, value, delta]) => `
        <article class="kpi">
          <div class="kpi__label">${label}</div>
          <div class="kpi__value">${value}</div>
          <div class="kpi__delta">${delta}</div>
        </article>
      `
    )
    .join("");
}

function renderScenarios(snapshot) {
  const container = document.getElementById("scenario-list");
  container.innerHTML = snapshot.scenarios
    .map(
      (scenario) => `
        <article class="scenario">
          <h3>${scenario.name}</h3>
          <p>${scenario.description}</p>
          <button data-scenario="${scenario.scenario_id}" class="button--scenario">Trigger Scenario</button>
        </article>
      `
    )
    .join("");

  container.querySelectorAll("button[data-scenario]").forEach((button) => {
    button.addEventListener("click", async () => {
      const scenarioId = button.dataset.scenario;
      try {
        const result = await postJson(`/api/scenarios/${scenarioId}`, { duration_seconds: 25 });
        setFeedback(result.detail);
      } catch (error) {
        setFeedback(error.message, true);
      }
    });
  });
}

function renderUploadPanel(snapshot) {
  ensureUploadInterface();
  const machineSelect = document.getElementById("upload-machine-select");
  const bindingList = document.getElementById("upload-binding-list");
  if (!machineSelect || !bindingList) {
    return;
  }
  const selectedMachineId = machineSelect.value;

  machineSelect.innerHTML = snapshot.machines
    .map(
      (machine) => `
        <option value="${machine.machine_id}">
          ${machine.machine_id} · ${machine.name}
        </option>
      `
    )
    .join("");
  if (snapshot.machines.some((machine) => machine.machine_id === selectedMachineId)) {
    machineSelect.value = selectedMachineId;
  }

  if (!snapshot.trace_uploads.length) {
    bindingList.innerHTML = `
      <div class="stack__item">
        <div class="stack__title">No uploaded traces assigned</div>
        <div class="stack__meta">The simulator is currently replaying the bundled real dataset only.</div>
      </div>
    `;
    return;
  }

  bindingList.innerHTML = snapshot.trace_uploads
    .map(
      (binding) => `
        <article class="stack__item">
          <div class="stack__header">
            <div class="stack__title">${binding.machine_id} · ${binding.role === "normal" ? "Normal Replay" : "Fault Replay"}</div>
            <span class="severity ${severityClass(binding.role === "fault" ? "warning" : "info")}">${binding.source_format.toUpperCase()}</span>
          </div>
          <div>${binding.file_name}</div>
          <div class="stack__meta">
            ${binding.trace_id} · ${number(binding.sample_rate_hz)} Hz · ${number(binding.window_count)} windows · ${number(binding.sample_count)} samples
          </div>
        </article>
      `
    )
    .join("");
}

function renderMachines(snapshot) {
  const container = document.getElementById("machines-grid");
  container.innerHTML = snapshot.machines
    .map(
      (machine) => `
        <article class="machine">
          <div class="machine__header">
            <div>
              <div class="machine__meta">${machine.machine_id}</div>
              <h3>${machine.name}</h3>
            </div>
            <span class="machine__pill ${stateClass(machine.state)}">${machine.state}</span>
          </div>
          <div class="machine__meta">${machine.trace_id} · ${machine.trace_quality} trace</div>
          <div class="machine__detail">${machine.detail}</div>
          <div class="machine__grid">
            <div class="machine__metric">
              <span class="chart__label">Throughput</span>
              <strong>${number(machine.throughput_uph)}</strong>
            </div>
            <div class="machine__metric">
              <span class="chart__label">Health</span>
              <strong>${number(machine.health_score, "%")}</strong>
            </div>
            <div class="machine__metric">
              <span class="chart__label">Temp</span>
              <strong>${number(machine.temperature_c, "°C")}</strong>
            </div>
            <div class="machine__metric">
              <span class="chart__label">Buffer</span>
              <strong>${number(machine.buffer_fill_pct, "%")}</strong>
            </div>
            <div class="machine__metric">
              <span class="chart__label">Trace RMS</span>
              <strong>${number(machine.signal_rms)}</strong>
            </div>
            <div class="machine__metric">
              <span class="chart__label">Trace Peak</span>
              <strong>${number(machine.signal_peak)}</strong>
            </div>
          </div>
          <div class="machine__controls">
            <label class="chart__label" for="speed-${machine.machine_id}">
              Speed Setpoint ${number(machine.speed_setpoint, "x")}
            </label>
            <input
              id="speed-${machine.machine_id}"
              type="range"
              min="0.5"
              max="1.3"
              step="0.05"
              value="${machine.speed_setpoint}"
              data-machine-speed="${machine.machine_id}"
            />
            <div class="machine__buttons">
              <button data-machine-action="${machine.machine_id}" data-action="clear_fault" class="button--ghost">Clear Fault</button>
              <button
                data-machine-action="${machine.machine_id}"
                data-action="maintenance"
                data-enabled="${machine.state !== "maintenance"}"
                class="button--ghost"
              >
                ${machine.state === "maintenance" ? "Resume Auto" : "Maintenance"}
              </button>
            </div>
          </div>
        </article>
      `
    )
    .join("");

  container.querySelectorAll("input[data-machine-speed]").forEach((input) => {
    input.addEventListener("change", async () => {
      try {
        const result = await postJson(`/api/control/machines/${input.dataset.machineSpeed}`, {
          action: "set_speed",
          speed: Number(input.value),
        });
        setFeedback(result.detail);
      } catch (error) {
        setFeedback(error.message, true);
      }
    });
  });

  container.querySelectorAll("button[data-machine-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        const action = button.dataset.action;
        const machineId = button.dataset.machineAction;
        const payload = action === "maintenance"
          ? { action, enabled: button.dataset.enabled === "true", duration_seconds: 20 }
          : { action };
        const result = await postJson(`/api/control/machines/${machineId}`, payload);
        setFeedback(result.detail);
      } catch (error) {
        setFeedback(error.message, true);
      }
    });
  });
}

function renderCharts(snapshot) {
  const history = [...snapshot.history].reverse();
  const charts = [
    ["Throughput", snapshot.metrics.throughput_uph, history.map((item) => item.throughput_uph), "UPH"],
    ["Health", snapshot.metrics.overall_health, history.map((item) => item.health_score), "%"],
    ["Energy", snapshot.metrics.energy_kw, history.map((item) => item.power_kw), "kW"],
    ["Alarms", snapshot.metrics.active_alarm_count, history.map((item) => item.active_alarm_count), ""],
  ];
  document.getElementById("chart-grid").innerHTML = charts
    .map(
      ([label, value, values, suffix]) => `
        <article class="chart">
          <div class="chart__label">${label}</div>
          <div class="chart__value">${number(value, suffix ? ` ${suffix}` : "")}</div>
          ${sparkline(values)}
        </article>
      `
    )
    .join("");
}

function renderAlarms(snapshot) {
  const container = document.getElementById("alarm-list");
  if (!snapshot.alarms.length) {
    container.innerHTML = `<div class="stack__item"><div class="stack__title">No active alarms</div><div class="stack__meta">The line is operating within configured thresholds.</div></div>`;
    return;
  }
  container.innerHTML = snapshot.alarms
    .map(
      (alarm) => `
        <article class="stack__item">
          <div class="stack__header">
            <div class="stack__title">${alarm.machine_id} · ${alarm.code}</div>
            <span class="severity ${severityClass(alarm.severity)}">${alarm.severity}</span>
          </div>
          <div>${alarm.message}</div>
          <div class="stack__meta">${new Date(alarm.timestamp).toLocaleTimeString()}</div>
        </article>
      `
    )
    .join("");
}

function renderEvents(snapshot) {
  const container = document.getElementById("event-list");
  container.innerHTML = snapshot.events
    .map(
      (event) => `
        <article class="stack__item">
          <div class="stack__header">
            <div class="stack__title">${event.title}</div>
            <span class="severity ${severityClass(event.severity)}">${event.severity}</span>
          </div>
          <div>${event.detail}</div>
          <div class="stack__meta">${event.source} · ${new Date(event.timestamp).toLocaleTimeString()}</div>
        </article>
      `
    )
    .join("");
}

function sparkline(values) {
  if (!values.length) {
    return `<svg viewBox="0 0 240 140"></svg>`;
  }
  const clean = values.map((value) => Number(value || 0));
  const max = Math.max(...clean, 1);
  const min = Math.min(...clean, 0);
  const range = max - min || 1;
  const points = clean
    .map((value, index) => {
      const x = (index / Math.max(clean.length - 1, 1)) * 240;
      const y = 120 - ((value - min) / range) * 100;
      return `${x},${y}`;
    })
    .join(" ");
  return `
    <svg viewBox="0 0 240 140" preserveAspectRatio="none">
      <defs>
        <linearGradient id="spark-fill" x1="0%" y1="0%" x2="0%" y2="100%">
          <stop offset="0%" stop-color="rgba(91,230,255,0.35)"></stop>
          <stop offset="100%" stop-color="rgba(91,230,255,0.02)"></stop>
        </linearGradient>
      </defs>
      <polyline fill="none" stroke="rgba(91, 230, 255, 0.96)" stroke-width="3" points="${points}"></polyline>
      <line x1="0" y1="120" x2="240" y2="120" stroke="rgba(255,255,255,0.1)" stroke-width="1"></line>
    </svg>
  `;
}

async function loadInitial() {
  const [snapshotResponse, channelsResponse] = await Promise.all([
    fetch("/api/dashboard"),
    fetch("/api/stream/channels"),
  ]);
  const snapshot = await snapshotResponse.json();
  state.channels = await channelsResponse.json();
  if (!state.channels.find((channel) => channel.channel_id === state.channelId) && state.channels.length) {
    state.channelId = state.channels[0].channel_id;
  }
  render(snapshot);
  renderStreamControls();
}

function disconnectStream(manual = true) {
  if (state.source) {
    const source = state.source;
    state.source = null;
    source.close();
  }
  if (state.socket) {
    const socket = state.socket;
    state.socket = null;
    socket.close();
  }
  setStreamStatus("disconnected", manual ? "Channel closed by operator." : "");
  if (manual) {
    setFeedback("Live channel disconnected.");
  }
}

function connectStream(channelId = state.channelId, options = {}) {
  const { silent = false } = options;
  const channel = state.channels.find((item) => item.channel_id === channelId);
  if (!channel) {
    setStreamStatus("disconnected", "No stream channel is available.");
    return;
  }

  state.channelId = channel.channel_id;
  disconnectStream(false);
  setStreamStatus("connecting", `Opening ${channel.label}...`);

  if (channel.protocol === "sse") {
    const source = new EventSource(channel.endpoint);
    state.source = source;
    source.addEventListener("open", () => {
      if (state.source !== source) {
        return;
      }
      setStreamStatus("connected", `${channel.label} is streaming live telemetry.`);
    });
    source.addEventListener("snapshot", (event) => {
      if (state.source !== source) {
        return;
      }
      render(JSON.parse(event.data));
    });
    source.onerror = () => {
      if (state.source !== source) {
        return;
      }
      setStreamStatus("connecting", `${channel.label} dropped. Retrying automatically...`);
    };
  } else {
    const socket = new WebSocket(websocketUrl(channel.endpoint));
    state.socket = socket;
    socket.onopen = () => {
      if (state.socket !== socket) {
        return;
      }
      setStreamStatus("connected", `${channel.label} is streaming live telemetry.`);
    };
    socket.onmessage = (event) => {
      if (state.socket !== socket) {
        return;
      }
      render(JSON.parse(event.data));
    };
    socket.onerror = () => {
      if (state.socket !== socket) {
        return;
      }
      setStreamStatus("connecting", `${channel.label} encountered a transport issue.`);
    };
    socket.onclose = () => {
      if (state.socket !== socket) {
        return;
      }
      state.socket = null;
      setStreamStatus("disconnected", `${channel.label} closed. Select Connect to resume.`);
    };
  }

  if (!silent) {
    setFeedback(`Connecting to ${channel.label}.`);
  }
}

document.querySelectorAll("button[data-line-action]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      const result = await postJson("/api/control/line", { action: button.dataset.lineAction });
      setFeedback(result.detail);
    } catch (error) {
      setFeedback(error.message, true);
    }
  });
});

document.getElementById("stream-channel-select").addEventListener("change", (event) => {
  state.channelId = event.target.value;
  if (state.connectionStatus === "connected" || state.connectionStatus === "connecting") {
    connectStream(state.channelId, { silent: true });
  } else {
    state.connectionMessage = "Channel selected. Choose Connect to start streaming.";
    renderStreamControls();
  }
});

document.getElementById("stream-connect-button").addEventListener("click", () => {
  connectStream(state.channelId);
});

document.getElementById("stream-disconnect-button").addEventListener("click", () => {
  disconnectStream();
});

ensureUploadInterface();

document.getElementById("upload-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const fileInput = document.getElementById("upload-file-input");
  const machineId = document.getElementById("upload-machine-select").value;
  const role = document.getElementById("upload-role-select").value;
  const sampleRate = Number(document.getElementById("upload-sample-rate").value || 0);
  const file = fileInput.files[0];

  if (!file) {
    setUploadFeedback("Choose a trace file before uploading.", true);
    return;
  }
  if (!machineId) {
    setUploadFeedback("Choose a target machine for this trace.", true);
    return;
  }
  if (!Number.isFinite(sampleRate) || sampleRate <= 0) {
    setUploadFeedback("Sample rate must be a positive number.", true);
    return;
  }

  const params = new URLSearchParams({
    file_name: file.name,
    sample_rate_hz: String(sampleRate),
  });

  try {
    const response = await fetch(`/api/data/upload/${machineId}/${role}?${params.toString()}`, {
      method: "PUT",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body.detail || "Upload failed");
    }
    setUploadFeedback(body.detail);
    setFeedback(body.detail);
    fileInput.value = "";
    const snapshot = await fetch("/api/dashboard").then((item) => item.json());
    render(snapshot);
  } catch (error) {
    setUploadFeedback(error.message, true);
  }
});

loadInitial()
  .then(() => connectStream(state.channelId, { silent: true }))
  .catch((error) => setFeedback(error.message, true));
