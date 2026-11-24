// LoRaWAN network map of the Milesight Gateway integration: each gateway
// entry with the LoRaWAN data source, every gateway of its network server
// (silent fleet gateways too) and the devices. Each uplink reception is a line
// labelled with RSSI and SNR and coloured by link quality. Data comes from the
// integration's own milesight_gateway/network command, which any logged-in
// user may read.
//
// One file, two elements: the sidebar panel and the dashboard card
// custom:milesight-lorawan-map.
//
// Rendering keeps one SVG element per node and link and only moves them; the
// layout settles a few steps per animation frame, so new data glides in.

// Uplinks arrive every 10 s at most (the packet log poll); polling the
// integration as often costs one in-memory read.
const REFRESH_MS = 10000;
const SVG = "http://www.w3.org/2000/svg";
// Below this zoom the link labels are unreadable anyway; hiding them keeps
// panning smooth with hundreds of devices.
const LABEL_MIN_SCALE = 0.45;
const NAME_MIN_SCALE = 0.3;
const SCALE_KEY = "milesight-lorawan-map-scale";

// Worst of RSSI and SNR class wins. RSSI in dBm, SNR in dB.
const QUALITY = [
  { name: "good", color: "#43a047", rssi: -100, snr: 0 },
  { name: "fair", color: "#fdd835", rssi: -110, snr: -5 },
  { name: "weak", color: "#fb8c00", rssi: -120, snr: -10 },
  { name: "bad", color: "#e53935", rssi: -Infinity, snr: -Infinity },
];

function qualityOf(rssi, snr) {
  const level = (value, key) =>
    value == null ? 0 : QUALITY.findIndex((q) => value >= q[key]);
  return QUALITY[Math.max(level(rssi, "rssi"), level(snr, "snr"))];
}

function linkText(rssi, snr) {
  const parts = [];
  if (rssi != null) parts.push(`${Math.round(rssi)} dBm`);
  if (snr != null) parts.push(`${snr} dB`);
  return parts.join(" · ");
}

