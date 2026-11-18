/* 结果图：不引第三方库，只用 SVG 画两种图，黑白线框保持同一套语言
   - 时间序列（首列是月份）→ 折线 + 面积
   - 分类对比 / 占比 / 排名 → 横向条形
   设计约束：不靠颜色表达含义，粗细与位置承担全部信息。 */
window.SQChart = (() => {
  const MONTH = /^(月|月份|month|date|stat_month|时间)$/i;

  function compact(v, pct) {
    if (typeof v !== 'number' || !isFinite(v)) return '—';
    if (pct) return `${v.toFixed(2)}%`;
    const a = Math.abs(v);
    if (a >= 1e8) return `${(v / 1e8).toFixed(2)}亿`;
    if (a >= 1e4) return `${(v / 1e4).toFixed(a >= 1e6 ? 0 : 1)}万`;
    if (a >= 100) return `${Math.round(v).toLocaleString('zh-CN')}`;
    return `${v.toLocaleString('zh-CN', { maximumFractionDigits: 1 })}`;
  }

  function pickValueCol(cols, rows) {
    const idx = cols
      .map((c, i) => (rows.some((r) => typeof r[i] === 'number') ? i : -1))
      .filter((i) => i >= 0);
    if (!idx.length) return -1;
    // 优先带"率/占比/金额/保费/件数"的列，避免把 id 当数值
    const rate = idx.find((i) => /率|占比|比例|ratio|rate|share|percent/i.test(cols[i]));
    if (rate != null) return rate;
    const amount = idx.find((i) => /保费|金额|收入|件数|premium|amount|count/i.test(cols[i]));
    return amount != null ? amount : idx[0];
  }

  function svgEl(tag, attrs) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs || {}).forEach(([k, v]) => el.setAttribute(k, v));
    return el;
  }

  function figure(caption) {
    const fig = document.createElement('figure');
    fig.className = 'chart';
    const cap = document.createElement('figcaption');
    cap.className = 'chart-cap';
    cap.innerHTML = `<span>${caption}</span>`;
    fig.appendChild(cap);
    return fig;
  }

  function lineChart(labels, values, pct, caption) {
    const narrow = window.innerWidth < 640;
    const W = narrow ? 360 : 720;
    const H = narrow ? 168 : 190;
    const pad = narrow ? { t: 16, r: 10, b: 24, l: 48 } : { t: 18, r: 16, b: 26, l: 58 };
    const max = Math.max(...values, 0);
    const min = Math.min(...values, 0);
    const span = max - min || 1;
    const x = (i) => pad.l + (i * (W - pad.l - pad.r)) / Math.max(labels.length - 1, 1);
    const y = (v) => pad.t + (1 - (v - min) / span) * (H - pad.t - pad.b);

    const fig = figure(caption);
    const svg = svgEl('svg', {
      viewBox: `0 0 ${W} ${H}`, class: 'chart-svg', role: 'img',
      'aria-label': `${caption}：从 ${labels[0]} 的 ${compact(values[0], pct)} 到 ${labels[labels.length - 1]} 的 ${compact(values[values.length - 1], pct)}`,
    });

    // 三条横线 + 刻度
    [0, 0.5, 1].forEach((t) => {
      const v = min + span * t;
      const yy = y(v);
      svg.appendChild(svgEl('line', { x1: pad.l, x2: W - pad.r, y1: yy, y2: yy, class: 'chart-grid' }));
      const label = svgEl('text', { x: pad.l - 8, y: yy + 3.5, class: 'chart-tick', 'text-anchor': 'end' });
      label.textContent = compact(v, pct);
      svg.appendChild(label);
    });

    const pts = values.map((v, i) => [x(i), y(v)]);
    const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
    const area = `${line} L${pts[pts.length - 1][0].toFixed(1)},${y(min)} L${pts[0][0].toFixed(1)},${y(min)} Z`;

    svg.appendChild(svgEl('path', { d: area, class: 'chart-area' }));
    svg.appendChild(svgEl('path', { d: line, class: 'chart-line', pathLength: '1' }));
    pts.forEach((p, i) => {
      const isLast = i === pts.length - 1;
      svg.appendChild(svgEl('circle', {
        cx: p[0], cy: p[1], r: isLast ? 3 : 1.8,
        class: isLast ? 'chart-dot chart-dot-last' : 'chart-dot',
      }));
    });

    // 只标首、中、尾，避免拥挤
    const marks = narrow
      ? [0, labels.length - 1]
      : labels.length > 3 ? [0, Math.floor((labels.length - 1) / 2), labels.length - 1] : labels.map((_, i) => i);
    marks.forEach((i) => {
      const t = svgEl('text', {
        x: x(i), y: H - 8, class: 'chart-tick',
        'text-anchor': i === 0 ? 'start' : i === labels.length - 1 ? 'end' : 'middle',
      });
      t.textContent = labels[i];
      svg.appendChild(t);
    });

    // 末值直接标在点上
    const last = pts[pts.length - 1];
    const val = svgEl('text', {
      x: last[0] - 6, y: last[1] - 10, class: 'chart-value', 'text-anchor': 'end',
    });
    val.textContent = compact(values[values.length - 1], pct);
    svg.appendChild(val);

    fig.appendChild(svg);
    return fig;
  }

  function barChart(labels, values, pct, caption) {
    const narrow = window.innerWidth < 640;
    const W = narrow ? 360 : 720;
    const rowH = narrow ? 28 : 30;
    const pad = { t: 8, r: narrow ? 8 : 16, b: 8, l: narrow ? 86 : 118 };
    const H = pad.t + pad.b + rowH * labels.length;
    const max = Math.max(...values.map((v) => Math.abs(v)), 0) || 1;
    const track = W - pad.l - pad.r - (narrow ? 62 : 84);

    const fig = figure(caption);
    const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart-svg', role: 'img', 'aria-label': caption });

    labels.forEach((label, i) => {
      const top = pad.t + i * rowH;
      const cy = top + rowH / 2;
      const w = (Math.abs(values[i]) / max) * track;

      const name = svgEl('text', { x: pad.l - 10, y: cy + 4, class: 'chart-label', 'text-anchor': 'end' });
      const maxChars = narrow ? 6 : 9;
      name.textContent = label.length > maxChars ? `${label.slice(0, maxChars - 1)}…` : label;
      svg.appendChild(name);

      svg.appendChild(svgEl('rect', {
        x: pad.l, y: cy - 5, width: track, height: 10, class: 'chart-track',
      }));
      if (w > 0.5) {
        const bar = svgEl('rect', {
          x: pad.l, y: cy - 5, width: w, height: 10, class: 'chart-bar',
          style: `--i:${i}`,
        });
        svg.appendChild(bar);
      }

      const value = svgEl('text', { x: W - pad.r, y: cy + 4, class: 'chart-value chart-value-row', 'text-anchor': 'end' });
      value.textContent = compact(values[i], pct);
      svg.appendChild(value);
    });

    fig.appendChild(svg);
    return fig;
  }

  function render(data) {
    const cols = data.columns || [];
    const rows = data.rows || [];
    if (!cols.length || rows.length < 3 || rows.length > 24) return null;

    const vi = pickValueCol(cols, rows);
    if (vi < 0) return null;
    const name = cols[vi];
    const pct = /率|占比|比例|ratio|rate|share|percent/i.test(name);

    const labelIdx = cols.findIndex((c, i) => i !== vi && rows.every((r) => typeof r[i] !== 'number'));
    if (labelIdx < 0) return null;
    const labels = rows.map((r) => String(r[labelIdx] ?? '').replace(/-01$/, ''));
    const values = rows.map((r) => (typeof r[vi] === 'number' ? r[vi] : 0));

    const monthLike = MONTH.test(cols[labelIdx]) || labels.every((l) => /^\d{4}-\d{2}/.test(l));
    const clean = name.replace(/[_\s]*(元|万元)$/, '');
    const dim = String(cols[labelIdx] || '').replace(/(名称|_name|_code|渠道|险种)$/g, '') || '分组';
    const caption = monthLike ? `${clean} · 月度趋势` : `${clean} · 按${dim || '分组'}`;

    if (monthLike && rows.length >= 4) return lineChart(labels, values, pct, caption);
    if (rows.length <= 12) return barChart(labels, values, pct, caption);
    return null;
  }

  return { render };
})();
