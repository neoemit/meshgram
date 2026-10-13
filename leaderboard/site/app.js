"use strict";
// MeshCore check-in leaderboard. Polls the leaderboard (this site's nginx proxies it from
// Meshgram's /api/plugins/check_in/leaderboard) and renders it; no frameworks, no build step.
// Node names come from the mesh: they're only ever set as text, never as HTML.

(() => {
  const API_URL = "api/leaderboard";
  const CONFIG_URL = "config.json";
  const DEFAULT_REFRESH_SECONDS = 30;
  const MAX_RETRY_SECONDS = 120;
  const COUNT_UP_MS = 900;

  const $ = (id) => document.getElementById(id);
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const state = {
    data: null,
    refreshSeconds: DEFAULT_REFRESH_SECONDS,
    updatedAt: null,
    failures: 0,
    timer: null,
    query: "",
    activeOnly: false,
    seen: null,          // keys checked in this period (today or this week) at the last update
    shown: new Map(),    // stat element -> number on screen
  };

  // --- Helpers --------------------------------------------------------------------------------

  function h(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key.startsWith("--")) node.style.setProperty(key, value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function svg(markup, className) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    node.setAttribute("viewBox", "0 0 24 24");
    node.setAttribute("aria-hidden", "true");
    if (className) node.setAttribute("class", className);
    node.innerHTML = markup; // constant icon markup only
    return node;
  }

  const ICON_SIGNAL = '<circle cx="12" cy="12" r="2"/><path d="M7.8 16.2a6 6 0 0 1 0-8.4M16.2 7.8a6 6 0 0 1 0 8.4M4.9 19.1a10 10 0 0 1 0-14.2M19.1 4.9a10 10 0 0 1 0 14.2"/>';
  const ICON_QUIET = '<circle cx="12" cy="12" r="2"/><path d="M7.8 16.2a6 6 0 0 1 0-8.4M16.2 7.8a6 6 0 0 1 0 8.4" opacity=".35"/>';

  const keyOf = (entry) => entry.id || `name:${entry.name}`;
  const plural = (n, one, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

  function hueOf(entry) {
    if (entry.id) return parseInt(entry.id.slice(0, 6), 16) % 360;
    let hash = 0;
    for (const char of entry.name) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
    return hash % 360;
  }

  function badge(entry) {
    return h("span", {
      class: "badge",
      "data-unresolved": !entry.resolved,
      "--hue": String(hueOf(entry)),
      title: entry.id ? `Node ${entry.id}` : "Known by name only (no advert heard yet)",
    }, entry.hash || "?");
  }

  function relative(iso) {
    if (!iso) return "";
    const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
    const days = Math.floor(seconds / 86400);
    return days < 14 ? `${days} d ago` : new Date(iso).toLocaleDateString([], { day: "numeric", month: "short" });
  }

  function countdown(ms) {
    const total = Math.max(0, Math.floor(ms / 1000));
    const days = Math.floor(total / 86400);
    const time = [Math.floor((total % 86400) / 3600), Math.floor((total % 3600) / 60), total % 60]
      .map((part) => String(part).padStart(2, "0")).join(":");
    return days ? `${days}d ${time}` : time;
  }

  // Check-ins are daily or weekly (on one day). A Meshgram from before daily check-ins sends the
  // weekly names only (week, check_in_days, checked_in_this_week, week_rank, …): read either.
  function normalize(data) {
    const days = Array.isArray(data.check_in_days) ? data.check_in_days : [];
    return {
      ...data,
      daily: data.frequency === "daily",
      period: data.period || data.week,
      checkInDay: data.check_in_day || (days.length === 1 ? days[0] : null),
      checkInDays: days,
      participantsThisPeriod: data.participants_this_period ?? data.participants_this_week ?? 0,
      leaderboard: data.leaderboard.map((entry) => ({
        ...entry,
        inThisPeriod: Boolean(entry.checked_in_this_period ?? entry.checked_in_this_week),
        periodRank: entry.period_rank ?? entry.week_rank,
      })),
    };
  }

  function schedulePhrase(data) {
    if (data.daily) return "every day";
    if (data.checkInDay) return `every ${data.checkInDay}`;
    const days = data.checkInDays;
    if (!days.length) return "once a week, any day";
    const short = days.map((day) => day.slice(0, 3));
    return `every ${short.slice(0, -1).join(", ")} & ${short[short.length - 1]}`;
  }

  const thisPeriod = (data) => (data.daily ? "today" : "this week");
  const unit = (data) => (data.daily ? "day" : "week");

  // The check-in channel ({name, kind}), or null while Meshgram hasn't read it from its radio.
  const CHANNEL_LABELS = { public: "Channel", hashtag: "Hashtag channel", private: "Private channel" };
  const CHANNEL_HINTS = {
    hashtag: "Not on it yet? Join it as a hashtag channel in your MeshCore app.",
    private: "It's a private channel: ask for its key to join.",
  };

  function channelPhrase(channel) {
    return channel ? `the ${channel.name} channel` : "the mesh";
  }

  // --- Feedback ---------------------------------------------------------------------------------

  let toastTimer = null;
  function toast(text) {
    const box = $("toast");
    box.textContent = text;
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { box.hidden = true; }, 4500);
  }

  function announce(text) {
    $("announcer").textContent = text;
  }

  function setSignal(kind) {
    const signal = $("signal");
    signal.dataset.state = kind;
    $("signal-text").textContent = {
      live: "Live",
      lost: state.data ? "Signal lost · retrying" : "No signal · retrying",
      connecting: "Tuning in…",
    }[kind];
  }

  // Numbers count up when they change; the first time they're just shown.
  function countUp(node, value) {
    const from = state.shown.has(node) ? state.shown.get(node) : value;
    state.shown.set(node, value);
    if (from === value || reducedMotion.matches || document.hidden) {
      node.textContent = value.toLocaleString();
      return;
    }
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / COUNT_UP_MS);
      const eased = 1 - Math.pow(1 - t, 3);
      node.textContent = Math.round(from + (value - from) * eased).toLocaleString();
      if (t < 1 && state.shown.get(node) === value) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // --- Rendering -----------------------------------------------------------------------------------

  // "LMB Meshgram": the last word stands out.
  function renderBrand(name) {
    const words = name.trim().split(/\s+/);
    const last = words.pop();
    $("brand-name").replaceChildren(...(words.length ? [`${words.join(" ")} `, h("span", {}, last)] : [last]));
    $("brand").setAttribute("aria-label", `${name} leaderboard home`);
  }

  function render(fresh) {
    const data = state.data;
    const entries = data.leaderboard;
    const brand = data.meshgram_title || "Meshgram";
    document.title = `${data.title} · ${brand}`;
    renderBrand(brand);
    $("title").textContent = data.title;
    $("keyword-text").textContent = data.keyword;
    renderChannel(data.channel);
    $("eyebrow").textContent = `${data.daily ? "Daily" : "Weekly"} check-in · ${schedulePhrase(data)}`;
    $("lede").textContent = `Send ${data.keyword} on ${channelPhrase(data.channel)} ${schedulePhrase(data)} to check in. `
      + `Come back ${unit(data)} after ${unit(data)} to grow your streak and climb the tower.`;
    renderPeriodLabels(data);

    renderStats(entries, data);
    renderPodium(entries);
    renderRollCall(entries, fresh);
    renderBoard(fresh);
    renderAir();
    const zone = data.timezone && data.timezone !== "UTC" ? ` (${data.timezone})` : " (UTC)";
    const day = new Date(`${data.period}T12:00:00Z`).toLocaleDateString([], { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
    $("footer-text").textContent = `${data.daily ? "Today" : "Week of"} ${day}${zone}`
      + ` · updated ${new Date(state.updatedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
      + ` · refreshes every ${state.refreshSeconds} s · live from the mesh via ${brand}`;
  }

  // The page's fixed words for "this week", "today" when check-ins are daily.
  function renderPeriodLabels(data) {
    const period = thisPeriod(data);
    $("stats").setAttribute("aria-label", `${data.daily ? "Today" : "This week"} in numbers`);
    $("stat-week-label").textContent = `Checked in ${period}`;
    $("rollcall-title").textContent = data.daily ? "Today's roll call" : "This week's roll call";
    $("board-period").textContent = data.daily ? "Today" : "This week";
  }

  function renderChannel(channel) {
    $("channel").hidden = !channel;
    if (!channel) return;
    $("channel-label").textContent = CHANNEL_LABELS[channel.kind] || "Channel";
    $("channel-text").textContent = channel.name;
  }

  function renderStats(entries, data) {
    const active = entries.filter((entry) => entry.streak > 0);
    const longest = entries.reduce((best, entry) => Math.max(best, entry.streak), 0);
    const leaders = entries.filter((entry) => longest > 0 && entry.streak === longest);
    const total = entries.reduce((sum, entry) => sum + entry.total_check_ins, 0);
    const first = entries.find((entry) => entry.periodRank === 1);

    countUp($("stat-week"), data.participantsThisPeriod);
    $("stat-week-sub").replaceChildren(...(first ? ["First on air", h("strong", {}, first.name)] : [`Be the first ${thisPeriod(data)}`]));
    countUp($("stat-active"), active.length);
    $("stat-active-sub").textContent = entries.length ? `of ${plural(entries.length, "node")}` : "No nodes yet";
    countUp($("stat-longest"), longest);
    $("stat-longest-sub").replaceChildren(...(leaders.length
      ? [h("strong", {}, leaders[0].name, leaders.length > 1 ? ` +${leaders.length - 1}` : ""), plural(longest, unit(data))]
      : ["No streaks yet"]));
    countUp($("stat-total"), total);
    $("stat-total-sub").textContent = entries.length ? `by ${plural(entries.length, "node")}` : " ";
  }

  function renderPodium(entries) {
    const top = entries.filter((entry) => entry.streak > 0).slice(0, 3);
    const podium = $("podium");
    if (!top.length) {
      podium.replaceChildren(h("li", { class: "podium-empty" }, h("p", { class: "empty" }, "No streaks yet. Check in to claim the top spot!")));
      return;
    }
    podium.replaceChildren(...top.map((entry, index) => h("li", { class: `place-${index + 1}` },
      badge(entry),
      h("span", { class: "podium-name", title: entry.name }, entry.name),
      h("span", { class: "podium-streak" }, "🔥", h("strong", {}, entry.streak), entry.streak === 1 ? unit(state.data) : `${unit(state.data)}s`),
      h("span", { class: "podium-meta" }, h("span", {}, `best ${entry.best_streak}`), " · ", h("span", {}, plural(entry.total_check_ins, "check-in"))),
      h("div", { class: "podium-block", "aria-label": `Rank ${entry.rank}` }, entry.rank))));
  }

  function renderRollCall(entries, fresh) {
    const list = $("rollcall");
    const checked = entries.filter((entry) => entry.inThisPeriod).sort((a, b) => a.periodRank - b.periodRank);
    $("rollcall-note").textContent = checked.length ? `${plural(checked.length, "node")} in order of check-in` : "In order of check-in";
    if (!checked.length) {
      list.replaceChildren(h("li", { class: "rollcall-empty" }, h("span", { class: "rollcall-name" },
        state.data.window.open ? "Nobody yet. The air is yours!" : `Nobody yet ${thisPeriod(state.data)}.`)));
      return;
    }
    list.replaceChildren(...checked.map((entry, index) => h("li", {
      class: fresh.has(keyOf(entry)) ? "fresh" : null,
      "--delay": `${index * 30}ms`,
    }, badge(entry), h("span", { class: "rollcall-rank" }, `#${entry.periodRank}`), h("span", { class: "rollcall-name", title: entry.name }, entry.name))));
  }

  function renderBoard(fresh = new Set()) {
    const data = state.data;
    if (!data) return;
    const query = state.query.trim().toLowerCase();
    const maxStreak = Math.max(1, ...data.leaderboard.map((entry) => entry.streak));
    const rows = data.leaderboard.filter((entry) =>
      (!state.activeOnly || entry.streak > 0)
      && (!query || entry.name.toLowerCase().includes(query) || (entry.id || "").toLowerCase().startsWith(query)));

    $("board").replaceChildren(...rows.map((entry) => {
      const shortKey = entry.id ? `${entry.id.slice(0, 8)}…` : "name only";
      const last = relative(entry.last_check_in);
      const period = thisPeriod(data);
      const weekStatus = entry.inThisPeriod
        ? h("span", { class: "week-status", "data-in": true, title: `Checked in ${period}` }, svg(ICON_SIGNAL),
          h("span", {}, h("span", { class: "sr-only" }, `Checked in ${period}: `), `#${entry.periodRank}`, h("small", {}, last)))
        : h("span", { class: "week-status", title: `Not checked in ${period}` }, svg(ICON_QUIET),
          h("span", {}, h("span", { class: "sr-only" }, `Not checked in ${period}. `), h("span", { "aria-hidden": "true" }, "—"),
            last ? h("small", {}, h("span", { class: "sr-only" }, "Last check-in "), last) : null));
      return h("li", { class: [entry.rank <= 3 && entry.streak > 0 ? "top" : "", fresh.has(keyOf(entry)) ? "fresh" : ""].join(" ").trim() || null },
        h("span", { class: "board-rank" }, h("span", { class: "sr-only" }, "Rank "), entry.rank),
        h("div", { class: "board-node" },
          badge(entry),
          h("div", { class: "board-name" },
            h("strong", {}, entry.name, entry.resolved ? null : h("span", { class: "tag-unverified", title: "Known by name only until its advert is heard" }, "unverified")),
            h("small", { title: entry.id || "" }, shortKey))),
        h("div", { class: "board-streak" },
          h("span", { class: "board-streak-value" },
            h("span", { class: "sr-only" }, "Streak: "),
            entry.streak ? [h("span", { class: "flame", "aria-hidden": "true" }, "🔥"), plural(entry.streak, unit(data))] : h("span", { class: "zero" }, "No streak")),
          h("div", { class: "meter", "aria-hidden": "true" }, h("span", { "--fill": `${(entry.streak / maxStreak) * 100}%` }))),
        h("div", { class: "board-meta" },
          h("span", { class: "board-num", title: "Best streak" }, h("span", { class: "board-label" }, "Best "), entry.best_streak),
          h("span", { class: "board-num board-total", title: "Total check-ins" }, entry.total_check_ins,
            h("span", { class: "board-label" }, entry.total_check_ins === 1 ? " check-in" : " check-ins"))),
        weekStatus);
    }));

    const empty = $("board-empty");
    if (rows.length) {
      empty.hidden = true;
    } else {
      empty.hidden = false;
      empty.textContent = data.leaderboard.length
        ? "No node matches."
        : `Nobody has checked in yet. Send ${data.keyword} on ${channelPhrase(data.channel)} to be the first!`;
    }
  }

  // The on-air lamp and countdown, ticking every second.
  function renderAir() {
    const data = state.data;
    if (!data) return;
    const { open, opens_at: opensAt, closes_at: closesAt } = data.window;
    const target = Date.parse(open ? closesAt : opensAt);
    const left = target - Date.now();
    $("air").dataset.open = String(open);
    $("air-label").textContent = open ? "On air · closes in" : "Next check-in in";
    $("air-time").textContent = countdown(left);
    $("air").title = new Date(target).toLocaleString([], { weekday: "long", hour: "2-digit", minute: "2-digit" });
    if (left <= 0) refreshSoon();
  }


  // --- Data ---------------------------------------------------------------------------------------------

  function apply(raw) {
    const data = normalize(raw);
    const current = new Set(data.leaderboard.filter((entry) => entry.inThisPeriod).map(keyOf));
    const fresh = state.seen ? new Set([...current].filter((key) => !state.seen.has(key))) : new Set();
    state.seen = current;
    state.data = data;
    state.updatedAt = Date.now();
    render(fresh);
    $("offline").hidden = true;
    if (document.body.classList.contains("loading")) {
      document.body.classList.remove("loading");
      setTimeout(() => document.body.classList.remove("intro"), 1500);
    }
    if (fresh.size) {
      const names = data.leaderboard.filter((entry) => fresh.has(keyOf(entry))).map((entry) => entry.name);
      const text = names.length === 1 ? `📡 ${names[0]} just checked in!` : `📡 ${names.length} new check-ins: ${names.join(", ")}`;
      toast(text);
      announce(text);
    }
  }

  async function poll() {
    clearTimeout(state.timer);
    try {
      const response = await fetch(API_URL, { headers: { Accept: "application/json" }, cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!data || !Array.isArray(data.leaderboard) || !data.window) throw new Error("Unexpected response");
      state.failures = 0;
      setSignal("live");
      apply(data);
    } catch (err) {
      state.failures += 1;
      setSignal("lost");
      if (!state.data) $("offline").hidden = false;
    }
    const delay = state.failures
      ? Math.min(MAX_RETRY_SECONDS, state.refreshSeconds * 2 ** (state.failures - 1))
      : state.refreshSeconds;
    state.timer = setTimeout(poll, delay * 1000);
  }

  let soon = null;
  function refreshSoon() {
    if (soon) return;
    soon = setTimeout(() => { soon = null; poll(); }, 3000);
  }

  async function loadConfig() {
    try {
      const response = await fetch(CONFIG_URL, { cache: "no-store" });
      const config = response.ok ? await response.json() : {};
      const seconds = Number(config.refreshSeconds);
      if (Number.isFinite(seconds) && seconds >= 5) state.refreshSeconds = Math.round(seconds);
    } catch { /* defaults */ }
  }

  // --- Interactions ---------------------------------------------------------------------------------------

  $("search").addEventListener("input", (event) => { state.query = event.target.value; renderBoard(); });
  $("active-only").addEventListener("change", (event) => { state.activeOnly = event.target.checked; renderBoard(); });
  async function copy(button, text, copied, fallback) {
    try {
      await navigator.clipboard.writeText(text);
      button.dataset.copied = "";
      setTimeout(() => delete button.dataset.copied, 1500);
      toast(copied);
    } catch {
      toast(fallback);
    }
  }

  $("keyword").addEventListener("click", () => {
    const text = $("keyword-text").textContent;
    const where = channelPhrase(state.data && state.data.channel);
    copy($("keyword"), text, `Copied ${text}. Send it on ${where}!`, `Send ${text} on ${where} to check in`);
  });
  $("channel").addEventListener("click", () => {
    const channel = state.data && state.data.channel;
    if (!channel) return;
    const hint = CHANNEL_HINTS[channel.kind];
    copy($("channel"), channel.name, `Copied ${channel.name}.${hint ? ` ${hint}` : ""}`,
      `Check in on ${channelPhrase(channel)}.${hint ? ` ${hint}` : ""}`);
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.updatedAt && Date.now() - state.updatedAt > state.refreshSeconds * 1000) poll();
  });
  setInterval(() => { if (!document.hidden) renderAir(); }, 1000);

  // --- Animated mesh background ---------------------------------------------------------------------------
  // Drifting nodes, links between neighbours, packets hopping along them and the odd transmit ripple.

  function meshBackground(canvas) {
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const colors = ["#3dffa2", "#2fe0ff", "#ff4fd8", "#ffbf3c", "#8b7bff"];
    const light = window.matchMedia("(prefers-color-scheme: light)");
    let nodes = [];
    let packets = [];
    let ripples = [];
    let width = 0;
    let height = 0;
    let frame = null;
    let last = 0;
    const LINK = 170;

    function resize() {
      const ratio = Math.min(2, window.devicePixelRatio || 1);
      width = window.innerWidth;
      height = window.innerHeight;
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      const count = Math.round(Math.min(70, Math.max(18, (width * height) / 26000)));
      nodes = Array.from({ length: count }, () => ({
        x: Math.random() * width,
        y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.12,
        vy: (Math.random() - 0.5) * 0.12,
        r: 1.2 + Math.random() * 1.8,
        color: colors[Math.floor(Math.random() * colors.length)],
      }));
      packets = [];
      ripples = [];
    }

    function neighbours(node) {
      return nodes.filter((other) => other !== node && Math.hypot(other.x - node.x, other.y - node.y) < LINK);
    }

    function draw(dt) {
      ctx.clearRect(0, 0, width, height);
      const alpha = light.matches ? 0.35 : 1;
      for (const node of nodes) {
        node.x += node.vx * dt;
        node.y += node.vy * dt;
        if (node.x < -20) node.x = width + 20;
        if (node.x > width + 20) node.x = -20;
        if (node.y < -20) node.y = height + 20;
        if (node.y > height + 20) node.y = -20;
      }
      ctx.lineWidth = 1;
      for (let i = 0; i < nodes.length; i += 1) {
        for (let j = i + 1; j < nodes.length; j += 1) {
          const a = nodes[i];
          const b = nodes[j];
          const distance = Math.hypot(a.x - b.x, a.y - b.y);
          if (distance > LINK) continue;
          ctx.strokeStyle = `rgba(120, 170, 255, ${(1 - distance / LINK) * 0.16 * alpha})`;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
      for (const ripple of ripples) {
        ripple.r += 0.05 * dt;
        ripple.life -= 0.0009 * dt;
        ctx.strokeStyle = ripple.color;
        ctx.globalAlpha = Math.max(0, ripple.life) * 0.5 * alpha;
        ctx.beginPath();
        ctx.arc(ripple.x, ripple.y, ripple.r, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
      ripples = ripples.filter((ripple) => ripple.life > 0);
      for (const node of nodes) {
        ctx.fillStyle = node.color;
        ctx.globalAlpha = 0.75 * alpha;
        ctx.beginPath();
        ctx.arc(node.x, node.y, node.r, 0, Math.PI * 2);
        ctx.fill();
      }
      for (const packet of packets) {
        packet.t += 0.0012 * dt;
        const x = packet.from.x + (packet.to.x - packet.from.x) * packet.t;
        const y = packet.from.y + (packet.to.y - packet.from.y) * packet.t;
        ctx.globalAlpha = alpha;
        ctx.shadowColor = packet.color;
        ctx.shadowBlur = 12;
        ctx.fillStyle = packet.color;
        ctx.beginPath();
        ctx.arc(x, y, 2.4, 0, Math.PI * 2);
        ctx.fill();
        ctx.shadowBlur = 0;
        if (packet.t >= 1 && packet.hops > 0) {
          const next = neighbours(packet.to).filter((node) => node !== packet.from);
          if (next.length) packets.push({ from: packet.to, to: next[Math.floor(Math.random() * next.length)], t: 0, hops: packet.hops - 1, color: packet.color });
        }
      }
      ctx.globalAlpha = 1;
      packets = packets.filter((packet) => packet.t < 1);
      if (packets.length < 6 && Math.random() < 0.02 * dt / 16) {
        const from = nodes[Math.floor(Math.random() * nodes.length)];
        const options = neighbours(from);
        if (options.length) {
          packets.push({ from, to: options[Math.floor(Math.random() * options.length)], t: 0, hops: 3 + Math.floor(Math.random() * 4), color: from.color });
          ripples.push({ x: from.x, y: from.y, r: 4, life: 1, color: from.color });
        }
      }
    }

    function loop(now) {
      const dt = Math.min(64, now - (last || now));
      last = now;
      draw(dt);
      frame = requestAnimationFrame(loop);
    }

    function start() {
      cancelAnimationFrame(frame);
      frame = null;
      last = 0;
      if (reducedMotion.matches) {
        draw(0); // a still constellation
      } else if (!document.hidden) {
        frame = requestAnimationFrame(loop);
      }
    }

    resize();
    start();
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { resize(); start(); }, 150);
    });
    document.addEventListener("visibilitychange", start);
    reducedMotion.addEventListener("change", start);
    light.addEventListener("change", () => { if (reducedMotion.matches) draw(0); });
  }

  // --- Start --------------------------------------------------------------------------------------------

  meshBackground($("mesh-bg"));
  setSignal("connecting");
  loadConfig().then(poll);
})();
