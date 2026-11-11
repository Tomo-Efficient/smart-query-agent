/* 智能问数 Agent · 控制台前端 */
(() => {
  const $ = (sel) => document.querySelector(sel);
  const thread = $('#thread');
  const inner = $('.thread-inner');
  const input = $('#input');
  const sendBtn = $('#send');
  const stepsEl = $('#steps');
  const evidenceEl = $('#evidence');
  const sqlEl = $('#sql');
  const checksEl = $('#checks');
  const totalTimeEl = $('#total-time');

  let busy = false;
  let semanticsLoaded = false;

  const STATUS = {
    ok: ['ok', '已完成'],
    clarification: ['clarification', '需要澄清'],
    blocked: ['blocked', '已拦截'],
    error: ['error', '执行失败'],
    running: ['running', '运行中'],
  };

  const HINTS = [
    [/环比|同比/, '时间对比'],
    [/件均|平均/, '指标口径'],
    [/占比|结构/, '结构分析'],
    [/退保/, '比率指标'],
    [/趋势|每月/, '趋势序列'],
    [/多少/, '单值查询'],
    [/手机号|身份证/, '安全拦截演示'],
    [/减去|然后/, '澄清演示'],
  ];

  function hintFor(q) {
    for (const [re, hint] of HINTS) if (re.test(q)) return hint;
    return '自由提问';
  }

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function fmtCell(v) {
    if (v === null || v === undefined) return '—';
    if (typeof v === 'number') {
      if (!Number.isInteger(v)) return v.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
      return v.toLocaleString('zh-CN');
    }
    if (typeof v === 'string') {
      const m = v.match(/^(\d{4})-(\d{2})-(\d{2})(?:T[\d:.]+)?$/);
      if (m) return m[3] === '01' ? `${m[1]}-${m[2]}` : v.slice(0, 10);
    }
    return String(v);
  }

  function fmtDuration(ms) {
    const n = Math.max(0, Math.round(ms || 0));
    return n >= 1000 ? `${(n / 1000).toFixed(1)} s` : `${n} ms`;
  }

  function toast(msg) {
    const el = $('#toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove('show'), 2600);
  }

  // ------------------------------------------------------------------ health
  async function boot() {
    try {
      const h = await (await fetch('/api/health')).json();
      $('#as-of').innerHTML = `<span class="dot"></span>数据截止 <b>${esc(h.as_of)}</b>`;
      const mode = $('#mode');
      if (h.mode === 'live') {
        mode.className = 'pill';
        mode.innerHTML = `<span class="dot"></span>在线 <b>${esc(h.model)}</b>`;
      } else {
        mode.className = 'pill mock';
        mode.innerHTML = `<span class="dot"></span>离线演示 <b>模板生成</b>`;
      }
      $('#card-count').textContent = h.cards;
      $('#footer-note').textContent = `只读查询 · 单条 SQL · ${h.tables} 张表 / ${h.cards} 张卡片`;
      if (!h.ok) toast('数据库文件缺失，请先运行 make data');
    } catch {
      toast('后端未连接：请运行 make dev');
    }

    try {
      const { examples } = await (await fetch('/api/examples')).json();
      const box = $('#suggest');
      examples.slice(0, 4).forEach((q) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'chip';
        btn.innerHTML = `<strong>${esc(q)}</strong><span>${esc(hintFor(q))}</span>`;
        btn.addEventListener('click', () => submit(q));
        box.appendChild(btn);
      });
    } catch { /* ignore */ }

    // 支持 ?q= 预填问题（演示深链、截图与分享用）
    const preset = new URLSearchParams(location.search).get('q');
    if (preset) setTimeout(() => submit(preset), 80);
  }

  // ------------------------------------------------------------------ stream
  async function submit(question) {
    question = (question || '').trim();
    if (!question || busy) return;
    busy = true;
    sendBtn.disabled = true;
    $('#intro')?.remove();

    addUser(question);
    const view = addAgentShell();
    resetInspector();

    const t0 = performance.now();
    try {
      const res = await fetch('/api/ask/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
      });
      if (!res.ok || !res.body) {
        const text = await res.text().catch(() => '');
        throw new Error(res.status === 429 ? '提问太频繁，稍等一分钟再试' : (text || `HTTP ${res.status}`));
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let finished = false;

      while (!finished) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf('\n\n')) >= 0) {
          const chunk = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const line = chunk.split('\n').find((l) => l.startsWith('data: '));
          if (!line) continue;
          let evt;
          try { evt = JSON.parse(line.slice(6)); } catch { continue; }
          if (evt.type === 'step') {
            pushStep(evt.step);
          } else if (evt.type === 'final') {
            renderFinal(view, evt.data);
            finished = true;
          } else if (evt.type === 'error') {
            renderFailure(view, evt.message);
            finished = true;
          }
        }
      }
      if (!finished) renderFailure(view, '连接中断，未收到最终结果');
    } catch (err) {
      renderFailure(view, err.message || String(err));
    } finally {
      busy = false;
      sendBtn.disabled = false;
      view.live?.remove();
      totalTimeEl.textContent = fmtDuration(performance.now() - t0);
      scrollBottom();
    }
  }

  function addUser(text) {
    const el = document.createElement('div');
    el.className = 'msg user';
    el.innerHTML = `<div class="avatar">你</div><div class="bubble">${esc(text)}</div>`;
    inner.appendChild(el);
    scrollBottom();
  }

  function addAgentShell() {
    const el = document.createElement('div');
    el.className = 'msg agent';
    el.innerHTML = `
      <div class="avatar">数</div>
      <div class="bubble">
        <div class="answer-head"><span class="status running">运行中</span></div>
        <div class="answer" style="color:var(--faint);font-family:var(--sans);font-size:13px">正在检索语义资产并规划查询…</div>
      </div>`;
    inner.appendChild(el);
    scrollBottom();
    return { root: el, bubble: el.querySelector('.bubble'), live: null };
  }

  function resetInspector() {
    stepsEl.innerHTML = '';
    evidenceEl.innerHTML = '';
    checksEl.innerHTML = '';
    sqlEl.textContent = '-- 生成中';
    totalTimeEl.textContent = '—';
    $('#trace-empty')?.remove();
    $('#evidence-empty')?.remove();
    const live = document.createElement('li');
    live.className = 'step running';
    live.innerHTML = '<div class="dot"></div><div class="t"><strong>运行中…</strong><span>—</span></div>';
    stepsEl.appendChild(live);
    return live;
  }

  // ------------------------------------------------------------------ steps
  function stepEl(step) {
    const li = document.createElement('li');
    li.className = `step ${step.status || 'done'}`;
    const icon = step.status === 'fail' ? '×' : '';
    li.innerHTML = `
      <div class="dot"></div>
      <div class="t"><strong>${esc(step.title)}</strong><span>${step.ms != null ? step.ms + ' ms' : ''}</span></div>
      <div class="d">${esc(step.detail || '')}</div>`;
    if (icon) li.querySelector('.dot').textContent = '';
    return li;
  }

  function pushStep(step) {
    const live = stepsEl.querySelector('.step.running');
    const li = stepEl(step);
    if (live) stepsEl.insertBefore(li, live);
    else stepsEl.appendChild(li);

    if (step.key === 'retrieve' && step.detail) {
      // 证据 chips 由 final 覆盖；这里先显示访问过的卡片数
    }
    if (step.key === 'guard' && Array.isArray(step.checks)) renderChecks(step.checks);
    if (step.key === 'generate' && step.sql) sqlEl.textContent = step.sql;
    if (step.key === 'repair' && step.sql) sqlEl.textContent = step.sql;
    scrollBottom();
  }

  function renderChecks(checks) {
    checksEl.innerHTML = checks.map((c) => `
      <div class="check ${c.status === 'pass' ? 'pass' : 'fail'}">
        <span class="ic">${c.status === 'pass' ? '✓' : '×'}</span>
        <span><b>${esc(c.name)}</b>${c.detail ? ' · ' + esc(c.detail) : ''}</span>
      </div>`).join('');
  }

  // ------------------------------------------------------------------ final
  function renderFinal(view, data) {
    const [cls, label] = STATUS[data.status] || ['ok', data.status];
    stepsEl.querySelector('.step.running')?.remove();
    view.bubble.querySelector('.status')?.remove();
    view.bubble.querySelector('.answer')?.remove();

    const head = document.createElement('div');
    head.className = 'answer-head';
    head.innerHTML = `<span class="status ${cls}">${label}</span>`;

    const answer = document.createElement('div');
    answer.className = 'answer';
    answer.textContent = data.answer || '';

    view.bubble.innerHTML = '';
    view.bubble.append(head, answer);

    if (data.assumptions?.length) {
      const box = document.createElement('div');
      box.className = 'assumptions';
      box.innerHTML = data.assumptions.map((a) => `<span class="tag">口径：${esc(a)}</span>`).join('');
      view.bubble.appendChild(box);
    }

    const meta = document.createElement('div');
    meta.className = 'meta-row';
    const bits = [
      `路由 <b>${esc(data.route?.label || '—')}</b>`,
      `模型 <b>${esc(data.mode === 'live' ? data.model : '离线模板')}</b>`,
      `耗时 <b>${data.timings?.total_ms ? data.timings.total_ms + ' ms' : '<1 ms'}</b>`,
      `SQL <b>${data.timings?.sql_ms ?? 0} ms</b>`,
      `Token <b>${(data.usage?.input_tokens || 0)} / ${(data.usage?.output_tokens || 0)}</b>`,
    ];
    meta.innerHTML = bits.map((b) => `<span>${b}</span>`).join('');
    view.bubble.appendChild(meta);

    if (data.row_count > 0) {
      view.bubble.appendChild(tableFold(data));
    }

    if (data.sql) {
      view.bubble.appendChild(sqlFold(data.sql));
    }

    // inspector
    evidenceEl.innerHTML = (data.evidence || []).map((e) => `
      <span class="ev ${e.kind}" title="相关度 ${e.score}">
        <b>${esc(e.title)}</b> · ${esc(e.kind)}
      </span>`).join('') || '<span class="ev">无命中</span>';
    sqlEl.textContent = data.sql || '-- 本次未生成 SQL';
    if (Array.isArray(data.checks) && data.checks.length) renderChecks(data.checks);
    totalTimeEl.textContent = fmtDuration(data.timings?.total_ms || 0);
    scrollBottom();
  }

  function tableFold(data) {
    const det = document.createElement('details');
    det.className = 'fold';
    const truncated = data.truncated ? ' · 已截断' : '';
    det.innerHTML = `<summary>结果表<span class="count">${data.row_count} 行${truncated}</span></summary>`;

    const wrap = document.createElement('div');
    wrap.className = 'fold-body';
    const cols = data.columns || [];
    const numeric = cols.map((_, i) => data.rows?.some((r) => typeof r[i] === 'number'));
    const table = document.createElement('div');
    table.className = 'table-wrap';
    table.innerHTML = `<table class="result">
      <thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead>
      <tbody>${(data.rows || []).slice(0, 100).map((r) => `<tr>${r.map((v, i) =>
        `<td class="${numeric[i] ? 'num' : ''}">${esc(fmtCell(v))}</td>`).join('')}</tr>`).join('')}</tbody>
    </table>`;
    wrap.appendChild(table);
    if (data.row_count > 100) {
      const note = document.createElement('div');
      note.className = 'empty-rows';
      note.textContent = `仅显示前 100 行，共 ${data.row_count} 行`;
      wrap.appendChild(note);
    }
    det.appendChild(wrap);
    return det;
  }

  function sqlFold(sql) {
    const det = document.createElement('details');
    det.className = 'fold';
    det.innerHTML = `<summary>查看 SQL<span class="count">DuckDB</span></summary>`;
    const body = document.createElement('div');
    body.className = 'fold-body';
    const pre = document.createElement('pre');
    pre.className = 'sql';
    pre.textContent = sql;
    const btn = document.createElement('button');
    btn.className = 'ghost-btn';
    btn.type = 'button';
    btn.style.marginTop = '8px';
    btn.textContent = '复制 SQL';
    btn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(sql);
        btn.textContent = '已复制';
        setTimeout(() => (btn.textContent = '复制 SQL'), 1500);
      } catch { toast('复制失败，请手动选择'); }
    });
    body.append(pre, btn);
    det.appendChild(body);
    return det;
  }

  function renderFailure(view, message) {
    view.bubble.innerHTML = `
      <div class="answer-head"><span class="status error">执行失败</span></div>
      <div class="answer" style="font-family:var(--sans);font-size:13.5px">${esc(message)}</div>
      <div class="meta-row">可以重试一次，或把问题描述得更具体（指标 + 时间 + 维度）。</div>`;
    toast('请求失败');
    scrollBottom();
  }

  function scrollBottom() {
    thread.scrollTop = thread.scrollHeight;
  }

  // ------------------------------------------------------------------ drawer
  async function toggleDrawer(open) {
    $('#drawer').classList.toggle('open', open);
    $('#backdrop').classList.toggle('open', open);
    if (open && !semanticsLoaded) {
      semanticsLoaded = true;
      $('#drawer-body').innerHTML = '<div class="placeholder">加载中…</div>';
      try {
        const data = await (await fetch('/api/semantics')).json();
        const KIND = { metric: '指标定义', table: '表卡', join: '关系卡', term: '术语卡' };
        $('#drawer-body').innerHTML = Object.entries(KIND).map(([kind, label]) => {
          const cards = data.cards.filter((c) => c.kind === kind);
          return `<div class="section-label">${label} · ${cards.length}</div>` + cards.map((c) => `
            <div class="card">
              <div class="k">${esc(c.id)}</div>
              <h3>${esc(c.title)}</h3>
              <p>${esc(c.text)}</p>
              ${c.tags?.length ? `<div class="tags">${c.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join('')}</div>` : ''}
            </div>`).join('');
        }).join('');
      } catch {
        semanticsLoaded = false;
        $('#drawer-body').innerHTML = '<div class="placeholder">语义层接口未响应</div>';
      }
    }
  }

  // ------------------------------------------------------------------ events
  $('#composer').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = input.value;
    input.value = '';
    autoGrow();
    submit(q);
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $('#composer').requestSubmit();
    }
  });

  function autoGrow() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  }
  input.addEventListener('input', autoGrow);

  $('#open-drawer').addEventListener('click', () => toggleDrawer(true));
  $('#close-drawer').addEventListener('click', () => toggleDrawer(false));
  $('#backdrop').addEventListener('click', () => toggleDrawer(false));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') toggleDrawer(false);
  });

  boot();
})();
