/* WWP Launch Radar dashboard. All user-generated text is inserted with textContent. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    status: null,
    tz: "Europe/Warsaw",
    range: "24h",
    custom: null,          // {from, to} epoch seconds
    population: "all_all",
    feedOffset: 0,
    annotations: [],
    charts: {},
  };

  // ---------------------------------------------------------------- helpers
  async function api(path, opts = {}) {
    const res = await fetch(path, { credentials: "same-origin", ...opts,
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
    if (res.status === 401) { location.href = "/login"; throw new Error("auth"); }
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    return res.json();
  }

  function el(tag, attrs = {}, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null) continue;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v);
    }
    for (const c of children) if (c != null) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return e;
  }

  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.hidden = true), 3500);
  }

  const nf = new Intl.NumberFormat("pl-PL");
  const fmtN = (n) => (n == null ? "–" : nf.format(n));

  function fmtTime(ts, withDate = true) {
    if (ts == null) return "–";
    const o = { timeZone: state.tz, hour: "2-digit", minute: "2-digit" };
    if (withDate) Object.assign(o, { day: "2-digit", month: "2-digit", year: "numeric" });
    return new Intl.DateTimeFormat("pl-PL", o).format(new Date(ts * 1000));
  }

  function ago(ts) {
    if (ts == null) return "nigdy";
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 90) return `${s} s temu`;
    if (s < 5400) return `${Math.round(s / 60)} min temu`;
    if (s < 172800) return `${Math.round(s / 3600)} h temu`;
    return `${Math.round(s / 86400)} dni temu`;
  }

  // Offset (seconds) of the display timezone at a given epoch.
  function tzOffset(epoch, tz) {
    const parts = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit",
      day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).formatToParts(new Date(epoch * 1000));
    const g = (t) => Number(parts.find((p) => p.type === t).value);
    return Date.UTC(g("year"), g("month") - 1, g("day"), g("hour"), g("minute"), g("second")) / 1000 - epoch;
  }
  // "YYYY-MM-DDTHH:MM" interpreted in the display timezone -> epoch seconds.
  function zonedToEpoch(str) {
    if (!str) return null;
    const [d, t] = str.split("T");
    const [Y, M, D] = d.split("-").map(Number);
    const [h, m] = (t || "00:00").split(":").map(Number);
    const naive = Date.UTC(Y, M - 1, D, h, m) / 1000;
    let e = naive - tzOffset(naive, state.tz);
    const off2 = tzOffset(e, state.tz);
    if (naive - off2 !== e) e = naive - off2;
    return e;
  }
  function epochToZoned(epoch) {
    if (epoch == null) return "";
    const local = new Date((epoch + tzOffset(epoch, state.tz)) * 1000);
    return local.toISOString().slice(0, 16);
  }

  // ---------------------------------------------------------------- range
  function currentRange() {
    const now = Math.floor(Date.now() / 1000);
    const s = state.status;
    switch (state.range) {
      case "1h": return { from: now - 3600, to: now };
      case "6h": return { from: now - 6 * 3600, to: now };
      case "24h": return { from: now - 86400, to: now };
      case "launch": return { from: s && s.game.launch_at ? s.game.launch_at : now - 86400, to: now };
      case "all": return { from: s ? s.data_start : now - 86400, to: now };
      case "custom": return state.custom || { from: now - 86400, to: now };
    }
  }
  function rangeQuery(r = currentRange()) { return `from=${r.from}&to=${r.to}`; }

  function initRange() {
    for (const b of $("range-seg").querySelectorAll("button")) {
      b.addEventListener("click", () => {
        if (b.disabled) return;
        state.range = b.dataset.range;
        $("range-seg").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
        $("custom-range").hidden = state.range !== "custom";
        if (state.range === "custom") {
          const r = state.custom || currentRange();
          $("cr-from").value = epochToZoned(r.from);
          $("cr-to").value = epochToZoned(r.to);
          return;
        }
        refreshSeries();
      });
    }
    $("cr-apply").addEventListener("click", () => {
      const from = zonedToEpoch($("cr-from").value), to = zonedToEpoch($("cr-to").value);
      if (!from || !to || from >= to) return toast("Nieprawidłowy zakres dat");
      state.custom = { from, to };
      refreshSeries();
    });
  }

  function updateRangeLabel() {
    const r = currentRange();
    $("range-label").textContent = `${fmtTime(r.from)} → ${fmtTime(r.to)} (${state.tz})`;
  }

  // ---------------------------------------------------------------- status + cards
  async function loadStatus() {
    const s = await api("/api/status");
    state.status = s;
    state.tz = s.display_tz;
    document.querySelectorAll(".tzname").forEach((e) => (e.textContent = s.display_tz));
    $("demo-banner").hidden = !s.demo;
    $("logout-form").hidden = !s.auth;
    $("game-label").textContent = `${s.game.name} · App ID ${s.game.app_id}` +
      (s.game.launch_at ? ` · premiera ${fmtTime(s.game.launch_at)}` : " · premiera: nie ustawiono");
    $("range-launch").disabled = !s.game.launch_at;
    $("range-launch").title = s.game.launch_at ? "" : "Ustaw czas premiery w Ustawieniach";

    const popSel = $("population");
    if (!popSel.options.length) {
      for (const [k, label] of Object.entries(s.summary_populations)) {
        popSel.append(el("option", { value: k, text: k === "all_all" ? "wszystkie" : "zakupy Steam", title: label }));
      }
    }
    renderStatusPills(s);
    renderHealthTable(s);
    $("ai-panel").hidden = !s.ai.enabled;
    $("ai-run").hidden = !s.ai.available;
    return s;
  }

  function sourceState(h, maxAge) {
    if (!h) return "none";
    const now = Date.now() / 1000;
    if (h.last_success_at && now - h.last_success_at <= maxAge && h.consecutive_failures === 0) return "ok";
    if (h.last_success_at && now - h.last_success_at <= maxAge) return "warn";
    return "bad";
  }

  function renderStatusPills(s) {
    const app = $("st-app");
    if (s.collector.running) { app.className = "pill ok"; app.textContent = "Aplikacja: działa, kolektor aktywny"; }
    else if (s.demo) { app.className = "pill warn"; app.textContent = "Aplikacja: tryb demo (bez kolektora)"; }
    else { app.className = "pill bad"; app.textContent = "Aplikacja: kolektor zatrzymany"; }
    if (s.collector.lock_error) app.title = s.collector.lock_error;

    const ccu = sourceState(s.sources.ccu, s.intervals.ccu * 3);
    const rev = sourceState(s.sources.review_summary, s.intervals.reviews * 3);
    const steam = $("st-steam");
    const ccuH = s.sources.ccu;
    const unavailableOnly = ccuH && ccuH.last_error && ccuH.last_error.startsWith("[unavailable]");
    if (ccu === "ok" && rev === "ok") { steam.className = "pill ok"; steam.textContent = "Steam: dostępny"; }
    else if (ccu === "none" && rev === "none") { steam.className = "pill"; steam.textContent = "Steam: brak odczytów"; }
    else if (unavailableOnly && rev === "ok") { steam.className = "pill warn"; steam.textContent = "Steam: CCU niedostępne (brak danych dla gry)"; }
    else { steam.className = ccu === "bad" && rev === "bad" ? "pill bad" : "pill warn";
      steam.textContent = `Steam: CCU ${ccu === "ok" ? "OK" : "problem"}, recenzje ${rev === "ok" ? "OK" : "problem"}`; }

    const imp = s.import, pill = $("st-import");
    if (imp.status === "complete") { pill.className = "pill ok"; pill.textContent = `Import: kompletny (${fmtN(imp.stored_reviews)} recenzji)`; }
    else if (imp.status === "in_progress") {
      const pct = imp.expected_total ? Math.min(100, Math.round((100 * imp.reviews_seen) / imp.expected_total)) : null;
      pill.className = "pill warn";
      pill.textContent = `Import: w toku ${fmtN(imp.reviews_seen)}${imp.expected_total ? " / ~" + fmtN(imp.expected_total) : ""}${pct != null ? ` (${pct}%)` : ""}`;
    } else { pill.className = "pill warn"; pill.textContent = "Import: nie rozpoczęty"; }
  }

  function renderHealthTable(s) {
    const names = { ccu: "CCU", reviews_recent: "Recenzje - nowe", reviews_updated: "Recenzje - edytowane",
      review_summary: "Podsumowanie recenzji", ai: "Analiza AI" };
    const t = $("health-table");
    t.replaceChildren(el("tr", {}, el("th", { text: "Źródło" }), el("th", { text: "Ostatni sukces" }),
      el("th", { text: "Ostatni błąd" })));
    const keys = Object.keys(s.sources);
    if (!keys.length) t.append(el("tr", {}, el("td", { colspan: 3, class: "muted", text: "Brak prób zbierania." })));
    for (const k of keys) {
      const h = s.sources[k];
      const fail = h.consecutive_failures ? ` (${h.consecutive_failures}× z rzędu)` : "";
      t.append(el("tr", {},
        el("td", { text: names[k] || k }),
        el("td", { text: h.last_success_at ? `${fmtTime(h.last_success_at)} (${ago(h.last_success_at)})` : "nigdy" }),
        el("td", { class: h.consecutive_failures ? "neg" : "muted",
          text: h.last_error_at ? `${fmtTime(h.last_error_at)}${fail}: ${h.last_error || ""}` : "–" })));
    }
  }

  function setChange(id, ch) {
    const big = $(id), meta = $(id + "-meta");
    if (!ch) {
      big.replaceChildren(el("span", { class: "unavail", text: "niedostępne" }));
      meta.textContent = "brak wystarczająco bliskiej próbki historycznej";
      return;
    }
    const sign = ch.diff > 0 ? "+" : "";
    big.replaceChildren(el("span", { class: ch.diff > 0 ? "up" : ch.diff < 0 ? "down" : "",
      text: `${sign}${fmtN(ch.diff)}${ch.pct != null ? ` (${sign}${ch.pct}%)` : ""}` }));
    meta.textContent = `vs ${fmtN(ch.reference_count)} o ${fmtTime(ch.reference_at, false)}`;
  }

  async function loadOverview() {
    const o = await api(`/api/overview?population=${state.population}`);
    const s = state.status;
    const cur = o.ccu.current;
    if (cur) {
      $("c-ccu").textContent = fmtN(cur.player_count);
      $("c-ccu-meta").textContent = `odczyt ${fmtTime(cur.observed_at, false)} (${ago(cur.observed_at)})` + (o.ccu.stale ? " · NIEAKTUALNE" : "");
      $("c-ccu-meta").className = o.ccu.stale ? "meta neg" : "meta";
    } else {
      $("c-ccu").replaceChildren(el("span", { class: "unavail", text: "brak danych" }));
      const lr = o.ccu.last_run;
      $("c-ccu-meta").textContent = lr ? `ostatnia próba: ${lr.status}${lr.error ? " - " + lr.error : ""}` : "jeszcze nie odpytano";
    }
    if (o.ccu.peak) {
      $("c-peak").textContent = fmtN(o.ccu.peak.player_count);
      $("c-peak-meta").textContent = `${fmtTime(o.ccu.peak.observed_at)} · najwyższe od rozpoczęcia monitoringu`;
    }
    setChange("c-d15", o.ccu.change["15"]);
    setChange("c-d60", o.ccu.change["60"]);

    const sm = o.summary;
    if (sm) {
      $("c-pos").textContent = fmtN(sm.total_positive);
      $("c-neg").textContent = fmtN(sm.total_negative);
      $("c-posneg-meta").textContent = `Steam, ${sm.population_label}; migawka ${ago(sm.observed_at)}`;
      $("c-pct").textContent = sm.positive_pct == null ? "–" : `${sm.positive_pct.toLocaleString("pl-PL")}%`;
      $("c-pct-meta").textContent = `n = ${fmtN(sm.total_positive + sm.total_negative)} · ${sm.population_label}` +
        (sm.review_score_desc ? ` · „${sm.review_score_desc}”` : "");
    } else {
      $("c-pos").textContent = "–"; $("c-neg").textContent = "–";
      $("c-posneg-meta").textContent = "brak migawki podsumowania Steam";
      $("c-pct").replaceChildren(el("span", { class: "unavail", text: "niedostępne" }));
      $("c-pct-meta").textContent = "";
    }
    const sr = o.stored_reviews;
    $("c-newhour").textContent = fmtN(sr.last_hour);
    $("c-newhour-meta").textContent = `wg czasu utworzenia na Steam · ${fmtN(sr.last_hour_pos)} poz. / ${fmtN(sr.last_hour - sr.last_hour_pos)} neg.` +
      (o.import.status !== "complete" ? " · import niekompletny" : "");

    // health card
    const ccuH = s.sources.ccu, sumH = s.sources.review_summary;
    const fresh = [ccuH && ccuH.last_success_at, sumH && sumH.last_success_at].filter(Boolean);
    const newest = fresh.length ? Math.max(...fresh) : null;
    const healthy = s.collector.running && ccuH && ccuH.consecutive_failures === 0;
    $("c-health").textContent = s.collector.running ? (healthy ? "OK" : "Problemy") : (s.demo ? "Demo" : "Zatrzymane");
    $("c-health").className = "big small " + (healthy ? "up" : s.collector.running ? "down" : "");
    $("c-health-meta").textContent = `CCU: ${ago(ccuH && ccuH.last_success_at)} · recenzje: ${ago(sumH && sumH.last_success_at)}` +
      (newest ? "" : " · brak udanych odczytów");
  }

  // ---------------------------------------------------------------- charts
  function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }

  function annotationPlugin() {
    return {
      hooks: {
        draw: [(u) => {
          const ctx = u.ctx;
          const [min, max] = [u.scales.x.min, u.scales.x.max];
          ctx.save();
          ctx.font = `${11 * devicePixelRatio}px system-ui`;
          ctx.textBaseline = "top";
          ctx.textAlign = "left";
          let i = 0;
          for (const a of state.annotations) {
            if (a.event_at < min || a.event_at > max) continue;
            const x = Math.round(u.valToPos(a.event_at, "x", true));
            ctx.strokeStyle = css("--accent");
            ctx.setLineDash([4 * devicePixelRatio, 4 * devicePixelRatio]);
            ctx.lineWidth = 1 * devicePixelRatio;
            ctx.beginPath();
            ctx.moveTo(x, u.bbox.top);
            ctx.lineTo(x, u.bbox.top + u.bbox.height);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = css("--accent");
            ctx.fillText(a.title.slice(0, 40), x + 4 * devicePixelRatio, u.bbox.top + (2 + (i++ % 3) * 14) * devicePixelRatio);
          }
          ctx.restore();
        }],
      },
    };
  }

  // Polish, 24-hour tick labels in the display timezone.
  function xTicks(u, splits) {
    const span = u.scales.x.max - u.scales.x.min;
    const o = { timeZone: state.tz };
    if (span <= 86400 * 1.5) Object.assign(o, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
    else if (span <= 86400 * 10) Object.assign(o, { day: "2-digit", month: "2-digit", hour: "2-digit", hourCycle: "h23" });
    else Object.assign(o, { day: "2-digit", month: "2-digit" });
    const f = new Intl.DateTimeFormat("pl-PL", o);
    return splits.map((v) => f.format(new Date(v * 1000)));
  }

  function baseOpts(container, series, extra = {}) {
    const grid = { stroke: css("--grid"), width: 1 };
    const axis = { stroke: css("--muted"), grid, ticks: grid };
    return {
      width: Math.max(300, container.clientWidth),
      height: container.closest('.charts > .panel:first-child') ? 320 : 260,
      tzDate: (ts) => uPlot.tzDate(new Date(ts * 1e3), state.tz),
      series: [{ label: "Czas", value: (u, v) => (v == null ? "–" : fmtTime(v)) }, ...series],
      axes: [{ ...axis, values: xTicks }, { ...axis, size: 60 }],
      legend: { live: true },
      cursor: { drag: { x: true, y: false } },
      plugins: [annotationPlugin()],
      ...extra,
    };
  }

  function renderChart(key, containerId, opts, data, emptyText) {
    const c = $(containerId);
    if (state.charts[key]) { state.charts[key].destroy(); delete state.charts[key]; }
    c.replaceChildren();
    const hasData = data[0].length && data.slice(1).some((s) => s.some((v) => v != null));
    if (!hasData) { c.append(el("div", { class: "empty", text: emptyText })); return; }
    state.charts[key] = new uPlot(opts, data, c);
  }

  const resLabel = { raw: "surowe odczyty", "5m": "kubełki 5 min", "1h": "kubełki 1 h", "1d": "dni kalendarzowe" };

  async function loadCcuChart() {
    const d = await api(`/api/ccu?${rangeQuery()}`);
    $("ccu-res").textContent = resLabel[d.resolution] || d.resolution;
    const c = $("chart-ccu");
    const r = currentRange();
    const series = [{ label: d.resolution === "raw" ? "CCU" : "CCU (średnia)", stroke: css("--accent"), width: 2,
      fill: css("--accent") + "22", spanGaps: false, value: (u, v) => (v == null ? "–" : nf.format(Math.round(v))) }];
    const data = [d.t, d.value];
    if (d.max) {
      series.push({ label: "CCU (maks.)", stroke: css("--info"), width: 1, dash: [4, 3], spanGaps: false,
        value: (u, v) => (v == null ? "–" : nf.format(v)) });
      data.push(d.max);
      series.push({ label: "Pokrycie", scale: "%", stroke: css("--muted"), width: 1, show: false,
        value: (u, v) => (v == null ? "–" : `${Math.round(v * 100)}%`) });
      data.push(d.coverage);
    }
    const opts = baseOpts(c, series, { scales: { x: { time: true, min: r.from, max: r.to }, "%": { range: [0, 1] } } });
    renderChart("ccu", "chart-ccu", opts, data, "Brak odczytów CCU w tym zakresie");
  }

  async function loadReviewChart() {
    const d = await api(`/api/reviews/series?${rangeQuery()}&steam_only=${$("rv-steam").checked}`);
    $("rv-res").textContent = resLabel[d.resolution] || d.resolution;
    const c = $("chart-reviews");
    const bars = uPlot.paths.bars({ size: [0.4, 40], align: -1 });
    const opts = baseOpts(c, [
      { label: "Nowe pozytywne", stroke: css("--pos"), fill: css("--pos") + "cc", paths: bars, points: { show: false } },
      { label: "Nowe negatywne", stroke: css("--neg"), fill: css("--neg") + "cc", paths: bars, points: { show: false } },
      { label: "Zmiana na poz.", stroke: css("--pos"), width: 0, points: { show: true, size: 7, fill: css("--panel") }, paths: () => null },
      { label: "Zmiana na neg.", stroke: css("--neg"), width: 0, points: { show: true, size: 7, fill: css("--panel") }, paths: () => null },
    ], { scales: { x: { time: true }, y: { range: (u, mn, mx) => [0, Math.max(1, mx)] } } });
    opts.series[2].paths = uPlot.paths.bars({ size: [0.4, 40], align: 1 });  // negative bars right of the tick
    const nz = (arr) => arr.map((v) => (v ? v : null));
    renderChart("reviews", "chart-reviews", opts, [d.t, d.positive, d.negative, nz(d.to_positive), nz(d.to_negative)],
      "Brak nowych recenzji w tym zakresie");
  }

  async function loadPctChart() {
    const d = await api(`/api/summary/series?${rangeQuery()}&population=${state.population}`);
    $("pct-label").textContent = d.label || "";
    const c = $("chart-pct");
    const r = currentRange();
    const opts = baseOpts(c, [
      { label: "% pozytywnych", stroke: css("--pos"), width: 2, value: (u, v) => (v == null ? "–" : v.toFixed(1) + "%") },
      { label: "Liczba recenzji", scale: "n", stroke: css("--muted"), width: 1, dash: [3, 3],
        value: (u, v) => (v == null ? "–" : nf.format(v)) },
    ], {
      scales: { x: { time: true, min: r.from, max: r.to }, y: { range: (u, mn, mx) => [Math.max(0, Math.floor(mn - 2)), Math.min(100, Math.ceil(mx + 2))] } },
    });
    opts.axes.push({ scale: "n", side: 1, stroke: css("--muted"), grid: { show: false }, size: 60 });
    renderChart("pct", "chart-pct", opts, [d.t, d.pct, d.total], "Brak migawek podsumowania w tym zakresie");
  }

  async function refreshSeries() {
    updateRangeLabel();
    await Promise.allSettled([loadAnnotations(), loadCcuChart(), loadReviewChart(), loadPctChart()]);
    if ($("f-range").checked) loadFeed(true);
  }

  // ---------------------------------------------------------------- review feed
  function fmtPlaytime(min) {
    if (min == null) return "czas gry: brak danych";
    return `czas gry przy recenzji: ${(min / 60).toLocaleString("pl-PL", { maximumFractionDigits: 1 })} h`;
  }

  async function loadFeed(reset = false) {
    if (reset) state.feedOffset = 0;
    const p = new URLSearchParams({
      sentiment: $("f-sent").value, language: $("f-lang").value, purchase: $("f-purchase").value,
      q: $("f-q").value.trim(), edited: $("f-edited").checked, limit: 50, offset: state.feedOffset,
    });
    if ($("f-range").checked) { const r = currentRange(); p.set("from", r.from); p.set("to", r.to); }
    const d = await api(`/api/reviews?${p}`);
    const list = $("feed-list");
    if (reset) list.replaceChildren();
    for (const r of d.items) list.append(reviewCard(r));
    state.feedOffset += d.items.length;
    $("feed-count").textContent = `${fmtN(d.total)} recenzji (zebrane przez monitor)`;
    $("feed-more").hidden = state.feedOffset >= d.total;
    if (!d.total) list.append(el("div", { class: "muted", text: "Brak recenzji dla wybranych filtrów." }));
  }

  function reviewCard(r) {
    const pos = r.voted_up === 1;
    const purchase = r.steam_purchase ? "zakup Steam" : r.received_for_free ? "otrzymana za darmo" : "inne pozyskanie";
    const head = el("div", { class: "head" },
      el("span", { class: "verdict", text: pos ? "👍 Poleca" : "👎 Nie poleca" }),
      el("span", { title: "Czas utworzenia na Steam", text: `utworzono ${fmtTime(r.timestamp_created)}` }),
      el("span", { title: "Kiedy monitor pierwszy raz zobaczył tę recenzję", text: `wykryto ${fmtTime(r.first_seen_at)}` }),
      el("span", { text: fmtPlaytime(r.playtime_at_review) }),
      el("span", { class: "tag", text: r.language || "?" }),
      el("span", { class: "tag", text: purchase }),
      r.written_during_early_access ? el("span", { class: "tag", text: "EA" }) : null,
      r.version_count > 1 ? el("button", { class: "tag edit", type: "button", text: `edytowana (${r.version_count} wersje)`,
        onclick: () => showVersions(r.recommendation_id) }) : null,
      r.timestamp_updated && r.timestamp_updated !== r.timestamp_created
        ? el("span", { title: "Czas ostatniej aktualizacji na Steam", text: `akt. ${fmtTime(r.timestamp_updated)}` }) : null,
      el("a", { href: r.steam_url, target: "_blank", rel: "noopener noreferrer", text: "Otwórz na Steam ↗" }),
    );
    const card = el("div", { class: `review ${pos ? "pos" : "neg"}` }, head, el("div", { class: "text", text: r.review_text || "" }));
    if (r.developer_response) card.append(el("div", { class: "dev", text: `Odpowiedź dewelopera: ${r.developer_response}` }));
    return card;
  }

  async function showVersions(rid) {
    const vs = await api(`/api/reviews/${encodeURIComponent(rid)}/versions`);
    const body = $("versions-body");
    body.replaceChildren();
    for (const v of vs) {
      body.append(el("div", { class: "version" },
        el("div", { class: "muted", text: `Wersja ${v.version_no} · zaobserwowana ${fmtTime(v.observed_at)} · aktualizacja Steam ${fmtTime(v.timestamp_updated)} · ` +
          `${v.voted_up ? "poleca" : "nie poleca"}${v.sentiment_changed ? " · ZMIANA REKOMENDACJI" : ""}` }),
        el("div", { class: "text", text: v.review_text || "" }),
        v.developer_response ? el("div", { class: "dev", text: `Odpowiedź dewelopera: ${v.developer_response}` }) : null));
    }
    $("dlg-versions").showModal();
  }

  async function loadLanguages() {
    const langs = await api("/api/languages");
    const sel = $("f-lang"), cur = sel.value;
    sel.replaceChildren(el("option", { value: "", text: "Wszystkie języki" }));
    for (const l of langs) sel.append(el("option", { value: l.language || "", text: `${l.language} (${l.n})` }));
    sel.value = cur;
  }

  function initFeed() {
    for (const id of ["f-sent", "f-lang", "f-purchase", "f-edited", "f-range"]) $(id).addEventListener("change", () => loadFeed(true));
    let t;
    $("f-q").addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => loadFeed(true), 300); });
    $("feed-more").addEventListener("click", () => loadFeed(false));
  }

  // ---------------------------------------------------------------- annotations
  const kindPl = { launch: "Premiera", hotfix: "Hotfix", patch: "Patch", stream: "Stream", marketing: "Marketing", other: "Inne" };

  async function loadAnnotations() {
    state.annotations = await api("/api/annotations");
    const ul = $("ann-list");
    ul.replaceChildren();
    if (!state.annotations.length) ul.append(el("li", { class: "muted", text: "Brak zdarzeń." }));
    for (const a of [...state.annotations].reverse()) {
      ul.append(el("li", {},
        el("span", { class: "t", text: fmtTime(a.event_at) }),
        el("span", { class: "kind", text: kindPl[a.kind] || a.kind }),
        el("span", { class: "ttl", text: a.title, title: a.description || "" }),
        el("button", { type: "button", title: "Edytuj", text: "✎", onclick: () => editAnnotation(a) }),
        el("button", { type: "button", title: "Usuń", text: "✕", onclick: () => deleteAnnotation(a) })));
    }
  }

  function resetAnnForm() {
    $("ann-id").value = ""; $("ann-title").value = ""; $("ann-desc").value = "";
    $("ann-at").value = epochToZoned(Math.floor(Date.now() / 1000));
    $("ann-submit").textContent = "Dodaj"; $("ann-cancel").hidden = true;
  }
  function editAnnotation(a) {
    $("ann-id").value = a.id; $("ann-at").value = epochToZoned(a.event_at); $("ann-kind").value = a.kind;
    $("ann-title").value = a.title; $("ann-desc").value = a.description || "";
    $("ann-submit").textContent = "Zapisz"; $("ann-cancel").hidden = false;
  }
  async function deleteAnnotation(a) {
    if (!confirm(`Usunąć zdarzenie „${a.title}”?`)) return;
    await api(`/api/annotations/${a.id}`, { method: "DELETE" });
    await refreshSeries();
  }
  function initAnnotations() {
    resetAnnForm();
    $("ann-cancel").addEventListener("click", resetAnnForm);
    $("ann-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = JSON.stringify({ event_at: zonedToEpoch($("ann-at").value), kind: $("ann-kind").value,
        title: $("ann-title").value, description: $("ann-desc").value });
      const id = $("ann-id").value;
      try {
        await api(id ? `/api/annotations/${id}` : "/api/annotations", { method: id ? "PUT" : "POST", body });
        resetAnnForm();
        await refreshSeries();
      } catch (err) { toast("Błąd: " + err.message); }
    });
  }

  // ---------------------------------------------------------------- AI
  async function loadAi() {
    if (!state.status || !state.status.ai.enabled) return;
    const d = await api("/api/ai");
    const b = $("ai-body");
    b.replaceChildren();
    if (!d.available) b.append(el("p", { class: "muted", text: "Analiza AI włączona, ale brak klucza ANTHROPIC_API_KEY." }));
    b.append(el("p", { class: "muted", text: `Model: ${d.model}. Przeanalizowane recenzje: ${fmtN(d.analysed_reviews)}` +
      `, oczekujące: ${fmtN(d.pending_reviews)}. Zakres (czas utworzenia): ${fmtTime(d.window_from)} – ${fmtTime(d.window_to)}. ` +
      "Liczby policzone przez aplikację z przypisań; wnioski AI to zgłoszenia graczy, nie potwierdzone błędy." }));
    const lastOk = d.runs.find((r) => r.status === "ok");
    if (lastOk && lastOk.summary_pl) {
      b.append(el("div", { class: "summary" }, el("span", { class: "ai-badge", text: "AI" }), " ",
        `Ostatnia paczka (${fmtTime(lastOk.finished_at)}, ${lastOk.review_count} recenzji): ${lastOk.summary_pl}`));
    }
    for (const t of d.themes) {
      const lastRunTheme = d.runs.flatMap((r) => r.themes || []).find((x) => x.key === t.key);
      b.append(el("div", { class: `theme ${t.polarity}` },
        el("div", {}, el("span", { class: "n", text: fmtN(t.unique_reviews) }), " ", el("span", { class: "lbl", text: t.label }),
          el("span", { class: "muted", text: " unikalnych recenzji" })),
        lastRunTheme ? el("div", { class: "muted", text: lastRunTheme.summary_pl }) : null,
        el("div", { class: "links" }, ...t.examples.map((x, i) => x.url
          ? el("a", { href: x.url, target: "_blank", rel: "noopener noreferrer", text: `#${i + 1}` }) : null))));
    }
    const errs = d.runs.filter((r) => r.status !== "ok" && r.status !== "running").slice(0, 2);
    for (const r of errs) b.append(el("p", { class: "neg", text: `Błąd analizy ${fmtTime(r.started_at)}: ${r.error || r.status}` }));
  }

  // ---------------------------------------------------------------- export & settings
  function initExport() {
    const dlg = $("dlg-export");
    $("btn-export").addEventListener("click", () => {
      const sel = $("ex-game");
      sel.replaceChildren(...state.status.games.map((g) => el("option", { value: g.id, text: `${g.name} (${g.app_id})` })));
      sel.value = state.status.active_game_id;
      dlg.showModal();
    });
    $("ex-use-range").addEventListener("click", () => {
      const r = currentRange();
      $("ex-from").value = epochToZoned(r.from); $("ex-to").value = epochToZoned(r.to);
    });
    $("ex-clear").addEventListener("click", () => { $("ex-from").value = ""; $("ex-to").value = ""; });
    dlg.querySelectorAll("a[data-ds]").forEach((a) => a.addEventListener("click", () => {
      const p = new URLSearchParams({ game_id: $("ex-game").value, delimiter: $("ex-delim").value });
      const f = zonedToEpoch($("ex-from").value), t = zonedToEpoch($("ex-to").value);
      if (f) p.set("from", f);
      if (t) p.set("to", t);
      if (a.dataset.fmt) p.set("format", a.dataset.fmt);
      window.location.href = `/api/export/${a.dataset.ds}?${p}`;
    }));
  }

  function initSettings() {
    const dlg = $("dlg-settings");
    $("btn-settings").addEventListener("click", () => {
      const g = state.status.game;
      $("set-launch").value = epochToZoned(g.launch_at);
      $("set-appid").value = g.app_id; $("set-name").value = g.name;
      dlg.showModal();
    });
    $("set-cancel").addEventListener("click", () => dlg.close());
    $("set-rebuild").addEventListener("click", async () => {
      await api("/api/aggregates/rebuild", { method: "POST" });
      toast("Agregaty przebudowane"); refreshSeries();
    });
    $("settings-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const g = state.status.game;
      const appId = Number($("set-appid").value);
      if (appId !== g.app_id && !confirm(`Przełączyć monitoring na App ID ${appId}? Dane gry ${g.app_id} zostaną zachowane.`)) return;
      const body = { launch_at: zonedToEpoch($("set-launch").value) };
      if (appId !== g.app_id || $("set-name").value !== g.name) { body.app_id = appId; body.name = $("set-name").value; }
      try {
        await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
        dlg.close(); toast("Zapisano ustawienia");
        await refreshAll();
      } catch (err) { toast("Błąd: " + err.message); }
    });
  }

  function initTheme() {
    let theme = "dark";
    try { theme = localStorage.getItem("wwp-theme") || "dark"; } catch (_) {}
    document.documentElement.dataset.theme = theme;
    $("btn-theme").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("wwp-theme", next); } catch (_) {}
      refreshSeries();
    });
  }

  // ---------------------------------------------------------------- main loop
  async function refreshAll() {
    try {
      await loadStatus();
      await Promise.allSettled([loadOverview(), refreshSeries(), loadLanguages(), loadAi()]);
      await loadFeed(true);
    } catch (e) { if (e.message !== "auth") toast("Błąd odświeżania: " + e.message); }
  }

  async function tick() {
    try {
      await loadStatus();
      await loadOverview();
    } catch (e) { if (e.message !== "auth") $("st-app").className = "pill bad", ($("st-app").textContent = "Aplikacja: brak połączenia"); }
  }

  function init() {
    initTheme(); initRange(); initFeed(); initAnnotations(); initExport(); initSettings();
    $("population").addEventListener("change", (e) => { state.population = e.target.value; loadOverview(); loadPctChart(); });
    $("rv-steam").addEventListener("change", loadReviewChart);
    $("ai-run").addEventListener("click", async () => {
      try { await api("/api/ai/analyze", { method: "POST" }); toast("Analiza AI uruchomiona"); setTimeout(loadAi, 15000); }
      catch (err) { toast("Błąd: " + err.message); }
    });
    let rs;
    window.addEventListener("resize", () => { clearTimeout(rs); rs = setTimeout(refreshSeries, 250); });
    refreshAll();
    setInterval(tick, 20000);                       // cards & statuses
    setInterval(() => { refreshSeries(); loadAi(); }, 60000);   // charts
    setInterval(() => { loadLanguages(); if (state.feedOffset <= 50) loadFeed(true); }, 120000);
  }

  init();
})();
