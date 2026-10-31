// Live mini-charts on the overview: polls /ui/series (the hub's own 15-minute samples) every 5 s.
(function () {
  "use strict";
  const css = getComputedStyle(document.documentElement);
  const c = (n) => css.getPropertyValue(n).trim();
  const charts = {};
  const defs = {
    "ch-speed": [["gen_tps", "total tok/s", c("--c1")], ["per_stream_tps", "per stream", c("--c2")]],
    "ch-load": [["running", "running", c("--c1")], ["waiting", "waiting", c("--c3")]],
    "ch-kv": [["kv_pct", "KV %", c("--c1")]],
    "ch-mem": [["mem_used_gib", "memory GiB", c("--c1")], ["gpu_temp_c", "GPU °C", c("--c2")]],
  };
  function opts(el, series) {
    return {
      width: el.clientWidth || 500, height: 170,
      scales: { x: { time: true } },
      // canvas text does not inherit CSS, so the axis labels get the dashboard's font explicitly
      axes: [{ stroke: c("--muted"), grid: { stroke: c("--rule") }, font: "11px " + c("--mono") },
             { stroke: c("--muted"), grid: { stroke: c("--rule") }, font: "11px " + c("--mono") }],
      // the first series gets a soft fill under its line (colors are #rrggbb, so "22" is ~13 % alpha)
      series: [{}].concat(series.map(([, label, color], i) =>
        ({ label, stroke: color, width: 2, spanGaps: false, fill: i === 0 ? color + "22" : undefined }))),
    };
  }
  async function tick() {
    let d;
    try {
      const r = await fetch("/ui/series", { credentials: "same-origin" });
      if (!r.ok) return;
      d = await r.json();
    } catch (e) { return; }
    for (const [id, series] of Object.entries(defs)) {
      const el = document.getElementById(id);
      if (!el) continue;
      const data = [d.t].concat(series.map(([k]) => d[k]));
      if (!charts[id]) charts[id] = new uPlot(opts(el, series), data, el);
      else charts[id].setData(data);
    }
  }
  window.addEventListener("resize", () => {
    for (const [id, ch] of Object.entries(charts)) {
      const el = document.getElementById(id);
      ch.setSize({ width: el.clientWidth, height: 170 });
    }
  });
  tick();
  setInterval(tick, 5000);
})();