function el(name, attrs = {}, parent = null) {
  const node = document.createElementNS(SVG, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (parent) parent.appendChild(node);
  return node;
}

class MilesightLorawanNetworkPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._connections = [];
    this._config = {};
    this._nodes = new Map(); // id -> node, kept across updates (positions)
    this._links = [];
    this._nodeEls = new Map();
    this._linkEls = new Map();
    this._view = { k: 1, x: 0, y: 0 };
    this._userView = false;
    this._alpha = 0;
    this._frame = null;
    this._moved = new Map(); // dragged connections and gateways: id -> {x, y}
    this._dragNode = null;
    this._stats = { frames: 0, since: 0, fps: 0, tickMs: 0, updateMs: 0 };
    // Stem lengths from the sliders: hub to gateway, gateway to device.
    this._scale = { ring: 1, stem: 1.5 };
    try {
      Object.assign(this._scale, JSON.parse(localStorage.getItem(SCALE_KEY)) || {});
    } catch (_err) {
      // No storage (private window): the defaults hold.
    }
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._start();
  }

  set narrow(_value) {}
  set panel(_value) {}

  async _start() {
    this._renderShell();
    await this._load();
    this._timer = setInterval(() => this._load(), REFRESH_MS);
  }

  disconnectedCallback() {
    if (this._resize) this._resize.disconnect();
    if (this._frame) cancelAnimationFrame(this._frame);
    if (this._meterFrame) cancelAnimationFrame(this._meterFrame);
    clearInterval(this._timer);
    this._timer = null;
    // The next hass assignment starts it again when it is re-attached.
    this._hass = null;
  }

  async _load() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({ type: "milesight_gateway/network" });
      const only = this._config && this._config.gateway;
      this._connections = result.connections.filter(
        (c) => !only || c.entry_id === only || c.title === only
      );
      this._error = this._connections
        .filter((c) => c.error)
        .map((c) => `${c.title}: ${c.error}`)
        .join("; ");
    } catch (err) {
      this._error = err.message || String(err);
    }
    this._update();
  }

  // --- graph --------------------------------------------------------------

  _graph() {
    const nodes = new Map();
    const links = [];
    const add = (node) => {
      if (!nodes.has(node.id)) nodes.set(node.id, node);
      return nodes.get(node.id);
    };
    const connections = this._connections || [];
    for (const conn of connections) {
      add({ id: `c:${conn.entry_id}`, kind: "conn", label: conn.title, group: conn.entry_id });
    }
    for (const conn of connections) {
      for (const gw of conn.gateways) {
        add({
          id: `g:${gw.id}`, kind: "gw", label: gw.name || gw.id, sub: gw.name ? gw.id : "",
          online: gw.connected !== false, lastSeen: gw.last_seen, group: conn.entry_id,
        });
        links.push({ source: `c:${conn.entry_id}`, target: `g:${gw.id}`, kind: "backhaul", online: gw.connected !== false });
      }
    }
    const devices = connections.flatMap((conn) =>
      conn.devices.map((dev) => ({ ...dev, group: conn.entry_id }))
    );
    for (const dev of devices) {
      const group = dev.group;
      const id = `d:${group}:${dev.dev_eui}`;
      add({ id, kind: "dev", label: dev.name, sub: dev.model, group, lastUplink: dev.last_uplink });
      if (!dev.receptions.length) {
        links.push({ source: `c:${group}`, target: id, kind: "none" });
        continue;
      }
      // The strongest reception carries colour, label and position; the
      // others stay faint, or a device heard by every gateway crosses the ring.
      const best = dev.receptions.reduce((a, b) =>
        (b.rssi ?? -999) + (b.snr ?? 0) / 10 > (a.rssi ?? -999) + (a.snr ?? 0) / 10 ? b : a
      );
      for (const rx of dev.receptions) {
        const gwId = `g:${rx.gateway_id}`;
        if (!nodes.has(gwId)) {
          // Heard by a gateway the connection did not list (other stacks).
          add({ id: gwId, kind: "gw", label: rx.gateway_id, sub: "", online: true, group });
          links.push({ source: `c:${group}`, target: gwId, kind: "backhaul", online: true });
        }
        links.push({ source: gwId, target: id, kind: "uplink", rssi: rx.rssi, snr: rx.snr, quality: qualityOf(rx.rssi, rx.snr), best: rx === best });
      }
    }
    return { nodes: [...nodes.values()], links: links.filter((l) => nodes.has(l.source) && nodes.has(l.target)) };
  }

  // --- layout -------------------------------------------------------------

  // A fixed frame: each connection in the middle of its cluster, its gateways
  // evenly on a ring around it, devices outside the ring next to the gateways
  // that hear them, devices without reception inside. Only devices move.
  _merge(graph) {
    const previous = this._nodes;
    this._nodes = new Map();
    for (const fresh of graph.nodes) {
      const node = previous.get(fresh.id);
      this._nodes.set(fresh.id, node ? Object.assign(node, fresh, { x: node.x, y: node.y }) : fresh);
    }
    this._links = graph.links;
    this._heardBy = new Map();
    for (const link of this._links) {
      if (link.kind !== "uplink") continue;
      if (!this._heardBy.has(link.target)) this._heardBy.set(link.target, []);
      this._heardBy.get(link.target).push(link);
    }

    const all = [...this._nodes.values()];
    const groups = [...new Set(all.filter((n) => n.kind === "conn").map((n) => n.group))];
    for (const node of all) if (!groups.includes(node.group)) groups.push(node.group);
    this._frameOf = new Map();
    const before = new Map(all.filter((n) => n.kind === "gw").map((n) => [n.id, { x: n.x, y: n.y }]));
    let left = 0;
    for (const group of groups) {
      const count = all.filter((n) => n.group === group && n.kind === "dev").length;
      const ring = (160 + 25 * Math.sqrt(count)) * this._scale.ring;
      const outer = ring + 250 * this._scale.stem;
      const center = { x: left + outer + 120, y: 0 };
      this._frameOf.set(group, { center, ring, outer });
      left = center.x + outer + 420; // room for labels to the right
      const conn = this._nodes.get(`c:${group}`);
      if (conn) Object.assign(conn, center);
      const gateways = all
        .filter((n) => n.kind === "gw" && n.group === group)
        .sort((a, b) => a.label.localeCompare(b.label));
      gateways.forEach((gw, i) => {
        // One gateway sits to the right; several start at the top.
        const angle = gateways.length === 1 ? 0 : -Math.PI / 2 + (2 * Math.PI * i) / gateways.length;
        gw.x = center.x + ring * Math.cos(angle);
        gw.y = center.y + ring * Math.sin(angle);
      });
    }

    let fresh = 0;
    this._devices = all.filter((n) => n.kind === "dev");
    // Gateways moved (the named list arrived after the first receptions, one
    // was added, or the hub stem changed): a device moves along with its
    // strongest gateway; one without a placed gateway starts anew.
    // Taken before dragged positions apply, so a drag keeps the devices.
    const frame = all
      .filter((n) => n.kind !== "dev")
      .map((n) => `${n.id}@${Math.round(n.x)},${Math.round(n.y)}`)
      .sort()
      .join("|");
    if (frame !== this._frameKey) {
      this._frameKey = frame;
      for (const node of this._devices) {
        if (node.pinned) continue;
        const old = node.bestGw && before.get(node.bestGw);
        const gw = node.bestGw && this._nodes.get(node.bestGw);
        if (old && gw && old.x !== undefined && node.x !== undefined) {
          node.x += gw.x - old.x;
          node.y += gw.y - old.y;
        } else {
          node.x = undefined;
        }
      }
    }
    for (const [id, position] of this._moved) {
      const node = this._nodes.get(id);
      if (node) Object.assign(node, position);
    }
    this._clusterSize = new Map();
    for (const node of this._devices) {
      const strongest = (this._heardBy.get(node.id) || []).find((l) => l.best);
      const bestGw = strongest ? strongest.source : null;
      if (bestGw) this._clusterSize.set(bestGw, (this._clusterSize.get(bestGw) || 0) + 1);
      if (node.bestGw !== bestGw) {
        node.bestGw = bestGw;
        if (!node.pinned) node.x = undefined;
      }
      if (node.x !== undefined) continue;
      fresh += 1;
      const { center, ring, outer } = this._frameOf.get(node.group);
      const links = this._heardBy.get(node.id) || [];
      if (links.length) {
        // Start just outside the strongest gateway.
        const best = links.find((l) => l.best) || links[0];
        const gw = this._nodes.get(best.source);
        const dx = gw.x - center.x;
        const dy = gw.y - center.y;
        const d = Math.hypot(dx, dy) || 1;
        node.x = center.x + (dx / d) * outer + (Math.random() - 0.5) * 120;
        node.y = center.y + (dy / d) * outer + (Math.random() - 0.5) * 120;
      } else {
        const angle = Math.random() * 2 * Math.PI;
        node.x = center.x + Math.cos(angle) * ring * 0.45;
        node.y = center.y + Math.sin(angle) * ring * 0.45;
      }
      node.vx = 0;
      node.vy = 0;
    }
    return fresh;
  }

  _step() {
    const alpha = this._alpha;
    const devices = this._devices;
    for (let a = 0; a < devices.length; a += 1) {
      const na = devices[a];
      for (let b = a + 1; b < devices.length; b += 1) {
        const nb = devices[b];
        let dx = na.x - nb.x;
        let dy = na.y - nb.y;
        let d2 = dx * dx + dy * dy;
        if (d2 > 160000) continue;
        if (d2 < 1) { dx = Math.random(); dy = Math.random(); d2 = 1; }
        const force = (20000 / d2) * alpha;
        const d = Math.sqrt(d2);
        na.vx += (dx / d) * force; na.vy += (dy / d) * force;
        nb.vx -= (dx / d) * force; nb.vy -= (dy / d) * force;
      }
    }
    for (const node of devices) {
      const { center, ring } = this._frameOf.get(node.group);
      const links = this._heardBy.get(node.id) || [];
      // Towards each gateway that hears it.
      for (const link of links) {
        const gw = this._nodes.get(link.source);
        const dx = gw.x - node.x;
        const dy = gw.y - node.y;
        const d = Math.hypot(dx, dy) || 1;
        // The strongest gateway holds its devices in a cluster that widens
        // with its size; the others barely pull.
        const length = (110 + 30 * Math.sqrt(this._clusterSize.get(link.source) || 1)) * this._scale.stem;
        const pull = ((d - length) / d) * (link.best ? 0.09 : 0.002) * alpha;
        node.vx += dx * pull; node.vy += dy * pull;
      }
      const dx = node.x - center.x;
      const dy = node.y - center.y;
      const d = Math.hypot(dx, dy) || 1;
      if (links.length) {
        // Heard devices stay outside the ring, clear of the hub's lines.
        const min = ring + 60;
        if (d < min) {
          const push = ((min - d) / d) * 0.2 * alpha;
          node.vx += dx * push; node.vy += dy * push;
        }
      } else {
        // Unheard devices gather inside, near the hub.
        const radial = ((ring * 0.45 - d) / d) * 0.06 * alpha;
        node.vx += dx * radial; node.vy += dy * radial;
      }
      // A dragged device stays where it was put.
      if (!node.pinned && node !== this._dragNode) {
        node.x += Math.max(-30, Math.min(30, node.vx));
        node.y += Math.max(-30, Math.min(30, node.vy));
      }
      node.vx *= 0.6;
      node.vy *= 0.6;
    }
    this._alpha *= 0.97;
  }

  // --- rendering ------------------------------------------------------------

  _renderShell() {
    // A second render (new card config) starts from fresh elements.
    if (this._meterFrame) cancelAnimationFrame(this._meterFrame);
    if (this._resize) this._resize.disconnect();
    this._nodeEls = new Map();
    this._linkEls = new Map();
    // The card lives in a dashboard grid: own height, card look, less chrome.
    const card = this._isCard;
    const config = this._config || {};
    const height = card ? config.height || "450px" : "100vh";
    const title = card ? config.title ?? "LoRaWAN network" : "LoRaWAN network";
    this._labels = config.labels !== false;
    this.shadowRoot.innerHTML = `
      <style>
        :host { --map-bg: ${card ? "var(--ha-card-background, var(--card-background-color))" : "var(--primary-background-color)"}; display: flex; flex-direction: column; height: ${height}; background: var(--map-bg); color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); ${card ? "border-radius: var(--ha-card-border-radius, 12px); border: var(--ha-card-border-width, 1px) solid var(--ha-card-border-color, var(--divider-color)); overflow: hidden;" : ""} }
        .card-only, .panel-only { display: none; }
        ${card ? ".card-only { display: inline; }" : ".panel-only { display: inline; }"}
        header { display: flex; flex-wrap: wrap; align-items: center; gap: 16px; padding: 12px 16px; border-bottom: 1px solid var(--divider-color); }
        h1 { font-size: 20px; font-weight: 400; margin: 0 16px 0 0; }
        .legend { display: flex; gap: 12px; font-size: 13px; color: var(--secondary-text-color); align-items: center; flex-wrap: wrap; }
        .swatch { display: inline-block; width: 18px; height: 4px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }
        .spacer { flex: 1; }
        .controls { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
        .controls label { display: inline-flex; align-items: center; gap: 6px; }
        .meta, label { font-size: 13px; color: var(--secondary-text-color); }
        .error { color: var(--error-color, #db4437); font-size: 13px; width: 100%; }
        #canvas { flex: 1; min-height: 0; width: 100%; cursor: grab; touch-action: none; user-select: none; }
        #canvas.dragging { cursor: grabbing; }
        #viewport { will-change: transform; }
        text { fill: var(--primary-text-color); font-size: 13px; pointer-events: none; }
        text.sub { fill: var(--secondary-text-color); font-size: 12px; }
        text.link { font-size: 11px; paint-order: stroke; stroke: var(--map-bg); stroke-width: 4px; }
        .no-links text.link, .no-names text { display: none; }
        line { pointer-events: stroke; }
        .node { cursor: move; }
        .node.pinned circle { stroke: var(--primary-text-color); stroke-width: 2; }
        #fps { font-variant-numeric: tabular-nums; min-width: 230px; }
      </style>
      <header>
        ${title ? `<h1>${title.replace(/[<>&]/g, "")}</h1>` : ""}
        <div class="legend">${QUALITY.map((q) => `<span><span class="swatch" style="background:${q.color}"></span>${q.name}</span>`).join("")}
          <span class="panel-only">good: RSSI ≥ −100 dBm and SNR ≥ 0 dB</span></div>
        <span class="spacer"></span>
        <div class="controls">
          <label title="Length of the stems from the hub to its gateways">Hub stem <input type="range" id="ring" min="0.3" max="4" step="0.1" value="${this._scale.ring}"></label>
          <label title="Length of the stems from a gateway to its devices">Device stem <input type="range" id="stem" min="0.3" max="4" step="0.1" value="${this._scale.stem}"></label>
          <label><input type="checkbox" id="labels" ${this._labels ? "checked" : ""}> RSSI labels</label>
          <button id="fit">Fit</button>
          <button id="reset" title="Forget dragged positions">Reset layout</button>
        </div>
        <span class="meta panel-only" id="fps"></span>
        <span class="meta panel-only" id="meta"></span>
        <div class="error" id="error"></div>
      </header>
      <svg id="canvas" xmlns="${SVG}"><g id="viewport"><g id="links"></g><g id="linklabels"></g><g id="nodes"></g></g></svg>`;
    const root = this.shadowRoot;
    this._svg = root.getElementById("canvas");
    this._viewport = root.getElementById("viewport");
    this._linkLayer = root.getElementById("links");
    this._labelLayer = root.getElementById("linklabels");
    this._nodeLayer = root.getElementById("nodes");
    root.getElementById("labels").addEventListener("change", (ev) => {
      this._labels = ev.target.checked;
      this._applyView();
    });
    const slider = (id, apply) =>
      root.getElementById(id).addEventListener("input", (ev) => {
        this._scale[id] = Number(ev.target.value);
        try {
          localStorage.setItem(SCALE_KEY, JSON.stringify(this._scale));
        } catch (_err) {
          // Not remembered, still applied.
        }
        apply();
      });
    // The hub stem moves the gateways, and their devices with them.
    slider("ring", () => this._update());
    // The device stem only changes the springs: let the layout settle again.
    slider("stem", () => {
      this._alpha = Math.max(this._alpha, 0.5);
      this._refit = true;
      this._animate();
    });
    root.getElementById("fit").addEventListener("click", () => {
      this._userView = false;
      this._fit();
    });
    root.getElementById("reset").addEventListener("click", () => {
      this._moved.clear();
      for (const node of this._nodes.values()) node.pinned = false;
      this._frameKey = null; // place every device anew
      this._update();
    });
    this._fpsEl = root.getElementById("fps");
    this._meter();
    this._bindPanZoom();
    this._resize = new ResizeObserver(() => {
      if (!this._userView) this._fit();
    });
    this._resize.observe(this._svg);
  }

  // Frames the browser draws per second, and what one layout step and one
  // data update cost. Updated twice a second.
  _meter() {
    const loop = (now) => {
      const stats = this._stats;
      stats.frames += 1;
      if (!stats.since) stats.since = now;
      if (now - stats.since >= 500) {
        stats.fps = (stats.frames * 1000) / (now - stats.since);
        stats.frames = 0;
        stats.since = now;
        const state = this._alpha >= 0.005 ? "settling" : "idle";
        this._fpsEl.textContent =
          `${stats.fps.toFixed(0)} fps · step ${stats.tickMs.toFixed(1)} ms · update ${stats.updateMs.toFixed(1)} ms · ${state}`;
      }
      this._meterFrame = requestAnimationFrame(loop);
    };
    this._meterFrame = requestAnimationFrame(loop);
  }

  _toGraph(ev) {
    const rect = this._svg.getBoundingClientRect();
    return {
      x: (ev.clientX - rect.left - this._view.x) / this._view.k,
      y: (ev.clientY - rect.top - this._view.y) / this._view.k,
    };
  }

  _bindPanZoom() {
    const svg = this._svg;
    let drag = null;
    svg.addEventListener("pointerdown", (ev) => {
      const hit = ev.target.closest && ev.target.closest("[data-id]");
      const node = hit && this._nodes.get(hit.getAttribute("data-id"));
      if (node) {
        // Drag a node; the rest of its cluster follows the springs.
        this._dragNode = node;
        drag = { node };
      } else {
        drag = { x: ev.clientX, y: ev.clientY, view: { ...this._view } };
      }
      svg.setPointerCapture(ev.pointerId);
      svg.classList.add("dragging");
    });
    svg.addEventListener("pointermove", (ev) => {
      if (!drag) return;
      if (drag.node) {
        const { x, y } = this._toGraph(ev);
        const node = drag.node;
        node.x = x;
        node.y = y;
        if (node.kind === "dev") {
          node.pinned = true;
          this._nodeEls.get(node.id)?.g.classList.add("pinned");
        } else {
          this._moved.set(node.id, { x, y });
        }
        this._alpha = Math.max(this._alpha, 0.3);
        this._draw();
        this._animate();
        return;
      }
      this._userView = true;
      this._view.x = drag.view.x + (ev.clientX - drag.x);
      this._view.y = drag.view.y + (ev.clientY - drag.y);
      this._requestView();
    });
    svg.addEventListener("dblclick", (ev) => {
      // Let a dragged node go back to where the layout wants it.
      const hit = ev.target.closest && ev.target.closest("[data-id]");
      const node = hit && this._nodes.get(hit.getAttribute("data-id"));
      if (!node) return;
      node.pinned = false;
      this._nodeEls.get(node.id)?.g.classList.remove("pinned");
      if (this._moved.delete(node.id)) this._update();
      this._alpha = Math.max(this._alpha, 0.3);
      this._animate();
    });
    const stop = () => {
      drag = null;
      this._dragNode = null;
      svg.classList.remove("dragging");
    };
    svg.addEventListener("pointerup", stop);
    svg.addEventListener("pointercancel", stop);
    svg.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      this._userView = true;
      // Smooth: proportional to the wheel distance, around the cursor.
      const factor = Math.exp(-ev.deltaY * (ev.deltaMode === 1 ? 0.05 : 0.0015));
      const rect = svg.getBoundingClientRect();
      const px = ev.clientX - rect.left;
      const py = ev.clientY - rect.top;
      const k = Math.min(8, Math.max(0.05, this._view.k * factor));
      const ratio = k / this._view.k;
      this._view.x = px - (px - this._view.x) * ratio;
      this._view.y = py - (py - this._view.y) * ratio;
      this._view.k = k;
      this._requestView();
    }, { passive: false });
  }

  _requestView() {
    if (this._viewFrame) return;
    this._viewFrame = requestAnimationFrame(() => {
      this._viewFrame = null;
      this._applyView();
    });
  }

  _applyView() {
    const { k, x, y } = this._view;
    this._viewport.setAttribute("transform", `translate(${x} ${y}) scale(${k})`);
    this._svg.classList.toggle("no-links", !this._labels || k < LABEL_MIN_SCALE);
    this._svg.classList.toggle("no-names", k < NAME_MIN_SCALE);
  }

  // Show every node; waits until the canvas has a size (a hidden panel has none).
  _fit() {
    const nodes = [...this._nodes.values()];
    const w = this._svg.clientWidth;
    const h = this._svg.clientHeight;
    if (!nodes.length || !w || !h) return;
    const pad = 60;
    const minX = Math.min(...nodes.map((n) => n.x)) - pad;
    const minY = Math.min(...nodes.map((n) => n.y)) - pad;
    // Labels run to the right of a node.
    const maxX = Math.max(...nodes.map((n) => n.x)) + pad + 220;
    const maxY = Math.max(...nodes.map((n) => n.y)) + pad;
    const k = Math.min(w / (maxX - minX), h / (maxY - minY));
    this._view = { k, x: (w - (maxX - minX) * k) / 2 - minX * k, y: (h - (maxY - minY) * k) / 2 - minY * k };
    this._applyView();
  }

  _update() {
    if (!this._svg) return;
    const started = performance.now();
    const fresh = this._merge(this._graph());
    this._syncElements();
    if (fresh > this._devices.length / 3) {
      // First picture (or many newcomers): settle before showing it.
      this._alpha = 1;
      for (let i = 0; i < 150; i += 1) this._step();
    } else {
      this._alpha = Math.max(this._alpha, fresh ? 0.3 : 0.1);
    }
    this._draw();
    if (!this._userView) this._fit();
    this._animate();
    this._stats.updateMs = performance.now() - started;

    const counts = { conn: 0, gw: 0, dev: 0 };
    for (const node of this._nodes.values()) counts[node.kind] += 1;
    this.shadowRoot.getElementById("meta").textContent =
      `${counts.conn} connections · ${counts.gw} gateways · ${counts.dev} devices`;
    this.shadowRoot.getElementById("error").textContent = this._error || "";
  }

  _animate() {
    if (this._frame) return;
    const tick = () => {
      this._frame = null;
      if (this._alpha < 0.005) {
        // Settled after a stem change: frame the new size, unless zoomed by hand.
        if (this._refit && !this._userView) this._fit();
        this._refit = false;
        return;
      }
      const started = performance.now();
      this._step();
      this._step();
      this._draw();
      this._stats.tickMs = performance.now() - started;
      this._frame = requestAnimationFrame(tick);
    };
    this._frame = requestAnimationFrame(tick);
  }

  // Create, update and drop elements; positions come in _draw().
  _syncElements() {
    const seen = new Set();
    for (const node of this._nodes.values()) {
      seen.add(node.id);
      let item = this._nodeEls.get(node.id);
      if (!item) {
        const g = el("g", { "data-id": node.id, class: "node" }, this._nodeLayer);
        let shape;
        if (node.kind === "conn") shape = el("rect", { x: -16, y: -16, width: 32, height: 32, rx: 6, fill: "#039be5" }, g);
        else if (node.kind === "gw") shape = el("rect", { x: -13, y: -13, width: 26, height: 26, transform: "rotate(45)" }, g);
        else shape = el("circle", { r: 11, fill: "#2196f3" }, g);
        const title = el("title", {}, shape);
        const label = el("text", { x: 18, y: -2 }, g);
        const sub = el("text", { x: 18, y: 14, class: "sub" }, g);
        item = { g, shape, title, label, sub };
        this._nodeEls.set(node.id, item);
      }
      item.label.textContent = node.label;
      item.sub.textContent = node.sub || "";
      if (node.kind === "gw") {
        item.shape.setAttribute("fill", node.online ? "#00897b" : "#757575");
        item.title.textContent = `${node.label}${node.sub ? ` (${node.sub})` : ""}${node.lastSeen ? `\nlast seen ${node.lastSeen}` : ""}${node.online ? "" : "\noffline"}`;
      } else if (node.kind === "dev") {
        item.title.textContent = `${node.label} (${node.sub || "?"})${node.lastUplink ? `\nlast uplink ${node.lastUplink}` : "\nno uplink yet"}`;
      } else {
        item.title.textContent = node.label;
      }
    }
    for (const [id, item] of this._nodeEls) {
      if (!seen.has(id)) { item.g.remove(); this._nodeEls.delete(id); }
    }

    const seenLinks = new Set();
    for (const link of this._links) {
      const id = `${link.source}|${link.target}`;
      link.id = id;
      seenLinks.add(id);
      let item = this._linkEls.get(id);
      if (!item) {
        const line = el("line", {}, this._linkLayer);
        const title = el("title", {}, line);
        const text = el("text", { class: "link", "text-anchor": "middle" }, this._labelLayer);
        item = { line, title, text };
        this._linkEls.set(id, item);
      }
      if (link.kind === "uplink") {
        const text = linkText(link.rssi, link.snr);
        item.line.setAttribute("stroke", link.quality.color);
        item.line.setAttribute("stroke-width", link.best ? "2.5" : "1");
        item.line.setAttribute("stroke-opacity", link.best ? "1" : "0.18");
        item.line.removeAttribute("stroke-dasharray");
        item.title.textContent = `${text} (${link.quality.name}${link.best ? ", strongest" : ""})`;
        item.text.textContent = link.best ? text : "";
        item.text.style.fill = link.quality.color;
      } else {
        item.line.setAttribute("stroke", "var(--secondary-text-color)");
        item.line.setAttribute("stroke-opacity", "0.45");
        item.line.setAttribute("stroke-width", "1.5");
        if (link.kind === "none" || !link.online) item.line.setAttribute("stroke-dasharray", "6 6");
        else item.line.removeAttribute("stroke-dasharray");
        item.title.textContent = "";
        item.text.textContent = "";
      }
    }
    for (const [id, item] of this._linkEls) {
      if (!seenLinks.has(id)) { item.line.remove(); item.text.remove(); this._linkEls.delete(id); }
    }
  }

  _draw() {
    for (const node of this._nodes.values()) {
      const item = this._nodeEls.get(node.id);
      if (item) item.g.setAttribute("transform", `translate(${node.x.toFixed(1)} ${node.y.toFixed(1)})`);
    }
    for (const link of this._links) {
      const item = this._linkEls.get(link.id);
      const s = this._nodes.get(link.source);
      const t = this._nodes.get(link.target);
      if (!item || !s || !t) continue;
      item.line.setAttribute("x1", s.x.toFixed(1));
      item.line.setAttribute("y1", s.y.toFixed(1));
      item.line.setAttribute("x2", t.x.toFixed(1));
      item.line.setAttribute("y2", t.y.toFixed(1));
      if (item.text.textContent) {
        item.text.setAttribute("x", ((s.x + t.x) / 2).toFixed(1));
        item.text.setAttribute("y", ((s.y + t.y) / 2).toFixed(1));
      }
    }
  }
}

// The dashboard card. Options: gateway (entry title or ID; all when left
// out), height (CSS, default 450px), title (empty hides it), labels (false
// starts without RSSI labels).
class MilesightLorawanMapCard extends MilesightLorawanNetworkPanel {
  setConfig(config) {
    this._config = { ...(config || {}) };
    this._isCard = true;
    if (this._svg) {
      this._renderShell();
      this._load();
    }
  }

  getCardSize() {
    return Math.ceil((parseInt(this._config.height, 10) || 450) / 50);
  }

  static getStubConfig() {
    return { height: "450px" };
  }
}

// The script loads as the panel's module and as the card's resource.
if (!customElements.get("milesight-lorawan-network-panel")) {
  customElements.define("milesight-lorawan-network-panel", MilesightLorawanNetworkPanel);
}
if (!customElements.get("milesight-lorawan-map")) {
  customElements.define("milesight-lorawan-map", MilesightLorawanMapCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "milesight-lorawan-map",
    name: "Milesight LoRaWAN map",
    description: "Gateways and devices of the Milesight Gateway integration, with RSSI and SNR per reception.",
  });
}
