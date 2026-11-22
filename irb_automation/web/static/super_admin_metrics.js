/* 서비스 관리 > 사용량 통계: 서버가 data-metrics 에 넣은 일별 지표를 SVG 차트로 그린다. */
(() => {
  "use strict";
  const root = document.querySelector(".sa-metrics");
  if (!root) return;
  let data;
  try { data = JSON.parse(root.dataset.metrics || "{}"); } catch { return; }
  const NS = "http://www.w3.org/2000/svg";
  const SERIES = ["var(--sa-series-1)", "var(--sa-series-2)", "var(--sa-series-3)"];
  const FAIL = "var(--sa-fail)";
  const daily = data.daily || [];

  root.querySelectorAll(".sa-hbar[data-width]").forEach((bar) => {
    bar.style.width = `${Math.max(0, Math.min(100, Number(bar.dataset.width) || 0))}%`;
  });

  const el = (tag, attrs = {}, parent) => {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (parent) parent.appendChild(node);
    return node;
  };
  const html = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const fmt = (v, unit) => {
    if (v === null || v === undefined) return "–";
    if (unit === "초") return v >= 60 ? `${Math.floor(v / 60)}분 ${Math.round(v % 60)}초` : `${Number(v).toFixed(1)}초`;
    return `${Number(v).toLocaleString("ko-KR")}${unit || ""}`;
  };
  const shortDay = (iso) => `${Number(iso.slice(5, 7))}/${Number(iso.slice(8, 10))}`;
  const niceMax = (max) => {
    if (max <= 0) return 1;
    const mag = 10 ** Math.floor(Math.log10(max));
    return [1, 2, 2.5, 5, 10].map((s) => s * mag).find((s) => s >= max) || max;
  };

  const tip = html("div", "sa-tooltip");
  tip.setAttribute("role", "status");
  tip.hidden = true;
  document.body.appendChild(tip);
  const showTip = (event, title, rows) => {
    tip.replaceChildren(html("strong", "", title));
    rows.forEach(([color, label, value]) => {
      const row = html("div", "sa-tip-row");
      const swatch = html("span", "sa-swatch");
      swatch.style.background = color;
      row.append(swatch, html("span", "", label), html("b", "", value));
      tip.appendChild(row);
    });
    tip.hidden = false;
    const box = tip.getBoundingClientRect();
    let x = event.clientX + 14;
    if (x + box.width > window.innerWidth - 8) x = event.clientX - box.width - 14;
    tip.style.left = `${Math.max(8, x) + window.scrollX}px`;
    tip.style.top = `${Math.max(8, event.clientY - box.height - 10) + window.scrollY}px`;
  };
  const hideTip = () => { tip.hidden = true; };

  const legend = (series, colors) => {
    const list = html("ul", "sa-legend");
    series.forEach((s, i) => {
      const item = html("li");
      const swatch = html("span", "sa-swatch");
      swatch.style.background = colors[i];
      item.append(swatch, document.createTextNode(s.label));
      list.appendChild(item);
    });
    return list;
  };

  const tableView = (series, unit) => {
    const details = html("details", "sa-table-view");
    details.appendChild(html("summary", "", "표로 보기"));
    const wrap = html("div", "table-wrap");
    const table = html("table", "sa-table");
    const head = table.createTHead().insertRow();
    head.appendChild(html("th", "", "날짜"));
    series.forEach((s) => head.appendChild(html("th", "", s.label)));
    const body = table.createTBody();
    daily.forEach((d) => {
      const row = body.insertRow();
      const th = html("th", "", d.day);
      th.scope = "row";
      row.appendChild(th);
      series.forEach((s) => { row.insertCell().textContent = fmt(d[s.key], s.unit || unit); });
    });
    wrap.appendChild(table);
    details.appendChild(wrap);
    return details;
  };

  const frame = (container, series, colors, unit, max) => {
    const W = 560, H = 210, M = { t: 12, r: 16, b: 26, l: 44 };
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "sa-svg", role: "img",
      "aria-label": `${container.dataset.title}: ${series.map((s) => s.label).join(", ")}` });
    const iw = W - M.l - M.r, ih = H - M.t - M.b;
    const top = niceMax(max);
    const y = (v) => M.t + ih - (v / top) * ih;
    const step = iw / Math.max(1, daily.length);
    const xc = (i) => M.l + step * i + step / 2;
    for (let k = 0; k <= 4; k++) {
      const v = (top / 4) * k;
      el("line", { x1: M.l, x2: W - M.r, y1: y(v), y2: y(v), class: k ? "sa-grid" : "sa-axis" }, svg);
      el("text", { x: M.l - 6, y: y(v) + 4, class: "sa-tick", "text-anchor": "end" }, svg).textContent =
        unit === "초" ? fmt(v, "초").replace(".0초", "초") : (v >= 1000 ? `${v / 1000}k` : String(Math.round(v * 10) / 10));
    }
    const every = Math.ceil(daily.length / 7);
    daily.forEach((d, i) => {
      if (i % every && i !== daily.length - 1) return;
      el("text", { x: xc(i), y: H - 8, class: "sa-tick", "text-anchor": "middle" }, svg).textContent = shortDay(d.day);
    });
    container.append(html("h3", "sa-chart-title", container.dataset.title), legend(series, colors), svg,
      tableView(series, unit));
    return { svg, W, H, M, iw, ih, y, xc, step };
  };

  const empty = (container, series) => {
    container.append(html("h3", "sa-chart-title", container.dataset.title),
      html("p", "muted small sa-empty", "이 기간에 기록이 없습니다."));
  };

  const hover = (f, series, colors, unit) => {
    const guide = el("line", { y1: f.M.t, y2: f.M.t + f.ih, class: "sa-guide", visibility: "hidden" }, f.svg);
    const hit = el("rect", { x: f.M.l, y: f.M.t, width: f.iw, height: f.ih, class: "sa-hit" }, f.svg);
    const move = (event) => {
      const pt = f.svg.createSVGPoint();
      pt.x = event.clientX; pt.y = event.clientY;
      const local = pt.matrixTransform(f.svg.getScreenCTM().inverse());
      const i = Math.max(0, Math.min(daily.length - 1, Math.floor((local.x - f.M.l) / f.step)));
      guide.setAttribute("x1", f.xc(i)); guide.setAttribute("x2", f.xc(i));
      guide.setAttribute("visibility", "visible");
      showTip(event, daily[i].day, series.map((s, k) => [colors[k], s.label, fmt(daily[i][s.key], s.unit || unit)]));
    };
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerleave", () => { guide.setAttribute("visibility", "hidden"); hideTip(); });
  };

  const lines = (container) => {
    const series = JSON.parse(container.dataset.series);
    const unit = container.dataset.unit;
    const values = daily.flatMap((d) => series.map((s) => d[s.key])).filter((v) => v !== null && v !== undefined);
    if (!values.some((v) => v > 0)) return empty(container, series);
    const colors = series.map((_, i) => SERIES[i]);
    const f = frame(container, series, colors, unit, Math.max(...values));
    series.forEach((s, k) => {
      let path = "";
      let open = false;
      daily.forEach((d, i) => {
        const v = d[s.key];
        if (v === null || v === undefined) { open = false; return; }
        path += `${open ? "L" : "M"}${f.xc(i).toFixed(1)},${f.y(v).toFixed(1)}`;
        const lonely = !open && (i === daily.length - 1 || daily[i + 1][s.key] === null || daily[i + 1][s.key] === undefined);
        if (lonely || daily.length <= 14) el("circle", { cx: f.xc(i), cy: f.y(v), r: 3, fill: colors[k], class: "sa-dot" }, f.svg);
        open = true;
      });
      el("path", { d: path, stroke: colors[k], class: "sa-line" }, f.svg);
    });
    hover(f, series, colors, unit);
  };

  const stack = (container) => {
    const series = JSON.parse(container.dataset.series);
    const unit = container.dataset.unit;
    const totals = daily.map((d) => series.reduce((sum, s) => sum + (d[s.key] || 0), 0));
    if (!totals.some((v) => v > 0)) return empty(container, series);
    const colors = series.map((_, i) => (i === series.length - 1 && series.length > 1 ? FAIL : SERIES[i]));
    const f = frame(container, series, colors, unit, Math.max(...totals));
    const width = Math.max(2, Math.min(22, f.step - 4));
    daily.forEach((d, i) => {
      let base = 0;
      series.forEach((s, k) => {
        const v = d[s.key] || 0;
        if (!v) return;
        const y0 = f.y(base), y1 = f.y(base + v);
        const h = Math.max(1, y0 - y1 - (base ? 2 : 0));
        el("rect", { x: f.xc(i) - width / 2, y: y1, width, height: h, rx: 2, fill: colors[k] }, f.svg);
        base += v;
      });
    });
    hover(f, series, colors, unit);
  };

  const heatmap = (container) => {
    const heat = data.heatmap;
    if (!heat || !heat.max) {
      container.appendChild(html("p", "muted small", "이 기간에 기록이 없습니다."));
      return;
    }
    const cell = 22, gap = 2, left = 26, topPad = 18;
    const W = left + 24 * (cell + gap), H = topPad + 7 * (cell + gap);
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "sa-svg sa-heat", role: "img",
      "aria-label": "요일·시간대별 활동 건수" });
    for (let h = 0; h < 24; h += 3) {
      el("text", { x: left + h * (cell + gap), y: 12, class: "sa-tick" }, svg).textContent = `${h}시`;
    }
    heat.rows.forEach((row, r) => {
      el("text", { x: 0, y: topPad + r * (cell + gap) + 15, class: "sa-tick" }, svg).textContent = row.day;
      row.hours.forEach((v, h) => {
        const level = v ? Math.ceil((v / heat.max) * 6) : 0;
        const rect = el("rect", { x: left + h * (cell + gap), y: topPad + r * (cell + gap), width: cell, height: cell,
          rx: 3, class: `sa-heat-${level}` }, svg);
        rect.addEventListener("pointermove", (event) => showTip(event, `${row.day}요일 ${h}시`, [["var(--sa-series-1)", "활동", fmt(v, "건")]]));
        rect.addEventListener("pointerleave", hideTip);
      });
    });
    const scale = html("div", "sa-heat-scale");
    scale.append(html("span", "small muted", "적음"));
    for (let i = 0; i <= 6; i++) scale.appendChild(html("span", `sa-heat-key sa-heat-${i}`));
    scale.append(html("span", "small muted", `많음 (최대 ${heat.max}건)`));
    container.append(svg, scale);
  };

  root.querySelectorAll("[data-chart]").forEach((container) => {
    ({ lines, stack, heatmap })[container.dataset.chart]?.(container);
  });
})();
