"use strict";
// Control panel: the check_in plugin's participants, shown in its plugin card while it runs.
// Edit a participant (name, node key, weeks, streak), add one, import summaries from another
// check-in database (CSV or JSON) and export them. Talks to meshgram/plugins/check_in_api.py.
// Loaded after control.js; uses the page's helpers (el, icon, esc, fmtAgo, plural, shortId)
// and the kit control.js hands to plugin panels (api, toast, act, button, field, …).

(() => {
  if (!window.MeshgramControl || !window.MeshgramControl.registerPluginPanel) return;

  const BASE = "api/plugins/check_in";
  const GRID_WEEKS = 26;
  const MAX_ROWS = 300;
  const SOURCE_LABELS = { radio: "Radio", observer: "Observer", manual: "Added here", import: "Imported" };

  // Weeks are "YYYY-MM-DD" Mondays in the plugin's time zone; plain date arithmetic keeps them so.
  const addDays = (iso, days) => {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
  };
  const fmtWeek = (iso) => new Date(`${iso}T12:00:00Z`).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
  const fmtShortWeek = (iso) => new Date(`${iso}T12:00:00Z`).toLocaleDateString([], { day: "numeric", month: "short", timeZone: "UTC" });
  const fmtStamp = (ts) => new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  const fmtWhen = (iso) => new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

  // --- Import files --------------------------------------------------------------------------

  // CSV with a header row (comma, semicolon or tab separated; quoted fields may hold separators).
  function parseCsv(text) {
    const firstLine = text.split(/\r?\n/, 1)[0] || "";
    const separator = [",", ";", "\t"].map((sep) => [sep, firstLine.split(sep).length]).sort((a, b) => b[1] - a[1])[0][0];
    const rows = [];
    let row = [];
    let cell = "";
    let quoted = false;
    for (let i = 0; i < text.length; i += 1) {
      const char = text[i];
      if (quoted) {
        if (char === '"' && text[i + 1] === '"') { cell += '"'; i += 1; }
        else if (char === '"') quoted = false;
        else cell += char;
      } else if (char === '"' && cell === "") {
        quoted = true;
      } else if (char === separator) {
        row.push(cell); cell = "";
      } else if (char === "\n" || char === "\r") {
        if (char === "\r" && text[i + 1] === "\n") i += 1;
        row.push(cell); cell = "";
        if (row.some((value) => value.trim() !== "")) rows.push(row);
        row = [];
      } else {
        cell += char;
      }
    }
    row.push(cell);
    if (row.some((value) => value.trim() !== "")) rows.push(row);
    if (rows.length < 2) throw new Error("The CSV file needs a header row and at least one record");
    const header = rows[0].map((name) => name.trim().toLowerCase().replace(/^﻿/, ""));
    return rows.slice(1).map((values) => Object.fromEntries(header.map((name, index) => [name, (values[index] || "").trim()])));
  }

  function parseImport(text, fileName = "") {
    const trimmed = text.trim();
    if (!trimmed) throw new Error("There's nothing to import");
    if (/\.json$/i.test(fileName) || trimmed.startsWith("[") || trimmed.startsWith("{")) {
      let data;
      try { data = JSON.parse(trimmed); } catch (err) { throw new Error(`That isn't valid JSON: ${err.message}`); }
      const records = Array.isArray(data) ? data : data && Array.isArray(data.records) ? data.records : null;
      if (!records) throw new Error('JSON must be a list of records, or {"records": [...]}');
      return records;
    }
    return parseCsv(trimmed);
  }

  // --- The panel -----------------------------------------------------------------------------------

  function render(container, kit) {
    const { api, toast, act, button, field, notice, confirmDialog, keyRow, canChange } = kit;
    const state = { data: null, error: null, query: "", loading: false, again: false };
    const dialogs = [];

    const summary = el("div", { class: "ci-summary", role: "status" });
    const search = el("input", { type: "search", placeholder: "Search name or key…", "aria-label": "Search participants", autocomplete: "off" });
    search.addEventListener("input", () => { state.query = search.value; drawTable(); });
    const addButton = button("Add", { iconName: "plus", onClick: () => openAddDialog() });
    const importButton = button("Import…", { iconName: "file", onClick: () => openImportDialog() });
    addButton.dataset.change = "";
    importButton.dataset.change = "";
    const exportCsv = el("a", { class: "btn btn-sm", href: `${BASE}/export?format=csv`, download: "", html: `${icon("download")}<span>CSV</span>`, title: "Export everyone as CSV (the import format)" });
    const exportJson = el("a", { class: "btn btn-sm", href: `${BASE}/export?format=json`, download: "", html: `${icon("download")}<span>JSON</span>`, title: "Export everyone as JSON (the import format)" });
    const tableBox = el("div", { class: "ci-table" });
    container.append(
      summary,
      el("div", { class: "ci-tools" },
        el("div", { class: "search" }, el("span", { html: icon("search") }), search),
        el("span", { class: "spacer" }),
        addButton, importButton, exportCsv, exportJson),
      tableBox);
    if (!canChange()) [addButton, importButton].forEach((node) => { node.disabled = true; });

    async function refresh() {
      if (state.loading) { state.again = true; return; }
      state.loading = true;
      try {
        state.data = await api("GET", `${BASE}/participants`);
        state.error = null;
      } catch (err) {
        state.error = err.message;
      } finally {
        state.loading = false;
      }
      draw();
      dialogs.forEach((dialog) => dialog.refresh && dialog.refresh());
      if (state.again) { state.again = false; refresh(); }
    }

    function draw() {
      drawSummary();
      drawTable();
    }

    function drawSummary() {
      const data = state.data;
      if (!data) { summary.replaceChildren(); return; }
      const win = data.window;
      const days = data.check_in_days.length ? data.check_in_days.join(", ") : "any day";
      summary.replaceChildren(
        el("span", { class: "ci-pill", "data-open": String(win.open) },
          el("span", { class: "ci-dot", "aria-hidden": "true" }),
          win.open ? `Open now, until ${fmtWhen(win.closes_at)}` : `Next check-in ${fmtWhen(win.opens_at)}`),
        el("span", { class: "ci-pill" }, `Week of ${fmtWeek(data.week)}`),
        el("span", { class: "ci-pill" }, `${plural(data.participants_this_week, "check-in", "check-ins")} this week`),
        el("span", { class: "ci-pill" }, plural(data.participants.length, "participant", "participants")),
        el("span", { class: "hint" }, `“${data.keyword}” on ${days} (${data.timezone})`));
    }

    function drawTable() {
      if (state.error && !state.data) {
        tableBox.replaceChildren(notice("bad", state.error), button("Try again", { iconName: "refresh", onClick: refresh }));
        return;
      }
      if (!state.data) { tableBox.replaceChildren(el("p", { class: "hint" }, "Loading…")); return; }
      const query = state.query.trim().toLowerCase();
      const all = state.data.participants;
      const matches = all.filter((p) => !query || p.name.toLowerCase().includes(query) || (p.node_id || "").toLowerCase().includes(query));
      if (!matches.length) {
        tableBox.replaceChildren(el("p", { class: "hint" }, all.length
          ? "Nobody matches."
          : `Nobody has checked in yet. Check-ins appear here as they're heard; import earlier ones with Import….`));
        return;
      }
      const rows = matches.slice(0, MAX_ROWS).map((p) => {
        const edit = button(`Edit ${p.name}`, { iconName: "edit", hideLabel: true, onClick: () => openEditDialog(p.id) });
        return el("tr", {},
          el("td", { class: "num muted" }, p.rank),
          el("td", { class: "name" }, p.name, p.node_id ? null : el("span", { class: "tag ci-tag", title: "Known by name only: linked to a node once its advert is heard (or link it here)" }, "name only")),
          el("td", { class: "mono", title: p.node_id || "" }, p.node_id ? shortId(p.node_id) : el("span", { class: "muted" }, "—")),
          el("td", { class: "num" }, p.streak ? el("span", { class: "ci-streak" }, `🔥 ${p.streak}`) : el("span", { class: "muted" }, "0")),
          el("td", { class: "num" }, p.best_streak),
          el("td", { class: "num" }, p.total_check_ins),
          el("td", {}, p.checked_in_this_week ? el("span", { class: "ci-this-week" }, el("span", { html: icon("check") }), `#${p.week_rank}`) : el("span", { class: "muted" }, "—")),
          el("td", { class: "muted" }, p.last_check_in ? fmtAgo(p.last_check_in) : "—"),
          el("td", { class: "actions" }, edit));
      });
      tableBox.replaceChildren(
        el("div", { class: "table-scroll" },
          el("table", { class: "data-table" },
            el("caption", { class: "sr-only" }, "Check-in participants"),
            el("thead", {}, el("tr", {},
              el("th", { scope: "col", class: "num" }, "#"),
              ...["Name", "Key"].map((label) => el("th", { scope: "col" }, label)),
              ...["Streak", "Best", "Total"].map((label) => el("th", { scope: "col", class: "num" }, label)),
              el("th", { scope: "col" }, "This week"),
              el("th", { scope: "col" }, "Last check-in"),
              el("th", { scope: "col", class: "actions" }, el("span", { class: "sr-only" }, "Actions")))),
            el("tbody", {}, ...rows))),
        matches.length > MAX_ROWS ? el("p", { class: "hint", style: "margin-top:10px" }, `Showing ${MAX_ROWS} of ${matches.length}; search to narrow it down.`) : null);
    }

    // --- Dialogs ---------------------------------------------------------------------------------

    function openDialog(title, { wide = false } = {}) {
      const heading = el("h2", { id: kit.nextId("ci-title") }, title);
      const body = el("div", { class: "ci-dialog-body" });
      const close = button("Close", { iconName: "x", hideLabel: true, onClick: () => dialog.close() });
      const dialog = el("dialog", { class: `dialog ci-dialog ${wide ? "wide" : ""}`.trim(), "aria-labelledby": heading.id },
        el("div", { class: "ci-dialog-head" }, heading, close), body);
      const entry = { dialog, body, heading, refresh: null };
      dialog.addEventListener("close", () => {
        dialog.remove();
        dialogs.splice(dialogs.indexOf(entry), 1);
      });
      document.body.append(dialog);
      dialogs.push(entry);
      dialog.showModal();
      return entry;
    }

    // A node key picker: type a name or key, pick a node the radio or observers heard advertise.
    function nodePicker(initial) {
      const input = el("input", { type: "text", placeholder: "Search known nodes by name or key…", autocomplete: "off", spellcheck: "false", value: initial || "" });
      const results = el("div", { class: "ci-node-results", role: "listbox" });
      let timer = null;
      let seq = 0;
      input.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
          const query = input.value.trim();
          const mine = ++seq;
          if (query.length < 2 || /^[0-9a-f]{64}$/i.test(query)) { results.replaceChildren(); return; }
          try {
            const { nodes } = await api("GET", `${BASE}/nodes?q=${encodeURIComponent(query)}`);
            if (mine !== seq) return;
            results.replaceChildren(...(nodes.length ? nodes.map((node) => {
              const option = el("button", { type: "button", class: "ci-node-option", role: "option" },
                el("span", { class: "name" }, node.name), el("code", {}, shortId(node.node_id)),
                el("span", { class: "muted" }, fmtAgo(node.seen_at)));
              option.addEventListener("click", () => { input.value = node.node_id; results.replaceChildren(); });
              return option;
            }) : [el("p", { class: "hint" }, "No known node matches.")]));
          } catch (err) {
            results.replaceChildren(el("p", { class: "hint" }, err.message));
          }
        }, 250);
      });
      return { el: el("div", { class: "ci-node-picker" }, input, results), input };
    }

    const keyOrNull = (text) => {
      const key = text.trim();
      if (!key) return null;
      if (!/^[0-9a-f]{64}$/i.test(key)) throw new Error("The node key must be a public key: 64 hex characters (pick a node from the list)");
      return key.toUpperCase();
    };

    function openAddDialog() {
      const { body } = openDialog("Add a participant");
      const name = el("input", { type: "text", maxlength: 64, autocomplete: "off", required: true });
      const picker = nodePicker("");
      const save = el("button", { class: "btn btn-primary", type: "submit", "data-change": "" }, "Add");
      const form = el("form", { class: "ci-form" },
        field("Name", name, { required: true, hint: "As the node calls itself in its messages." }),
        field("Node key", picker.el, { labelFor: picker.input, hint: "Optional. Without it the participant is linked once a node of that name advertises." }),
        el("div", { class: "dialog-actions" }, save));
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        await act(save, async () => {
          await api("POST", `${BASE}/participants`, { name: name.value.trim(), node_id: keyOrNull(picker.input.value) });
          form.closest("dialog").close();
          await refresh();
        }, `${name.value.trim()} added`);
      });
      body.append(form);
      kit.disableIfReadOnly(form);
      name.focus();
    }

    function openEditDialog(id) {
      const entry = openDialog("Participant", { wide: true });
      let participantId = id;
      const find = () => (state.data ? state.data.participants.find((p) => p.id === participantId) : null);
      const initial = find();
      if (!initial) { entry.dialog.close(); return; }

      const facts = el("div", { class: "ci-facts" });
      const grid = el("div", { class: "ci-weeks", role: "group", "aria-label": "Weeks checked in (newest last)" });
      const log = el("div", { class: "ci-log" });

      // Name and node.
      const name = el("input", { type: "text", maxlength: 64, autocomplete: "off", value: initial.name });
      const picker = nodePicker(initial.node_id || "");
      const saveIdentity = el("button", { class: "btn btn-sm btn-primary", type: "submit", "data-change": "" }, "Save");
      const identity = el("form", { class: "ci-form" },
        el("div", { class: "form-grid" },
          field("Name", name, { hint: "A linked node's name follows its adverts." }),
          field("Node key", picker.el, { labelFor: picker.input, hint: "Linking to a key another participant has merges the two." })),
        el("div", { class: "input-group ci-actions-row" }, saveIdentity));
      identity.addEventListener("submit", async (event) => {
        event.preventDefault();
        const current = find();
        if (!current) return;
        await act(saveIdentity, async () => {
          const changes = {};
          const key = keyOrNull(picker.input.value);
          if (key !== (current.node_id || null)) changes.node_id = key;
          if (name.value.trim() !== current.name) changes.name = name.value.trim();
          if (!Object.keys(changes).length) return;
          const result = await api("PATCH", `${BASE}/participants/${current.id}`, changes);
          participantId = result.id;
          await refresh();
        }, "Saved");
      });

      // Streak.
      const streak = kit.numberInput(initial.streak, { min: 0, max: 1000, step: 1 });
      const asOf = el("input", { type: "date", value: state.data.week });
      const setStreak = el("button", { class: "btn btn-sm", type: "submit", "data-change": "" }, "Set streak");
      const streakForm = el("form", { class: "ci-form" },
        el("div", { class: "form-grid" },
          field("Streak (weeks)", streak),
          field("Up to the week of", asOf, { hint: "Those weeks get a check-in; the week before them none." })),
        el("div", { class: "input-group ci-actions-row" }, setStreak));
      streakForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const value = Number(streak.value);
        if (!Number.isInteger(value) || value < 0) { toast("The streak must be a whole number", "error"); return; }
        await act(setStreak, async () => {
          await api("PUT", `${BASE}/participants/${participantId}/streak`, { streak: value, week: asOf.value || null });
          await refresh();
        }, `Streak set to ${plural(value, "week", "weeks")}`);
      });

      const remove = button("Delete participant", { iconName: "trash", kind: "btn-danger" });
      remove.dataset.change = "";
      remove.onclick = async () => {
        const current = find();
        if (!current) return;
        const ok = await confirmDialog({ title: `Delete ${current.name}?`, text: "Their check-ins are deleted too. This can't be undone.", ok: "Delete", danger: true });
        if (!ok) return;
        await act(remove, async () => {
          await api("DELETE", `${BASE}/participants/${current.id}`);
          entry.dialog.close();
          await refresh();
        }, `${current.name} deleted`);
      };

      entry.body.append(
        facts,
        el("section", { class: "ci-section" }, el("h3", {}, "Weeks"), el("p", { class: "hint" }, "Click a week to add or remove its check-in."), grid),
        el("section", { class: "ci-section" }, el("h3", {}, "Name and node"), identity),
        el("section", { class: "ci-section" }, el("h3", {}, "Streak"), streakForm),
        el("section", { class: "ci-section" }, el("h3", {}, "Check-ins on record"), log),
        el("div", { class: "dialog-actions ci-danger-row" }, remove));
      kit.disableIfReadOnly(entry.body);

      async function toggleWeek(week, present, cell) {
        await act(cell, async () => {
          await api(present ? "DELETE" : "PUT", `${BASE}/participants/${participantId}/weeks/${week}`);
          await refresh();
        });
      }

      async function loadLog() {
        try {
          const { check_ins: rows } = await api("GET", `${BASE}/participants/${participantId}/check-ins`);
          log.replaceChildren(rows.length ? el("ul", { class: "ci-log-list" }, ...rows.slice(0, 30).map((row) => el("li", {},
            el("span", { class: "ci-log-when" }, fmtStamp(row.received_at)),
            el("span", { class: `tag ci-source ${row.source}` }, SOURCE_LABELS[row.source] || row.source),
            row.sender_name ? el("span", { class: "name" }, row.sender_name) : null,
            row.text ? el("span", { class: "ci-log-text" }, row.text) : el("span", { class: "muted" }, `week of ${fmtShortWeek(row.week)}`)))) : el("p", { class: "hint" }, "None."));
        } catch (err) {
          log.replaceChildren(el("p", { class: "hint" }, err.message));
        }
      }

      entry.refresh = () => {
        const p = find();
        if (!p) { entry.dialog.close(); return; }
        entry.heading.textContent = p.name;
        facts.replaceChildren(
          el("div", { class: "ci-fact" }, el("span", {}, "Key"), p.node_id ? keyRow(p.node_id) : el("span", { class: "muted" }, "Not linked yet (name only)")),
          el("div", { class: "ci-fact" }, el("span", {}, "Streak"), el("strong", {}, p.streak ? `🔥 ${p.streak}` : "0")),
          el("div", { class: "ci-fact" }, el("span", {}, "Best"), el("strong", {}, p.best_streak)),
          el("div", { class: "ci-fact" }, el("span", {}, "Total"), el("strong", {}, p.total_check_ins,
            p.extra_check_ins ? el("small", { class: "muted", title: "Imported as a total, beyond the weeks on record" }, ` (${p.extra_check_ins} imported)`) : null)),
          el("div", { class: "ci-fact" }, el("span", {}, "Rank"), el("strong", {}, `#${p.rank}`)));
        const weeks = new Set(p.weeks);
        const cells = [];
        for (let offset = GRID_WEEKS - 1; offset >= 0; offset -= 1) {
          const week = addDays(state.data.week, -7 * offset);
          const present = weeks.has(week);
          const cell = el("button", {
            type: "button", class: "ci-week", "data-on": String(present), "data-current": String(offset === 0), "data-change": "",
            "aria-pressed": String(present), title: `Week of ${fmtWeek(week)}${present ? ": checked in" : ""}`,
            "aria-label": `Week of ${fmtWeek(week)}`,
          }, offset % 4 === 0 ? el("span", { class: "ci-week-label" }, fmtShortWeek(week)) : null);
          cell.addEventListener("click", () => toggleWeek(week, present, cell));
          if (!canChange()) cell.disabled = true;
          cells.push(cell);
        }
        grid.replaceChildren(...cells);
        loadLog();
      };
      entry.refresh();
    }

    function openImportDialog() {
      const { body, dialog } = openDialog("Import check-ins", { wide: true });
      const file = el("input", { type: "file", accept: ".csv,.json,text/csv,application/json" });
      const paste = el("textarea", { rows: 6, spellcheck: "false", placeholder: "node_id,name,streak,total,last_check_in\nA1B2…,LMB-T,15,22,2026-10-05" });
      const asOf = el("input", { type: "date" });
      const mode = kit.selectInput([["merge", "Add to what's here"], ["replace", "Replace everything here"]], "merge");
      const result = el("div", { class: "ci-import-result", role: "status" });
      const preview = el("button", { class: "btn", type: "button", "data-change": "" }, "Preview");
      const submit = el("button", { class: "btn btn-primary", type: "submit", "data-change": "", disabled: true }, "Import");
      let records = null;
      let previewed = false;

      const invalidate = () => { previewed = false; submit.disabled = true; result.replaceChildren(); };
      [paste, asOf, mode].forEach((node) => node.addEventListener("input", () => { records = null; invalidate(); }));
      mode.addEventListener("change", invalidate);
      file.addEventListener("change", async () => {
        records = null;
        invalidate();
        const chosen = file.files && file.files[0];
        if (!chosen) return;
        try {
          paste.value = await chosen.text();
          records = parseImport(paste.value, chosen.name);
          result.replaceChildren(el("p", { class: "hint" }, `${plural(records.length, "record", "records")} read from ${chosen.name}. Preview to check them.`));
        } catch (err) {
          result.replaceChildren(notice("bad", err.message));
        }
      });

      const request = (dryRun) => {
        if (!records) records = parseImport(paste.value);
        return api("POST", `${BASE}/import`, { records, as_of: asOf.value || null, mode: mode.value, dry_run: dryRun });
      };

      const describe = (outcome, dryRun) => {
        const counts = [
          outcome.created && plural(outcome.created, "new participant", "new participants"),
          outcome.updated && plural(outcome.updated, "update", "updates"),
          outcome.linked && plural(outcome.linked, "link to a node", "links to nodes"),
        ].filter(Boolean).join(", ") || "no changes";
        const errors = outcome.errors || [];
        return el("div", {},
          errors.length
            ? notice("bad", `${plural(errors.length, "record has", "records have")} problems; nothing is imported until they're fixed.`,
              [el("ul", { class: "change-list" }, ...errors.slice(0, 50).map((error) => el("li", {}, `Record ${error.row}: ${error.message}`)))])
            : notice("info", `${dryRun ? "Importing gives" : "Imported:"} ${counts}. Streaks without a last check-in date count up to the week of ${fmtWeek(outcome.as_of)}.`));
      };

      preview.addEventListener("click", () => act(preview, async () => {
        const outcome = await request(true);
        result.replaceChildren(describe(outcome, true));
        previewed = !(outcome.errors || []).length;
        submit.disabled = !previewed;
      }));

      const form = el("form", { class: "ci-form" },
        el("p", { class: "hint" },
          "One record per node, from another check-in database: its key (or a key prefix) and/or name, its current streak, and optionally its total check-ins and the date of its last check-in. ",
          "CSV columns: ", el("code", {}, "node_id, name, streak, total, last_check_in"), " (an export from here has them). ",
          "Each record gives the node that many consecutive weekly check-ins up to its last check-in."),
        field("File", file, { hint: "CSV with a header row, or JSON (a list of records)." }),
        field("…or paste it", paste),
        el("div", { class: "form-grid" },
          field("Streaks without a date run up to", asOf, { hint: "Empty: the latest check-in week." }),
          field("Mode", mode)),
        result,
        el("div", { class: "dialog-actions" }, preview, submit));
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!previewed) return;
        if (mode.value === "replace") {
          const ok = await confirmDialog({ title: "Replace everything?", text: "Every participant and check-in here is deleted first. This can't be undone.", ok: "Replace", danger: true });
          if (!ok) return;
        }
        await act(submit, async () => {
          const outcome = await request(false);
          if (!outcome.saved) {
            result.replaceChildren(describe(outcome, true));
            throw new Error("Nothing was imported");
          }
          dialog.close();
          await refresh();
          return outcome;
        }, (outcome) => `Imported ${plural(outcome.created + outcome.updated + outcome.linked, "record", "records")}`);
      });
      body.append(form);
      kit.disableIfReadOnly(form);
    }

    refresh();
    return {
      refresh,
      destroy() { dialogs.slice().forEach((entry) => entry.dialog.close()); },
    };
  }

  window.MeshgramControl.registerPluginPanel("check_in", { title: "Check-ins", render });
})();
