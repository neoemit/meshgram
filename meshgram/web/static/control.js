"use strict";
// Control panel: the radio's settings, channels and contacts, the plugins, and
// config.yaml with the plugin changes made here.
// Talks to the JSON API in meshgram/web/api.py. Loaded after the page's main
// script and uses its helpers ($, el, icon, store, fmtAgo, plural, NODE_TYPES)
// and state (controlState, connections, currentView).

(() => {
  const SECTIONS = ["radio", "channels", "contacts", "plugins", "config"];
  const SECRET_MASK = "••••••••";
  const BANDWIDTHS = [7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125, 250, 500];
  const TELEMETRY = [[0, "Nobody"], [1, "Contacts allowed to"], [2, "Everyone"]];
  const PATH_HASH_SIZES = [[0, "1 byte"], [1, "2 bytes"], [2, "3 bytes"]];
  const MAX_CONTACT_ROWS = 300;
  const KIND_LABELS = { public: "Public", hashtag: "Hashtag", private: "Private" };

  const savedSection = store.get("controlSection", "radio");
  const ui = {
    section: SECTIONS.includes(savedSection) ? savedSection : "radio",
    data: { radio: null, channels: null, contacts: null, plugins: null, config: null },
    errors: {},
    loading: {},
    stale: new Set(SECTIONS),
    openPlugins: new Set(),
    busyPlugins: new Set(),
    editors: new Map(),        // plugin name -> open settings editor
    editingChannel: null,      // slot being renamed
    contactQuery: "",
    contactType: "all",
    radioState: null,
    allows: null,
    focusPlugin: null,         // plugin whose settings a #control/plugins/<name> link opened
    configView: null,          // "diff" or "file": what the config file section shows
  };
  let uid = 0;
  const nextId = (prefix) => `c-${prefix}-${++uid}`;
  const canChange = () => Boolean(controlState && controlState.allows_changes);

  // --- API ---------------------------------------------------------------------------

  class ApiError extends Error {
    constructor(message, status, details) {
      super(message);
      this.status = status;
      this.details = details;
    }
  }

  async function api(method, path, body) {
    const options = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
    if (method !== "GET" && method !== "DELETE") {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body === undefined ? {} : body);
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch {
      throw new ApiError("Can't reach Meshgram; check that it's running", 0);
    }
    if (response.status === 204) return null;
    let data = null;
    try { data = await response.json(); } catch { /* not JSON */ }
    if (!response.ok) {
      throw new ApiError((data && data.error) || `${response.status} ${response.statusText}`, response.status, data && data.details);
    }
    return data;
  }

  // --- Feedback ------------------------------------------------------------------------

  let toastTimer = null;
  function toast(text, kind = "ok") {
    const box = $("ctl-toast");
    box.dataset.kind = kind;
    box.replaceChildren(el("span", { html: icon(kind === "ok" ? "check" : "alert") }), el("span", {}, text));
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { box.hidden = true; }, kind === "ok" ? 3500 : 8000);
  }

  function confirmDialog({ title, text, ok = "OK", danger = false }) {
    const dialog = $("confirm-dialog");
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    const okButton = $("confirm-ok");
    okButton.textContent = ok;
    okButton.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
      dialog.returnValue = "cancel";
      dialog.showModal();
      $("confirm-cancel").focus();
    });
  }

  async function copyText(text, what) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        // The Clipboard API needs HTTPS or localhost; fall back to a hidden textarea.
        const area = el("textarea", { style: "position:fixed;opacity:0", readonly: true });
        area.value = text;
        document.body.append(area);
        area.select();
        const copied = document.execCommand("copy");
        area.remove();
        if (!copied) throw new Error();
      }
      toast(`${what} copied`);
    } catch {
      toast(`Couldn't copy the ${what.toLowerCase()}; select it and copy it by hand`, "error");
    }
  }

  // Runs an action from a button, keeping the button busy meanwhile.
  async function act(button, work, success) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      const result = await work();
      if (success) toast(typeof success === "function" ? success(result) : success);
      return result;
    } catch (err) {
      toast(err.message, "error");
      return undefined;
    } finally {
      if (button.isConnected) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
      }
    }
  }

  // --- Formatting -------------------------------------------------------------------------

  const dash = "—";
  const utf8Bytes = (text) => new TextEncoder().encode(text).length;
  const nodeTypeLabel = (type) => (NODE_TYPES[type] || NODE_TYPES.unknown).name;

  function fmtDuration(seconds) {
    if (!Number.isFinite(seconds)) return dash;
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d) return `${d} d ${h} h`;
    if (h) return `${h} h ${m} min`;
    if (m) return `${m} min`;
    return `${Math.round(seconds)} s`;
  }

  function fmtDrift(seconds) {
    if (!Number.isFinite(seconds)) return dash;
    if (Math.abs(seconds) < 5) return "In sync";
    return `${fmtDuration(Math.abs(seconds))} ${seconds > 0 ? "ahead" : "behind"}`;
  }

  const fmtNumber = (value, digits = 0) => (Number.isFinite(value) ? value.toLocaleString([], { maximumFractionDigits: digits }) : dash);

  // --- Building blocks -------------------------------------------------------------------

  function card({ title, sub, actions = [], body = [], foot = null, tag = "section", className = "" }) {
    return el(tag, { class: `card ${className}`.trim() },
      el("div", { class: "card-head" },
        el("h3", {}, title),
        sub ? el("span", { class: "sub" }, sub) : null,
        el("span", { class: "spacer" }),
        ...actions),
      el("div", { class: "card-body" }, ...body),
      foot);
  }

  function button(label, { iconName, onClick, kind = "", small = true, title, disabled, type = "button", hideLabel = false } = {}) {
    return el("button", {
      class: `btn ${small ? "btn-sm" : ""} ${kind} ${hideLabel ? "btn-icon" : ""}`.replace(/\s+/g, " ").trim(),
      type,
      title: title || (hideLabel ? label : null),
      "aria-label": hideLabel ? label : null,
      disabled: disabled || null,
      onclick: onClick,
      html: `${iconName ? icon(iconName) : ""}${hideLabel ? "" : `<span>${esc(label)}</span>`}`,
    });
  }

  // ``labelFor``: the input the label names, when ``control`` wraps it (with a button, say).
  function field(label, control, { hint, wide, required, labelFor } = {}) {
    const target = labelFor || control;
    const controlId = target.id || nextId("f");
    target.id = controlId;
    const hintId = hint ? `${controlId}-hint` : null;
    if (hintId) target.setAttribute("aria-describedby", hintId);
    return el("div", { class: `field ${wide ? "wide" : ""}`.trim() },
      el("label", { for: controlId }, label, required ? el("span", { "aria-hidden": "true" }, " *") : null),
      control,
      hint ? el("p", { class: "hint", id: hintId }, hint) : null);
  }

  function switchField(label, checked, { hint, id } = {}) {
    const inputId = id || nextId("sw");
    const input = el("input", { class: "switch", type: "checkbox", role: "switch", id: inputId, checked: checked || null });
    const row = el("div", { class: "field switch-field" },
      input,
      el("div", { class: "text" }, el("label", { for: inputId }, label), hint ? el("p", { class: "hint" }, hint) : null));
    return { row, input };
  }

  function selectInput(options, value, attrs = {}) {
    return el("select", attrs, ...options.map(([optionValue, label]) =>
      el("option", { value: String(optionValue), selected: String(optionValue) === String(value) || null }, label)));
  }

  function numberInput(value, { min, max, step = "any", placeholder } = {}) {
    return el("input", {
      type: "number", inputmode: step === 1 ? "numeric" : "decimal",
      value: Number.isFinite(value) ? String(value) : null, min, max, step, placeholder,
    });
  }

  function keyRow(key, label = "Public key") {
    if (!key) return dash;
    return el("span", { class: "key-row" },
      el("code", { title: key }, key),
      button(`Copy ${label.toLowerCase()}`, { iconName: "copy", hideLabel: true, onClick: () => copyText(key, label) }));
  }

  function notice(kind, text, extra = []) {
    return el("div", { class: `notice ${kind}`, role: kind === "bad" ? "alert" : "note" },
      el("span", { html: icon(kind === "bad" ? "alert" : "info") }),
      el("div", {}, el("p", {}, text), ...extra));
  }

  function disableIfReadOnly(root) {
    if (canChange()) return;
    root.querySelectorAll("input, select, textarea, button[data-change]").forEach((node) => { node.disabled = true; });
  }

  function placeholder(section) {
    if (ui.errors[section]) {
      return card({
        title: "Couldn't load this",
        body: [notice("bad", ui.errors[section]), button("Try again", { iconName: "refresh", onClick: () => load(section) })],
      });
    }
    return el("div", { class: "card skeleton", "aria-busy": "true" }, "Loading…");
  }

  function radioOffline() {
    const radio = connections.list.find((conn) => conn.key === "radio");
    return card({
      title: "The radio isn't connected",
      body: [el("p", { class: "hint" }, radio && radio.detail ? radio.detail : "Meshgram keeps trying to reach it."),
        el("p", { class: "hint" }, "Its settings, channels and contacts can be changed once it's back.")],
      actions: [button("Refresh", { iconName: "refresh", onClick: () => load("radio") })],
    });
  }

  // A card holding a form: tracks unsaved edits, disables itself when read-only.
  function formCard({ title, sub, fields, onSubmit, submitLabel = "Save", note }) {
    const status = el("span", { class: "hint", role: "status" });
    const submit = el("button", { class: "btn btn-sm btn-primary", type: "submit", "data-change": "" }, submitLabel);
    const reset = el("button", { class: "btn btn-sm", type: "reset", "data-change": "" }, "Undo changes");
    const form = el("form", { class: "card", novalidate: true },
      el("div", { class: "card-head" }, el("h3", {}, title), sub ? el("span", { class: "sub" }, sub) : null),
      el("div", { class: "card-body" }, note || null, el("div", { class: "form-grid" }, ...fields)),
      el("div", { class: "card-foot" }, submit, reset, el("span", { class: "spacer" }), status));
    const markDirty = () => { form.dataset.dirty = "1"; status.textContent = "Unsaved changes"; };
    form.addEventListener("input", markDirty);
    form.addEventListener("change", markDirty);
    form.addEventListener("reset", () => { delete form.dataset.dirty; status.textContent = ""; });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      status.textContent = "Saving…";
      try {
        const message = await onSubmit(form);
        delete form.dataset.dirty;
        status.textContent = "";
        if (message) toast(message);
      } catch (err) {
        status.textContent = "";
        if (!(err instanceof Cancelled)) toast(err.message, "error");
      } finally {
        submit.disabled = !canChange();
      }
    });
    disableIfReadOnly(form);
    return form;
  }

  class Cancelled extends Error {}

  // --- Sections ---------------------------------------------------------------------------

  // #control/<section>, or #control/plugins/<name> to open a plugin's settings.
  const parseHash = () => {
    const match = /^#control\/(\w+)(?:\/([\w.:-]+))?/.exec(location.hash);
    if (!match || !SECTIONS.includes(match[1])) return { section: null, plugin: null };
    return { section: match[1], plugin: match[1] === "plugins" ? match[2] || null : null };
  };

  function openPluginFromLink(plugin) {
    if (!plugin) return;
    ui.openPlugins.add(plugin);
    ui.focusPlugin = plugin;
  }

  function selectSection(section, { focus = false } = {}) {
    ui.section = section;
    store.set("controlSection", section);
    for (const name of SECTIONS) {
      const tab = $(`ctab-${name}`);
      const selected = name === section;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      $(`cpanel-${name}`).hidden = !selected;
      if (selected && focus) tab.focus();
    }
    if (location.hash !== `#control/${section}`) history.replaceState(history.state, "", `#control/${section}`);
    render(section);
    // config.yaml can change on disk without anyone saying so.
    if (ui.stale.has(section) || ui.errors[section] || section === "config") load(section);
  }

  const panelDirty = (section) => Boolean($(`cpanel-${section}`).querySelector("form[data-dirty]"));

  async function load(section, { refresh = false } = {}) {
    if (ui.loading[section]) return;
    ui.loading[section] = true;
    ui.stale.delete(section);
    if (!ui.data[section]) render(section);
    try {
      if (section === "radio") {
        ui.data.radio = await api("GET", "api/radio");
      } else if (section === "channels") {
        ui.data.channels = await api("GET", `api/radio/channels${refresh ? "?refresh=1" : ""}`);
      } else if (section === "contacts") {
        ui.data.contacts = await api("GET", `api/radio/contacts${refresh ? "?refresh=1" : ""}`);
      } else if (section === "plugins") {
        // Channel settings are picked from the radio's channels by name.
        const [plugins, channels] = await Promise.all([
          api("GET", "api/plugins"),
          ui.data.channels ? Promise.resolve(ui.data.channels) : api("GET", "api/radio/channels").catch(() => null),
        ]);
        ui.data.plugins = plugins;
        if (channels) ui.data.channels = channels;
      } else if (section === "config") {
        // It holds secrets, so the server only hands it out where settings can be changed.
        ui.data.config = canChange() ? await api("GET", "api/config") : { locked: true };
      }
      ui.errors[section] = null;
    } catch (err) {
      ui.errors[section] = err.message;
    } finally {
      ui.loading[section] = false;
      render(section);
    }
  }

  function render(section) {
    const panel = $(`cpanel-${section}`);
    if (!panel) return;
    // Swapping the content would otherwise clamp the scroll position while the panel is empty.
    const view = $("view-control");
    const scrollTop = view.scrollTop;
    if (!ui.data[section]) panel.replaceChildren(placeholder(section));
    else ({ radio: renderRadio, channels: renderChannels, contacts: renderContacts, plugins: renderPlugins, config: renderConfig })[section](panel);
    view.scrollTop = scrollTop;
    if (section === "plugins") revealLinkedPlugin(panel);
  }

  // Something changed (here or in another tab): reload what's on screen, mark the rest.
  function invalidate(sections) {
    for (const section of sections) {
      ui.stale.add(section);
      if (currentView === "control" && ui.section === section && !panelDirty(section)) load(section);
    }
  }

  function renderReadOnly() {
    $("control-readonly").hidden = canChange();
    $("control-readonly-text").textContent = (controlState && controlState.read_only_reason) || "";
  }

  // --- Radio ------------------------------------------------------------------------------

  function renderRadio(panel) {
    const data = ui.data.radio;
    if (!data.connected) {
      panel.replaceChildren(radioOffline());
      return;
    }
    panel.replaceChildren(statusCard(data), identityCard(data), radioParamsCard(data), behaviourCard(data),
      data.tuning ? tuningCard(data) : null);
  }

  function statusCard(data) {
    const { identity, device, radio, battery, stats, clock } = data;
    const core = stats.core || {};
    const rf = stats.radio || {};
    const packets = stats.packets || {};
    const fact = (label, value, sub) => el("div", {}, el("dt", {}, label), el("dd", {}, value, sub ? el("span", { class: "sub" }, sub) : null));
    const batteryMv = battery ? battery.level : core.battery_mv;
    const facts = el("dl", { class: "facts" },
      fact("Name", identity.name || dash, nodeTypeLabel(identity.type)),
      fact("Public key", keyRow(identity.public_key)),
      fact("Device", device.model || dash, device.firmware ? `Firmware ${device.firmware}${device.build ? ` (${device.build})` : ""}` : null),
      fact("Battery", Number.isFinite(batteryMv) ? `${(batteryMv / 1000).toFixed(2)} V` : dash,
        battery && Number.isFinite(battery.total_kb) && battery.total_kb > 0 ? `Storage ${fmtNumber(battery.used_kb)} of ${fmtNumber(battery.total_kb)} KB used` : null),
      fact("Uptime", fmtDuration(core.uptime_secs), stats.core ? `${plural(core.queue_len, "message", "messages")} queued` : null),
      fact("Radio", radio.freq ? `${fmtNumber(radio.freq, 3)} MHz` : dash,
        radio.freq ? `BW ${radio.bw} kHz · SF ${radio.sf} · CR 4/${radio.cr} · ${radio.tx_power} dBm` : null),
      fact("Noise floor", Number.isFinite(rf.noise_floor) ? `${rf.noise_floor} dBm` : dash,
        Number.isFinite(rf.last_rssi) ? `Last packet ${rf.last_rssi} dBm, SNR ${fmtNumber(rf.last_snr, 1)} dB` : null),
      fact("Airtime", stats.radio ? `${fmtDuration(rf.tx_air_secs)} sending` : dash, stats.radio ? `${fmtDuration(rf.rx_air_secs)} receiving` : null),
      fact("Packets", stats.packets ? `${fmtNumber(packets.recv)} in · ${fmtNumber(packets.sent)} out` : dash,
        stats.packets ? `Flood ${fmtNumber(packets.flood_rx)}/${fmtNumber(packets.flood_tx)} · direct ${fmtNumber(packets.direct_rx)}/${fmtNumber(packets.direct_tx)}` : null),
      fact("Clock", clock ? fmtDrift(clock.drift_seconds) : dash));

    const advert = button("Send advert", { iconName: "broadcast", title: "Announce this radio to its direct neighbours" });
    const flood = button("Flood advert", { iconName: "broadcast", title: "Announce this radio across the whole mesh" });
    const sync = button("Sync clock", { iconName: "clock", title: "Set the radio's clock to this computer's" });
    const reboot = button("Reboot", { iconName: "power", kind: "btn-danger" });
    for (const node of [advert, flood, sync, reboot]) node.dataset.change = "";
    advert.onclick = () => act(advert, () => api("POST", "api/radio/advert", { flood: false }), "Advert sent");
    flood.onclick = () => act(flood, () => api("POST", "api/radio/advert", { flood: true }), "Flood advert sent");
    sync.onclick = () => act(sync, async () => { await api("POST", "api/radio/sync-clock"); await load("radio"); }, "Clock set");
    reboot.onclick = async () => {
      const ok = await confirmDialog({
        title: "Reboot the radio?",
        text: "It's offline for a few seconds; Meshgram reconnects by itself.",
        ok: "Reboot",
        danger: true,
      });
      if (ok) act(reboot, () => api("POST", "api/radio/reboot"), "Rebooting the radio…");
    };
    const section = card({
      title: "Status",
      actions: [button("Refresh", { iconName: "refresh", onClick: (event) => act(event.currentTarget, () => load("radio")) })],
      body: [facts],
      foot: el("div", { class: "card-foot" }, advert, flood, sync, el("span", { class: "spacer" }), reboot),
    });
    disableIfReadOnly(section);
    return section;
  }

  async function patchRadio(changes, message = "Saved") {
    if (!Object.keys(changes).length) return "Nothing changed";
    ui.data.radio = await api("PATCH", "api/radio", changes);
    render("radio");
    return message;
  }

  function identityCard(data) {
    const { identity, settings } = data;
    const name = el("input", { type: "text", value: identity.name || "", maxlength: 31, autocomplete: "off", required: true });
    const lat = numberInput(identity.lat, { min: -90, max: 90, placeholder: "45.5017" });
    const lon = numberInput(identity.lon, { min: -180, max: 180, placeholder: "-73.5673" });
    const share = switchField("Share the position in adverts", settings.adv_loc_policy === 1,
      { hint: "Other nodes then show this radio on their maps." });
    return formCard({
      title: "Identity and position",
      fields: [
        field("Name", name, { hint: "How this radio shows up to others (up to 31 bytes)." }),
        field("Latitude", lat),
        field("Longitude", lon),
        el("div", { class: "wide" }, share.row),
      ],
      onSubmit: () => {
        const changes = {};
        const newName = name.value.trim();
        if (!newName) throw new Error("The name can't be empty");
        if (utf8Bytes(newName) > 31) throw new Error("The name is too long (31 bytes at most)");
        if (newName !== identity.name) changes.name = newName;
        const newLat = lat.value === "" ? 0 : Number(lat.value);
        const newLon = lon.value === "" ? 0 : Number(lon.value);
        if (newLat !== (identity.lat || 0) || newLon !== (identity.lon || 0)) Object.assign(changes, { lat: newLat, lon: newLon });
        const policy = share.input.checked ? 1 : 0;
        if (policy !== settings.adv_loc_policy) changes.adv_loc_policy = policy;
        return patchRadio(changes);
      },
    });
  }

  function radioParamsCard(data) {
    const { radio } = data;
    const freq = numberInput(radio.freq, { min: 137, max: 2500, step: 0.001 });
    const bw = selectInput(BANDWIDTHS.map((b) => [b, `${b} kHz`]), BANDWIDTHS.find((b) => Math.abs(b - radio.bw) < 0.05));
    const sf = selectInput([5, 6, 7, 8, 9, 10, 11, 12].map((v) => [v, `SF${v}`]), radio.sf);
    const cr = selectInput([5, 6, 7, 8].map((v) => [v, `4/${v}`]), radio.cr);
    const maxPower = Number.isFinite(radio.max_tx_power) ? radio.max_tx_power : 30;
    const power = numberInput(radio.tx_power, { min: 1, max: maxPower, step: 1 });
    return formCard({
      title: "Radio",
      note: notice("warn", "Every radio in a mesh uses the same frequency, bandwidth, spreading factor and coding rate. Changing them cuts this radio off until the others match; some firmware applies them after a reboot."),
      fields: [
        field("Frequency (MHz)", freq),
        field("Bandwidth", bw),
        field("Spreading factor", sf),
        field("Coding rate", cr),
        field("TX power (dBm)", power, { hint: `At most ${maxPower} dBm on this radio.` }),
      ],
      onSubmit: async () => {
        const changes = {};
        const params = { freq: Number(freq.value), bw: Number(bw.value), sf: Number(sf.value), cr: Number(cr.value) };
        const paramsChanged = Math.abs(params.freq - radio.freq) > 1e-6 || Math.abs(params.bw - radio.bw) > 0.05
          || params.sf !== radio.sf || params.cr !== radio.cr;
        if (paramsChanged) {
          const ok = await confirmDialog({
            title: "Change the radio parameters?",
            text: `${params.freq} MHz, ${params.bw} kHz, SF${params.sf}, CR 4/${params.cr}. Radios on other settings can't hear this one any more.`,
            ok: "Change them",
            danger: true,
          });
          if (!ok) throw new Cancelled();
          changes.radio = params;
        }
        if (power.value !== "" && Number(power.value) !== radio.tx_power) changes.tx_power = Number(power.value);
        return patchRadio(changes);
      },
    });
  }

  function behaviourCard(data) {
    const { settings } = data;
    const autoAdd = switchField("Add contacts automatically", settings.manual_add_contacts === false,
      { hint: "Off: new nodes have to be added by hand in a MeshCore app." });
    const multiAcks = switchField("Send extra ACKs", settings.multi_acks === 1,
      { hint: "Repeats acknowledgements so senders hear them on lossy links." });
    const telemetry = ["base", "loc", "env"].map((part) => [part, selectInput(TELEMETRY, settings[`telemetry_mode_${part}`])]);
    const pathHash = settings.path_hash_mode === null || settings.path_hash_mode === undefined
      ? null : selectInput(PATH_HASH_SIZES, settings.path_hash_mode);
    const labels = { base: "Battery and status telemetry", loc: "Location telemetry", env: "Sensor telemetry" };
    return formCard({
      title: "Behaviour",
      fields: [
        el("div", { class: "wide" }, autoAdd.row),
        el("div", { class: "wide" }, multiAcks.row),
        ...telemetry.map(([part, input]) => field(labels[part], input, { hint: "Who may request it." })),
        pathHash ? field("Path hash size", pathHash, { hint: "Bytes per repeater in packet paths. Larger avoids ambiguous routes; every repeater needs firmware that supports it." }) : null,
      ],
      onSubmit: () => {
        const changes = {};
        if (autoAdd.input.checked === settings.manual_add_contacts) changes.manual_add_contacts = !autoAdd.input.checked;
        const acks = multiAcks.input.checked ? 1 : 0;
        if (acks !== settings.multi_acks) changes.multi_acks = acks;
        for (const [part, input] of telemetry) {
          if (Number(input.value) !== settings[`telemetry_mode_${part}`]) changes[`telemetry_mode_${part}`] = Number(input.value);
        }
        if (pathHash && Number(pathHash.value) !== settings.path_hash_mode) changes.path_hash_mode = Number(pathHash.value);
        return patchRadio(changes);
      },
    });
  }

  function tuningCard(data) {
    const { tuning } = data;
    const rxDelay = numberInput(tuning.rx_delay, { min: 0, max: 1000, step: 0.001 });
    const airtime = numberInput(tuning.airtime_factor, { min: 0, max: 100, step: 0.001 });
    return formCard({
      title: "Tuning",
      sub: "Advanced",
      fields: [
        field("RX delay base", rxDelay, { hint: "How long the radio waits before repeating, scaled by signal strength. 0 turns it off." }),
        field("Airtime factor", airtime, { hint: "Airtime budget: 1 means it may transmit as much as it listens." }),
      ],
      onSubmit: () => {
        const next = { rx_delay: Number(rxDelay.value), airtime_factor: Number(airtime.value) };
        if (next.rx_delay === tuning.rx_delay && next.airtime_factor === tuning.airtime_factor) return "Nothing changed";
        return patchRadio({ tuning: next });
      },
    });
  }

  // --- Channels -----------------------------------------------------------------------------

  function renderChannels(panel) {
    const data = ui.data.channels;
    const used = data.channels.length;
    const list = card({
      title: "Channels",
      sub: `${used} of ${data.max_channels} slots used`,
      actions: [button("Re-read from the radio", { iconName: "refresh", onClick: (event) => act(event.currentTarget, () => load("channels", { refresh: true })) })],
      body: [
        data.connected ? null : notice("warn", "The radio isn't connected; these are the channels last read from it."),
        used ? channelTable(data) : el("p", { class: "hint" }, "No channels yet. Add one below."),
      ],
    });
    panel.replaceChildren(list, addChannelCard(data), used ? composerCard(data) : null);
  }

  function channelTable(data) {
    const rows = data.channels.map((channel) => (ui.editingChannel === channel.index ? editChannelRow(channel) : channelRow(channel)));
    return el("div", { class: "table-scroll" },
      el("table", { class: "data-table" },
        el("caption", { class: "sr-only" }, "Channels on the radio"),
        el("thead", {}, el("tr", {},
          el("th", { scope: "col", class: "num" }, "Slot"),
          el("th", { scope: "col" }, "Channel"),
          el("th", { scope: "col" }, "Type"),
          el("th", { scope: "col" }, "Hash"),
          el("th", { scope: "col" }, "Used by"),
          el("th", { scope: "col", class: "actions" }, el("span", { class: "sr-only" }, "Actions")))),
        el("tbody", {}, ...rows)));
  }

  function channelRow(channel) {
    const actions = [];
    if (channel.secret) {
      actions.push(button("Copy key", { iconName: "copy", hideLabel: true, title: "Copy the channel key (to share it)", onClick: () => copyText(channel.secret, "Channel key") }));
    }
    const edit = button("Rename", { iconName: "edit", hideLabel: true, onClick: () => { ui.editingChannel = channel.index; render("channels"); } });
    const remove = button("Remove", { iconName: "trash", hideLabel: true, kind: "btn-danger" });
    edit.dataset.change = "";
    remove.dataset.change = "";
    remove.onclick = async () => {
      const users = channel.used_by.length ? `${channel.used_by.join(", ")} ${channel.used_by.length === 1 ? "uses" : "use"} it. ` : "";
      const back = channel.kind === "private" ? "Copy its key first if you want to add it back later." : "You can add it back any time.";
      const ok = await confirmDialog({
        title: `Remove ${channel.name}?`,
        text: `${users}The radio stops sending and receiving on slot ${channel.index}. ${back}`,
        ok: "Remove",
        danger: true,
      });
      if (!ok) return;
      await act(remove, async () => {
        await api("DELETE", `api/radio/channels/${channel.index}`);
        await load("channels");
      }, `${channel.name} removed`);
    };
    actions.push(edit, remove);
    const row = el("tr", {},
      el("td", { class: "num" }, channel.index),
      el("td", { class: "name" }, channel.name),
      el("td", {}, el("span", { class: `kind ${channel.kind}` }, KIND_LABELS[channel.kind] || channel.kind)),
      el("td", { class: "mono" }, channel.hash),
      el("td", { class: channel.used_by.length ? "" : "muted" }, channel.used_by.length ? channel.used_by.join(", ") : "—"),
      el("td", { class: "actions" }, ...actions));
    disableIfReadOnly(row);
    return row;
  }

  function editChannelRow(channel) {
    const name = el("input", { class: "input", type: "text", value: channel.name, maxlength: 31, "aria-label": "Channel name" });
    const key = channel.kind === "private"
      ? el("input", { class: "input mono", type: "text", placeholder: "Key: leave empty to keep it", maxlength: 32, spellcheck: "false", autocomplete: "off", "aria-label": "New channel key" })
      : null;
    const save = el("button", { class: "btn btn-sm btn-primary", type: "submit" }, "Save");
    const cancel = el("button", { class: "btn btn-sm", type: "button", onclick: () => { ui.editingChannel = null; render("channels"); } }, "Cancel");
    const form = el("form", { class: "input-group" }, name, key, save, cancel);
    form.addEventListener("input", () => { form.dataset.dirty = "1"; });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const body = { name: name.value.trim() };
      if (key && key.value.trim()) body.secret = key.value.trim();
      const result = await act(save, () => api("PUT", `api/radio/channels/${channel.index}`, body), "Channel saved");
      if (result) {
        ui.editingChannel = null;
        await load("channels");
      }
    });
    form.addEventListener("keydown", (event) => { if (event.key === "Escape") cancel.click(); });
    setTimeout(() => name.focus(), 0);
    return el("tr", {}, el("td", { class: "num" }, channel.index), el("td", { colspan: "5" }, form));
  }

  function addChannelCard(data) {
    const usedSlots = new Set(data.channels.map((channel) => channel.index));
    const freeSlots = [];
    for (let slot = 0; slot < data.max_channels; slot++) if (!usedSlots.has(slot)) freeSlots.push(slot);
    const hasPublic = data.channels.some((channel) => channel.kind === "public");
    const group = `kind-${++uid}`;
    const kinds = [["hashtag", "Hashtag channel"], ["private", "Private channel"], ["public", "Public channel"]];
    const kindInputs = kinds.map(([value, label]) => {
      const input = el("input", { type: "radio", name: group, value, checked: value === "hashtag" || null, disabled: value === "public" && hasPublic ? true : null });
      return { input, chip: el("label", { class: "check-chip" }, input, label) };
    });
    const hashtag = el("input", { type: "text", placeholder: "local", autocomplete: "off", spellcheck: "false", maxlength: 30 });
    const hashtagField = field("Hashtag", el("div", { class: "prefix-input" }, el("span", { "aria-hidden": "true" }, "#"), hashtag),
      { labelFor: hashtag, hint: "Anyone who adds the same hashtag joins: the key comes from the name. Names are case-sensitive; most apps use lowercase." });
    const name = el("input", { type: "text", placeholder: "Friends", autocomplete: "off", maxlength: 31 });
    const key = el("input", { type: "text", class: "mono", placeholder: "Leave empty for a new random key", maxlength: 32, spellcheck: "false", autocomplete: "off" });
    const privateFields = [
      field("Name", name),
      field("Key", key, { hint: "32 hex characters, from whoever shared the channel. Empty: a new channel with a random key — share it from the list afterwards." }),
    ];
    const publicNote = el("p", { class: "hint wide" }, hasPublic
      ? "The radio already has the Public channel."
      : "The channel every MeshCore radio starts with; its key is well known.");
    const slot = selectInput([["", freeSlots.length ? `First free (slot ${freeSlots[0]})` : "No free slot"], ...freeSlots.map((s) => [s, `Slot ${s}`])], "");
    const full = !freeSlots.length;

    const show = () => {
      const kind = kindInputs.find(({ input }) => input.checked).input.value;
      hashtagField.hidden = kind !== "hashtag";
      privateFields.forEach((node) => { node.hidden = kind !== "private"; });
      publicNote.hidden = kind !== "public";
    };
    kindInputs.forEach(({ input }) => input.addEventListener("change", show));

    const form = formCard({
      title: "Add a channel",
      sub: full ? `All ${data.max_channels} slots are in use` : null,
      submitLabel: "Add channel",
      fields: [
        el("div", { class: "field wide", role: "radiogroup", "aria-label": "Channel type" }, el("div", { class: "check-list" }, ...kindInputs.map(({ chip }) => chip))),
        hashtagField,
        ...privateFields,
        publicNote,
        field("Slot", slot),
      ],
      onSubmit: async (formNode) => {
        const kind = kindInputs.find(({ input }) => input.checked).input.value;
        const body = {};
        if (kind === "hashtag") {
          const tag = hashtag.value.trim().replace(/^#/, "");
          if (!tag) throw new Error("Type the hashtag first");
          body.name = `#${tag}`;
        } else if (kind === "private") {
          if (!name.value.trim()) throw new Error("Give the channel a name");
          body.name = name.value.trim();
          if (key.value.trim()) body.secret = key.value.trim();
        } else {
          body.name = "Public";
        }
        if (slot.value !== "") body.index = Number(slot.value);
        const channel = await api("POST", "api/radio/channels", body);
        formNode.reset();
        await load("channels");
        invalidate(["plugins"]);
        return `${channel.name} added on slot ${channel.index}`;
      },
    });
    show();
    if (full) form.querySelectorAll("input, select, button").forEach((node) => { node.disabled = true; });
    return form;
  }

  function composerCard(data) {
    const remembered = store.get("composerChannel", data.channels[0].index);
    const initial = data.channels.some((c) => c.index === remembered) ? remembered : data.channels[0].index;
    const channel = selectInput(data.channels.map((c) => [c.index, c.name]), initial, { "aria-label": "Channel" });
    const text = el("textarea", { rows: 2, autocomplete: "off", "aria-label": "Message", "aria-keyshortcuts": "Enter" });
    const counter = el("span", { class: "counter", "aria-live": "polite" });
    const max = data.message_max_bytes;
    const channelName = () => channel.selectedOptions[0].textContent;
    const update = () => {
      const bytes = utf8Bytes(text.value.trim());
      counter.textContent = `${bytes} / ${max} bytes`;
      counter.dataset.level = bytes > max ? "over" : bytes >= max * 0.9 ? "near" : "";
      text.placeholder = `Message ${channelName()}`;
      // Grow with the text, up to the max-height in the stylesheet.
      text.style.height = "auto";
      text.style.height = `${text.scrollHeight}px`;
    };
    const send = el("button", { class: "btn btn-sm btn-primary", type: "submit", "data-change": "", html: `${icon("send")}<span>Send</span>` });
    const form = el("form", { class: "card" },
      el("div", { class: "card-head" }, el("h3", {}, "Send a message"), el("span", { class: "sub" }, "From this radio, not relayed to Telegram")),
      el("div", { class: "card-body" },
        el("div", { class: "composer" },
          text,
          el("div", { class: "composer-bar" },
            el("label", { class: "composer-to" }, el("span", { html: icon("radio") }), channel, el("span", { html: icon("chevron") })),
            el("span", { class: "composer-send" }, counter, send))),
        el("p", { class: "hint composer-hint" }, "Enter sends, Shift+Enter starts a new line.")));
    text.addEventListener("input", () => {
      update();
      // Don't wipe a half-written message when the channel list reloads.
      if (text.value) form.dataset.dirty = "1";
      else delete form.dataset.dirty;
    });
    text.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
    channel.addEventListener("change", () => {
      store.set("composerChannel", Number(channel.value));
      update();
      text.focus();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!text.value.trim() || send.disabled) return;
      const name = channelName();
      const sent = await act(send, () => api("POST", `api/radio/channels/${channel.value}/messages`, { text: text.value }), `Sent to ${name}`);
      if (sent !== undefined) {
        text.value = "";
        delete form.dataset.dirty;
        update();
        text.focus();
      }
    });
    disableIfReadOnly(form);
    requestAnimationFrame(update);
    return form;
  }

  // --- Contacts -------------------------------------------------------------------------------

  function renderContacts(panel) {
    const data = ui.data.contacts;
    const search = el("input", { type: "search", placeholder: "Search name or key…", "aria-label": "Search contacts", value: ui.contactQuery, autocomplete: "off" });
    const types = ["all", ...Object.keys(NODE_TYPES).filter((type) => type !== "unknown")];
    const type = selectInput(types.map((t) => [t, t === "all" ? "All types" : NODE_TYPES[t].plural]), ui.contactType, { "aria-label": "Contact type" });
    const body = el("div", {});
    const draw = () => body.replaceChildren(contactTable(data));
    search.addEventListener("input", () => { ui.contactQuery = search.value; draw(); });
    type.addEventListener("change", () => { ui.contactType = type.value; draw(); });
    draw();
    panel.replaceChildren(card({
      title: "Contacts",
      sub: `${plural(data.contacts.length, "contact", "contacts")} on the radio`,
      actions: [button("Re-read from the radio", { iconName: "refresh", onClick: (event) => act(event.currentTarget, () => load("contacts", { refresh: true })) })],
      body: [
        data.connected ? null : notice("warn", "The radio isn't connected; these are the contacts last read from it."),
        el("div", { class: "messages-tools", style: "max-width:none;margin-bottom:12px" },
          el("div", { class: "search" }, el("span", { html: icon("search") }), search),
          el("label", { class: "sort-select", style: "display:inline-flex" }, el("span", { class: "sr-only" }, "Type"), type)),
        body,
      ],
    }));
  }

  function routeText(pathLen) {
    if (pathLen === null || pathLen === undefined) return dash;
    if (pathLen < 0) return "Flood (no route yet)";
    if (pathLen === 0) return "Direct";
    return plural(pathLen, "hop", "hops");
  }

  function contactTable(data) {
    const query = ui.contactQuery.trim().toLowerCase();
    const matches = data.contacts.filter((contact) =>
      (ui.contactType === "all" || contact.type === ui.contactType)
      && (!query || (contact.name || "").toLowerCase().includes(query) || contact.public_key.includes(query)));
    if (!matches.length) {
      return el("p", { class: "hint" }, data.contacts.length ? "No contacts match." : "The radio has no contacts yet. They appear as it hears adverts.");
    }
    const rows = matches.slice(0, MAX_CONTACT_ROWS).map((contact) => {
      const label = contact.name || shortId(contact.public_key);
      const reset = button("Reset route", { iconName: "refresh", hideLabel: true, title: "Forget the route; the next message is flooded to find a new one" });
      const remove = button("Remove", { iconName: "trash", hideLabel: true, kind: "btn-danger" });
      reset.dataset.change = "";
      remove.dataset.change = "";
      const key = encodeURIComponent(contact.public_key);
      reset.onclick = () => act(reset, async () => { await api("POST", `api/radio/contacts/${key}/reset-path`); await load("contacts"); }, `Route to ${label} reset`);
      remove.onclick = async () => {
        const ok = await confirmDialog({
          title: `Remove ${label}?`,
          text: "The radio forgets this contact. It comes back when the radio next hears its advert, if contacts are added automatically.",
          ok: "Remove",
          danger: true,
        });
        if (ok) act(remove, async () => { await api("DELETE", `api/radio/contacts/${key}`); await load("contacts"); }, `${label} removed`);
      };
      const row = el("tr", {},
        el("td", { class: "name" }, contact.name || el("span", { class: "muted" }, "Unnamed")),
        el("td", {}, nodeTypeLabel(contact.type)),
        el("td", { class: "mono", title: contact.public_key }, shortId(contact.public_key)),
        el("td", { class: "muted" }, contact.last_advert ? fmtAgo(contact.last_advert) : dash),
        el("td", {}, routeText(contact.path_len)),
        el("td", { class: "actions" }, reset, remove));
      disableIfReadOnly(row);
      return row;
    });
    return el("div", {},
      el("div", { class: "table-scroll" },
        el("table", { class: "data-table" },
          el("caption", { class: "sr-only" }, "Contacts on the radio"),
          el("thead", {}, el("tr", {},
            ...["Name", "Type", "Key", "Last advert", "Route"].map((label) => el("th", { scope: "col" }, label)),
            el("th", { scope: "col", class: "actions" }, el("span", { class: "sr-only" }, "Actions")))),
          el("tbody", {}, ...rows))),
      matches.length > MAX_CONTACT_ROWS ? el("p", { class: "hint", style: "margin-top:10px" }, `Showing ${MAX_CONTACT_ROWS} of ${matches.length}; search to narrow it down.`) : null);
  }

  // --- Plugins --------------------------------------------------------------------------------

  function renderPlugins(panel) {
    const plugins = ui.data.plugins.plugins;
    const changed = plugins.some((plugin) => plugin.overridden.length);
    const note = changed
      ? el("div", { class: "notice", role: "note" },
        el("span", { html: icon("info") }),
        el("p", {}, "Changes made here are saved in Meshgram's data directory and win over config.yaml. ",
          el("a", { href: "#control/config" }, "Put them in config.yaml"), " to make them the defaults."))
      : null;
    panel.replaceChildren(el("div", { class: "plugin-list" }, note, ...plugins.map(pluginCard)));
  }

  // After a #control/plugins/<name> link: bring that plugin's settings into view.
  function revealLinkedPlugin(panel) {
    if (!ui.focusPlugin || !ui.data.plugins) return;
    const card = panel.querySelector(`[data-plugin="${CSS.escape(ui.focusPlugin)}"]`);
    ui.focusPlugin = null;
    if (!card) return;
    card.scrollIntoView({ block: "start" });
    (card.querySelector(".plugin-settings input, .plugin-settings select") || card.querySelector(".disclosure")).focus({ preventScroll: true });
  }

  function pluginState(plugin) {
    if (ui.busyPlugins.has(plugin.name)) return ["busy", plugin.enabled ? "Starting…" : "Stopping…"];
    if (plugin.running) return ["running", "Running"];
    if (plugin.enabled && plugin.error) return ["error", "Failed"];
    if (plugin.enabled) return ["error", "Not running"];
    return ["off", "Off"];
  }

  function replacePlugin(updated) {
    const list = ui.data.plugins.plugins;
    const index = list.findIndex((plugin) => plugin.name === updated.name);
    if (index >= 0) list[index] = updated;
  }

  function pluginCard(plugin) {
    const [stateKey, stateLabel] = pluginState(plugin);
    const toggle = el("input", {
      class: "switch", type: "checkbox", role: "switch", checked: plugin.enabled || null,
      "aria-label": `${plugin.title}: ${plugin.enabled ? "on" : "off"}`, "data-change": "",
      disabled: ui.busyPlugins.has(plugin.name) || null,
    });
    toggle.addEventListener("change", async () => {
      ui.busyPlugins.add(plugin.name);
      plugin.enabled = toggle.checked;
      render("plugins");
      try {
        replacePlugin(await api("PATCH", `api/plugins/${encodeURIComponent(plugin.name)}`, { enabled: toggle.checked }));
        toast(`${plugin.title} turned ${toggle.checked ? "on" : "off"}`);
        invalidate(["channels"]);
      } catch (err) {
        plugin.enabled = !toggle.checked;
        toast(err.message, "error");
      } finally {
        ui.busyPlugins.delete(plugin.name);
        render("plugins");
      }
    });

    const open = ui.openPlugins.has(plugin.name);
    const settingsId = `plugin-settings-${plugin.name.replace(/\W/g, "_")}`;
    const disclosure = el("button", { class: "disclosure", type: "button", "aria-expanded": String(open), "aria-controls": settingsId, html: `${icon("chevron")}<span>Settings</span>` });
    disclosure.addEventListener("click", () => {
      if (ui.openPlugins.has(plugin.name)) {
        ui.openPlugins.delete(plugin.name);
        ui.editors.delete(plugin.name);
      } else {
        ui.openPlugins.add(plugin.name);
      }
      render("plugins");
    });

    const changed = plugin.overridden.length
      ? el("span", { class: "tag", title: "Saved by Meshgram in its data directory; config.yaml says otherwise" }, "Changed here")
      : null;
    const article = el("article", { class: "card plugin-card", "data-plugin": plugin.name, "aria-labelledby": `${settingsId}-title` },
      el("div", { class: "card-head plugin-head" },
        el("div", { class: "plugin-title" },
          el("h3", { id: `${settingsId}-title` }, plugin.title),
          el("span", { class: "plugin-id" }, plugin.name),
          el("span", { class: "badge-state", "data-state": stateKey }, stateLabel),
          changed),
        toggle),
      plugin.description ? el("p", { class: "desc" }, plugin.description) : null,
      plugin.error ? notice("bad", plugin.error) : null,
      disclosure,
      open ? pluginSettings(plugin, settingsId) : null);
    disableIfReadOnly(article);
    return article;
  }

  function pluginSettings(plugin, id) {
    let editor = ui.editors.get(plugin.name);
    if (!editor || editor.plugin !== plugin) {
      // Keep unsaved edits when the list re-renders for another plugin.
      const previous = editor && editor.form.dataset.dirty ? editor : null;
      editor = previous || buildPluginEditor(plugin);
      ui.editors.set(plugin.name, editor);
    }
    editor.form.id = id;
    return editor.form;
  }

  function buildPluginEditor(plugin) {
    const ctx = { fields: [] };
    const schema = plugin.schema || { type: "object" };
    const hasForm = schema.properties && Object.keys(schema.properties).length;
    const root = hasForm ? objectEditor(schema, plugin.settings || {}, "", ctx, { top: true }) : jsonEditor(plugin.settings || {}, "", ctx, "Settings (JSON)");
    const errors = el("ul", { class: "form-errors", role: "alert", hidden: true });
    const save = el("button", { class: "btn btn-sm btn-primary", type: "submit", "data-change": "" }, "Save");
    const cancel = el("button", { class: "btn btn-sm", type: "button" }, "Discard changes");
    const reset = plugin.overridden.includes("settings")
      ? el("button", { class: "btn btn-sm btn-danger", type: "button", "data-change": "" }, "Reset to config.yaml") : null;
    const form = el("form", { class: "plugin-settings", novalidate: true },
      errors,
      root.el,
      el("div", { class: "input-group", style: "flex-wrap:wrap" }, save, cancel, el("span", { class: "spacer", style: "flex:1" }), reset));
    const editor = { plugin, form };

    form.addEventListener("input", () => { form.dataset.dirty = "1"; });
    form.addEventListener("change", () => { form.dataset.dirty = "1"; });
    cancel.addEventListener("click", () => {
      ui.editors.delete(plugin.name);
      render("plugins");
    });
    if (reset) {
      reset.addEventListener("click", async () => {
        const ok = await confirmDialog({
          title: `Reset ${plugin.title}?`,
          text: "Its settings and on/off state go back to what config.yaml says, and it restarts.",
          ok: "Reset",
          danger: true,
        });
        if (!ok) return;
        const updated = await act(reset, () => api("DELETE", `api/plugins/${encodeURIComponent(plugin.name)}/overrides`), `${plugin.title} reset to config.yaml`);
        if (updated) {
          replacePlugin(updated);
          ui.editors.delete(plugin.name);
          render("plugins");
        }
      });
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearErrors(ctx, errors);
      let settings;
      try {
        settings = root.get() || {};
      } catch (err) {
        showErrors(ctx, errors, [{ path: err.path || "", message: err.message }]);
        return;
      }
      save.disabled = true;
      try {
        const updated = await api("PATCH", `api/plugins/${encodeURIComponent(plugin.name)}`, { settings });
        replacePlugin(updated);
        ui.editors.delete(plugin.name);
        render("plugins");
        invalidate(["channels"]);
        toast(updated.restarted ? `${plugin.title} saved and restarted` : `${plugin.title} saved`);
      } catch (err) {
        if (err.details) showErrors(ctx, errors, err.details);
        else toast(err.message, "error");
      } finally {
        if (save.isConnected) save.disabled = !canChange();
      }
    });
    disableIfReadOnly(form);
    return editor;
  }

  function clearErrors(ctx, list) {
    list.hidden = true;
    list.replaceChildren();
    for (const entry of ctx.fields) {
      delete entry.node.dataset.invalid;
      if (entry.error) entry.error.remove();
      entry.error = null;
    }
  }

  function showErrors(ctx, list, details) {
    const general = [];
    for (const { path, message } of details) {
      // The field for the path, or the closest one above it.
      const entry = ctx.fields.filter((f) => path === f.path || path.startsWith(`${f.path}.`))
        .sort((a, b) => b.path.length - a.path.length)[0];
      const fieldName = path.split(".").pop();
      if (entry) {
        entry.node.dataset.invalid = "";
        const text = entry.path === path ? message : `${fieldName}: ${message}`;
        entry.error = el("p", { class: "field-error" }, text);
        entry.node.append(entry.error);
      } else {
        general.push(path ? `${path}: ${message}` : message);
      }
    }
    if (general.length) {
      list.replaceChildren(...general.map((text) => el("li", {}, text)));
      list.hidden = false;
    }
    const first = ctx.fields.find((f) => "invalid" in f.node.dataset);
    if (first) first.node.querySelector("input, select, textarea")?.focus();
    toast("Some settings need fixing", "error");
  }

  // --- Settings editors (from a plugin's JSON Schema) -----------------------------------------
  // Each editor is { el, get() } where get() returns the value, or undefined to leave it out.

  class FieldError extends Error {
    constructor(path, message) { super(message); this.path = path; }
  }

  const join = (path, key) => (path ? `${path}.${key}` : String(key));
  const types = (schema) => [].concat(schema.type || []);
  const isChannel = (schema) => schema && schema.format === "channel";
  const labelOf = (schema, key) => schema.title || String(key).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

  function channelOptions() {
    const channels = (ui.data.channels && ui.data.channels.channels) || [];
    return channels.map((channel) => [channel.index, `${channel.name} (slot ${channel.index})`]);
  }

  // Values in config.yaml can be looser than the schema ("0,1" for a list, "8" for a number).
  function coerce(value, schema) {
    if (value === null || value === undefined) return undefined;
    const t = types(schema);
    if (t.includes("array") && typeof value === "string") {
      value = value.split(",").map((part) => part.trim()).filter(Boolean);
    } else if (t.includes("array") && !Array.isArray(value)) {
      value = [value];
    }
    if (Array.isArray(value) && schema.items) return value.map((item) => coerce(item, schema.items)).filter((item) => item !== undefined);
    if ((t.includes("integer") || t.includes("number")) && typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) return Number(value);
    if (t.includes("boolean") && typeof value === "string") return ["1", "true", "yes", "on"].includes(value.trim().toLowerCase());
    return value;
  }

  function register(ctx, path, node) {
    ctx.fields.push({ path, node, error: null });
    return node;
  }

  function editorFor(schema, value, path, ctx, key, required) {
    value = coerce(value, schema);
    const t = types(schema);
    if (isChannel(schema)) return channelEditor(schema, value, path, ctx, key, required);
    if (t.includes("boolean")) return booleanEditor(schema, value, path, ctx, key);
    if (schema.enum) return enumEditor(schema, value, path, ctx, key, required);
    if (t.includes("integer") || t.includes("number")) return numberEditor(schema, value, path, ctx, key, required);
    if (t.includes("string")) return stringEditor(schema, value, path, ctx, key, required);
    if (t.includes("array")) {
      if (isChannel(schema.items)) return channelListEditor(schema, value, path, ctx, key);
      if (schema.items && schema.items.enum) return enumListEditor(schema, value, path, ctx, key);
      return listEditor(schema, value, path, ctx, key);
    }
    if (t.includes("object") && schema.properties) return objectEditor(schema, value || {}, path, ctx, { key });
    if (t.includes("object") && schema.additionalProperties && typeof schema.additionalProperties === "object") {
      return mapEditor(schema, value || {}, path, ctx, key);
    }
    return jsonEditor(value, path, ctx, labelOf(schema, key));
  }

  function hintFor(schema) {
    const parts = [];
    if (schema.description) parts.push(schema.description);
    return parts.join(" ") || undefined;
  }

  function booleanEditor(schema, value, path, ctx, key) {
    const initial = value === undefined ? Boolean(schema.default) : value;
    const { row, input } = switchField(labelOf(schema, key), initial, { hint: hintFor(schema) });
    register(ctx, path, row);
    // Left alone at its default: leave it out, so nothing changes needlessly.
    return { el: row, get: () => (value === undefined && input.checked === Boolean(schema.default) ? undefined : input.checked) };
  }

  function enumEditor(schema, value, path, ctx, key, required) {
    const titles = schema["x-enum-titles"] || [];
    const options = schema.enum.map((option, index) => [option, titles[index] || String(option)]);
    if (!required) {
      const fallback = schema.default !== undefined ? `Default (${titles[schema.enum.indexOf(schema.default)] || schema.default})` : "Not set";
      options.unshift(["", fallback]);
    }
    const select = selectInput(options, value === undefined ? "" : value);
    const node = register(ctx, path, field(labelOf(schema, key), select, { hint: hintFor(schema), required }));
    return {
      el: node,
      get: () => {
        if (select.value === "") return undefined;
        const index = schema.enum.findIndex((option) => String(option) === select.value);
        return schema.enum[index];
      },
    };
  }

  function numberEditor(schema, value, path, ctx, key, required) {
    const integer = types(schema).includes("integer");
    const input = numberInput(typeof value === "number" ? value : undefined, {
      min: schema.minimum ?? schema.exclusiveMinimum, max: schema.maximum, step: integer ? 1 : "any",
      placeholder: schema.default !== undefined ? String(schema.default) : null,
    });
    const node = register(ctx, path, field(labelOf(schema, key), input, { hint: hintFor(schema), required }));
    return {
      el: node,
      get: () => {
        if (input.value === "") {
          if (required) throw new FieldError(path, "is required");
          return undefined;
        }
        const number = Number(input.value);
        if (!Number.isFinite(number)) throw new FieldError(path, "must be a number");
        if (integer && !Number.isInteger(number)) throw new FieldError(path, "must be a whole number");
        return number;
      },
    };
  }

  function stringEditor(schema, value, path, ctx, key, required) {
    const secret = Boolean(schema.writeOnly);
    const stored = secret && value === SECRET_MASK;
    let cleared = false;
    const input = el("input", {
      type: secret ? "password" : "text",
      value: stored ? null : value === undefined ? null : String(value),
      autocomplete: secret ? "new-password" : "off",
      spellcheck: "false",
      maxlength: schema.maxLength,
      placeholder: stored ? "Saved; type to replace it" : schema.default !== undefined ? String(schema.default) : null,
    });
    let control = input;
    if (stored) {
      const clear = button("Clear", { onClick: () => { cleared = true; input.value = ""; input.placeholder = "Removed when you save"; clear.disabled = true; input.dispatchEvent(new Event("input", { bubbles: true })); } });
      clear.dataset.change = "";
      control = el("div", { class: "input-group" }, input, clear);
    }
    const node = register(ctx, path, field(labelOf(schema, key), control, { hint: hintFor(schema), required, labelFor: input }));
    return {
      el: node,
      get: () => {
        const text = secret ? input.value : input.value.trim();
        if (text) return text;
        if (stored && !cleared) return SECRET_MASK;
        if (required) throw new FieldError(path, "is required");
        return schema["x-keep-empty"] && value !== undefined ? "" : undefined;
      },
    };
  }

  function channelEditor(schema, value, path, ctx, key, required) {
    const options = channelOptions();
    if (Number.isInteger(value) && !options.some(([index]) => index === value)) options.push([value, `Slot ${value} (no channel there)`]);
    if (!required) options.unshift(["", schema["x-empty-label"] || "Default"]);
    const select = selectInput(options, Number.isInteger(value) ? value : "");
    const node = register(ctx, path, field(labelOf(schema, key), select, { hint: hintFor(schema), required }));
    return { el: node, get: () => (select.value === "" ? undefined : Number(select.value)) };
  }

  function channelListEditor(schema, value, path, ctx, key) {
    const selected = new Set(Array.isArray(value) ? value : []);
    const options = channelOptions();
    for (const index of selected) if (!options.some(([i]) => i === index)) options.push([index, `Slot ${index} (no channel there)`]);
    const inputs = options.map(([index, label]) => {
      const input = el("input", { type: "checkbox", value: String(index), checked: selected.has(index) || null });
      return { input, chip: el("label", { class: "check-chip" }, input, label) };
    });
    const groupId = nextId("cl");
    const node = register(ctx, path, el("div", { class: "field wide" },
      el("span", { class: "label", id: groupId }, labelOf(schema, key)),
      inputs.length ? el("div", { class: "check-list", role: "group", "aria-labelledby": groupId }, ...inputs.map(({ chip }) => chip))
        : el("p", { class: "hint" }, "The radio has no channels to pick from."),
      hintFor(schema) ? el("p", { class: "hint" }, hintFor(schema)) : null));
    return {
      el: node,
      get: () => {
        const picked = inputs.filter(({ input }) => input.checked).map(({ input }) => Number(input.value));
        return picked.length ? picked : undefined;
      },
    };
  }

  // A pick from a fixed set, as ticks. With ``x-inverted`` the setting lists what's left
  // unticked (hidden packet types, say), so new options start out ticked.
  function enumListEditor(schema, value, path, ctx, key) {
    const items = schema.items;
    const titles = items["x-enum-titles"] || [];
    const inverted = Boolean(schema["x-inverted"]);
    const known = (raw) => items.enum.find((option) => String(option).toLowerCase() === String(raw).trim().toLowerCase());
    const listed = new Set((Array.isArray(value) ? value : []).map(known).filter((option) => option !== undefined));
    const inputs = items.enum.map((option, index) => {
      const input = el("input", { type: "checkbox", value: String(option), checked: (inverted ? !listed.has(option) : listed.has(option)) || null });
      return { option, input, chip: el("label", { class: "check-chip" }, input, titles[index] || String(option)) };
    });
    const groupId = nextId("el");
    const node = register(ctx, path, el("div", { class: "field wide" },
      el("span", { class: "label", id: groupId }, labelOf(schema, key)),
      el("div", { class: "check-list", role: "group", "aria-labelledby": groupId }, ...inputs.map(({ chip }) => chip)),
      hintFor(schema) ? el("p", { class: "hint" }, hintFor(schema)) : null));
    const reset = button(inverted ? "Tick all" : "Clear", {
      onClick: () => {
        inputs.forEach(({ input }) => { input.checked = inverted; });
        node.dispatchEvent(new Event("change", { bubbles: true }));
      },
    });
    reset.dataset.change = "";
    node.querySelector(".check-list").append(reset);
    return {
      el: node,
      get: () => {
        const picked = inputs.filter(({ input }) => input.checked !== inverted).map(({ option }) => option);
        return picked.length ? picked : undefined;
      },
    };
  }

  function listEditor(schema, value, path, ctx, key) {
    const items = schema.items || { type: "string" };
    const numeric = types(items).some((t) => t === "integer" || t === "number");
    const input = el("input", { type: "text", value: Array.isArray(value) ? value.join(", ") : null, autocomplete: "off", spellcheck: "false" });
    const hint = [hintFor(schema), "Separate with commas."].filter(Boolean).join(" ");
    const node = register(ctx, path, field(labelOf(schema, key), input, { hint, wide: true }));
    return {
      el: node,
      get: () => {
        const parts = input.value.split(",").map((part) => part.trim()).filter(Boolean);
        if (!parts.length) return undefined;
        if (!numeric) return parts;
        return parts.map((part) => {
          const number = Number(part);
          if (!Number.isFinite(number)) throw new FieldError(path, `“${part}” isn't a number`);
          return number;
        });
      },
    };
  }

  function objectEditor(schema, value, path, ctx, { key, top = false } = {}) {
    const properties = schema.properties || {};
    const required = new Set(schema.required || []);
    const children = Object.entries(properties).map(([name, propertySchema]) =>
      [name, editorFor(propertySchema, value[name], join(path, name), ctx, name, required.has(name))]);
    // Settings the schema doesn't describe are kept as they are.
    const extras = Object.fromEntries(Object.entries(value).filter(([name]) => !(name in properties)));
    const isWide = (s) => types(s).includes("object") || (types(s).includes("array") && (isChannel(s.items) || Boolean(s.items && s.items.enum)));
    for (const [name, child] of children) if (isWide(properties[name])) child.el.classList.add("wide");
    const grid = el("div", { class: "form-grid" }, ...children.map(([, child]) => child.el));
    const node = top ? grid : el("fieldset", { class: "wide" }, el("legend", {}, labelOf(schema, key)), schema.description ? el("p", { class: "hint" }, schema.description) : null, grid);
    if (!top) register(ctx, path, node);
    return {
      el: node,
      get: () => {
        const result = { ...extras };
        for (const [name, child] of children) {
          const childValue = child.get();
          if (childValue === undefined) delete result[name];
          else result[name] = childValue;
        }
        return top || Object.keys(result).length ? result : undefined;
      },
    };
  }

  function mapEditor(schema, value, path, ctx, key) {
    const valueSchema = schema.additionalProperties;
    const nested = types(valueSchema).includes("object");
    const keyTitle = schema["x-key-title"] || "Name";
    const valueTitle = schema["x-value-title"] || "Value";
    const list = el("div", { class: "map-entries" });
    const entries = [];
    const sub = { fields: ctx.fields };

    const addEntry = (entryKey, entryValue) => {
      const keyInput = el("input", { class: "input", type: "text", value: entryKey || null, placeholder: keyTitle, "aria-label": keyTitle, autocomplete: "off", spellcheck: "false" });
      const entry = { keyInput, editor: null, node: null, originalKey: entryKey };
      const valuePath = () => join(path, keyInput.value.trim() || entryKey || "");
      entry.editor = nested
        ? objectEditor(valueSchema, entryValue || {}, join(path, entryKey || "new"), sub, { key: entryKey || "New" })
        : editorFor(valueSchema, entryValue, join(path, entryKey || "new"), sub, valueTitle, true);
      if (!nested) {
        // A compact row: the value editor's own label is redundant next to the key.
        const label = entry.editor.el.querySelector("label");
        if (label) label.classList.add("sr-only");
      }
      const remove = button(`Remove ${keyTitle.toLowerCase()}`, { iconName: "trash", hideLabel: true, kind: "btn-danger" });
      remove.dataset.change = "";
      remove.onclick = () => {
        entries.splice(entries.indexOf(entry), 1);
        entry.node.remove();
        list.dispatchEvent(new Event("change", { bubbles: true }));
      };
      if (nested) {
        const legend = entry.editor.el.querySelector("legend");
        if (legend) legend.remove();
        entry.editor.el.classList.remove("wide");
        entry.node = el("div", { class: "map-entry nested" },
          field(keyTitle, keyInput),
          remove,
          el("div", { class: "entry-body" }, entry.editor.el));
      } else {
        entry.node = el("div", { class: "map-entry" }, keyInput, entry.editor.el, remove);
      }
      entry.valuePath = valuePath;
      entries.push(entry);
      list.append(entry.node);
      disableIfReadOnly(entry.node);
      return entry;
    };

    for (const [entryKey, entryValue] of Object.entries(value || {})) addEntry(entryKey, entryValue);
    const add = button(`Add ${keyTitle.toLowerCase()}`, { iconName: "plus" });
    add.dataset.change = "";
    add.onclick = () => {
      const entry = addEntry("", undefined);
      entry.keyInput.focus();
    };
    const node = register(ctx, path, el("fieldset", { class: "wide" },
      el("legend", {}, labelOf(schema, key)),
      schema.description ? el("p", { class: "hint", style: "margin-bottom:10px" }, schema.description) : null,
      list,
      el("div", { style: "margin-top:10px" }, add)));
    return {
      el: node,
      get: () => {
        const result = {};
        for (const entry of entries) {
          const entryKey = entry.keyInput.value.trim();
          if (!entryKey) {
            // A row left blank is skipped; one with only a value needs its name.
            let blank = true;
            try { blank = entry.editor.get() === undefined; } catch { blank = true; }
            if (blank) continue;
            throw new FieldError(path, `every entry needs a ${keyTitle.toLowerCase()}`);
          }
          const entryValue = entry.editor.get();
          if (entryKey in result) throw new FieldError(path, `“${entryKey}” is listed twice`);
          result[entryKey] = entryValue === undefined ? (nested ? {} : "") : entryValue;
        }
        return Object.keys(result).length ? result : undefined;
      },
    };
  }

  function jsonEditor(value, path, ctx, label) {
    const area = el("textarea", { spellcheck: "false", rows: 8 });
    area.value = JSON.stringify(value === undefined ? {} : value, null, 2);
    const node = register(ctx, path, field(label, area, { wide: true, hint: "JSON. Secrets show as •••••••• and are kept if left that way." }));
    return {
      el: node,
      get: () => {
        if (!area.value.trim()) return undefined;
        try {
          return JSON.parse(area.value);
        } catch (err) {
          throw new FieldError(path, `isn't valid JSON (${err.message})`);
        }
      },
    };
  }

  // --- Config file ----------------------------------------------------------------------------

  function renderConfig(panel) {
    const data = ui.data.config;
    if (data.locked) {
      panel.replaceChildren(card({
        title: "config.yaml",
        body: [notice("warn", "config.yaml holds secrets, so it's only shown where the control panel can change settings: set web.password in config.yaml, or listen on 127.0.0.1 only.")],
      }));
      return;
    }
    const reread = button("Read config.yaml again", { iconName: "refresh", hideLabel: true, onClick: (event) => act(event.currentTarget, () => load("config")) });
    const copy = button("Copy", { iconName: "copy", onClick: () => copyText(data.yaml, "config.yaml") });
    const download = el("a", { class: "btn btn-sm", href: "api/config.yaml", download: "config.yaml", html: `${icon("download")}<span>Download</span>` });
    panel.replaceChildren(card({
      title: "config.yaml",
      sub: "With the plugin changes made in this control panel",
      actions: [reread, copy, download],
      body: [
        configStatus(data),
        data.problems.length
          ? notice("bad", "This file doesn't load back exactly as intended; check it before using it:", [el("ul", { class: "change-list" }, ...data.problems.map((text) => el("li", {}, text)))])
          : null,
        data.changes.length ? configSteps(data) : null,
        configViewer(data),
      ],
    }));
  }

  function configStatus(data) {
    if (data.changes.length) {
      const what = (change) => [change.enabled === null ? null : `turned ${change.enabled ? "on" : "off"}`, change.settings ? "settings changed" : null].filter(Boolean).join(", ");
      return notice("", `${plural(data.changes.length, "plugin differs", "plugins differ")} from config.yaml:`, [
        el("ul", { class: "change-list" }, ...data.changes.map((change) =>
          el("li", {}, el("strong", {}, change.title), el("span", { class: "what" }, ` — ${what(change)}`)))),
      ]);
    }
    if (data.saved.length) {
      const names = data.saved.map((plugin) => plugin.title).join(", ");
      return notice("", `config.yaml already has the changes made here (${names}). Restart Meshgram and they'll stop showing as changed.`);
    }
    return notice("", "Nothing has been changed in the control panel: this is config.yaml as it is.");
  }

  function configSteps(data) {
    return el("ol", { class: "steps" },
      el("li", {}, el("strong", {}, "Copy or download"), " this file. It holds your secrets, such as the Telegram bot token: keep it private."),
      el("li", {}, el("strong", {}, "Replace config.yaml"), " with it: ", el("code", {}, data.path),
        " as Meshgram sees it. With Docker, that's the config.yaml next to docker-compose.yml."),
      el("li", {}, el("strong", {}, "Restart Meshgram."), " The changes then come from config.yaml, and stop showing as changed here."));
  }

  function configViewer(data) {
    let view = ui.configView || (data.diff ? "diff" : null);
    if (view === "diff" && !data.diff) view = null;
    const show =(next) => { ui.configView = next; render("config"); };
    let controls;
    if (data.diff) {
      const seg = (value, label) => el("button", { class: "seg", type: "button", "aria-pressed": String(view === value), onclick: () => show(value) }, label);
      controls = el("div", { class: "view-switch", role: "group", "aria-label": "Show" }, seg("diff", "Changes"), seg("file", "Whole file"));
    } else {
      controls = el("button", { class: "disclosure", type: "button", "aria-expanded": String(view === "file"), style: "margin:0 0 10px",
        html: `${icon("chevron")}<span>Show the file</span>`, onclick: () => show(view === "file" ? "none" : "file") });
    }
    if (view !== "diff" && view !== "file") return el("div", {}, controls);
    const lines = view === "diff" ? diffLines(data.diff) : data.yaml.replace(/\n$/, "").split("\n").map((text) => [text, /^\s*#/.test(text) ? "comment" : ""]);
    return el("div", {},
      controls,
      el("pre", { class: "code-view", tabindex: "0", "aria-label": view === "diff" ? "Changes to config.yaml" : "config.yaml" },
        el("code", {}, ...lines.map(([text, kind]) => el("span", { class: `ln ${kind}`.trim() }, text || " ")))));
  }

  // A unified diff without its file header, as [text, kind] lines.
  function diffLines(diff) {
    return diff.replace(/\n$/, "").split("\n")
      .filter((line) => !line.startsWith("--- ") && !line.startsWith("+++ "))
      .map((line) => {
        if (line.startsWith("@@")) {
          const match = /\+(\d+)/.exec(line);
          return [match ? `Line ${match[1]}` : line, "hunk"];
        }
        return [line, { "+": "add", "-": "del" }[line[0]] || ""];
      });
  }

  // --- Tabs and wiring ------------------------------------------------------------------------

  const tablist = document.querySelector(".control-tabs");
  tablist.addEventListener("click", (event) => {
    const tab = event.target.closest("[role='tab']");
    if (tab) selectSection(tab.dataset.section);
  });
  tablist.addEventListener("keydown", (event) => {
    const index = SECTIONS.indexOf(ui.section);
    const next = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: SECTIONS.length - 1 }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    selectSection(SECTIONS[(next + SECTIONS.length) % SECTIONS.length], { focus: true });
  });

  window.MeshgramControl = {
    // The view became visible.
    show() {
      renderReadOnly();
      const { section, plugin } = parseHash();
      openPluginFromLink(plugin);
      selectSection(section || ui.section);
    },
    // The URL changed while the view is visible (#control/channels, say).
    route() {
      if (currentView !== "control") return;
      const { section, plugin } = parseHash();
      openPluginFromLink(plugin);
      if (section && (section !== ui.section || plugin)) selectSection(section);
    },
    // A fresh snapshot: the page (re)connected, or plugins changed what it shows.
    onSnapshot() {
      renderReadOnly();
      const allows = canChange();
      if (allows !== ui.allows) {
        ui.allows = allows;
        for (const section of SECTIONS) if (ui.data[section] && !panelDirty(section)) render(section);
      }
      invalidate(SECTIONS);
    },
    onConnection(conn) {
      if (conn.key !== "radio" || conn.state === ui.radioState) return;
      ui.radioState = conn.state;
      invalidate(["radio", "channels", "contacts"]);
    },
    // A change made from this or another open page.
    onChange(what) {
      const affected = { radio: ["radio"], channels: ["channels", "plugins"], contacts: ["contacts"], plugins: ["plugins", "channels", "config"] }[what];
      if (affected) invalidate(affected);
    },
  };

  if (currentView === "control") window.MeshgramControl.show();
})();
