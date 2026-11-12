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
  const session = { user: null, conversationId: null };
  let examplesCache = [];

  const STATUS = {
    ok: ['ok', '已完成'],
    clarification: ['clarification', '需要澄清'],
    blocked: ['blocked', '已拦截'],
    error: ['error', '执行失败'],
    running: ['running', '运行中'],
  };

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

  // ------------------------------------------------------------------ 会话
  async function loadSession() {
    try {
      const res = await fetch('/api/me');
      const data = await res.json();
      if (data.authenticated) {
        session.user = data.user;
        return true;
      }
      return !data.auth_required;
    } catch {
      return false;
    }
  }

  function setUserChip(user) {
    if (!user) return;
    $('#user-chip').hidden = false;
    $('#user-name').textContent = user.name || user.username;
    $('#user-scope').textContent = `${user.scope} · ${user.quota_used}/${user.quota}`;
    $('#open-audit').hidden = !user.is_admin;
  }

  function showLogin(message = '') {
    $('#login').hidden = false;
    $('#app').hidden = true;
    document.querySelector('.topbar').style.display = 'none';
    const err = $('#login-error');
    if (message) {
      err.hidden = false;
      err.textContent = message;
      err.classList.remove('shake');
      void err.offsetWidth;
      err.classList.add('shake');
    }
    setTimeout(() => $('#login-user')?.focus(), 60);
  }

  async function doLogin(username, password) {
    const btn = $('#login-btn');
    btn.disabled = true;
    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || '登录失败');
      session.user = data.user;
      $('#login').hidden = true;
      $('#app').hidden = false;
    if (window.innerWidth < 1180) {
      $('.layout').classList.add('hide-inspector');
    } else {
      $('#toggle-inspector').classList.add('active');
    }
      document.querySelector('.topbar').style.display = '';
      setUserChip(session.user);
      await startApp();
    } catch (err) {
      showLogin(err.message || String(err));
    } finally {
      btn.disabled = false;
    }
  }

  async function logout() {
    await fetch('/api/logout', { method: 'POST' }).catch(() => {});
    location.href = '/';
  }

  function newChat() {
    session.conversationId = null;
    $('#thread').querySelectorAll('.msg').forEach((el) => el.remove());
    $('#new-chat').hidden = true;
    resetInspector();
    location.reload();
  }

  // ------------------------------------------------------------------ 指令面板
  const palette = { open: false, items: [], idx: 0, root: null, input: null, list: null };

  function paletteSource() {
    const qs = examplesCache.slice(0, 6).map((q) => ({ k: '问', t: q, run: () => submit(q) }));
    const acts = [
      { k: '动作', t: '新对话', run: () => location.reload() },
      {
        k: '动作',
        t: '切换运行轨迹',
        run: () => $('#toggle-inspector').click(),
      },
      { k: '动作', t: '打开语义资产', run: () => toggleDrawer(true) },
    ];
    if (session.user?.role === 'admin') acts.push({ k: '动作', t: '打开审计日志', run: () => openAudit() });
    acts.push({ k: '动作', t: '退出登录', run: () => logout() });
    return [...qs, ...acts];
  }

  function paletteRender() {
    const q = palette.input.value.trim().toLowerCase();
    palette.items = paletteSource().filter((it) => !q || it.t.toLowerCase().includes(q));
    palette.idx = Math.min(palette.idx, Math.max(0, palette.items.length - 1));
    palette.list.innerHTML = palette.items.length
      ? palette.items.map((it, i) => `
        <div class="palette-item" role="option" data-i="${i}" aria-selected="${i === palette.idx}">
          <span class="k">${it.k}</span><span class="t">${esc(it.t)}</span>
        </div>`).join('')
      : '<div class="palette-empty">没有匹配的指令</div>';
    palette.list.querySelectorAll('.palette-item').forEach((el) => {
      el.addEventListener('mousedown', (e) => {
        e.preventDefault();
        paletteRun(Number(el.dataset.i));
      });
    });
  }

  function paletteRun(i) {
    const item = palette.items[i];
    if (!item) return;
    closePalette();
    setTimeout(() => item.run(), 30);
  }

  function openPalette() {
    if (!palette.root) {
      const layer = document.createElement('div');
      layer.className = 'palette-layer';
      layer.innerHTML = `
        <div class="palette" role="dialog" aria-label="指令">
          <input id="palette-input" placeholder="输入问题，或搜索指令…" autocomplete="off" />
          <div class="palette-list" role="listbox"></div>
          <div class="palette-foot"><span>↑↓ 选择</span><span>Enter 执行</span><span>Esc 关闭</span></div>
        </div>`;
      layer.addEventListener('mousedown', (e) => {
        if (e.target === layer) closePalette();
      });
      document.body.appendChild(layer);
      palette.root = layer;
      palette.input = layer.querySelector('#palette-input');
      palette.list = layer.querySelector('.palette-list');
      palette.input.addEventListener('input', () => {
        palette.idx = 0;
        paletteRender();
      });
      palette.input.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          palette.idx = Math.min(palette.idx + 1, palette.items.length - 1);
          paletteRender();
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          palette.idx = Math.max(palette.idx - 1, 0);
          paletteRender();
        } else if (e.key === 'Enter') {
          e.preventDefault();
          paletteRun(palette.idx);
        }
      });
    }
    palette.root.hidden = false;
    palette.idx = 0;
    palette.input.value = '';
    paletteRender();
    palette.open = true;
    setTimeout(() => palette.input.focus(), 20);
  }

  function closePalette() {
    if (!palette.root) return;
    palette.root.hidden = true;
    palette.open = false;
    input.focus();
  }

  // ------------------------------------------------------------------ 审计
  async function openAudit() {
    $('#audit-body').innerHTML = '<div class="placeholder">加载中…</div>';
    $('#audit-drawer').classList.add('open');
    $('#backdrop').classList.add('show');
    try {
      const { rows, stats } = await (await fetch('/api/audit?limit=60')).json();
      const head = stats && stats.users !== undefined ? `<div class="audit-meta">
        <span class="audit-stat"><b>${stats.users}</b>个账号</span>
        <span class="audit-stat"><b>${stats.asks}</b>次提问</span>
        <span class="audit-stat"><b>${stats.blocked}</b>次拦截</span>
      </div>` : '';
      const list = rows.map((r) => `
        <div class="audit-row">
          <div class="audit-top">
            <span class="audit-user">${esc(r.username || '—')}</span>
            <span class="badge ${esc(r.status || '')}">${esc(r.status || '—')}</span>
            <span class="audit-time">${esc((r.ts || '').replace('T', ' ').slice(0, 19))}</span>
            <span class="audit-time">${r.rows ?? 0} 行 · ${r.ms ?? 0} ms</span>
          </div>
          <div class="audit-q">${esc(r.question || '')}</div>
          ${r.sql ? `<pre class="audit-sql">${esc(r.sql)}</pre>` : ''}
        </div>`).join('');
      $('#audit-body').innerHTML = head + (list || '<div class="placeholder">还没有记录</div>');
    } catch {
      $('#audit-body').innerHTML = '<div class="placeholder">审计日志加载失败</div>';
    }
  }

  // ------------------------------------------------------------------ health
  async function startApp() {
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
      $('#footer-note').textContent = '只读 · 上限 200 行 · ⌘K 指令';
      if (!h.ok) toast('数据库文件缺失，请先运行 make data');
    } catch {
      toast('后端未连接：请运行 make dev');
    }

    try {
      const { examples } = await (await fetch('/api/examples')).json();
      examplesCache = examples || [];
      const box = $('#suggest');
      examples.slice(0, 4).forEach((q) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'chip';
        btn.textContent = q;
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
        body: JSON.stringify({ question, conversation_id: session.conversationId }),
      });
      if (res.status === 401) {
        showLogin('登录已过期，请重新登录');
        return;
      }
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
            if (evt.data.conversation_id) {
              session.conversationId = evt.data.conversation_id;
              $('#new-chat').hidden = false;
            }
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

  function syncIntro() {
    const hasMsg = !!inner.querySelector('.msg');
    inner.classList.toggle('only-intro', !hasMsg);
  }

  function addUser(text) {
    const el = document.createElement('div');
    el.className = 'msg user';
    el.innerHTML = `<div class="bubble">${esc(text)}</div>`;
    inner.appendChild(el);
    syncIntro();
    scrollBottom();
  }

  function addAgentShell() {
    const el = document.createElement('div');
    el.className = 'msg agent';
    el.innerHTML = `
      <div class="bubble">
        <div class="answer-head"><span class="status running">运行中</span></div>
        <div class="answer is-pending">正在检索语义资产并规划查询…</div>
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
  function fmtKpi(v, isPct) {
    if (typeof v !== 'number') return esc(fmtCell(v));
    if (isPct) return `${v.toFixed(2)}%`;
    if (Math.abs(v) >= 1e8) return `${(v / 1e8).toFixed(2)}亿`;
    if (Math.abs(v) >= 1e4) return `${(v / 1e4).toFixed(1)}万`;
    return esc(fmtCell(v));
  }

  function buildKpis(data) {
    const rows = data.rows || [];
    const cols = data.columns || [];
    if (!rows.length) return [];
    const num = (v) => (typeof v === 'number' ? v : null);
    const valIdx = cols
      .map((c, i) => (rows.some((r) => num(r[i]) !== null) && !/月|日期|时间|date|month/i.test(c) ? i : -1))
      .filter((i) => i >= 0);
    if (!valIdx.length || valIdx.length > 2) return [];

    const i = valIdx[0];
    const name = cols[i];
    const isPct = /率|占比|比例|ratio|rate|share/i.test(name);
    const isAvg = /均|平均|avg/i.test(name);

    if (rows.length === 1) {
      return [{ label: name, html: fmtKpi(rows[0][i], isPct) }];
    }

    const labelIdx = cols.findIndex((c, k) => k !== i && rows.every((r) => num(r[k]) === null));
    const labelOf = (r) => (labelIdx >= 0 ? String(r[labelIdx] ?? '') : '');
    const vals = rows.map((r) => num(r[i]) ?? 0);
    const maxV = Math.max(...vals);
    const minV = Math.min(...vals);
    const maxRow = rows[vals.indexOf(maxV)];
    const out = [];

    if (!isPct && !isAvg) {
      out.push({ label: '合计', html: fmtKpi(vals.reduce((a, b) => a + b, 0), false) });
    }
    out.push({ label: `最高${labelOf(maxRow) ? ' · ' + labelOf(maxRow) : ''}`, html: fmtKpi(maxV, isPct) });
    if (isPct && rows.length > 2) {
      const minRow = rows[vals.indexOf(minV)];
      out.push({ label: `最低${labelOf(minRow) ? ' · ' + labelOf(minRow) : ''}`, html: fmtKpi(minV, isPct) });
    }
    return out.slice(0, 3);
  }

  function kpiStrip(items) {
    const box = document.createElement('div');
    box.className = 'kpis';
    box.innerHTML = items.map((k, i) => `
      <div class="kpi" style="--i:${i}"><span class="kpi-k">${esc(k.label)}</span><span class="kpi-v">${k.html}</span></div>`).join('');
    return box;
  }

  function callout(kind, title, text) {
    const el = document.createElement('div');
    el.className = `callout ${kind}`;
    el.innerHTML = `<span class="callout-ic" aria-hidden="true">${kind === 'error' ? '!' : kind === 'blocked' ? '×' : '?'}</span>
      <div class="callout-body"><strong>${esc(title)}</strong><p>${esc(text)}</p></div>`;
    return el;
  }

  function checksList(checks) {
    const box = document.createElement('div');
    box.className = 'checks';
    box.innerHTML = (checks || []).map((c) => `<div class="check ${c.status === 'pass' ? 'pass' : 'fail'}">
      <span class="ic">${c.status === 'pass' ? '✓' : '✗'}</span>
      <span><b>${esc(c.name)}</b> · ${esc(c.detail)}</span></div>`).join('');
    return box;
  }

  function resultBlock(data) {
    const wrap = document.createElement('div');
    wrap.className = 'result-block';
    const cols = data.columns || [];
    const rows = data.rows || [];
    const numeric = cols.map((_, i) => rows.some((r) => typeof r[i] === 'number'));
    const pct = cols.map((c) => /率|占比|比例|ratio|rate|share|percent/i.test(c));
    const cell = (v, i) => (pct[i] && typeof v === 'number' ? `${v.toFixed(2)}%` : fmtCell(v));

    const head = document.createElement('div');
    head.className = 'result-head';
    head.innerHTML = `<span>结果</span><span>${data.row_count} 行${data.truncated ? ' · 已截断' : ''}</span>`;

    const table = document.createElement('div');
    table.className = 'table-wrap';
    table.innerHTML = `<table class="result">
      <thead><tr>${cols.map((c, i) => `<th class="${numeric[i] ? 'num' : ''}">${esc(c)}</th>`).join('')}</tr></thead>
      <tbody>${rows.slice(0, 100).map((r) => `<tr>${r.map((v, i) =>
        `<td class="${numeric[i] ? 'num' : ''}">${esc(cell(v, i))}</td>`).join('')}</tr>`).join('')}</tbody>
    </table>`;

    wrap.append(head, table);
    if (data.row_count > 100) {
      const note = document.createElement('div');
      note.className = 'result-note';
      note.textContent = `仅显示前 100 行，共 ${data.row_count} 行`;
      wrap.appendChild(note);
    }
    return wrap;
  }

  function renderFinal(view, data) {
    const [cls, label] = STATUS[data.status] || ['ok', data.status];
    stepsEl.querySelector('.step.running')?.remove();
    const bubble = view.bubble;
    bubble.innerHTML = '';

    const head = document.createElement('div');
    head.className = 'answer-head';
    head.innerHTML = `<span class="status ${cls}">${label}</span>`;
    bubble.appendChild(head);

    if (data.status === 'blocked') {
      bubble.appendChild(callout('blocked', '请求已拦截', data.answer || ''));
    } else if (data.status === 'clarification') {
      bubble.appendChild(callout('clarification', '需要补充一点信息', data.answer || ''));
    } else if (data.status === 'error') {
      bubble.appendChild(callout('error', '执行失败', data.answer || '请换一种问法。'));
    } else {
      const answer = document.createElement('div');
      answer.className = 'answer';
      answer.textContent = data.answer || '';
      bubble.appendChild(answer);

      const kpis = buildKpis(data);
      if (kpis.length) bubble.appendChild(kpiStrip(kpis));
      const single = data.row_count === 1 && (data.columns || []).length <= 2 && kpis.length > 0;
      if (data.row_count > 0 && !single) bubble.appendChild(resultBlock(data));
    }

    if (data.assumptions?.length) {
      const box = document.createElement('div');
      box.className = 'assumptions';
      box.innerHTML = data.assumptions.map((a) => `<span class="tag">口径 · ${esc(a)}</span>`).join('');
      bubble.appendChild(box);
    }

    if (data.sql || (data.checks || []).length) {
      const folds = document.createElement('div');
      folds.className = 'folds';
      const sqlText = data.sql_pretty || data.sql;
      if (sqlText) folds.appendChild(sqlFold(sqlText));
      if ((data.checks || []).length) {
        const det = document.createElement('details');
        det.className = 'fold';
        det.innerHTML = `<summary>校验项<span class="count">${data.checks.length} 项</span></summary>`;
        const body = document.createElement('div');
        body.className = 'fold-body';
        body.appendChild(checksList(data.checks));
        det.appendChild(body);
        folds.appendChild(det);
      }
      bubble.appendChild(folds);
    }

    const meta = document.createElement('div');
    meta.className = 'meta-row';
    const bits = [fmtDuration(data.timings?.total_ms || 0), `${data.row_count || 0} 行`];
    if (data.scope) bits.push(`数据域 ${esc(data.scope)}`);
    if (data.mode !== 'live') bits.push('离线模板');
    meta.textContent = bits.join(' · ');
    bubble.appendChild(meta);

    evidenceEl.innerHTML = (data.evidence || []).map((e) => `
      <span class="ev ${e.kind}" title="相关度 ${e.score}">
        <b>${esc(e.title)}</b> · ${esc(e.kind)}
      </span>`).join('') || '<span class="ev">无命中</span>';
    sqlEl.textContent = data.sql_pretty || data.sql || '-- 本次未生成 SQL';
    if (Array.isArray(data.checks) && data.checks.length) renderChecks(data.checks);
    totalTimeEl.textContent = fmtDuration(data.timings?.total_ms || 0);
    scrollBottom();
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
    view.bubble.innerHTML = '';
    const head = document.createElement('div');
    head.className = 'answer-head';
    head.innerHTML = '<span class="status error">执行失败</span>';
    view.bubble.append(head, callout('error', '这次没跑通', message));
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
  $('#close-audit').addEventListener('click', () => {
    $('#audit-drawer').classList.remove('open');
    $('#backdrop').classList.remove('show');
  });
  $('#backdrop').addEventListener('click', () => {
    toggleDrawer(false);
    $('#audit-drawer').classList.remove('open');
    $('#backdrop').classList.remove('show');
  });
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      if ($('#login').hidden) {
        palette.open ? closePalette() : openPalette();
      }
      return;
    }
    if (e.key === 'Escape' && palette.open) {
      closePalette();
      return;
    }
    if (e.key === 'Escape') {
      toggleDrawer(false);
      $('#audit-drawer').classList.remove('open');
      $('#backdrop').classList.remove('show');
    }
  });

  $('#login-form').addEventListener('submit', (e) => {
    e.preventDefault();
    doLogin($('#login-user').value.trim(), $('#login-pass').value);
  });
  document.querySelectorAll('.demo-row').forEach((btn) => {
    btn.addEventListener('click', () => {
      $('#login-user').value = btn.dataset.user;
      $('#login-pass').value = btn.dataset.pass;
      doLogin(btn.dataset.user, btn.dataset.pass);
    });
  });
  $('#logout').addEventListener('click', logout);
  $('#new-chat').addEventListener('click', newChat);
  $('#toggle-inspector').addEventListener('click', () => {
    const layout = $('.layout');
    layout.classList.toggle('hide-inspector');
    $('#toggle-inspector').classList.toggle('active', !layout.classList.contains('hide-inspector'));
  });
  $('#open-audit').addEventListener('click', openAudit);

  (async () => {
    const ok = await loadSession();
    if (!ok) {
      showLogin();
      return;
    }
    $('#app').hidden = false;
    if (window.innerWidth < 1180) {
      $('.layout').classList.add('hide-inspector');
    } else {
      $('#toggle-inspector').classList.add('active');
    }
    if (session.user) setUserChip(session.user);
    syncIntro();
    await startApp();
  })();
})();
