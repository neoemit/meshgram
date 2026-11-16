"use strict";
// AR view of the map: the phone's camera with the mesh's nodes drawn where they are.
//
// The phone's location (Geolocation API), its attitude (DeviceOrientation: compass + gyro) and each
// node's position give a direction to draw every node in. Nodes carry no altitude, so heights come
// from a terrain model (Terrarium elevation tiles), which also gives the line of sight: the terrain
// profile between the phone and a node, checked against the straight path over a 4/3 Earth and the
// first Fresnel zone at the radio's frequency. Compass headings are magnetic; the World Magnetic
// Model turns them into true ones.
//
// Loaded after the page script (uses its helpers and `state`); adds its own button to the map, on
// touch devices only. The maths is exported for Node (`require("./ar.js")`), where there's no page.
const MeshgramAR = (() => {
  const DEG = Math.PI / 180;
  const EARTH_RADIUS = 6371008.8;
  // Radio waves bend with the atmosphere: the standard "4/3 Earth".
  const K_FACTOR = 4 / 3;
  const FRESNEL_CLEAR = 0.6;
  const DEFAULT_FREQ_MHZ = 869.5;

  // --- World Magnetic Model 2025 (NOAA/NCEI, public domain; valid 2025-2030) -----------------
  // g, h, dg/dt, dh/dt (nT, nT/year) for n = 1..12, m = 0..n.
  const WMM_EPOCH = 2025;
  const WMM = [-29351.8,0,12,0,-1410.8,4545.4,9.7,-21.5,-2556.6,0,-11.6,0,2951.1,-3133.6,-5.2,-27.7,1649.3,-815.1,-8,-12.1,1361,0,-1.3,0,-2404.1,-56.6,-4.2,4,1243.8,237.5,0.4,-0.3,453.6,-549.5,-15.6,-4.1,895,0,-1.6,0,799.5,278.6,-2.4,-1.1,55.7,-133.9,-6,4.1,-281.1,212,5.6,1.6,12.1,-375.6,-7,-4.4,-233.2,0,0.6,0,368.9,45.4,1.4,-0.5,187.2,220.2,0,2.2,-138.7,-122.9,0.6,0.4,-142,43,2.2,1.7,20.9,106.1,0.9,1.9,64.4,0,-0.2,0,63.8,-18.4,-0.4,0.3,76.9,16.8,0.9,-1.6,-115.7,48.8,1.2,-0.4,-40.9,-59.8,-0.9,0.9,14.9,10.9,0.3,0.7,-60.7,72.7,0.9,0.9,79.5,0,-0,0,-77,-48.9,-0.1,0.6,-8.8,-14.4,-0.1,0.5,59.3,-1,0.5,-0.8,15.8,23.4,-0.1,0,2.5,-7.4,-0.8,-1,-11.1,-25.1,-0.8,0.6,14.2,-2.3,0.8,-0.2,23.2,0,-0.1,0,10.8,7.1,0.2,-0.2,-17.5,-12.6,0,0.5,2,11.4,0.5,-0.4,-21.7,-9.7,-0.1,0.4,16.9,12.7,0.3,-0.5,15,0.7,0.2,-0.6,-16.8,-5.2,-0,0.3,0.9,3.9,0.2,0.2,4.6,0,-0,0,7.8,-24.8,-0.1,-0.3,3,12.2,0.1,0.3,-0.2,8.3,0.3,-0.3,-2.5,-3.3,-0.3,0.3,-13.1,-5.2,0,0.2,2.4,7.2,0.3,-0.1,8.6,-0.6,-0.1,-0.2,-8.7,0.8,0.1,0.4,-12.9,10,-0.1,0.1,-1.3,0,0.1,0,-6.4,3.3,0,0,0.2,0,0.1,-0,2,2.4,0.1,-0.2,-1,5.3,-0,0.1,-0.6,-9.1,-0.3,-0.1,-0.9,0.4,0,0.1,1.5,-4.2,-0.1,0,0.9,-3.8,-0.1,-0.1,-2.7,0.9,-0,0.2,-3.9,-9.1,-0,-0,2.9,0,0,0,-1.5,0,-0,-0,-2.5,2.9,0,0.1,2.4,-0.6,0,-0,-0.6,0.2,0,0.1,-0.1,0.5,-0.1,-0,-0.6,-0.3,0,-0,-0.1,-1.2,-0,0.1,1.1,-1.7,-0.1,-0,-1,-2.9,-0.1,0,-0.2,-1.8,-0.1,0,2.6,-2.3,-0.1,0,-2,0,0,0,-0.2,-1.3,0,-0,0.3,0.7,-0,0,1.2,1,-0,-0.1,-1.3,-1.4,-0,0.1,0.6,-0,-0,-0,0.6,0.6,0.1,-0,0.5,-0.1,-0,-0,-0.1,0.8,0,0,-0.4,0.1,0,-0,-0.2,-1,-0.1,-0,-1.3,0.1,-0,0,-0.7,0.2,-0.1,-0.1];

  const decimalYear = (date = new Date()) => {
    const start = Date.UTC(date.getUTCFullYear(), 0, 1);
    return date.getUTCFullYear() + (date.getTime() - start) / (Date.UTC(date.getUTCFullYear() + 1, 0, 1) - start);
  };

  // Magnetic declination in degrees (east positive): true heading = magnetic heading + declination.
  function declination(lat, lon, altMeters = 0, year = decimalYear()) {
    const a = 6378137, f = 1 / 298.257223563, e2 = f * (2 - f), re = 6371200, N = 12;
    const phi = Math.max(-89.999, Math.min(89.999, lat)) * DEG, lam = lon * DEG;
    const sinP = Math.sin(phi), cosP = Math.cos(phi);
    const rc = a / Math.sqrt(1 - e2 * sinP * sinP);
    const p = (rc + altMeters) * cosP, z = (rc * (1 - e2) + altMeters) * sinP;
    const r = Math.hypot(p, z), ct = z / r, st = p / r;   // geocentric colatitude
    const dt = year - WMM_EPOCH;
    const P = [[1]], dP = [[0]];
    let br = 0, bt = 0, bp = 0, i = 0, rn = (re / r) * (re / r);
    for (let n = 1; n <= N; n++) {
      P[n] = []; dP[n] = [];
      rn *= re / r;
      for (let m = 0; m <= n; m++) {
        if (m === n) {
          const k = n === 1 ? 1 : Math.sqrt(1 - 1 / (2 * n));
          P[n][m] = k * st * P[n - 1][n - 1];
          dP[n][m] = k * (st * dP[n - 1][n - 1] + ct * P[n - 1][n - 1]);
        } else {
          const d = Math.sqrt(n * n - m * m), k2 = Math.sqrt(Math.max(0, (n - 1) * (n - 1) - m * m));
          const p2 = m <= n - 2 ? P[n - 2][m] : 0, dp2 = m <= n - 2 ? dP[n - 2][m] : 0;
          P[n][m] = ((2 * n - 1) * ct * P[n - 1][m] - k2 * p2) / d;
          dP[n][m] = ((2 * n - 1) * (ct * dP[n - 1][m] - st * P[n - 1][m]) - k2 * dp2) / d;
        }
        const g = WMM[i] + dt * WMM[i + 2], h = WMM[i + 1] + dt * WMM[i + 3];
        i += 4;
        const cosM = Math.cos(m * lam), sinM = Math.sin(m * lam), t = g * cosM + h * sinM;
        br += (n + 1) * rn * t * P[n][m];
        bt -= rn * t * dP[n][m];
        bp += rn * m * (g * sinM - h * cosM) * P[n][m];
      }
    }
    bp /= Math.max(st, 1e-9);
    // Geocentric north/down to geodetic.
    const psi = Math.asin(ct) - phi;
    const north = -bt * Math.cos(psi) - -br * Math.sin(psi);
    return Math.atan2(bp, north) / DEG;
  }

  // --- Geometry ------------------------------------------------------------------------------

  function distance(a, b) {
    const dLat = (b.lat - a.lat) * DEG, dLon = (b.lon - a.lon) * DEG;
    const s = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * DEG) * Math.cos(b.lat * DEG) * Math.sin(dLon / 2) ** 2;
    return 2 * EARTH_RADIUS * Math.asin(Math.min(1, Math.sqrt(s)));
  }

  // Initial great-circle bearing from a to b, degrees clockwise from true north.
  function bearing(a, b) {
    const dLon = (b.lon - a.lon) * DEG, la = a.lat * DEG, lb = b.lat * DEG;
    const y = Math.sin(dLon) * Math.cos(lb);
    const x = Math.cos(la) * Math.sin(lb) - Math.sin(la) * Math.cos(lb) * Math.cos(dLon);
    return (Math.atan2(y, x) / DEG + 360) % 360;
  }

  // The angle above the horizontal at which something `dist` away at height h2 is seen from h1.
  const elevationAngle = (dist, h1, h2) => Math.atan2(h2 - h1 - (dist * dist) / (2 * K_FACTOR * EARTH_RADIUS), dist) / DEG;

  const earthBulge = (d1, d2) => (d1 * d2) / (2 * K_FACTOR * EARTH_RADIUS);

  const freeSpaceLoss = (dist, freqMHz) => 32.45 + 20 * Math.log10(Math.max(dist, 1) / 1000) + 20 * Math.log10(freqMHz);

  // Line of sight over a terrain profile: `heights` are ground heights (m) at equal steps from one
  // end (antenna at h1) to the other (antenna at h2), `dist` metres apart.
  //   clear: 60% of the first Fresnel zone is free; marginal: the straight line is, the zone isn't;
  //   blocked: terrain cuts the straight line.
  function analyzePath(heights, dist, h1, h2, freqMHz = DEFAULT_FREQ_MHZ) {
    const n = heights.length - 1, lambda = 299.792458 / freqMHz;
    let clearance = Infinity, fresnel = Infinity, index = -1;
    for (let i = 1; i < n; i++) {
      const d1 = (i / n) * dist, d2 = dist - d1;
      // Right at the antennas the ground is under them by definition.
      if (d1 < 50 || d2 < 50) continue;
      const clear = h1 + ((h2 - h1) * i) / n - heights[i] - earthBulge(d1, d2);
      const ratio = clear / Math.sqrt((lambda * d1 * d2) / dist);
      if (ratio < fresnel) { fresnel = ratio; index = i; }
      if (clear < clearance) clearance = clear;
    }
    if (index < 0) return { status: "clear", clearance: null, fresnel: null, at: null, index };
    const status = clearance < 0 ? "blocked" : fresnel < FRESNEL_CLEAR ? "marginal" : "clear";
    return { status, clearance, fresnel, at: (index / n) * dist, index };
  }

  // The camera's axes in the world (east, north, up) from DeviceOrientation angles (degrees, W3C
  // Z-X'-Y'' convention, alpha counted from north), the screen's rotation and a heading correction
  // (degrees clockwise: declination + trim). The back camera looks along the device's -Z.
  function cameraBasis(alpha, beta, gamma, screenAngle = 0, headingCorrection = 0) {
    const a = (alpha - headingCorrection) * DEG, b = beta * DEG, g = gamma * DEG, s = screenAngle * DEG;
    const cA = Math.cos(a), sA = Math.sin(a), cB = Math.cos(b), sB = Math.sin(b), cG = Math.cos(g), sG = Math.sin(g);
    const m = [
      [cA * cG - sA * sB * sG, -cB * sA, cG * sA * sB + cA * sG],
      [cG * sA + cA * sB * sG, cA * cB, sA * sG - cA * cG * sB],
      [-cB * sG, sB, cB * cG],
    ];
    const world = (x, y, z) => m.map((row) => row[0] * x + row[1] * y + row[2] * z);
    return {
      right: world(Math.cos(s), -Math.sin(s), 0),
      up: world(Math.sin(s), Math.cos(s), 0),
      fwd: world(0, 0, -1),
    };
  }

  const direction = (azimuth, elevation) => {
    const az = azimuth * DEG, el = elevation * DEG;
    return [Math.sin(az) * Math.cos(el), Math.cos(az) * Math.cos(el), Math.sin(el)];
  };
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const headingOf = (basis) => (Math.atan2(basis.fwd[0], basis.fwd[1]) / DEG + 360) % 360;
  // Signed difference a - b in (-180, 180].
  const angleDiff = (a, b) => ((((a - b) % 360) + 540) % 360) - 180;

  // Web Mercator tile coordinates (fractional) of a position.
  function tilePoint(lat, lon, zoom) {
    const n = 2 ** zoom, rad = Math.max(-85.05, Math.min(85.05, lat)) * DEG;
    return { x: ((lon + 180) / 360) * n, y: ((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * n };
  }

  const pure = {
    declination, distance, bearing, elevationAngle, earthBulge, freeSpaceLoss, analyzePath,
    cameraBasis, direction, headingOf, angleDiff, tilePoint,
  };
  if (typeof document === "undefined") return pure;

  // --- Terrain -------------------------------------------------------------------------------
  // Terrarium tiles (Mapzen's open terrain data, hosted by AWS): height = R*256 + G + B/256 - 32768.

  const TERRAIN_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png";
  const TERRAIN_CACHE = 72;
  const terrain = {
    tiles: new Map(),   // "z/x/y" -> Promise<Int16Array | null>
    failed: 0,
    load(z, x, y) {
      const key = `${z}/${x}/${y}`;
      let tile = this.tiles.get(key);
      if (tile) { this.tiles.delete(key); this.tiles.set(key, tile); return tile; }
      tile = new Promise((resolve) => {
        const image = new Image();
        image.crossOrigin = "anonymous";
        image.onload = () => {
          try {
            const canvas = document.createElement("canvas");
            canvas.width = canvas.height = 256;
            const context = canvas.getContext("2d", { willReadFrequently: true });
            context.drawImage(image, 0, 0, 256, 256);
            const rgba = context.getImageData(0, 0, 256, 256).data, out = new Int16Array(65536);
            // Sea floor counts as sea level.
            for (let i = 0; i < 65536; i++) out[i] = Math.max(0, Math.round(rgba[i * 4] * 256 + rgba[i * 4 + 1] + rgba[i * 4 + 2] / 256 - 32768));
            resolve(out);
          } catch { resolve(null); }
        };
        image.onerror = () => resolve(null);
        image.src = TERRAIN_URL.replace("{z}", z).replace("{x}", x).replace("{y}", y);
      }).then((data) => {
        // A failed tile is asked for again later (offline for a moment, say).
        if (!data) { this.failed = Date.now(); setTimeout(() => this.tiles.delete(key), 30000); }
        return data;
      });
      this.tiles.set(key, tile);
      while (this.tiles.size > TERRAIN_CACHE) this.tiles.delete(this.tiles.keys().next().value);
      return tile;
    },
    // Ground heights at n + 1 equal steps from a to b; null when the terrain can't be had.
    async profile(a, b, n, zoom) {
      const max = 2 ** zoom, points = [], wanted = new Map();
      for (let i = 0; i <= n; i++) {
        const t = i / n, point = tilePoint(a.lat + (b.lat - a.lat) * t, a.lon + (b.lon - a.lon) * t, zoom);
        const x = Math.min(max - 1, Math.max(0, Math.floor(point.x))), y = Math.min(max - 1, Math.max(0, Math.floor(point.y)));
        points.push([`${zoom}/${x}/${y}`, (point.x - x) * 256 - 0.5, (point.y - y) * 256 - 0.5]);
        if (!wanted.has(points[i][0])) wanted.set(points[i][0], this.load(zoom, x, y));
      }
      const tiles = new Map();
      for (const [key, promise] of wanted) {
        const data = await promise;
        if (!data) return null;
        tiles.set(key, data);
      }
      return Float32Array.from(points, ([key, px, py]) => {
        const data = tiles.get(key);
        const x = Math.min(255, Math.max(0, px)), y = Math.min(255, Math.max(0, py));
        const x0 = Math.floor(x), y0 = Math.floor(y), x1 = Math.min(255, x0 + 1), y1 = Math.min(255, y0 + 1), fx = x - x0, fy = y - y0;
        return (data[y0 * 256 + x0] * (1 - fx) + data[y0 * 256 + x1] * fx) * (1 - fy)
          + (data[y1 * 256 + x0] * (1 - fx) + data[y1 * 256 + x1] * fx) * fy;
      });
    },
  };

  // --- Settings and state --------------------------------------------------------------------

  const LOS = {
    clear: { text: "line of sight", short: "clear", color: "#56d364" },
    marginal: { text: "partly obstructed", short: "marginal", color: "#e3b341" },
    blocked: { text: "blocked", short: "blocked", color: "#ff7b72" },
    unknown: { text: "no terrain data", short: "unknown", color: "#9aa7b5" },
    pending: { text: "checking…", short: "checking", color: "#9aa7b5" },
  };
  const MAX_LABELS = 60;
  const CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  const cardinal = (deg) => CARDINALS[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16];
  const fmtDist = (m) => (m < 950 ? `${Math.round(m / 10) * 10} m` : m < 9950 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m / 1000)} km`);
  const fmtSigned = (value, digits = 0) => `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(digits)}`;
  const rangeFromSlider = (v) => 1000 * 300 ** (v / 100);
  const sliderFromRange = (m) => (Math.log(m / 1000) / Math.log(300)) * 100;

  const saved = store.get("ar", {}) || {};
  const number = (value, fallback, lo, hi) => (Number.isFinite(value) ? clamp(value, lo, hi) : fallback);
  const settings = {
    range: number(saved.range, 30000, 1000, 300000),
    types: Array.isArray(saved.types) ? saved.types.filter((t) => t in NODE_TYPES) : ["repeater", "room"],
    losOnly: saved.losOnly === true,
    myHeight: number(saved.myHeight, 2, 0, 60),
    nodeHeight: number(saved.nodeHeight, 5, 0, 100),
    fov: number(saved.fov, 65, 35, 110),
    trim: number(saved.trim, 0, -180, 180),
  };
  const save = () => store.set("ar", settings);

  const ar = {
    open: false, dialog: null, ui: {}, w: 0, h: 0, dpr: 1,
    stream: null, cameraError: null,
    pos: null, geoError: null, geoWatch: null, declination: 0, ground: null,
    raw: null, rawAt: 0, absolute: false, yaw: null, compassAccuracy: null, motion: "granted", basis: null, lastFrame: 0,
    targets: new Map(), sorted: [], selected: null, sheet: null, aligning: false,
    freq: DEFAULT_FREQ_MHZ, losBusy: false, losAgain: false, timer: null, wakeLock: null, raf: 0, startedAt: 0,
  };

  // --- Page ----------------------------------------------------------------------------------

  const CSS = `
  .ar-open-btn svg { width: 20px; height: 20px; }
  dialog.ar { position: fixed; inset: 0; width: 100vw; height: 100vh; height: 100dvh; max-width: none; max-height: none; margin: 0; padding: 0;
    border: 0; overflow: hidden; background: #05080c; color: #f2f5f8; font: 14px/1.35 var(--font); touch-action: none; user-select: none; -webkit-user-select: none; }
  dialog.ar::backdrop { background: #000; }
  .ar video, .ar canvas.ar-canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
  .ar video { object-fit: cover; background: linear-gradient(#1b2633, #0a0e13 55%, #10151c); }
  .ar-canvas { pointer-events: none; }
  .ar-labels { position: absolute; inset: 0; overflow: hidden; }
  .ar-label { position: absolute; left: 0; top: 0; display: flex; align-items: center; gap: 7px; max-width: 220px; min-height: 34px; padding: 5px 10px 5px 7px;
    border: 1px solid rgba(255, 255, 255, .22); border-left: 3px solid var(--c); border-radius: 10px; background: rgba(8, 12, 17, .78); color: #fff;
    font: inherit; text-align: left; transform-origin: 50% 100%; will-change: transform; }
  .ar-label svg { flex: none; width: 15px; height: 15px; }
  .ar-label .shape { fill: var(--c); stroke: #05080c; stroke-width: 1.5; }
  .ar-label b { display: block; overflow: hidden; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
  .ar-label small { display: flex; align-items: center; gap: 5px; color: #c9d3de; font-size: 12px; white-space: nowrap; }
  .ar-label.compact { min-height: 26px; padding: 3px 8px 3px 6px; }
  .ar-label.compact small { display: none; }
  .ar-label.blocked { border-style: dashed; border-left-style: solid; }
  .ar-label.sel { z-index: 2; border-color: #fff; background: rgba(8, 12, 17, .94); box-shadow: 0 0 0 2px var(--c); filter: none !important; opacity: 1 !important; }
  .ar-dot { flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--los, #9aa7b5); }
  .ar-pill { border: 1px solid rgba(255, 255, 255, .16); border-radius: 12px; background: rgba(8, 12, 17, .72); -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px); }
  .ar-top { position: absolute; top: 0; left: 0; right: 0; display: flex; align-items: center; gap: 8px;
    padding: calc(10px + env(safe-area-inset-top)) calc(10px + env(safe-area-inset-right)) 0 calc(10px + env(safe-area-inset-left)); }
  .ar-btn { flex: none; width: 44px; height: 44px; display: grid; place-items: center; padding: 0; color: #fff; }
  .ar-btn svg { width: 20px; height: 20px; }
  .ar-btn[aria-pressed="true"] { background: #fff; color: #05080c; }
  .ar-heading { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; padding: 4px 10px; font-variant-numeric: tabular-nums; }
  .ar-heading b { font-size: 17px; }
  .ar-heading span { overflow: hidden; max-width: 100%; color: #c9d3de; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
  .ar-notice { position: absolute; top: calc(104px + env(safe-area-inset-top)); left: 50%; transform: translateX(-50%); width: max-content; max-width: calc(100% - 32px);
    padding: 8px 12px; text-align: center; }
  .ar-guide { position: absolute; top: 46%; display: flex; align-items: center; gap: 6px; max-width: 46%; padding: 8px 10px; color: #fff; font: inherit; font-weight: 600; }
  .ar-guide.left { left: calc(8px + env(safe-area-inset-left)); }
  .ar-guide.right { right: calc(8px + env(safe-area-inset-right)); flex-direction: row-reverse; }
  .ar-guide span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ar-bottom { position: absolute; left: 0; right: 0; bottom: 0; display: grid; gap: 8px;
    padding: 0 calc(10px + env(safe-area-inset-right)) calc(10px + env(safe-area-inset-bottom)) calc(10px + env(safe-area-inset-left)); pointer-events: none; }
  .ar-bottom > * { pointer-events: auto; }
  .ar-radar { justify-self: start; width: 116px; height: 116px; padding: 0; border-radius: 50%; }
  .ar-radar canvas { width: 116px; height: 116px; display: block; }
  .ar-bar { display: grid; gap: 8px; padding: 10px 12px; }
  .ar-summary { display: flex; justify-content: space-between; gap: 10px; font-variant-numeric: tabular-nums; }
  .ar-summary b { font-weight: 650; }
  .ar-chips { display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none; touch-action: pan-x; }
  .ar-chip { flex: none; display: inline-flex; align-items: center; gap: 6px; min-height: 36px; padding: 0 11px; border: 1px solid rgba(255, 255, 255, .2); border-radius: 18px;
    background: none; color: #c9d3de; font: inherit; }
  .ar-chip i { width: 9px; height: 9px; border-radius: 50%; background: var(--c); }
  .ar-chip[aria-pressed="true"] { border-color: #fff; background: rgba(255, 255, 255, .16); color: #fff; }
  .ar input[type="range"] { width: 100%; height: 30px; margin: 0; accent-color: #4cc2ff; touch-action: pan-x; }
  .ar-sheet { position: absolute; left: 0; right: 0; bottom: 0; max-height: 78%; display: flex; flex-direction: column; border: 1px solid rgba(255, 255, 255, .16); border-bottom: 0;
    border-radius: 18px 18px 0 0; background: rgba(10, 14, 19, .95); -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px);
    padding-bottom: env(safe-area-inset-bottom); touch-action: pan-y; user-select: text; -webkit-user-select: text; }
  .ar-sheet-head { display: flex; align-items: center; gap: 10px; padding: 12px 10px 8px 16px; }
  .ar-sheet-head h2 { flex: 1; min-width: 0; margin: 0; overflow: hidden; font-size: 17px; text-overflow: ellipsis; white-space: nowrap; }
  .ar-sheet-head .ar-btn { border: 0; background: none; }
  .ar-sheet-body { overflow-y: auto; padding: 0 16px 16px; display: grid; gap: 12px; }
  .ar-badge { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 12px; background: color-mix(in srgb, var(--los) 22%, transparent); color: var(--los); font-weight: 650; }
  .ar-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 14px; margin: 0; }
  .ar-facts div { min-width: 0; }
  .ar-facts dt { color: #9aa7b5; font-size: 12px; }
  .ar-facts dd { margin: 0; font-variant-numeric: tabular-nums; }
  .ar-profile { width: 100%; height: 120px; display: block; border-radius: 10px; background: rgba(255, 255, 255, .05); }
  .ar-note { margin: 0; color: #9aa7b5; font-size: 12.5px; }
  .ar-note a { color: #7fd3ff; }
  .ar-action { min-height: 44px; display: inline-flex; align-items: center; justify-content: center; gap: 8px; padding: 0 16px; border: 1px solid rgba(255, 255, 255, .22); border-radius: 12px;
    background: rgba(255, 255, 255, .1); color: #fff; font: inherit; font-weight: 600; }
  .ar-action svg { width: 18px; height: 18px; }
  .ar-actions { display: flex; flex-wrap: wrap; gap: 8px; }
  .ar-field { display: grid; gap: 2px; }
  .ar-field-head { display: flex; justify-content: space-between; gap: 10px; }
  .ar-field-head output { color: #7fd3ff; font-variant-numeric: tabular-nums; }
  .ar-list { margin: 0; padding: 0; list-style: none; display: grid; gap: 6px; }
  .ar-row { width: 100%; min-height: 52px; display: flex; align-items: center; gap: 10px; padding: 6px 12px; border: 1px solid rgba(255, 255, 255, .12); border-radius: 12px;
    background: rgba(255, 255, 255, .05); color: #fff; font: inherit; text-align: left; }
  .ar-row svg { flex: none; width: 16px; height: 16px; }
  .ar-row .shape { fill: var(--c); stroke: #05080c; stroke-width: 1.5; }
  .ar-row-main { flex: 1; min-width: 0; }
  .ar-row-main b { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ar-row-main small { color: #9aa7b5; }
  .ar-row .ar-badge { flex: none; font-size: 12px; }
  .ar-align { position: absolute; left: 50%; bottom: calc(24px + env(safe-area-inset-bottom)); transform: translateX(-50%); width: max-content; max-width: calc(100% - 32px);
    display: grid; gap: 10px; justify-items: center; padding: 12px 16px; text-align: center; }
  .ar-crosshair { position: absolute; left: 50%; top: 0; bottom: 0; width: 0; border-left: 1px dashed rgba(255, 255, 255, .6); pointer-events: none; }
  .ar [hidden] { display: none !important; }
  .ar button:focus-visible, .ar input:focus-visible { outline: 2px solid #4cc2ff; outline-offset: 2px; }
  `;

  const SVG = {
    ar: '<path d="M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 2h-2M8 20H6a2 2 0 0 1-2-2v-2"/><path d="M12 7.5 16 9.8v4.4L12 16.5 8 14.2V9.8ZM8 9.8l4 2.2 4-2.2M12 12v4.5"/>',
    list: '<path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
    turn: '<path d="M15 6l-6 6 6 6"/>',
  };
  const svg = (body) => `<svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
  const nodeName = (node) => (node.is_self ? "This radio" : node.name || shortId(node.id));
  const nodeColor = (node) => (node.is_self ? SELF_COLOR : nodeMeta(node).color);

  function build() {
    document.head.append(el("style", {}, CSS));
    const ui = ar.ui;
    ui.video = el("video", { playsinline: true, muted: true, autoplay: true, "aria-hidden": "true" });
    ui.video.muted = true;
    ui.canvas = el("canvas", { class: "ar-canvas", "aria-hidden": "true" });
    ui.labels = el("div", { class: "ar-labels" });
    ui.heading = el("b", {}, "–");
    ui.fix = el("span", {}, "Waiting for location…");
    ui.listBtn = el("button", { class: "ar-btn ar-pill", type: "button", "aria-label": "Nodes in range", title: "Nodes in range", html: svg(SVG.list), onclick: () => toggleSheet("list") });
    ui.settingsBtn = el("button", { class: "ar-btn ar-pill", type: "button", "aria-label": "AR settings", title: "AR settings", html: icon("sliders"), onclick: () => toggleSheet("settings") });
    ui.notice = el("div", { class: "ar-notice ar-pill", role: "status", hidden: true });
    ui.guide = el("button", { class: "ar-guide ar-pill", type: "button", hidden: true, onclick: () => ar.selected && showDetail(ar.selected) });
    ui.radar = el("canvas", { width: 232, height: 232, "aria-hidden": "true" });
    ui.summary = el("b", {}, "");
    ui.rangeText = el("span", {}, "");
    ui.range = el("input", {
      type: "range", min: 0, max: 100, step: 1, value: Math.round(sliderFromRange(settings.range)), "aria-label": "Range",
      oninput: () => { settings.range = rangeFromSlider(Number(ui.range.value)); save(); refreshTargets(); },
    });
    ui.chips = el("div", { class: "ar-chips", role: "group", "aria-label": "Shown on camera" });
    ui.bottom = el("div", { class: "ar-bottom" },
      el("button", { class: "ar-radar ar-pill", type: "button", "aria-label": "Nodes in range", onclick: () => toggleSheet("list") }, ui.radar),
      el("div", { class: "ar-bar ar-pill" }, el("div", { class: "ar-summary" }, ui.summary, ui.rangeText), ui.range, ui.chips));
    ui.sheet = el("section", { class: "ar-sheet", hidden: true });
    ui.crosshair = el("div", { class: "ar-crosshair", hidden: true });
    ui.alignText = el("span", {}, "");
    ui.align = el("div", { class: "ar-align ar-pill", hidden: true },
      el("span", {}, "Drag sideways until the labels sit on what you see."), ui.alignText,
      el("div", { class: "ar-actions" },
        el("button", { class: "ar-action", type: "button", onclick: () => { settings.trim = 0; save(); renderAlign(); } }, "Reset"),
        el("button", { class: "ar-action", type: "button", onclick: () => setAligning(false) }, "Done")));
    ui.top = el("div", { class: "ar-top" },
      el("button", { class: "ar-btn ar-pill", type: "button", "aria-label": "Close AR view", title: "Close", html: icon("x"), onclick: close }),
      el("div", { class: "ar-heading ar-pill" }, ui.heading, ui.fix), ui.listBtn, ui.settingsBtn);
    ar.dialog = el("dialog", { class: "ar", "aria-label": "AR view" },
      ui.video, ui.canvas, ui.labels, ui.crosshair, ui.top, ui.notice, ui.guide, ui.bottom, ui.align, ui.sheet);
    document.body.append(ar.dialog);
    ar.dialog.addEventListener("close", stop);
    ar.dialog.addEventListener("cancel", (event) => {
      // Escape / the back gesture closes what's on top first.
      if (ar.sheet || ar.aligning) { event.preventDefault(); closeSheet(); setAligning(false); }
    });

    // A tap on the picture puts the open card away; while aligning, a drag turns the compass.
    let drag = null;
    ui.labels.addEventListener("pointerdown", (event) => {
      if (event.target !== ui.labels) return;
      if (ar.aligning) { drag = { x: event.clientX, trim: settings.trim }; ui.labels.setPointerCapture(event.pointerId); }
      else if (ar.sheet) closeSheet();
      else select(null);
    });
    ui.labels.addEventListener("pointermove", (event) => {
      if (!drag) return;
      settings.trim = clamp(angleDiff(drag.trim - (event.clientX - drag.x) / (focalPx() * DEG), 0), -180, 180);
      renderAlign();
    });
    const drop = () => { if (drag) { drag = null; save(); } };
    ui.labels.addEventListener("pointerup", drop);
    ui.labels.addEventListener("pointercancel", drop);
    renderChips();
  }

  function renderChips() {
    const chip = (label, pressed, color, toggle) => el("button", {
      class: "ar-chip", type: "button", "aria-pressed": String(pressed), style: color ? `--c:${color}` : null,
      onclick: () => { toggle(); save(); renderChips(); refreshTargets(); },
    }, color ? el("i") : null, label);
    ar.ui.chips.replaceChildren(
      ...Object.entries(NODE_TYPES).map(([type, meta]) => chip(meta.plural, settings.types.includes(type), meta.color, () => {
        settings.types = settings.types.includes(type) ? settings.types.filter((t) => t !== type) : [...settings.types, type];
      })),
      chip("Line of sight only", settings.losOnly, null, () => { settings.losOnly = !settings.losOnly; }));
  }

  // --- Opening and closing -------------------------------------------------------------------

  function explain(title, text) {
    const dialog = el("dialog", { class: "dialog" }, el("form", { method: "dialog" },
      el("h2", {}, title), el("p", {}, text),
      el("div", { class: "dialog-actions" }, el("button", { class: "btn btn-primary", type: "submit" }, "OK"))));
    dialog.addEventListener("close", () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  }

  async function open() {
    if (ar.open) return;
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.geolocation) {
      explain("AR view needs HTTPS", `Browsers only give a page the camera, the location and the compass over a secure connection, and this page was loaded from ${location.origin}. `
        + "Open Meshgram through an https:// address (a reverse proxy with a certificate, or a tunnel such as Tailscale Serve or Cloudflare Tunnel) and the AR view works.");
      return;
    }
    // iOS asks for the motion sensors, and only straight from a tap.
    ar.motion = "granted";
    if (typeof DeviceOrientationEvent !== "undefined" && typeof DeviceOrientationEvent.requestPermission === "function") {
      try { ar.motion = await DeviceOrientationEvent.requestPermission(); } catch { ar.motion = "denied"; }
    }
    if (!ar.dialog) build();
    Object.assign(ar, { open: true, raw: null, basis: null, absolute: false, yaw: null, compassAccuracy: null, ground: null, pos: null, geoError: null, cameraError: null, startedAt: Date.now(), lastFrame: 0 });
    ar.dialog.showModal();
    resize();
    window.addEventListener("resize", resize);
    window.addEventListener("deviceorientationabsolute", onOrientation);
    window.addEventListener("deviceorientation", onOrientation);
    document.addEventListener("visibilitychange", onVisibility);
    startCamera();
    keepAwake();
    ar.geoWatch = navigator.geolocation.watchPosition(onPosition, (error) => { ar.geoError = error; tick(); },
      { enableHighAccuracy: true, maximumAge: 2000, timeout: 30000 });
    fetch("/api/radio").then((response) => (response.ok ? response.json() : null)).then((overview) => {
      const freq = overview && overview.radio && Number(overview.radio.freq);
      if (freq > 100) { ar.freq = freq; ar.targets.forEach((target) => { target.losKey = null; }); runLos(); }
    }).catch(() => {});
    ar.timer = setInterval(tick, 1000);
    tick();
    ar.raf = requestAnimationFrame(frame);
  }

  function close() { if (ar.dialog && ar.dialog.open) ar.dialog.close(); }

  function stop() {
    if (!ar.open) return;
    ar.open = false;
    cancelAnimationFrame(ar.raf);
    clearInterval(ar.timer);
    window.removeEventListener("resize", resize);
    window.removeEventListener("deviceorientationabsolute", onOrientation);
    window.removeEventListener("deviceorientation", onOrientation);
    document.removeEventListener("visibilitychange", onVisibility);
    if (ar.geoWatch !== null) navigator.geolocation.clearWatch(ar.geoWatch);
    ar.geoWatch = null;
    stopCamera();
    if (ar.wakeLock) { ar.wakeLock.release().catch(() => {}); ar.wakeLock = null; }
    closeSheet();
    setAligning(false);
    save();
  }

  async function startCamera() {
    stopCamera();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: false, video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
      });
      if (!ar.open || document.hidden) { stream.getTracks().forEach((track) => track.stop()); return; }
      ar.stream = stream;
      ar.cameraError = null;
      ar.ui.video.srcObject = stream;
      await ar.ui.video.play().catch(() => {});
    } catch (error) {
      ar.cameraError = error;
    }
    tick();
  }

  function stopCamera() {
    if (ar.stream) ar.stream.getTracks().forEach((track) => track.stop());
    ar.stream = null;
    if (ar.ui.video) ar.ui.video.srcObject = null;
  }

  async function keepAwake() {
    try { if (navigator.wakeLock) ar.wakeLock = await navigator.wakeLock.request("screen"); } catch { /* the screen may dim */ }
  }

  // The camera is let go while the page is in the background.
  function onVisibility() {
    if (!ar.open) return;
    if (document.hidden) stopCamera();
    else { startCamera(); keepAwake(); }
  }

  function resize() {
    const canvas = ar.ui.canvas;
    ar.w = ar.dialog.clientWidth;
    ar.h = ar.dialog.clientHeight;
    ar.dpr = Math.min(window.devicePixelRatio || 1, 3);
    canvas.width = Math.round(ar.w * ar.dpr);
    canvas.height = Math.round(ar.h * ar.dpr);
  }

  // --- Sensors -------------------------------------------------------------------------------

  function onOrientation(event) {
    if (event.beta === null || event.gamma === null) return;
    let alpha = event.alpha;
    if (typeof event.webkitCompassHeading === "number") {
      // iOS: alpha starts anywhere, the compass heading says where north is. Their difference is
      // followed slowly, so the picture moves with the gyro and the compass only corrects it.
      if (alpha === null) alpha = 0;
      ar.compassAccuracy = event.webkitCompassAccuracy;
      if (!(event.webkitCompassAccuracy < 0)) {
        const offset = 360 - event.webkitCompassHeading - alpha;
        ar.yaw = ar.yaw === null ? offset : ar.yaw + angleDiff(offset, ar.yaw) * 0.04;
      }
      if (ar.yaw === null) return;
      alpha += ar.yaw;
      ar.absolute = true;
    } else {
      if (alpha === null) return;
      const absolute = event.type === "deviceorientationabsolute" || event.absolute === true;
      if (absolute) ar.absolute = true;
      else if (ar.absolute) return;   // the absolute events are the ones to follow
    }
    ar.raw = { alpha, beta: event.beta, gamma: event.gamma };
    ar.rawAt = Date.now();
  }

  function onPosition(position) {
    const c = position.coords;
    const first = !ar.pos;
    ar.pos = { lat: c.latitude, lon: c.longitude, accuracy: c.accuracy, altitude: c.altitude };
    ar.geoError = null;
    ar.declination = declination(c.latitude, c.longitude, c.altitude || 0);
    refreshTargets();
    if (first) tick();
  }

  const screenAngle = () => (screen.orientation && typeof screen.orientation.angle === "number" ? screen.orientation.angle : ((Number(window.orientation) || 0) + 360) % 360);

  // The size of a radian on screen: the camera's field of view along the picture's long side,
  // carried through the crop that fills the screen.
  function focalPx() {
    const video = ar.ui.video, vw = video.videoWidth, vh = video.videoHeight, tan = Math.tan((settings.fov * DEG) / 2);
    if (!vw || !vh) return Math.max(ar.w, ar.h) / 2 / tan;
    return (Math.max(vw, vh) / 2 / tan) * Math.max(ar.w / vw, ar.h / vh);
  }

  // --- Targets -------------------------------------------------------------------------------

  function refreshTargets() {
    if (!ar.open || !ar.pos) return;
    const seen = new Set();
    for (const node of state.nodes.values()) {
      if (!hasPos(node) || !settings.types.includes(node.type in NODE_TYPES ? node.type : "unknown")) continue;
      const dist = distance(ar.pos, node);
      // Not the radio in the user's own pocket.
      if (dist > settings.range || dist < 20) continue;
      let target = ar.targets.get(node.id);
      if (!target) {
        target = { id: node.id, los: { status: "pending" }, losKey: null, elev: null, text: "", w: 0, h: 0 };
        target.label = el("button", { class: "ar-label", type: "button", onclick: () => showDetail(target) });
        ar.targets.set(node.id, target);
      }
      Object.assign(target, { node, dist, az: bearing(ar.pos, node) });
      seen.add(node.id);
    }
    for (const [id, target] of ar.targets) {
      if (seen.has(id)) continue;
      target.label.remove();
      ar.targets.delete(id);
      if (ar.selected === target) select(null);
    }
    ar.sorted = [...ar.targets.values()].sort((a, b) => a.dist - b.dist);
    ar.sorted.forEach(renderLabel);
    renderSummary();
    runLos();
  }

  const shown = (target) => !settings.losOnly || target.los.status === "clear" || target.los.status === "marginal" || target === ar.selected;

  function renderLabel(target, index) {
    const los = LOS[target.los.status], node = target.node, label = target.label;
    const visible = index < MAX_LABELS && shown(target);
    if (!visible) { if (label.isConnected) label.remove(); return; }
    // Near: full size and sharp. Towards the edge of the range: smaller, fainter, blurred.
    const far = clamp(target.dist / settings.range, 0, 1), compact = far > 0.66 && target !== ar.selected;
    target.scale = 1 - 0.3 * far;
    const text = `${nodeName(node)}|${fmtDist(target.dist)}|${target.los.status}|${compact}|${node.type}`;
    if (text !== target.text) {
      target.text = text;
      label.replaceChildren(
        el("span", { html: shapeSvg(node.is_self ? "circle" : nodeMeta(node).shape) }),
        el("span", { style: "min-width:0" }, el("b", {}, compact ? `${nodeName(node)} · ${fmtDist(target.dist)}` : nodeName(node)),
          el("small", {}, el("i", { class: "ar-dot" }), `${fmtDist(target.dist)} · ${los.text}`)));
      label.setAttribute("aria-label", `${nodeMeta(node).name} ${nodeName(node)}, ${fmtDist(target.dist)} ${cardinal(target.az)}, ${los.text}`);
      target.w = 0;
    }
    label.className = `ar-label${compact ? " compact" : ""}${target.los.status === "blocked" ? " blocked" : ""}${target === ar.selected ? " sel" : ""}`;
    label.style.setProperty("--c", nodeColor(node));
    label.style.setProperty("--los", los.color);
    label.style.opacity = String(1 - 0.5 * far);
    label.style.filter = far > 0.3 ? `blur(${((far - 0.3) * 3.2).toFixed(2)}px)` : "";
    label.style.zIndex = target === ar.selected ? "" : String(MAX_LABELS - index);
    if (!label.isConnected) { label.style.visibility = "hidden"; ar.ui.labels.append(label); }
    if (!target.w) { target.w = label.offsetWidth; target.h = label.offsetHeight; }
  }

  function renderSummary() {
    const total = ar.sorted.length, clear = ar.sorted.filter((t) => t.los.status === "clear" || t.los.status === "marginal").length;
    const checked = ar.sorted.every((t) => t.los.status !== "pending");
    ar.ui.summary.textContent = !ar.pos ? "Waiting for location…" : !total ? "Nothing in range" : checked ? `Line of sight to ${clear} of ${total}` : `${plural(total, "node", "nodes")} in range…`;
    ar.ui.rangeText.textContent = `within ${fmtDist(settings.range)}`;
    ar.ui.range.setAttribute("aria-valuetext", fmtDist(settings.range));
  }

  // Where the path is looked at from: steady while the phone's fix wanders by a few metres.
  const losKey = (target) => `${ar.pos.lat.toFixed(4)},${ar.pos.lon.toFixed(4)}|${target.node.lat},${target.node.lon}|${settings.myHeight}|${settings.nodeHeight}|${ar.freq}`;

  async function runLos() {
    if (ar.losBusy) { ar.losAgain = true; return; }
    ar.losBusy = true;
    try {
      do {
        ar.losAgain = false;
        for (const target of [...ar.sorted]) {
          if (!ar.open || !ar.pos) return;
          const key = losKey(target);
          if (target.losKey === key || !ar.targets.has(target.id)) continue;
          const origin = { lat: ar.pos.lat, lon: ar.pos.lon };
          // Finer terrain for the paths where it matters, coarser for the long ones.
          const zoom = target.dist > 40000 ? 9 : 11, step = zoom === 9 ? 240 : 60;
          const heights = await terrain.profile(origin, target.node, clamp(Math.round(target.dist / step), 8, 320), zoom);
          if (!heights) { target.los = { status: "unknown" }; target.elev = null; }
          else {
            const h1 = heights[0] + settings.myHeight, h2 = heights[heights.length - 1] + settings.nodeHeight;
            target.los = { ...analyzePath(heights, target.dist, h1, h2, ar.freq), heights, h1, h2, dist: target.dist };
            target.elev = elevationAngle(target.dist, h1, h2);
            if (zoom === 11 || ar.ground === null) ar.ground = heights[0];
            target.losKey = key;
          }
          renderLabel(target, ar.sorted.indexOf(target));
          renderSummary();
          if (ar.selected === target && ar.sheet === "detail") renderDetail();
        }
      } while (ar.losAgain && ar.open);
    } finally {
      ar.losBusy = false;
    }
  }

  function select(target) {
    if (ar.selected === target) return;
    const previous = ar.selected;
    ar.selected = target;
    for (const t of [previous, target]) if (t && ar.targets.has(t.id)) renderLabel(t, ar.sorted.indexOf(t));
  }

  // --- Drawing -------------------------------------------------------------------------------

  function frame(now) {
    if (!ar.open) return;
    ar.raf = requestAnimationFrame(frame);
    const ctx = ar.ui.canvas.getContext("2d"), W = ar.w, H = ar.h;
    ctx.setTransform(ar.dpr, 0, 0, ar.dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const fresh = ar.raw && Date.now() - ar.rawAt < 3000;
    if (!fresh) {
      ar.basis = null;
      for (const target of ar.sorted) target.label.style.visibility = "hidden";
      ar.ui.guide.hidden = true;
      return;
    }
    // Follow the sensors with a short lag: steadier labels, still locked to the picture.
    const next = cameraBasis(ar.raw.alpha, ar.raw.beta, ar.raw.gamma, screenAngle(), ar.declination + settings.trim);
    const k = ar.basis ? 1 - Math.exp(-(now - ar.lastFrame) / 90) : 1;
    ar.lastFrame = now;
    if (!ar.basis) ar.basis = next;
    else for (const axis of ["right", "up", "fwd"]) for (let i = 0; i < 3; i++) ar.basis[axis][i] += (next[axis][i] - ar.basis[axis][i]) * k;
    for (const axis of ["right", "up", "fwd"]) {
      const v = ar.basis[axis], length = Math.hypot(v[0], v[1], v[2]) || 1;
      for (let i = 0; i < 3; i++) v[i] /= length;
    }
    const basis = ar.basis, f = focalPx(), cx = W / 2, cy = H / 2, heading = headingOf(basis);
    const project = (dir) => {
      const z = dot(dir, basis.fwd);
      return z > 0.05 ? { x: cx + (dot(dir, basis.right) / z) * f, y: cy - (dot(dir, basis.up) / z) * f } : null;
    };
    ar.ui.heading.textContent = `${Math.round(heading) % 360}° ${cardinal(heading)}`;

    // The horizon.
    const left = project(direction(heading - 25, 0)), right = project(direction(heading + 25, 0));
    if (left && right && Math.abs(right.x - left.x) > 1) {
      const slope = (right.y - left.y) / (right.x - left.x);
      ctx.strokeStyle = "rgba(255,255,255,.4)";
      ctx.lineWidth = 1;
      ctx.setLineDash([6, 6]);
      ctx.beginPath();
      ctx.moveTo(0, left.y - slope * left.x);
      ctx.lineTo(W, left.y + slope * (W - left.x));
      ctx.stroke();
      ctx.setLineDash([]);
    }
    drawCompass(ctx, heading, f, W);

    // Labels: nearest first, each lifted above the ones it would cover.
    const placed = [];
    let guide = null;
    ar.sorted.forEach((target, index) => {
      const label = target.label;
      if (!label.isConnected) return;
      const point = project(direction(target.az, target.elev === null ? elevationAngle(target.dist, 0, 0) : target.elev));
      const s = target === ar.selected ? 1 : target.scale, w = target.w * s, h = target.h * s;
      if (!point || point.x < -w / 2 || point.x > W + w / 2 || point.y < -20 || point.y > H + h) {
        label.style.visibility = "hidden";
        if (target === ar.selected) guide = target;
        return;
      }
      let top = point.y - 22 - h;
      const x0 = point.x - w / 2;
      for (let level = 0; level < 10 && placed.some((r) => x0 < r[0] + r[2] + 4 && x0 + w + 4 > r[0] && top < r[1] + r[3] + 3 && top + h + 3 > r[1]); level++) top -= h + 5;
      placed.push([x0, top, w, h]);
      label.style.visibility = "";
      label.style.transform = `translate3d(${(point.x - target.w / 2).toFixed(1)}px, ${(top + h - target.h).toFixed(1)}px, 0) scale(${s.toFixed(3)})`;
      const alpha = target === ar.selected ? 1 : 1 - 0.5 * clamp(target.dist / settings.range, 0, 1);
      ctx.globalAlpha = alpha;
      ctx.strokeStyle = "rgba(255,255,255,.75)";
      ctx.lineWidth = 1.25;
      ctx.beginPath();
      ctx.moveTo(point.x, point.y);
      ctx.lineTo(point.x, top + h);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(point.x, point.y, target === ar.selected ? 7 : 5, 0, Math.PI * 2);
      ctx.fillStyle = LOS[target.los.status].color;
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#05080c";
      ctx.stroke();
      ctx.globalAlpha = 1;
    });

    // The chosen node is out of the picture: say which way to turn.
    const ui = ar.ui;
    if (guide && !ar.aligning) {
      const turn = angleDiff(guide.az, heading), side = turn < 0 ? "left" : "right", text = `${Math.abs(Math.round(turn))}° · ${nodeName(guide.node)}`;
      if (ui.guide.dataset.text !== side + text) {
        ui.guide.dataset.text = side + text;
        ui.guide.className = `ar-guide ar-pill ${side}`;
        ui.guide.replaceChildren(el("span", { html: svg(SVG.turn), style: side === "right" ? "transform:scaleX(-1);display:grid" : "display:grid" }), el("span", {}, text));
        ui.guide.setAttribute("aria-label", `Turn ${side} ${Math.abs(Math.round(turn))} degrees for ${nodeName(guide.node)}`);
      }
      ui.guide.hidden = false;
    } else ui.guide.hidden = true;
    drawRadar(heading, f);
  }

  function drawCompass(ctx, heading, f, W) {
    const y = ar.ui.top.offsetHeight + 12, perDeg = f * DEG, span = Math.min(60, W / 2 / perDeg);
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.lineWidth = 1.5;
    for (let deg = Math.ceil((heading - span) / 5) * 5; deg <= heading + span; deg += 5) {
      const x = W / 2 + Math.tan((deg - heading) * DEG) * f, d = ((deg % 360) + 360) % 360, major = d % 45 === 0;
      ctx.strokeStyle = ctx.fillStyle = major ? "#fff" : "rgba(255,255,255,.6)";
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x, y + (major ? 12 : d % 15 === 0 ? 8 : 4));
      ctx.stroke();
      if (major || d % 15 === 0) {
        ctx.font = `${major ? "700 13px" : "11px"} system-ui, sans-serif`;
        ctx.shadowColor = "#000";
        ctx.shadowBlur = 4;
        ctx.fillText(major ? cardinal(d) : String(d), x, y + 14);
        ctx.shadowBlur = 0;
      }
    }
    ctx.fillStyle = "#4cc2ff";
    ctx.beginPath();
    ctx.moveTo(W / 2, y - 1);
    ctx.lineTo(W / 2 - 5, y - 8);
    ctx.lineTo(W / 2 + 5, y - 8);
    ctx.fill();
  }

  // What's around, seen from above, the way the phone points at the top.
  function drawRadar(heading, f) {
    const canvas = ar.ui.radar, ctx = canvas.getContext("2d"), size = canvas.width, c = size / 2, r = c - 10;
    ctx.clearRect(0, 0, size, size);
    ctx.strokeStyle = "rgba(255,255,255,.25)";
    ctx.lineWidth = 2;
    for (const ring of [1, 0.58]) { ctx.beginPath(); ctx.arc(c, c, r * ring, 0, Math.PI * 2); ctx.stroke(); }
    const half = Math.atan(ar.w / 2 / f);
    ctx.fillStyle = "rgba(76,194,255,.28)";
    ctx.beginPath();
    ctx.moveTo(c, c);
    ctx.arc(c, c, r, -Math.PI / 2 - half, -Math.PI / 2 + half);
    ctx.fill();
    const north = -heading * DEG;
    ctx.fillStyle = "#fff";
    ctx.font = "700 20px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("N", c + Math.sin(north) * (r - 12), c - Math.cos(north) * (r - 12));
    for (const target of [...ar.sorted].reverse()) {
      if (!shown(target)) continue;
      const angle = (target.az - heading) * DEG, rr = Math.sqrt(target.dist / settings.range) * (r - 6);
      ctx.beginPath();
      ctx.arc(c + Math.sin(angle) * rr, c - Math.cos(angle) * rr, target === ar.selected ? 9 : 6, 0, Math.PI * 2);
      ctx.fillStyle = LOS[target.los.status].color;
      ctx.fill();
      if (target === ar.selected) { ctx.strokeStyle = "#fff"; ctx.lineWidth = 3; ctx.stroke(); }
    }
    ctx.fillStyle = "#fff";
    ctx.beginPath();
    ctx.arc(c, c, 5, 0, Math.PI * 2);
    ctx.fill();
  }

  // --- Once a second: the facts line, what's in the way of a good picture -----------------------

  function tick() {
    if (!ar.open) return;
    const ui = ar.ui, pos = ar.pos;
    if (pos) {
      const ground = ar.ground !== null ? ar.ground : pos.altitude;
      ui.fix.textContent = [`±${Math.round(pos.accuracy)} m`, ground !== null && ground !== undefined ? `${Math.round(ground + settings.myHeight)} m asl` : null,
        `antenna ${settings.myHeight} m`].filter(Boolean).join(" · ");
    } else ui.fix.textContent = "Waiting for location…";
    refreshTargets();
    if (ar.selected && ar.sheet === "detail") renderDetail();
    const waited = Date.now() - ar.startedAt > 3000;
    let notice = null;
    if (ar.motion !== "granted" && !ar.raw) notice = "Motion sensor access was refused, so the view can't follow the phone. Close the AR view, reload the page and allow \"Motion & Orientation Access\".";
    else if (ar.geoError && !pos) notice = ar.geoError.code === 1 ? "Location access was refused. Allow it for this site in the browser's settings." : "No location fix yet. It works best outdoors.";
    else if (!ar.raw && waited) notice = "This device isn't reporting which way it points (no compass or motion sensor).";
    else if (ar.cameraError) notice = ar.cameraError.name === "NotAllowedError" ? "Camera access was refused; the labels are shown without the picture." : "The camera couldn't be started; the labels are shown without the picture.";
    else if (ar.raw && !ar.absolute) notice = "No compass on this device: line the view up yourself (settings › Align compass).";
    else if (ar.compassAccuracy > 25 || ar.compassAccuracy < 0) notice = "The compass is unsure. Move the phone in a figure of eight, away from metal.";
    else if (pos && pos.accuracy > 100) notice = `Location is only good to ±${Math.round(pos.accuracy)} m: directions to near nodes are rough.`;
    else if (pos && !ar.sorted.length) notice = [...state.nodes.values()].some(hasPos) ? `Nothing of the chosen kinds within ${fmtDist(settings.range)}. Widen the range below.` : "No node on the map has a position yet.";
    else if (pos && ar.sorted.length && ar.sorted.every((t) => t.los.status === "unknown")) notice = "Terrain data can't be loaded (offline?): directions are shown, heights and line of sight aren't.";
    ui.notice.hidden = !notice || ar.aligning || Boolean(ar.sheet);
    if (notice) ui.notice.textContent = notice;
  }

  // --- Sheets --------------------------------------------------------------------------------

  function openSheet(name, title, body) {
    ar.sheet = name;
    const ui = ar.ui;
    ui.sheetBody = el("div", { class: "ar-sheet-body" }, body);
    ui.sheetTitle = el("h2", {}, title);
    ui.sheet.replaceChildren(el("div", { class: "ar-sheet-head" }, ui.sheetTitle,
      el("button", { class: "ar-btn", type: "button", "aria-label": "Close", html: icon("x"), onclick: closeSheet })), ui.sheetBody);
    ui.sheet.hidden = false;
    ui.bottom.hidden = true;
    ui.notice.hidden = true;
    ui.listBtn.setAttribute("aria-pressed", String(name === "list"));
    ui.settingsBtn.setAttribute("aria-pressed", String(name === "settings"));
  }

  function closeSheet() {
    if (!ar.sheet) return;
    ar.sheet = null;
    const ui = ar.ui;
    ui.sheet.hidden = true;
    ui.sheet.replaceChildren();
    ui.bottom.hidden = ar.aligning;
    ui.listBtn.setAttribute("aria-pressed", "false");
    ui.settingsBtn.setAttribute("aria-pressed", "false");
  }

  function toggleSheet(name) {
    if (ar.sheet === name) { closeSheet(); return; }
    setAligning(false);
    if (name === "list") showList(); else showSettings();
  }

  function showList() {
    const rows = ar.sorted.map((target) => {
      const los = LOS[target.los.status];
      return el("li", {}, el("button", { class: "ar-row", type: "button", style: `--c:${nodeColor(target.node)};--los:${los.color}`, onclick: () => showDetail(target) },
        el("span", { html: shapeSvg(target.node.is_self ? "circle" : nodeMeta(target.node).shape), style: "display:grid" }),
        el("span", { class: "ar-row-main" }, el("b", {}, nodeName(target.node)),
          el("small", {}, `${fmtDist(target.dist)} · ${Math.round(target.az)}° ${cardinal(target.az)}`)),
        el("span", { class: "ar-badge" }, los.short)));
    });
    openSheet("list", `${plural(rows.length, "node", "nodes")} within ${fmtDist(settings.range)}`,
      rows.length ? el("ul", { class: "ar-list" }, rows) : el("p", { class: "ar-note" }, ar.pos ? "Nothing in range. Widen the range or show more kinds of nodes." : "Waiting for the phone's location…"));
  }

  function showDetail(target) {
    setAligning(false);
    select(target);
    openSheet("detail", nodeName(target.node), []);
    renderDetail();
  }

  function renderDetail() {
    const target = ar.selected, body = ar.ui.sheetBody;
    if (!target || !ar.targets.has(target.id)) { closeSheet(); return; }
    const node = target.node, los = target.los, meta = LOS[los.status], scroll = body.scrollTop;
    const fact = (name, value) => el("div", {}, el("dt", {}, name), el("dd", {}, value));
    const facts = [
      fact("Distance", fmtDist(target.dist)),
      fact("Bearing", `${Math.round(target.az)}° ${cardinal(target.az)}`),
      target.elev !== null ? fact("Elevation angle", `${fmtSigned(target.elev, 1)}°`) : null,
      los.heights ? fact("Ground there / here", `${Math.round(los.h2 - settings.nodeHeight)} m / ${Math.round(los.h1 - settings.myHeight)} m`) : null,
      los.clearance !== null && los.clearance !== undefined ? fact(los.clearance < 0 ? "Terrain above the path" : "Least clearance", `${Math.abs(Math.round(los.clearance))} m, ${fmtDist(los.at)} from you`) : null,
      los.fresnel !== null && los.fresnel !== undefined ? fact("Fresnel zone free", `${Math.round(clamp(los.fresnel, 0, 1) * 100)}%`) : null,
      fact("Free-space loss", `${Math.round(freeSpaceLoss(target.dist, ar.freq))} dB at ${Math.round(ar.freq)} MHz`),
      node.last_heard ? fact("Heard directly", [fmtAgo(node.last_heard), node.last_snr != null ? `SNR ${node.last_snr} dB` : null].filter(Boolean).join(" · ")) : null,
      node.last_seen ? fact("Last packet", fmtAgo(node.last_seen)) : null,
    ];
    const verdict = {
      clear: "Nothing in the way: a link from here should work if the radios reach that far.",
      marginal: "The straight line is free, but terrain reaches into the Fresnel zone: expect a weaker link. More height here would help.",
      blocked: "Terrain blocks the straight path. A link would need to bend over it (much weaker) or go through another repeater.",
      unknown: "Terrain data couldn't be loaded, so only the direction is known.",
      pending: "Checking the terrain along the path…",
    }[los.status];
    const profile = los.heights ? el("canvas", { class: "ar-profile", role: "img", "aria-label": `Terrain profile to ${nodeName(node)}: ${meta.text}` }) : null;
    body.replaceChildren(
      el("div", { class: "ar-actions", style: `--los:${meta.color}` }, el("span", { class: "ar-badge" }, el("i", { class: "ar-dot" }), meta.text),
        el("span", { class: "ar-note", style: "align-self:center" }, node.is_self ? "This radio" : nodeMeta(node).name)),
      el("p", { class: "ar-note" }, verdict), profile, el("dl", { class: "ar-facts" }, facts),
      el("p", { class: "ar-note" }, `Assumes your antenna ${settings.myHeight} m and theirs ${settings.nodeHeight} m above the ground (change both in settings). Buildings and trees aren't in the terrain model.`),
      el("div", { class: "ar-actions" }, el("button", { class: "ar-action", type: "button", html: icon("map"), onclick: () => showOnMap(node) }, "Show on map")));
    body.scrollTop = scroll;
    if (profile) drawProfile(profile, los);
  }

  // The path from the side: the ground (with the Earth's curve added), the straight line between
  // the antennas and the edge of the Fresnel zone that has to stay free.
  function drawProfile(canvas, los) {
    const dpr = Math.min(window.devicePixelRatio || 1, 3), W = canvas.clientWidth || 300, H = canvas.clientHeight || 120;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    const ctx = canvas.getContext("2d"), n = los.heights.length - 1, lambda = 299.792458 / ar.freq;
    ctx.scale(dpr, dpr);
    const ground = Array.from(los.heights, (h, i) => h + earthBulge((i / n) * los.dist, los.dist - (i / n) * los.dist));
    const lo = Math.min(...ground), hi = Math.max(...ground, los.h1, los.h2), pad = 8;
    const X = (i) => pad + ((W - pad * 2) * i) / n, Y = (h) => H - pad - ((H - pad * 2 - 6) * (h - lo)) / Math.max(1, hi - lo);
    const line = (i) => los.h1 + ((los.h2 - los.h1) * i) / n;
    ctx.beginPath();
    ctx.moveTo(X(0), H);
    ground.forEach((h, i) => ctx.lineTo(X(i), Y(h)));
    ctx.lineTo(X(n), H);
    ctx.fillStyle = "rgba(154,167,181,.45)";
    ctx.fill();
    ctx.beginPath();
    for (let i = 0; i <= n; i++) {
      const d1 = (i / n) * los.dist, y = Y(line(i) - FRESNEL_CLEAR * Math.sqrt((lambda * d1 * (los.dist - d1)) / los.dist));
      if (i) ctx.lineTo(X(i), y); else ctx.moveTo(X(i), y);
    }
    ctx.strokeStyle = "rgba(255,255,255,.4)";
    ctx.setLineDash([3, 4]);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(X(0), Y(los.h1));
    ctx.lineTo(X(n), Y(los.h2));
    ctx.strokeStyle = LOS[los.status].color;
    ctx.lineWidth = 2;
    ctx.stroke();
    if (los.index > 0 && los.status !== "clear") {
      ctx.beginPath();
      ctx.arc(X(los.index), Y(ground[los.index]), 4, 0, Math.PI * 2);
      ctx.fillStyle = LOS[los.status].color;
      ctx.fill();
    }
    ctx.fillStyle = "#c9d3de";
    ctx.font = "11px system-ui, sans-serif";
    ctx.textBaseline = "top";
    ctx.fillText("you", pad, 4);
    ctx.textAlign = "right";
    ctx.fillText(`${Math.round(hi)} m`, W - pad, 4);
  }

  function showOnMap(node) {
    close();
    location.hash = "#map";
    const entry = markers.get(node.id);
    map.setView([node.lat, node.lon], Math.max(map.getZoom(), 13));
    if (entry) setTimeout(() => entry.marker.openPopup(), 300);
  }

  function showSettings() {
    const field = (label, min, max, step, key, format, note) => {
      const output = el("output", {}, format(settings[key]));
      const input = el("input", { type: "range", min, max, step, value: settings[key], "aria-label": label });
      input.addEventListener("input", () => {
        settings[key] = Number(input.value);
        output.textContent = format(settings[key]);
        save();
        if (key !== "fov") refreshTargets();
      });
      return el("label", { class: "ar-field" }, el("span", { class: "ar-field-head" }, el("span", {}, label), output), input, note ? el("span", { class: "ar-note" }, note) : null);
    };
    const metres = (v) => `${v} m`;
    openSheet("settings", "AR settings", [
      field("Your antenna above the ground", 0, 30, 0.5, "myHeight", metres, "Raise it to see what a mast or a roof here would reach."),
      field("Their antennas above the ground", 0, 50, 1, "nodeHeight", metres, "Nodes don't report a height: an assumption for all of them."),
      field("Camera field of view", 35, 110, 1, "fov", (v) => `${v}°`, "If labels drift off their place as you turn, the camera sees wider or narrower than assumed."),
      el("div", { class: "ar-field" },
        el("span", { class: "ar-field-head" }, el("span", {}, "Compass"), el("output", {}, `trim ${fmtSigned(settings.trim, 0)}°`)),
        el("span", { class: "ar-note" }, `Magnetic declination here (${fmtSigned(ar.declination, 1)}°) is corrected for. Phone compasses are often some degrees off: line the labels up with a node you can see.`),
        el("div", { class: "ar-actions" }, el("button", { class: "ar-action", type: "button", onclick: () => { closeSheet(); setAligning(true); } }, "Align compass"))),
      el("p", { class: "ar-note", html: 'Terrain heights: <a href="https://github.com/tilezen/joerd/blob/master/docs/attribution.md" target="_blank" rel="noopener">Mapzen terrain tiles</a> on AWS (about 30–250 m per pixel); the phone asks for the tiles between you and the nodes. Line of sight is over bare ground, with standard (4/3 Earth) refraction.' }),
    ]);
  }

  function setAligning(on) {
    if (ar.aligning === on || !ar.dialog) return;
    ar.aligning = on;
    const ui = ar.ui;
    ui.align.hidden = ui.crosshair.hidden = !on;
    ui.bottom.hidden = on || Boolean(ar.sheet);
    if (on) renderAlign(); else save();
  }

  function renderAlign() { ar.ui.alignText.textContent = `Trim ${fmtSigned(settings.trim, 1)}°`; }

  // --- The button on the map: phones and tablets only ----------------------------------------

  const touchDevice = navigator.maxTouchPoints > 0 && window.matchMedia("(pointer: coarse)").matches;
  if (touchDevice && typeof DeviceOrientationEvent !== "undefined") {
    const controls = document.querySelector(".map-controls");
    if (controls) {
      controls.append(el("div", { class: "control-group glass" },
        el("button", { class: "ar-open-btn", type: "button", id: "ar-open", "aria-label": "AR view: find nodes with the camera", title: "AR view", html: svg(SVG.ar), onclick: open })));
    }
  }

  return { ...pure, open, close, settings };
})();
if (typeof module !== "undefined") module.exports = MeshgramAR;
