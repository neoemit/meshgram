"use strict";
// Control panel: the people seen in a bridge's Telegram group, in the bridge's plugin card (and in
// each further bridge's: "bridge@sos") while it runs. Give a person the name their messages carry
// on the radio (the bridge's `senders` setting): for MeshSOS, the name of their board.
// Talks to GET api/plugins/<bridge>/people (meshgram/plugins/bridge.py) and saves through the
// plugin's settings. Loaded after control.js; uses the page's helpers (el, fmtAgo) and the kit
// control.js hands to plugin panels (api, toast, act, button, notice, …).

(() => {
  if (!window.MeshgramControl || !window.MeshgramControl.registerPluginPanel) return;

  const NAME_MAX = 31;
  const NAME_OK = /^[^<>:\r\n]+$/;
  const RELAY_TEMPLATE = "<{display_name}> {message}";

  // A `senders` key as the bridge reads it: a numeric ID, or a username without "@", lower case.
  const userKey = (key) => {
    const text = String(key).trim();
    return /^\d+$/.test(text) ? text : text.replace(/^@+/, "").toLowerCase();
  };

  function render(container, kit, plugin) {
    const path = `api/plugins/${encodeURIComponent(plugin.name)}`;
    const listId = kit.nextId("bridge-nodes");
    let destroyed = false;

    async function currentPlugin() {
      const { plugins } = await kit.api("GET", "api/plugins");
      const current = plugins.find((item) => item.name === plugin.name);
      if (!current) throw new Error(`${plugin.title} is gone`);
      return current;
    }

    // Write one person's radio name ("" clears it) into the plugin's settings.
    async function saveName(person, name) {
      const current = await currentPlugin();
      const senders = {};
      for (const [key, value] of Object.entries(current.settings.senders || {})) {
        // Their earlier entries (by ID or by username) are replaced by the one by ID.
        const normalized = userKey(key);
        if (normalized === String(person.id)) continue;
        if (person.username && normalized === person.username.toLowerCase()) continue;
        senders[key] = value;
      }
      if (name) senders[String(person.id)] = name;
      const settings = { ...current.settings, senders };
      if (!Object.keys(senders).length) delete settings.senders;
      await kit.api("PATCH", path, { settings });
    }

    function personRow(person) {
      const input = el("input", {
        class: "input", type: "text", value: person.radio_name || null, maxlength: NAME_MAX, list: listId,
        placeholder: "Not mapped", "aria-label": `Radio name for ${person.name}`, autocomplete: "off", spellcheck: "false",
        "data-change": "",
      });
      const save = kit.button("Save", { kind: "btn-primary" });
      save.dataset.change = "";
      save.onclick = async () => {
        const name = input.value.trim().replace(/\s+/g, " ");
        if (name && !NAME_OK.test(name)) {
          kit.toast("A radio name can't contain < > or :", "error");
          input.focus();
          return;
        }
        const done = await kit.act(save, async () => { await saveName(person, name); return true; },
          name ? `${person.name} sends as ${name}` : `${person.name} is no longer mapped`);
        if (done) refresh();
      };
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") { event.preventDefault(); save.click(); }
      });
      return el("tr", {},
        el("td", {}, el("strong", {}, person.name || "Unknown"), person.username ? el("div", { class: "hint" }, `@${person.username}`) : null),
        el("td", {}, el("code", {}, String(person.id))),
        el("td", {}, fmtAgo(person.last_seen)),
        el("td", {}, input),
        el("td", { class: "actions" }, save));
    }

    function show(data, current) {
      if (destroyed) return;
      const relays = current.settings.mode === "sos" || (current.settings.sender_prefix_template || "").includes("<{display_name}>");
      const rows = data.people.map(personRow);
      const body = rows.length
        ? el("div", { class: "table-scroll" },
          el("table", { class: "data-table bridge-people" },
            el("thead", {}, el("tr", {},
              ...["Telegram user", "ID", "Last message", "Radio name"].map((label) => el("th", { scope: "col" }, label)),
              el("th", { scope: "col", class: "actions" }, el("span", { class: "sr-only" }, "Actions")))),
            el("tbody", {}, ...rows)))
        : el("p", { class: "hint" }, "Nobody has written or pressed a button in this bridge's Telegram group since it started. Ask everyone to do so once; they then appear here.");
      const parts = [
        el("p", { class: "hint", style: "margin-bottom:10px" },
          "Give a person the name their messages carry on the radio; for MeshSOS, the name of their board. Several people can share one name."),
        relays ? null : kit.notice("info", `MeshSOS boards only treat a message as that person's when the bridge's mode is MeshSOS, or its sender prefix is ${RELAY_TEMPLATE} (in its settings).`),
        body,
        el("datalist", { id: listId }, ...data.nodes.map((name) => el("option", { value: name }))),
      ];
      container.replaceChildren(...parts.filter(Boolean));
      kit.disableIfReadOnly(container);
    }

    async function refresh() {
      try {
        const [data, current] = await Promise.all([kit.api("GET", `${path}/people`), currentPlugin()]);
        // Don't replace the table under someone typing a name.
        if (container.contains(document.activeElement) && document.activeElement.tagName === "INPUT" && container.childElementCount) return;
        show(data, current);
      } catch (err) {
        if (!destroyed && !container.childElementCount) container.replaceChildren(kit.notice("bad", err.message));
      }
    }

    container.replaceChildren(el("p", { class: "hint" }, "Loading…"));
    refresh();
    return { refresh, destroy() { destroyed = true; } };
  }

  window.MeshgramControl.registerPluginPanel("bridge", { title: "People", render });
})();
