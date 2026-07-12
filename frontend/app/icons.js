/* SIGNAL — icon set. Minimal stroked SVG glyphs. Exposed on window.Icon. */
(function () {
  const React = window.React;
  const h = React.createElement;
  const S = (props, ...paths) =>
    h("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.7,
      strokeLinecap: "round", strokeLinejoin: "round", width: 18, height: 18,
      className: props && props.className, style: props && props.style }, ...paths);
  const P = (d, extra) => h("path", Object.assign({ d }, extra || {}));
  const C = (cx, cy, r) => h("circle", { cx, cy, r });
  const L = (x1, y1, x2, y2) => h("line", { x1, y1, x2, y2 });

  const Icon = {
    dashboard: (p) => S(p, P("M3 13h8V3H3zM13 21h8V3h-8zM3 21h8v-6H3z")),
    events: (p) => S(p, P("M4 5h16M4 12h16M4 19h10"), C(20, 19, 1.4)),
    radar: (p) => S(p, C(12, 12, 9), C(12, 12, 5), C(12, 12, 1.4), L(12, 12, 19, 7)),
    industries: (p) => S(p, P("M3 21V9l6 3V9l6 3V6l6 3v12z")),
    companies: (p) => S(p, P("M3 21h18M5 21V7l7-4 7 4v14M9 21v-4h6v4"), L(9, 10, 9, 10.01), L(15, 10, 15, 10.01)),
    historical: (p) => S(p, P("M3 12a9 9 0 1 0 3-6.7L3 8"), P("M3 4v4h4"), P("M12 8v4l3 2")),
    alerts: (p) => S(p, P("M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"), P("M13.7 21a2 2 0 0 1-3.4 0")),
    watchlist: (p) => S(p, P("M11 3 8.5 8.2 3 9l4 4-1 6 5-3 5 3-1-6 4-4-5.5-.8z")),
    reports: (p) => S(p, P("M6 2h9l5 5v15H6z"), P("M15 2v5h5"), L(9, 13, 16, 13), L(9, 17, 14, 17)),
    ask: (p) => S(p, P("M21 15a4 4 0 0 1-4 4H8l-5 3V6a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"), L(9, 10, 15, 10), L(9, 13, 13, 13)),
    admin: (p) => S(p, C(12, 12, 3), P("M19.4 13a7.8 7.8 0 0 0 0-2l2-1.5-2-3.4-2.3 1a7.8 7.8 0 0 0-1.7-1l-.3-2.6h-4l-.3 2.6a7.8 7.8 0 0 0-1.7 1l-2.3-1-2 3.4L4.6 11a7.8 7.8 0 0 0 0 2l-2 1.5 2 3.4 2.3-1a7.8 7.8 0 0 0 1.7 1l.3 2.6h4l.3-2.6a7.8 7.8 0 0 0 1.7-1l2.3 1 2-3.4z")),
    settings: (p) => S(p, L(4, 6, 20, 6), L(4, 12, 20, 12), L(4, 18, 20, 18), C(9, 6, 2), C(15, 12, 2), C(7, 18, 2)),
    search: (p) => S(p, C(11, 11, 7), L(21, 21, 16.65, 16.65)),
    bell: (p) => S(p, P("M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"), P("M13.7 21a2 2 0 0 1-3.4 0")),
    calendar: (p) => S(p, h("rect", { x: 3, y: 4, width: 18, height: 17, rx: 2 }), L(3, 9, 21, 9), L(8, 2, 8, 6), L(16, 2, 16, 6)),
    globe: (p) => S(p, C(12, 12, 9), L(3, 12, 21, 12), P("M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18")),
    chevR: (p) => S(p, P("M9 6l6 6-6 6")),
    chevD: (p) => S(p, P("M6 9l6 6 6-6")),
    chevL: (p) => S(p, P("M15 6l-6 6 6 6")),
    arrowUp: (p) => S(p, P("M12 19V5M6 11l6-6 6 6")),
    arrowDown: (p) => S(p, P("M12 5v14M6 13l6 6 6-6")),
    arrowRight: (p) => S(p, L(5, 12, 19, 12), P("M13 6l6 6-6 6")),
    flat: (p) => S(p, L(5, 12, 19, 12)),
    plus: (p) => S(p, L(12, 5, 12, 19), L(5, 12, 19, 12)),
    x: (p) => S(p, L(6, 6, 18, 18), L(18, 6, 6, 18)),
    check: (p) => S(p, P("M20 6 9 17l-5-5")),
    bolt: (p) => S(p, P("M13 2 4 14h7l-1 8 9-12h-7z")),
    clock: (p) => S(p, C(12, 12, 9), P("M12 7v5l3 2")),
    pin: (p) => S(p, P("M12 21s-7-6.3-7-11a7 7 0 0 1 14 0c0 4.7-7 11-7 11z"), C(12, 10, 2.4)),
    doc: (p) => S(p, P("M6 2h9l5 5v15H6z"), P("M15 2v5h5")),
    link: (p) => S(p, P("M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1"), P("M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7l1-1")),
    flame: (p) => S(p, P("M12 2c1 4-2 5-2 8a2 2 0 0 0 4 0c0-1 0-1.5-.3-2 2 1.3 3.3 3.5 3.3 6a5 5 0 0 1-10 0c0-4 3-7 5-12z")),
    shield: (p) => S(p, P("M12 3 5 6v6c0 4 3 7 7 9 4-2 7-5 7-9V6z")),
    target: (p) => S(p, C(12, 12, 9), C(12, 12, 5), C(12, 12, 1.4)),
    layers: (p) => S(p, P("M12 3 3 8l9 5 9-5z"), P("M3 13l9 5 9-5"), P("M3 16l9 5 9-5")),
    scale: (p) => S(p, L(12, 4, 12, 20), P("M7 8h10"), P("M5 8l-2.5 6h5z"), P("M19 8l-2.5 6h5z")),
    activity: (p) => S(p, P("M3 12h4l3 8 4-16 3 8h4")),
    trendUp: (p) => S(p, P("M3 17l6-6 4 4 8-8"), P("M17 7h4v4")),
    eye: (p) => S(p, P("M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"), C(12, 12, 3)),
    filter: (p) => S(p, P("M3 5h18l-7 8v6l-4-2v-4z")),
    grid: (p) => S(p, h("rect", { x: 3, y: 3, width: 7, height: 7, rx: 1 }), h("rect", { x: 14, y: 3, width: 7, height: 7, rx: 1 }), h("rect", { x: 3, y: 14, width: 7, height: 7, rx: 1 }), h("rect", { x: 14, y: 14, width: 7, height: 7, rx: 1 })),
    list: (p) => S(p, L(8, 6, 21, 6), L(8, 12, 21, 12), L(8, 18, 21, 18), L(3, 6, 3.01, 6), L(3, 12, 3.01, 12), L(3, 18, 3.01, 18)),
    sun: (p) => S(p, C(12, 12, 4), L(12, 2, 12, 4), L(12, 20, 12, 22), L(2, 12, 4, 12), L(20, 12, 22, 12), L(5, 5, 6.4, 6.4), L(17.6, 17.6, 19, 19), L(5, 19, 6.4, 17.6), L(17.6, 6.4, 19, 5)),
    moon: (p) => S(p, P("M21 12.8A8 8 0 1 1 11.2 3 6 6 0 0 0 21 12.8z")),
    spark: (p) => S(p, P("M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8")),
    refresh: (p) => S(p, P("M21 12a9 9 0 1 1-3-6.7L21 8"), P("M21 3v5h-5")),
    download: (p) => S(p, P("M12 3v12M7 10l5 5 5-5"), P("M5 21h14")),
    play: (p) => S(p, P("M6 4l14 8-14 8z")),
    pause: (p) => S(p, L(8, 5, 8, 19), L(16, 5, 16, 19)),
    dots: (p) => S(p, C(5, 12, 1.2), C(12, 12, 1.2), C(19, 12, 1.2)),
    quote: (p) => S(p, P("M7 7H4v6h3l-1 4h2l1-4V7zm10 0h-3v6h3l-1 4h2l1-4V7z")),
    book: (p) => S(p, P("M4 4h7a3 3 0 0 1 3 3v13a2.5 2.5 0 0 0-2.5-2.5H4zM20 4h-7a3 3 0 0 0-3 3v13a2.5 2.5 0 0 1 2.5-2.5H20z")),
    db: (p) => S(p, h("ellipse", { cx: 12, cy: 5, rx: 8, ry: 3 }), P("M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"), P("M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6")),
    cpu: (p) => S(p, h("rect", { x: 7, y: 7, width: 10, height: 10, rx: 1.5 }), L(9, 2, 9, 5), L(15, 2, 15, 5), L(9, 19, 9, 22), L(15, 19, 15, 22), L(2, 9, 5, 9), L(2, 15, 5, 15), L(19, 9, 22, 9), L(19, 15, 22, 15)),
    users: (p) => S(p, C(9, 8, 3.2), P("M3 20a6 6 0 0 1 12 0"), P("M16 5.2a3.2 3.2 0 0 1 0 5.6"), P("M18 14.5a6 6 0 0 1 3 5.5")),
    warn: (p) => S(p, P("M12 3 2 20h20z"), L(12, 9, 12, 14), L(12, 17, 12, 17.01)),
    info: (p) => S(p, C(12, 12, 9), L(12, 11, 12, 16), L(12, 8, 12, 8.01)),
  };

  window.Icon = Icon;
})();
