/* Hermes Skill Dashboard - local management single page app */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const STATUS = {
    confirmed: ['已确认', 'ok'],
    gated_by_platform: ['平台门控', 'warn'],
    archived: ['已归档', ''],
    pending: ['待确认', 'warn'],
    unknown: ['未知', ''],
  };
  const SOURCE = {
    builtin: '内置',
    local: '本地',
    'builtin-shadow': '内置覆盖',
    external: '外部',
    unknown: '未知',
  };
  /* 产生方式：后端按清单与 ledger 证据推断，界面只显示与手工更正 */
  const ORIGIN = {
    official: ['官方发布', ''],
    hermes: ['Hermes 生成', 'origin-hermes'],
    user: ['用户引入', 'origin-user'],
    unknown: ['待确认', 'warn'],
  };
  const ORIGIN_ORDER = ['official', 'hermes', 'user', 'unknown'];
  const originLabel = (v) => ORIGIN[v] || [v || '待确认', ''];
  const ORIGIN_LABELS = Object.fromEntries(ORIGIN_ORDER.map((k) => [k, ORIGIN[k][0]]));
  /* 分类显示名：API 里仍是英文 slug（筛选、排序都按 slug 走），只在界面上翻成中文 */
  const CATEGORY = {
    'apple': '苹果生态',
    'autonomous-ai-agents': '智能体协作',
    'creative': '创意设计',
    'desktop': '桌面控制',
    'devops': '研发运维',
    'email': '邮件处理',
    'media': '媒体处理',
    'note-taking': '笔记知识',
    'productivity': '效率办公',
    'research': '研究检索',
    'social-media': '社交媒体',
    'software-development': '软件开发',
    'troubleshooting': '排障诊断',
    'web': '网页与抓取',
    '未分类': '未分类',
  };
  const catLabel = (v) => CATEGORY[v] || v || '未分类';
  /* 严重程度与重复判定：值仍是英文枚举，界面显示中文 */
  const LEVEL = { High: '高危', Medium: '中危', Low: '低危' };
  const DUP_CLASS = {
    'High Overlap': '高度重叠',
    'Partial Overlap': '部分重叠',
    'Complementary': '互补',
    'Independent': '相互独立',
  };
  const lvLabel = (v) => LEVEL[v] || v || '—';
  const dupLabel = (v) => DUP_CLASS[v] || v || '—';
  /* 中文简介：来自 static/desc-zh.js（由 skm_desc_zh.py 维护），缺失时回退英文原文 */
  const DESC_ZH = (typeof window !== 'undefined' && window.SKM_DESC_ZH) || {};
  const zhDesc = (name) => DESC_ZH[name] || '';
  const descOf = (x) => zhDesc(x.name) || x.description || '';
  const REPORT_KIND_NAME = { daily: '日报', weekly: '周报', monthly: '月报', compare: '盘点对比' };
  const AUDIT_KIND_NAME = { quality: '质量', security: '安全', duplication: '重复', updates: '更新' };

  const state = {
    skills: { page: 1, pageSize: 20, q: '', category: '', status: '', source: '', origin: '', sort: 'name', order: 'asc' },
    reports: { kind: 'daily' },
    audits: {
      tab: 'overview', page: 1, pageSize: 20, q: '', grade: '', level: '', update: '',
      sort: 'quality', order: 'desc', dupQ: '', dupClass: '',
    },
    approvals: { tab: 'pending', log: 'change', current: null, currentId: null },
    csrf: '',
    loaded: {},
    fav: null,
  };

  /* ---------------- helpers ---------------- */

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    const units = ['KB', 'MB', 'GB'];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return n.toFixed(n < 10 ? 2 : 1) + ' ' + units[i];
  }

  function fmtTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return esc(iso);
    return d.toLocaleString('zh-CN', { hour12: false });
  }

  function badge(text, tone) {
    return `<span class="badge ${tone || ''}">${esc(text)}</span>`;
  }

  function skeleton(rows = 4) {
    const widths = ['42%', '86%', '68%', '92%', '56%', '74%'];
    let html = '<div class="skeleton" aria-hidden="true">';
    for (let i = 0; i < rows; i++) {
      html += `<div class="sk-row"><span class="sk-bar${i === 0 ? ' tall' : ''}" style="width:${widths[i % widths.length]}"></span></div>`;
    }
    return html + '</div>';
  }

  function shortHash(value) {
    return value ? String(value).slice(0, 16) + '…' : '—';
  }

  async function api(path, params) {
    const url = new URL(path, location.origin);
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        if (v !== '' && v != null) url.searchParams.set(k, v);
      }
    }
    const res = await fetch(url, {
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
    let body = null;
    try { body = await res.json(); } catch (e) { /* non-json */ }
    if (res.status === 401) {
      handleUnauthorized();
      throw new Error('会话已过期，请重新登录');
    }
    if (!res.ok) {
      const detail = body && body.detail ? body.detail : `HTTP ${res.status}`;
      const err = new Error(typeof detail === 'string' ? detail : (detail.message || JSON.stringify(detail)));
      err.detail = detail;
      throw err;
    }
    return body;
  }

  async function apiPost(path, payload) {
    const res = await fetch(new URL(path, location.origin), {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'X-CSRF-Token': state.csrf,
      },
      body: JSON.stringify(payload || {}),
    });
    let body = null;
    try { body = await res.json(); } catch (e) { /* non-json */ }
    if (res.status === 401) {
      handleUnauthorized();
      throw new Error('会话已过期，请重新登录');
    }
    if (!res.ok) {
      const detail = body && body.detail ? body.detail : `HTTP ${res.status}`;
      const err = new Error(typeof detail === 'string' ? detail : (detail.message || detail.error || JSON.stringify(detail)));
      err.detail = detail;
      throw err;
    }
    return body;
  }

  async function apiWrite(method, path, payload) {
    const opts = {
      method,
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        'X-CSRF-Token': state.csrf,
      },
    };
    if (payload !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(payload);
    }
    const res = await fetch(new URL(path, location.origin), opts);
    let body = null;
    try { body = await res.json(); } catch (e) { /* non-json */ }
    if (res.status === 401) {
      handleUnauthorized();
      throw new Error('会话已过期，请重新登录');
    }
    if (!res.ok) {
      const detail = body && body.detail ? body.detail : `HTTP ${res.status}`;
      const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
      err.detail = detail;
      throw err;
    }
    return body;
  }

  let unauthorizedHandled = false;  async function handleUnauthorized() {
    if (unauthorizedHandled) return;
    unauthorizedHandled = true;
    state.csrf = '';
    try {
      const res = await fetch('/api/auth/status', { credentials: 'same-origin' });
      const s = await res.json();
      showAuthGate(Boolean(s.setup_required));
    } catch (e) {
      showAuthGate(false);
    }
  }

  function toast(message, tone) {
    const el = $('#toast');
    if (!el) return;
    el.textContent = message;
    el.className = 'toast ' + (tone || '');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { el.className = 'toast hidden'; }, 4200);
  }

  function openModal(opts) {
    const modal = $('#modal');
    $('#modal-title').textContent = opts.title || '确认';
    $('#modal-body').innerHTML = opts.body || '';
    const foot = $('#modal-foot');
    foot.innerHTML = '';
    const cancel = document.createElement('button');
    cancel.className = 'ghost';
    cancel.textContent = '取消';
    cancel.addEventListener('click', closeModal);
    const confirm = document.createElement('button');
    confirm.className = opts.danger ? 'danger' : 'primary';
    confirm.textContent = opts.confirmText || '确认';
    confirm.addEventListener('click', async () => {
      confirm.disabled = true;
      cancel.disabled = true;
      const original = confirm.textContent;
      confirm.textContent = '处理中…';
      try {
        const keep = opts.onConfirm ? await opts.onConfirm(modal) : undefined;
        if (keep !== false) closeModal();
      } catch (e) {
        const box = $('#modal-error');
        if (box) { box.textContent = e.message || String(e); box.classList.remove('hidden'); }
        else toast(e.message || String(e), 'bad');
      } finally {
        confirm.disabled = false;
        cancel.disabled = false;
        confirm.textContent = original;
      }
    });
    foot.appendChild(cancel);
    foot.appendChild(confirm);
    modal.classList.remove('hidden');
    modal.setAttribute('aria-hidden', 'false');
    setTimeout(() => {
      const focus = modal.querySelector('textarea, input');
      if (focus) focus.focus();
    }, 30);
    return modal;
  }

  function closeModal() {
    const modal = $('#modal');
    if (!modal) return;
    modal.classList.add('hidden');
    modal.setAttribute('aria-hidden', 'true');
  }

  function bindModal() {
    $$('#modal [data-close]').forEach((el) => el.addEventListener('click', closeModal));
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeModal();
    });
  }

  function modalError() {
    return '<p id="modal-error" class="form-error hidden"></p>';
  }

  function diffHtml(text) {
    const lines = String(text || '').split('\n');
    return lines.map((line) => {
      let cls = 'diff-line';
      if (/^\+\+\+|^---/.test(line)) cls += ' diff-head';
      else if (/^\+/.test(line)) cls += ' diff-add';
      else if (/^-/.test(line)) cls += ' diff-del';
      else if (/^@@/.test(line)) cls += ' diff-hunk';
      else if (/^diff |^index /.test(line)) cls += ' diff-meta';
      return `<span class="${cls}">${esc(line) || ' '}</span>`;
    }).join('');
  }

  /* ---------------- auth gate ---------------- */

  async function authStatus() {
    const res = await fetch('/api/auth/status', { credentials: 'same-origin' });
    return res.json();
  }

  function showAuthGate(setupRequired) {
    const gate = $('#auth-gate');
    if (!gate) return;
    gate.classList.remove('hidden');
    $('#authbox').classList.add('hidden');
    const body = $('#auth-body');
    const setup = Boolean(setupRequired);
    body.innerHTML = `
      <h2 class="auth-title">${setup ? '首次使用：设置管理密码' : '登录审批中心'}</h2>
      <p class="auth-desc">${setup
        ? '密码只保存为 PBKDF2 哈希，至少 10 个字符。设置后本机浏览器可访问全部页面。'
        : '请输入本机管理密码。会话有效期 12 小时，Cookie 仅随本机请求发送。'}</p>
      <form id="auth-form" autocomplete="off">
        <label class="field"><span>密码</span>
          <input id="auth-password" type="password" minlength="10" maxlength="200" required autocomplete="${setup ? 'new-password' : 'current-password'}">
        </label>
        ${setup ? `<label class="field"><span>确认密码</span>
          <input id="auth-confirm" type="password" minlength="10" maxlength="200" required autocomplete="new-password">
        </label>` : ''}
        <p id="auth-error" class="form-error hidden"></p>
        <button type="submit" class="primary auth-submit">${setup ? '设置并进入' : '登录'}</button>
      </form>
      <p class="auth-note">服务仅绑定 127.0.0.1；如需重置密码，请停止服务后删除 <code>config/admin.json</code> 与 <code>config/session.key</code>，再启动服务。</p>
    `;
    const form = $('#auth-form');
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const err = $('#auth-error');
      err.classList.add('hidden');
      const password = $('#auth-password').value;
      const confirm = setup ? $('#auth-confirm').value : password;
      if (setup && password !== confirm) {
        err.textContent = '两次输入的密码不一致';
        err.classList.remove('hidden');
        return;
      }
      const btn = form.querySelector('button[type=submit]');
      btn.disabled = true;
      btn.textContent = '处理中…';
      try {
        const payload = setup ? { password, confirm } : { password };
        const res = await fetch(setup ? '/api/auth/setup' : '/api/auth/login', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`);
        }
        location.reload();
      } catch (ex) {
        err.textContent = ex.message || String(ex);
        err.classList.remove('hidden');
        btn.disabled = false;
        btn.textContent = setup ? '设置并进入' : '登录';
      }
    });
    setTimeout(() => { const p = $('#auth-password'); if (p) p.focus(); }, 50);
  }

  function bindAuth() {
    const logout = $('#logout-btn');
    if (logout) {
      logout.addEventListener('click', async () => {
        try { await apiPost('/api/auth/logout', {}); } catch (e) { /* expire is fine */ }
        location.reload();
      });
    }
  }

  function showAuthenticated(session) {
    $('#auth-gate').classList.add('hidden');
    $('#authbox').classList.remove('hidden');
    $('#auth-user').textContent = session.user || 'admin';
  }

  /* Minimal, escape-first Markdown renderer (headings, lists, code, tables, quotes). */
  function inlineMd(s) {
    s = esc(s);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, href) => {
      if (!/^https?:\/\//i.test(href)) return text;
      return `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`;
    });
    return s;
  }

  function renderMarkdown(src) {
    const lines = String(src || '').replace(/\r\n/g, '\n').split('\n');
    let html = '';
    let inCode = false;
    let code = [];
    let listType = null;
    const closeList = () => {
      if (listType) { html += `</${listType}>`; listType = null; }
    };

    for (let i = 0; i < lines.length; i++) {
      const raw = lines[i];

      if (/^\s*```/.test(raw)) {
        if (inCode) {
          html += '<pre class="code"><code>' + esc(code.join('\n')) + '</code></pre>';
          code = []; inCode = false;
        } else {
          closeList(); inCode = true;
        }
        continue;
      }
      if (inCode) { code.push(raw); continue; }

      if (/^\s*$/.test(raw)) { closeList(); continue; }

      // table block
      if (/^\s*\|/.test(raw)) {
        closeList();
        const rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(lines[i]); i++; }
        i--;
        const cells = (line) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
        const body = rows.filter((r) => !/^\s*\|?[\s:|-]+\|?\s*$/.test(r));
        if (body.length) {
          html += '<table><thead><tr>' + cells(body[0]).map((c) => `<th>${inlineMd(c)}</th>`).join('') + '</tr></thead><tbody>';
          for (const r of body.slice(1)) {
            html += '<tr>' + cells(r).map((c) => `<td>${inlineMd(c)}</td>`).join('') + '</tr>';
          }
          html += '</tbody></table>';
        }
        continue;
      }

      const h = raw.match(/^(#{1,6})\s+(.*)$/);
      if (h) { closeList(); html += `<h${h[1].length}>${inlineMd(h[2])}</h${h[1].length}>`; continue; }

      if (/^\s*([-*+])\s+/.test(raw)) {
        if (listType !== 'ul') { closeList(); html += '<ul>'; listType = 'ul'; }
        html += '<li>' + inlineMd(raw.replace(/^\s*[-*+]\s+/, '')) + '</li>';
        continue;
      }
      if (/^\s*\d+\.\s+/.test(raw)) {
        if (listType !== 'ol') { closeList(); html += '<ol>'; listType = 'ol'; }
        html += '<li>' + inlineMd(raw.replace(/^\s*\d+\.\s+/, '')) + '</li>';
        continue;
      }
      if (/^\s*(---+|\*\*\*+)\s*$/.test(raw)) { closeList(); html += '<hr>'; continue; }
      if (/^\s*>/.test(raw)) { closeList(); html += '<blockquote>' + inlineMd(raw.replace(/^\s*>\s?/, '')) + '</blockquote>'; continue; }

      closeList();
      html += '<p>' + inlineMd(raw) + '</p>';
    }
    if (inCode) html += '<pre class="code"><code>' + esc(code.join('\n')) + '</code></pre>';
    closeList();
    return html;
  }

  /* ---------------- health ---------------- */

  async function checkHealth() {
    const el = $('#health');
    try {
      const h = await api('/api/health');
      el.className = 'health ok';
      el.innerHTML = '<span class="dot"></span><span>本地管理 · 后端已连接</span>';
      return h;
    } catch (e) {
      el.className = 'health err';
      el.innerHTML = '<span class="dot"></span><span>后端未连接</span>';
      return null;
    }
  }

  /* ---------------- dashboard ---------------- */

  async function loadDashboard() {
    const view = $('#view-dashboard');
    view.innerHTML = skeleton(6);
    let d;
    try { d = await api('/api/dashboard'); }
    catch (e) { view.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; return; }

    state.loaded.dashboard = d;
    state.fav = d.favorites || null;
    $('#subtitle').textContent = `本地管理 · 扫描于 ${fmtTime(d.scanned_at)} · 共 ${d.total} 个技能`;

    const confirmed = d.by_status.confirmed || 0;
    const gated = d.by_status.gated_by_platform || 0;
    const archived = d.by_status.archived || 0;

    let html = '<div class="stat-row">'
      + statCard('技能总数', d.total, `已确认 ${confirmed} · 平台门控 ${gated}${archived ? ' · 归档 ' + archived : ''}`)
      + statCard('已确认加载', confirmed, 'Hermes 实际加载', 'ok')
      + statCard('平台门控', gated, '当前平台不可用', 'warn')
      + statCard('占用空间', fmtBytes(d.total_bytes), '全部技能文件体积')
      + statCard('扫描异常', d.registry_errors, `${d.collisions} 个重名冲突`, d.registry_errors ? 'bad' : 'ok')
      + statCard('最近扫描', '', `扫描器 ${d.scanner || '未知'}`, '', fmtTime(d.scanned_at))
      + '</div>';

    html += favoritesCard(d.favorites);

    const maxCat = Math.max(1, ...Object.values(d.by_category));
    html += '<div class="grid-2">';
    html += '<div class="panel card"><h2>分类分布 <span class="hint">' + Object.keys(d.by_category).length + ' 个分类</span></h2><div class="bars">';
    for (const [cat, n] of Object.entries(d.by_category)) {
      html += `<div class="bar-row"><span class="lbl" title="${esc(cat)}">${esc(catLabel(cat))}</span>`
        + `<span class="track"><span class="fill" style="width:${(n / maxCat) * 100}%"></span></span>`
        + `<span class="num">${n}</span></div>`;
    }
    html += '</div></div>';

    html += '<div class="panel card"><h2>来源与加载状态</h2>';
    html += '<div class="bars" style="margin-bottom:14px">';
    const maxSrc = Math.max(1, ...Object.values(d.by_source));
    for (const [src, n] of Object.entries(d.by_source)) {
      html += `<div class="bar-row"><span class="lbl">${esc(SOURCE[src] || src)}</span>`
        + `<span class="track"><span class="fill" style="width:${(n / maxSrc) * 100}%"></span></span>`
        + `<span class="num">${n}</span></div>`;
    }
    html += '</div>';
    html += '<div class="hint" style="margin:0 0 8px">产生方式</div>';
    html += '<div class="bars" style="margin-bottom:14px">';
    const byOrigin = d.by_origin || {};
    const maxOrigin = Math.max(1, ...Object.values(byOrigin));
    for (const key of ORIGIN_ORDER) {
      const n = byOrigin[key] || 0;
      if (!n) continue;
      html += `<div class="bar-row"><span class="lbl">${esc(originLabel(key)[0])}</span>`
        + `<span class="track"><span class="fill" style="width:${(n / maxOrigin) * 100}%"></span></span>`
        + `<span class="num">${n}</span></div>`;
    }
    html += '</div>';
    const pendingOrigin = byOrigin.unknown || 0;
    if (pendingOrigin) {
      html += `<p class="footnote">还有 ${pendingOrigin} 个技能待确认，<button class="linklike" id="origin-pending-link">去清单里处理</button></p>`;
    }
    if (d.origin_orphans) {
      html += `<p class="footnote">有 ${d.origin_orphans} 条手工记录已找不到对应技能。</p>`;
    }
    if (d.origin_error) {
      html += `<p class="footnote">${esc(d.origin_error)}</p>`;
    }
    html += '<dl class="kv"><dt>默认技能目录</dt><dd class="sha">' + esc(d.skills_dir || '未知') + '</dd>'
      + '<dt>外部技能目录</dt><dd>' + esc(d.external_dirs || '未配置') + '</dd>'
      + '<dt>归档（Curator）</dt><dd>' + esc(d.archived) + ' 个（不计入总数）</dd></dl>';
    html += '</div></div>';

    // audit card
    html += '<div class="grid-2">';
    html += '<div class="panel card"><h2>最近审计 <span class="hint">' + esc(d.latest_audit ? d.latest_audit.data.audit_date : '暂无') + '</span></h2>';
    if (d.latest_audit) {
      const t = d.latest_audit.data.totals || {};
      const q = t.quality || {};
      const s = t.security || {};
      const u = t.updates || {};
      const dup = t.duplication || {};
      const highCount = (dup.pair_list || []).filter((p) => p.classification === 'High Overlap').length;
      html += '<table class="mini-table"><thead><tr><th>维度</th><th>结果</th></tr></thead><tbody>'
        + `<tr><td>质量</td><td>均分 ${esc(q.average)} · ` + Object.entries(q.grades || {}).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(' · ') + '</td></tr>'
        + `<tr><td>安全</td><td>高危 ${esc((s.levels || {}).High || 0)} · 中危 ${esc((s.levels || {}).Medium || 0)} · 低危 ${esc((s.levels || {}).Low || 0)}</td></tr>`
        + `<tr><td>重复</td><td>${esc(dup.pairs || 0)} 对 · 高度重叠 ${esc(highCount)} 对</td></tr>`
        + `<tr><td>更新</td><td>` + Object.entries(u.conclusions || {}).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(' · ') + '</td></tr>'
        + '</tbody></table>';
      html += `<p class="footnote">报告文件：${esc(d.latest_audit.name)}</p>`;
    } else {
      html += '<div class="muted">尚未生成审计汇总。</div>';
    }
    html += '</div>';

    html += '<div class="panel card"><h2>报告与提案</h2>'
      + '<dl class="kv"><dt>日报</dt><dd>' + d.reports.daily + ' 份</dd>'
      + '<dt>周报</dt><dd>' + d.reports.weekly + ' 份</dd>'
      + '<dt>盘点对比</dt><dd>' + d.reports.compare + ' 份</dd>'
      + '<dt>待审批提案</dt><dd>' + d.proposals.pending + '</dd>'
      + '<dt>已批准</dt><dd>' + d.proposals.approved + '</dd></dl>'
      + '<ul class="list-links" style="margin-top:10px">'
      + (d.reports.latest_daily ? `<li><span>最新日报</span><span class="tag">${esc(d.reports.latest_daily)}</span></li>` : '')
      + (d.reports.latest_weekly ? `<li><span>最新周报</span><span class="tag">${esc(d.reports.latest_weekly)}</span></li>` : '')
      + (d.latest_compare ? `<li><span>最新对比</span><span class="tag">${esc(d.latest_compare)}</span></li>` : '')
      + '</ul></div>';
    html += '</div>';

    // high overlap pairs
    if (d.latest_audit && d.latest_audit.data.totals && d.latest_audit.data.totals.duplication) {
      const pairs = (d.latest_audit.data.totals.duplication.pair_list || []).filter((p) => p.classification === 'High Overlap');
      if (pairs.length) {
        html += '<div class="panel card"><h2>高风险重叠 <span class="hint">审计日期 ' + esc(d.latest_audit.data.audit_date) + '</span></h2>'
          + '<table class="mini-table"><thead><tr><th>技能 A</th><th>技能 B</th><th>判定</th></tr></thead><tbody>';
        for (const p of pairs) {
          html += `<tr><td>${esc(p.a)}</td><td>${esc(p.b)}</td><td>${badge(dupLabel(p.classification), 'bad')}</td></tr>`;
        }
        html += '</tbody></table></div>';
      }
    }

    view.innerHTML = html;
    bindFavorites(view);

    const pendingLink = $('#origin-pending-link');
    if (pendingLink) {
      pendingLink.addEventListener('click', () => {
        state.skills.origin = 'unknown';
        state.skills.page = 1;
        switchView('skills');
      });
    }
  }

  function statCard(k, v, sub, tone, vText) {
    return `<div class="stat ${tone || ''}"><div class="k">${esc(k)}</div>`
      + `<div class="v${vText ? ' is-text' : ''}">${vText ? esc(vText) : esc(v)}</div>`
      + `<div class="s">${esc(sub)}</div></div>`;
  }

  /* ---------------- favorites ---------------- */

  function favoritesCard(fav) {
    const items = (fav && Array.isArray(fav.items)) ? fav.items : [];
    const limit = (fav && fav.limit) || 12;
    let html = '<div class="panel card favorites-card"><h2>常用技能 <span class="hint">'
      + items.length + ' / ' + limit + ' · 我常让 agent 用的技能</span></h2>';
    if (fav && fav.error) html += `<p class="footnote">${esc(fav.error)}</p>`;
    if (!items.length) {
      html += '<div class="muted">还没有常用技能。到「技能清单」点名称前的 ☆，或打开技能详情点「加入常用」，最多 12 个。</div></div>';
      return html;
    }
    html += '<ul class="fav-list">';
    items.forEach((it, i) => {
      const s = it.skill;
      if (!s) {
        html += '<li class="fav-item missing">'
          + `<span class="fav-name">${esc(it.key)}</span>`
          + '<span class="fav-missing">已找不到这个技能</span>'
          + `<button class="ghost" data-fav-remove-key="${esc(it.key)}">从列表移除</button>`
          + '</li>';
        return;
      }
      const zh = zhDesc(s.name);
      const desc = zh || s.description || '（没有描述）';
      const untranslated = zh ? '' : '<span class="desc-tag" title="中文简介尚未生成">未译</span>';
      html += '<li class="fav-item">'
        + '<div class="fav-main">'
        + `<button class="linklike fav-name" data-fav-open="${esc(s.name)}">${esc(s.name)}</button>`
        + `<span class="fav-cat">${esc(catLabel(s.category))}</span>`
        + `<span class="fav-desc" title="${esc(desc)}">${untranslated}${esc(desc)}</span>`
        + '</div>'
        + (it.note ? `<div class="fav-note">备注：${esc(it.note)}</div>` : '')
        + '<div class="fav-actions">'
        + `<button class="ghost" data-fav-copy="${esc(s.name)}">复制调用提示</button>`
        + `<button class="ghost" data-fav-move="up" data-fav-name="${esc(s.name)}"${i === 0 ? ' disabled' : ''} title="上移">↑</button>`
        + `<button class="ghost" data-fav-move="down" data-fav-name="${esc(s.name)}"${i === items.length - 1 ? ' disabled' : ''} title="下移">↓</button>`
        + `<button class="ghost" data-fav-off="${esc(s.name)}">移除</button>`
        + '</div></li>';
    });
    html += '</ul></div>';
    return html;
  }

  function bindFavorites(root) {
    $$('[data-fav-open]', root).forEach((el) => {
      el.addEventListener('click', () => openSkill(el.dataset.favOpen));
    });
    $$('[data-fav-copy]', root).forEach((el) => {
      el.addEventListener('click', () => copyFavoritePrompt(el.dataset.favCopy));
    });
    $$('[data-fav-move]', root).forEach((el) => {
      el.addEventListener('click', () => moveFavorite(el.dataset.favName, el.dataset.favMove, el));
    });
    $$('[data-fav-off]', root).forEach((el) => {
      el.addEventListener('click', () => toggleFavorite(el.dataset.favOff, false, null, el));
    });
    $$('[data-fav-remove-key]', root).forEach((el) => {
      el.addEventListener('click', () => removeFavoriteByKey(el.dataset.favRemoveKey, el));
    });
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (e) { /* fall through to execCommand */ }
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch (e) {
      return false;
    }
  }

  async function copyFavoritePrompt(name) {
    const fav = state.fav || (state.loaded.dashboard && state.loaded.dashboard.favorites);
    const item = ((fav && fav.items) || []).find((x) => x.skill && x.skill.name === name);
    const note = item && item.note ? item.note : '';
    const text = `用「${name}」技能帮我：` + (note ? `\n（提示：${note}）` : '');
    const ok = await copyText(text);
    toast(ok ? '已复制调用提示，粘到对话里即可' : '复制失败，请手动选择文本', ok ? 'ok' : 'bad');
  }

  async function toggleFavorite(name, on, note, btn) {
    if (btn) btn.disabled = true;
    try {
      if (on) await apiWrite('PUT', '/api/favorites/' + encodeURIComponent(name), { note: note || '' });
      else await apiWrite('DELETE', '/api/favorites/' + encodeURIComponent(name));
      toast(on ? '已加入常用' : '已移出常用', 'ok');
      await afterFavoriteChange(name);
    } catch (e) {
      toast('操作失败：' + e.message, 'bad');
      if (btn) btn.disabled = false;
    }
  }

  async function moveFavorite(name, direction, btn) {
    if (btn) btn.disabled = true;
    try {
      await apiWrite('POST', '/api/favorites/' + encodeURIComponent(name) + '/move', { direction });
      await loadDashboard();
    } catch (e) {
      toast('移动失败：' + e.message, 'bad');
      if (btn) btn.disabled = false;
    }
  }

  async function removeFavoriteByKey(key, btn) {
    if (btn) btn.disabled = true;
    try {
      await apiWrite('DELETE', '/api/favorites?key=' + encodeURIComponent(key));
      toast('已从常用列表移除', 'ok');
      await loadDashboard();
    } catch (e) {
      toast('移除失败：' + e.message, 'bad');
      if (btn) btn.disabled = false;
    }
  }

  async function saveFavoriteFromDrawer(name, already, btn) {
    const input = $('#fav-note');
    const note = input ? input.value.trim() : '';
    if (btn) btn.disabled = true;
    try {
      await apiWrite('PUT', '/api/favorites/' + encodeURIComponent(name), { note });
      toast(already ? '备注已保存' : '已加入常用', 'ok');
      await afterFavoriteChange(name);
    } catch (e) {
      toast('保存失败：' + e.message, 'bad');
      if (btn) btn.disabled = false;
    }
  }

  async function afterFavoriteChange(name) {
    if ($('#view-skills').classList.contains('active')) await loadSkills();
    if ($('#view-dashboard').classList.contains('active')) await loadDashboard();
    if (name && !$('#drawer').classList.contains('hidden')) await openSkill(name);
  }

  /* ---------------- skills ---------------- */

  function skillRow(x) {
    const st = STATUS[x.load_status] || [x.load_status || '未知', ''];
    const o = originLabel(x.origin);
    const zh = zhDesc(x.name);
    const desc = zh || x.description || '（该技能没有填写描述）';
    const tag = zh ? '' : '<span class="desc-tag" title="中文简介尚未生成，显示的是技能自带的英文描述">未译</span>';
    return `<tr data-name="${esc(x.name)}">`
      + `<td><div class="name"><button class="star${x.favorite ? ' on' : ''}" data-fav="${esc(x.name)}"`
      + ` title="${x.favorite ? '取消常用' : '加入常用'}" aria-label="${x.favorite ? '取消常用' : '加入常用'}"`
      + ` aria-pressed="${x.favorite ? 'true' : 'false'}">${x.favorite ? '★' : '☆'}</button><span>${esc(x.name)}</span></div></td>`
      + `<td class="intro"><div class="desc" title="${esc(desc)}">${tag}${esc(desc)}</div></td>`
      + `<td>${esc(catLabel(x.category))}</td>`
      + `<td>${esc(x.version || '—')}</td>`
      + `<td>${esc(SOURCE[x.source_type] || x.source_type || '未知')}</td>`
      + `<td title="${esc(originReason(x))}">${badge(o[0], o[1])}</td>`
      + `<td>${badge(st[0], st[1])}</td>`
      + `<td>${fmtBytes(x.total_bytes)}</td>`
      + '</tr>';
  }

  async function loadSkills() {
    const table = $('#skill-table');
    const tbody = table.querySelector('tbody');
    const s = state.skills;
    let res;
    try {
      res = await api('/api/skills', {
        q: s.q, category: s.category, status: s.status, source: s.source, origin: s.origin,
        sort: s.sort, order: s.order, page: s.page, page_size: s.pageSize,
      });
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="8" class="empty">加载失败：${esc(e.message)}</td></tr>`;
      return;
    }

    // facet selects (populate once, keep current values)
    const catSel = $('#skill-category');
    const stSel = $('#skill-status');
    const srcSel = $('#skill-source');
    function fillSelect(sel, values, labels, current) {
      const opts = ['<option value="">全部</option>'].concat(values.map((v) => `<option value="${esc(v)}">${esc((labels && labels[v]) || v)}</option>`));
      sel.innerHTML = opts.join('');
      sel.value = current;
    }
    fillSelect(catSel, res.facets.categories, CATEGORY, s.category);
    fillSelect(stSel, res.facets.statuses, Object.fromEntries(Object.entries(STATUS).map(([k, v]) => [k, v[0]])), s.status);
    fillSelect(srcSel, res.facets.sources, SOURCE, s.source);
    fillSelect($('#skill-origin'), ORIGIN_ORDER, ORIGIN_LABELS, s.origin);

    tbody.innerHTML = res.items.length
      ? res.items.map(skillRow).join('')
      : '<tr><td colspan="8" class="empty">没有匹配的技能。试试清空搜索词，或把筛选条件改回「全部」。</td></tr>';

    $$('#skill-table tbody tr[data-name]').forEach((tr) => {
      tr.addEventListener('click', () => openSkill(tr.dataset.name));
    });
    $$('#skill-table .star[data-fav]').forEach((btn) => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        toggleFavorite(btn.dataset.fav, !btn.classList.contains('on'), null, btn);
      });
    });

    $('#skill-count').textContent = `共 ${res.total} 个`;
    $('#skill-page').textContent = `${res.page} / ${res.pages} 页`;
    $('#skill-prev').disabled = res.page <= 1;
    $('#skill-next').disabled = res.page >= res.pages;

    $$('#skill-table th[data-sort]').forEach((th) => {
      const active = state.skills.sort === th.dataset.sort;
      th.setAttribute('aria-sort', active ? (state.skills.order === 'asc' ? 'ascending' : 'descending') : 'none');
    });
  }

  function bindSkills() {
    const reload = () => { state.skills.page = 1; loadSkills(); };
    $('#skill-q').addEventListener('input', debounce((e) => { state.skills.q = e.target.value.trim(); reload(); }, 250));
    $('#skill-category').addEventListener('change', (e) => { state.skills.category = e.target.value; reload(); });
    $('#skill-status').addEventListener('change', (e) => { state.skills.status = e.target.value; reload(); });
    $('#skill-source').addEventListener('change', (e) => { state.skills.source = e.target.value; reload(); });
    $('#skill-origin').addEventListener('change', (e) => { state.skills.origin = e.target.value; reload(); });
    $('#skill-reset').addEventListener('click', () => {
      state.skills = { page: 1, pageSize: 20, q: '', category: '', status: '', source: '', origin: '', sort: 'name', order: 'asc' };
      $('#skill-q').value = '';
      loadSkills();
    });
    $('#skill-prev').addEventListener('click', () => {
      if (state.skills.page > 1) { state.skills.page--; loadSkills(); }
    });
    $('#skill-next').addEventListener('click', () => { state.skills.page++; loadSkills(); });

    $$('#skill-table th[data-sort]').forEach((th) => {
      th.tabIndex = 0;
      const applySort = () => {
        const key = th.dataset.sort;
        if (state.skills.sort === key) {
          state.skills.order = state.skills.order === 'asc' ? 'desc' : 'asc';
        } else {
          state.skills.sort = key;
          state.skills.order = 'asc';
        }
        state.skills.page = 1;
        loadSkills();
      };
      th.addEventListener('click', applySort);
      th.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); applySort(); }
      });
    });
  }

  async function openSkill(name) {
    const drawer = $('#drawer');
    $('#drawer-title').textContent = name;
    $('#drawer-body').innerHTML = skeleton(8);
    drawer.classList.remove('hidden');
    drawer.setAttribute('aria-hidden', 'false');

    let d;
    try { d = await api('/api/skills/' + encodeURIComponent(name)); }
    catch (e) {
      $('#drawer-body').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
      return;
    }

    const st = STATUS[d.load_status] || [d.load_status || '未知', ''];
    let html = '';
    if (d.description) {
      const zh = zhDesc(d.name);
      if (zh) {
        html += `<div class="detail-desc">${esc(zh)}</div>`
          + `<div class="detail-desc-en">原文：${esc(d.description)}</div>`;
      } else {
        html += `<div class="detail-desc"><span class="desc-tag">未译</span>${esc(d.description)}</div>`;
      }
    }
    html += '<div class="detail-grid">'
      + cell('分类', catLabel(d.category))
      + cell('版本', d.version || '—')
      + cell('来源', SOURCE[d.source_type] || d.source_type || '未知')
      + cell('产生方式', originLabel(d.origin)[0])
      + cell('加载状态', st[0])
      + cell('文件数', d.file_count == null ? '—' : d.file_count)
      + cell('占用空间', fmtBytes(d.total_bytes))
      + cell('最近扫描', fmtTime(d.last_scanned))
      + cell('相对路径', d.relative_path || '—')
      + '</div>';
    html += favoritePanel(d);
    html += originPanel(d);
    if (d.audit) {
      const a = d.audit;
      const dims = Object.entries(a.quality_dimensions || {});
      html += '<div class="detail-section"><h4>最近审计'
        + (a.audit_date ? ' · ' + esc(a.audit_date) : '') + '</h4>'
        + '<div class="detail-grid">'
        + cell('质量总分', a.quality_total == null ? '—' : a.quality_total + ' / 100')
        + `<div class="cell"><div class="k">质量等级</div><div class="v">${a.quality_grade ? badge(a.quality_grade, GRADE_TONE[a.quality_grade] || '') : '—'}</div></div>`
        + `<div class="cell"><div class="k">安全等级</div><div class="v">${a.security_level ? badge(lvLabel(a.security_level), SECURITY_TONE[a.security_level] || '') : '—'}</div></div>`
        + `<div class="cell"><div class="k">更新结论</div><div class="v">${a.update_conclusion ? badge(a.update_conclusion, UPDATE_TONE[a.update_conclusion] || '') : '—'}</div></div>`
        + '</div>';
      if (dims.length) {
        html += '<div class="dims-line">'
          + dims.map(([k, v]) => `<span>${esc(k)} <b>${esc(v)}</b></span>`).join('')
          + '</div>';
      }
      if (a.security_summary) html += `<p class="audit-note"><b>安全</b>：${esc(a.security_summary)}</p>`;
      const upParts = [];
      if (a.upstream) upParts.push('上游：' + a.upstream);
      if (a.update_note) upParts.push(a.update_note);
      if (upParts.length) html += `<p class="audit-note"><b>更新</b>：${esc(upParts.join(' · '))}</p>`;
      if (a.duplication && a.duplication.length) {
        html += '<div class="dims-line">'
          + a.duplication.map((x) => `<span>${esc(x.skill)} <b>${esc(x.classification)}</b></span>`).join('')
          + '</div>';
      }
      html += '</div>';
    }
    html += `<div class="detail-section"><h4>SHA-256</h4><div class="sha">${esc(d.sha256 || '—')}</div></div>`;
    if (d.platforms && d.platforms.length) {
      html += `<div class="detail-section"><h4>适用平台</h4><div>${d.platforms.map((p) => badge(p, 'info')).join(' ')}</div></div>`;
    }
    if (d.tags && d.tags.length) {
      html += `<div class="detail-section"><h4>标签</h4><div>${d.tags.map((t) => badge(t)).join(' ')}</div></div>`;
    }
    if (d.dependencies && d.dependencies.length) {
      html += `<div class="detail-section"><h4>依赖</h4><div>${
        d.dependencies.map((x) => badge(typeof x === 'string' ? x : JSON.stringify(x), 'info')).join(' ')
      }</div></div>`;
    }
    html += `<div class="detail-section"><h4>SKILL.md（只读原文）</h4><pre>${esc(d.skill_md || '')}</pre></div>`;
    $('#drawer-body').innerHTML = html;
    $$('#drawer-body [data-origin]').forEach((btn) => {
      btn.addEventListener('click', () => setOrigin(d.name, btn.dataset.origin, btn));
    });
    const clearBtn = $('#drawer-body [data-origin-clear]');
    if (clearBtn) clearBtn.addEventListener('click', () => clearOrigin(d.name, clearBtn));
    const favSaveBtn = $('#drawer-body [data-fav-save]');
    if (favSaveBtn) {
      favSaveBtn.addEventListener('click', () => saveFavoriteFromDrawer(d.name, !!(d.favorite && d.favorite.on), favSaveBtn));
    }
    const favNote = $('#fav-note');
    if (favNote) {
      favNote.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          saveFavoriteFromDrawer(d.name, !!(d.favorite && d.favorite.on), favSaveBtn);
        }
      });
    }
    const favOffBtn = $('#drawer-body [data-fav-unfav]');
    if (favOffBtn) favOffBtn.addEventListener('click', () => toggleFavorite(d.name, false, null, favOffBtn));
  }

  function sessionBit(id) {
    return id ? '（会话 ' + id + '）' : '';
  }

  function originReason(d) {
    const ev = d.origin_evidence || {};
    const rule = d.origin_rule || 'no-evidence';
    if (rule === 'manual') {
      return '手工指定于 ' + fmtTime(ev.decided_at) + (ev.note ? ' · ' + ev.note : '');
    }
    if (rule === 'official-bundled') {
      const head = ev.manifest
        ? '命中官方内置清单'
        : '安装位置标记为' + (SOURCE[ev.source_type] || ev.source_type || '内置');
      const conflict = ev.ledger_create
        ? '；另有一次 ' + (ev.ledger_create.actor || '未知') + ' 会话写入记录'
        : '';
      return head + conflict;
    }
    if (rule === 'official-hub') {
      return '从官方技能库安装' + (ev.identifier ? '：' + ev.identifier : '');
    }
    if (rule === 'hermes-curator') {
      return 'Hermes 策展器于 ' + fmtTime(ev.created_at) + ' 创建' + sessionBit(ev.session_id);
    }
    if (rule === 'hermes-cron') {
      return '定时任务自主创建于 ' + fmtTime(ev.created_at) + sessionBit(ev.session_id);
    }
    if (rule === 'user-agent') {
      return '你在会话中要求后由 Hermes 写入' + sessionBit(ev.session_id);
    }
    if (ev.events) {
      const actors = (ev.actors || []).join(' / ') || '未知';
      return `有 ${ev.events} 次修改记录（${actors}），但没有创建记录`;
    }
    return '没有任何来源记录，需要你确认';
  }

  function favoritePanel(d) {
    const fav = d.favorite || { on: false, note: '' };
    const value = fav.on ? fav.note : '';
    return '<div class="detail-section"><h4>常用技能</h4>'
      + `<div class="origin-line">${fav.on
        ? '<span class="badge origin-user">已加入常用</span><span class="origin-why">会出现在首页「常用技能」里</span>'
        : '<span class="muted">未加入常用</span>'}</div>`
      + '<div class="fav-note-edit">'
      + `<input id="fav-note" type="text" maxlength="200" autocomplete="off" placeholder="备注：什么时候用它 / 触发词（可留空）" value="${esc(value)}">`
      + `<button class="ghost" data-fav-save="1">${fav.on ? '保存备注' : '加入常用'}</button>`
      + (fav.on ? '<button class="ghost" data-fav-unfav="1">取消常用</button>' : '')
      + '</div></div>';
  }

  function originPanel(d) {
    const o = originLabel(d.origin);
    const buttons = ORIGIN_ORDER.filter((k) => k !== 'unknown').map((k) => {
      const active = d.origin === k && d.origin_overridden;
      return `<button class="ghost" data-origin="${k}"${active ? ' disabled' : ''}>${esc(ORIGIN[k][0])}</button>`;
    }).join('');
    const reset = d.origin_overridden
      ? '<button class="ghost" data-origin-clear="1">恢复自动判断</button>'
      : '';
    return '<div class="detail-section"><h4>产生方式</h4>'
      + `<div class="origin-line">${badge(o[0], o[1])}<span class="origin-why">${esc(originReason(d))}</span></div>`
      + (d.origin_overridden ? '<p class="footnote">当前为手工指定，自动推断不再生效。</p>' : '')
      + `<div class="origin-actions">${buttons}${reset}</div></div>`;
  }

  async function setOrigin(name, origin, btn) {
    btn.disabled = true;
    try {
      await apiWrite('PUT', '/api/skills/' + encodeURIComponent(name) + '/origin', { origin });
      toast('已记为' + ORIGIN[origin][0], 'ok');
      await afterOriginChange(name);
    } catch (e) {
      toast('保存失败：' + e.message, 'bad');
      btn.disabled = false;
    }
  }

  async function clearOrigin(name, btn) {
    btn.disabled = true;
    try {
      await apiWrite('DELETE', '/api/skills/' + encodeURIComponent(name) + '/origin');
      toast('已恢复自动判断', 'ok');
      await afterOriginChange(name);
    } catch (e) {
      toast('恢复失败：' + e.message, 'bad');
      btn.disabled = false;
    }
  }

  async function afterOriginChange(name) {
    await openSkill(name);
    if ($('#view-skills').classList.contains('active')) loadSkills();
  }

  function cell(k, v) {
    return `<div class="cell"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`;
  }

  function bindDrawer() {
    $$('#drawer [data-close]').forEach((el) => {
      el.addEventListener('click', closeDrawer);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeDrawer();
    });
  }

  function closeDrawer() {
    const drawer = $('#drawer');
    drawer.classList.add('hidden');
    drawer.setAttribute('aria-hidden', 'true');
  }

  /* ---------------- reports ---------------- */

  async function loadReports() {
    const listEl = $('#report-list');
    const docEl = $('#report-doc');
    const kind = state.reports.kind;
    listEl.innerHTML = '<div class="pad">' + skeleton(4) + '</div>';
    docEl.innerHTML = '<div class="muted pad">从左侧选择一份报告</div>';

    let res;
    try { res = await api('/api/reports', { kind }); }
    catch (e) { listEl.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; return; }

    if (!res.items.length) {
      listEl.innerHTML = '<div class="empty">暂无报告</div>';
      docEl.innerHTML = `<div class="empty">还没有${esc(REPORT_KIND_NAME[kind] || kind)}</div>`;
      return;
    }
    listEl.innerHTML = `<div class="group">${esc(REPORT_KIND_NAME[kind] || kind)} · ${res.items.length} 份</div>`
      + res.items.map((n) => `<button class="item" data-name="${esc(n)}">${esc(n)}</button>`).join('');
    $$('#report-list button.item').forEach((b) => {
      b.addEventListener('click', () => showReport(kind, b.dataset.name, b));
    });
    showReport(kind, res.items[0], $('#report-list button.item'));
  }

  async function showReport(kind, name, btn) {
    if (btn) {
      $$('#report-list button.item').forEach((b) => b.classList.toggle('active', b === btn));
    }
    const docEl = $('#report-doc');
    docEl.innerHTML = '<div class="pad">' + skeleton(8) + '</div>';
    try {
      const res = await api('/api/reports/content', { kind, name });
      docEl.innerHTML = `<div class="doc-title">${esc(name)}</div>`
        + `<div class="doc-meta">${esc(REPORT_KIND_NAME[kind] || kind)} · 只读</div>`
        + `<div class="md">${renderMarkdown(res.content)}</div>`;
      docEl.scrollTop = 0;
    } catch (e) {
      docEl.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
    }
  }

  function bindReports() {
    $$('#report-kinds button').forEach((b) => {
      b.addEventListener('click', () => {
        $$('#report-kinds button').forEach((x) => x.classList.toggle('active', x === b));
        state.reports.kind = b.dataset.kind;
        loadReports();
      });
    });
  }

  /* ---------------- audits ---------------- */

  const GRADE_TONE = { A: 'ok', B: 'info', C: 'warn' };
  const SECURITY_TONE = { High: 'bad', Medium: 'warn', Low: 'ok' };
  const UPDATE_TONE = { '已确认': 'ok', '未知': 'warn', '不适用': '' };
  const DUP_TONE = {
    'High Overlap': 'bad',
    'Partial Overlap': 'warn',
    Complementary: 'info',
    Independent: '',
  };

  function barList(values) {
    const entries = Object.entries(values);
    const max = Math.max(1, ...entries.map(([, n]) => Number(n) || 0));
    return entries.map(([k, n]) => (
      `<div class="bar-row"><span class="lbl" title="${esc(k)}">${esc(k)}</span>`
      + `<span class="track"><span class="fill" style="width:${((Number(n) || 0) / max) * 100}%"></span></span>`
      + `<span class="num">${esc(n)}</span></div>`
    )).join('');
  }

  function bindAuditSkillLinks() {
    $$('#audit-body [data-open-skill]').forEach((b) => {
      b.addEventListener('click', (e) => { e.stopPropagation(); openSkill(b.dataset.openSkill); });
    });
  }

  async function loadAudits() {
    const body = $('#audit-body');
    body.innerHTML = skeleton(8);
    try {
      state.loaded.auditsIndex = await api('/api/audits');
    } catch (e) {
      body.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
      return;
    }
    return renderAuditTab();
  }

  function renderAuditTab() {
    const tab = state.audits.tab;
    if (tab === 'overview') return renderAuditOverview();
    if (tab === 'duplication') return renderAuditDuplication();
    if (tab === 'history') return renderAuditHistory();
    if (tab === 'reports') return renderAuditReports();
    return renderAuditTable();
  }

  function renderAuditOverview() {
    const body = $('#audit-body');
    const res = state.loaded.auditsIndex || {};
    const s = res.summary ? res.summary.data : null;
    if (!s) {
      body.innerHTML = '<div class="panel card"><div class="muted">暂无审计汇总。</div></div>';
      return;
    }
    const t = s.totals || {};
    const q = t.quality || {};
    const sec = t.security || {};
    const up = t.updates || {};
    const dup = t.duplication || {};
    const grades = q.grades || {};
    const levels = sec.levels || {};
    const conclusions = up.conclusions || {};
    const pairs = dup.pair_list || [];
    const highPairs = pairs.filter((p) => p.classification === 'High Overlap');
    const pairCount = (cls) => pairs.filter((p) => p.classification === cls).length;
    const baseline = s.baseline || {};
    const scope = s.scope || {};

    let html = '<div class="stat-row">'
      + statCard('质量均分', q.average == null ? '—' : q.average, `A ${grades.A || 0} · B ${grades.B || 0} · C ${grades.C || 0}`, 'ok')
      + statCard('高危', levels.High || 0, `中危 ${levels.Medium || 0} · 低危 ${levels.Low || 0}`, (levels.High || 0) ? 'warn' : 'ok')
      + statCard('重复对数', dup.pairs || 0, `高度重叠 ${highPairs.length} 对`, 'warn')
      + statCard('更新未知', conclusions['未知'] || 0, `已确认 ${conclusions['已确认'] || 0} · 不适用 ${conclusions['不适用'] || 0}`, 'warn')
      + '</div>';

    html += '<div class="grid-2">'
      + '<div class="panel card"><h2>质量等级分布 <span class="hint">均分 ' + esc(q.average == null ? '—' : q.average) + '</span></h2><div class="bars">'
      + barList({ A: grades.A || 0, B: grades.B || 0, C: grades.C || 0 })
      + '</div></div>'
      + '<div class="panel card"><h2>安全等级分布</h2><div class="bars">'
      + barList({ '高危': levels.High || 0, '中危': levels.Medium || 0, '低危': levels.Low || 0 })
      + '</div><h2 style="margin-top:16px">更新结论</h2><div class="bars">'
      + barList({ '已确认': conclusions['已确认'] || 0, '未知': conclusions['未知'] || 0, '不适用': conclusions['不适用'] || 0 })
      + '</div></div>'
      + '</div>';

    html += '<div class="grid-2">'
      + '<div class="panel card"><h2>重复分类分布 <span class="hint">' + esc(dup.pairs || 0) + ' 对关系</span></h2><div class="bars">'
      + barList({
        '高度重叠': pairCount('High Overlap'),
        '部分重叠': pairCount('Partial Overlap'),
        '互补': pairCount('Complementary'),
        '相互独立': pairCount('Independent'),
      })
      + '</div><p class="footnote">判定由审计脚本生成；高度重叠建议人工复核是否合并或补充边界说明。</p></div>'
      + '<div class="panel card"><h2>审计元信息</h2><dl class="kv">'
      + `<dt>审计日期</dt><dd>${esc(s.audit_date || '—')}</dd>`
      + `<dt>生成时间</dt><dd>${esc(fmtTime(s.generated_at))}</dd>`
      + `<dt>基线扫描</dt><dd>${esc(fmtTime(baseline.scan_time))}</dd>`
      + `<dt>基线技能数</dt><dd>${esc(baseline.skill_count == null ? '—' : baseline.skill_count)}</dd>`
      + `<dt>审计范围</dt><dd>${esc(scope.total == null ? '—' : scope.total)} 个技能</dd>`
      + `<dt>只读</dt><dd>${s.read_only ? badge('是', 'ok') : badge('否', 'bad')}</dd>`
      + `<dt>registry 修改</dt><dd>${s.registry_modified ? badge('是', 'bad') : badge('否', 'ok')}</dd>`
      + `<dt>skills 修改</dt><dd>${s.skills_modified ? badge('是', 'bad') : badge('否', 'ok')}</dd>`
      + '</dl></div>'
      + '</div>';

    if (highPairs.length) {
      html += '<div class="panel card audit-block"><h2>高风险重叠 <span class="hint">' + highPairs.length + ' 对</span></h2>'
        + '<table class="mini-table"><thead><tr><th>技能 A</th><th>技能 B</th><th>判定</th></tr></thead><tbody>';
      for (const p of highPairs) {
        html += `<tr><td><button class="name-link" data-open-skill="${esc(p.a)}">${esc(p.a)}</button></td>`
          + `<td><button class="name-link" data-open-skill="${esc(p.b)}">${esc(p.b)}</button></td>`
          + `<td>${badge(p.classification, 'bad')}</td></tr>`;
      }
      html += '</tbody></table></div>';
    }

    html += `<p class="footnote">汇总文件 ${esc(res.summary.name)} · 审计只读运行，不修改登记表与技能内容。</p>`;
    body.innerHTML = html;
    bindAuditSkillLinks();
  }

  async function renderAuditTable() {
    const body = $('#audit-body');
    const s = state.audits;
    const isQuality = s.tab === 'quality';
    const isSecurity = s.tab === 'security';
    const isUpdates = s.tab === 'updates';

    let toolbar = '<div class="toolbar">'
      + `<input id="audit-q" type="search" placeholder="搜索名称 / 分类 / 摘要 / 说明" autocomplete="off" value="${esc(s.q)}">`;
    if (isQuality) {
      toolbar += '<select id="audit-grade"><option value="">全部等级</option>'
        + ['A', 'B', 'C'].map((g) => `<option value="${g}"${s.grade === g ? ' selected' : ''}>${g}</option>`).join('')
        + '</select>';
    }
    if (isSecurity) {
      toolbar += '<select id="audit-level"><option value="">全部等级</option>'
        + ['High', 'Medium', 'Low'].map((g) => `<option value="${g}"${s.level === g ? ' selected' : ''}>${lvLabel(g)}</option>`).join('')
        + '</select>';
    }
    if (isUpdates) {
      toolbar += '<select id="audit-update"><option value="">全部结论</option>'
        + ['已确认', '未知', '不适用'].map((g) => `<option value="${esc(g)}"${s.update === g ? ' selected' : ''}>${esc(g)}</option>`).join('')
        + '</select>';
    }
    toolbar += '<button id="audit-reset" class="ghost">重置</button></div>';

    let head = '<th data-audit-sort="name">名称</th><th data-audit-sort="category">分类</th>';
    if (isQuality) head += '<th data-audit-sort="quality">总分</th><th>等级</th><th>维度得分</th>';
    if (isSecurity) head += '<th data-audit-sort="security">等级</th><th>安全摘要</th>';
    if (isUpdates) head += '<th data-audit-sort="update">结论</th><th>上游</th><th>说明</th>';

    body.innerHTML = toolbar
      + '<div class="panel"><table class="table compact" id="audit-table"><thead><tr>' + head + '</tr></thead>'
      + '<tbody><tr><td colspan="8">' + skeleton(3) + '</td></tr></tbody></table>'
      + '<div class="pager"><span id="audit-count" class="muted"></span><div class="pager-btns">'
      + '<button id="audit-prev" class="ghost">上一页</button><span id="audit-page"></span><button id="audit-next" class="ghost">下一页</button>'
      + '</div></div></div>';

    $('#audit-q').addEventListener('input', debounce((e) => {
      s.q = e.target.value.trim();
      s.page = 1;
      loadAuditRows();
    }, 250));
    if (isQuality) $('#audit-grade').addEventListener('change', (e) => { s.grade = e.target.value; s.page = 1; loadAuditRows(); });
    if (isSecurity) $('#audit-level').addEventListener('change', (e) => { s.level = e.target.value; s.page = 1; loadAuditRows(); });
    if (isUpdates) $('#audit-update').addEventListener('change', (e) => { s.update = e.target.value; s.page = 1; loadAuditRows(); });
    $('#audit-reset').addEventListener('click', () => {
      s.q = ''; s.grade = ''; s.level = ''; s.update = ''; s.page = 1;
      renderAuditTable();
    });
    $('#audit-prev').addEventListener('click', () => { if (s.page > 1) { s.page--; loadAuditRows(); } });
    $('#audit-next').addEventListener('click', () => { s.page++; loadAuditRows(); });
    $$('#audit-body th[data-audit-sort]').forEach((th) => {
      th.addEventListener('click', () => {
        const key = th.dataset.auditSort;
        if (s.sort === key) {
          s.order = s.order === 'asc' ? 'desc' : 'asc';
        } else {
          s.sort = key;
          s.order = (key === 'name' || key === 'category') ? 'asc' : 'desc';
        }
        s.page = 1;
        loadAuditRows();
      });
    });

    await loadAuditRows();
  }

  async function loadAuditRows() {
    const tbody = $('#audit-table tbody');
    if (!tbody) return;
    const s = state.audits;
    let res;
    try {
      res = await api('/api/audits/skills', {
        q: s.q,
        grade: s.tab === 'quality' ? s.grade : '',
        level: s.tab === 'security' ? s.level : '',
        update: s.tab === 'updates' ? s.update : '',
        sort: s.sort,
        order: s.order,
        page: s.page,
        page_size: s.pageSize,
      });
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="8" class="empty">加载失败：${esc(e.message)}</td></tr>`;
      return;
    }
    tbody.innerHTML = res.items.length
      ? res.items.map((r) => auditRow(r, s.tab === 'quality', s.tab === 'security')).join('')
      : '<tr><td colspan="8" class="empty">没有匹配的技能</td></tr>';
    $$('#audit-table tbody tr[data-audit-name]').forEach((tr) => {
      tr.addEventListener('click', () => openSkill(tr.dataset.auditName));
    });
    $('#audit-count').textContent = `共 ${res.total} 个`;
    $('#audit-page').textContent = `${res.page} / ${res.pages}`;
    $('#audit-prev').disabled = res.page <= 1;
    $('#audit-next').disabled = res.page >= res.pages;
  }

  function auditRow(r, isQuality, isSecurity) {
    let cells = '';
    if (isQuality) {
      const dims = Object.entries(r.quality_dimensions || {}).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(' · ');
      cells = `<td class="nowrap">${r.quality_total == null ? '—' : esc(r.quality_total)}</td>`
        + `<td>${r.quality_grade ? badge(r.quality_grade, GRADE_TONE[r.quality_grade] || '') : '—'}</td>`
        + `<td class="dims">${dims || '—'}</td>`;
    } else if (isSecurity) {
      cells = `<td>${r.security_level ? badge(lvLabel(r.security_level), SECURITY_TONE[r.security_level] || '') : '—'}</td>`
        + `<td class="wrap">${esc(r.security_summary || '—')}</td>`;
    } else {
      cells = `<td>${r.update_conclusion ? badge(r.update_conclusion, UPDATE_TONE[r.update_conclusion] || '') : '—'}</td>`
        + `<td class="wrap">${esc(r.upstream || '—')}</td>`
        + `<td class="wrap">${esc(r.update_note || '—')}</td>`;
    }
    return `<tr data-audit-name="${esc(r.name)}">`
      + `<td><div class="name">${esc(r.name)}</div></td>`
      + `<td>${esc(catLabel(r.category))}</td>`
      + cells
      + '</tr>';
  }

  function renderAuditDuplication() {
    const body = $('#audit-body');
    const res = state.loaded.auditsIndex || {};
    const s = res.summary ? res.summary.data : null;
    if (!s) {
      body.innerHTML = '<div class="panel card"><div class="muted">暂无重复分析数据。</div></div>';
      return;
    }
    const dup = (s.totals || {}).duplication || {};
    const pairs = dup.pair_list || [];
    const pairCount = (cls) => pairs.filter((p) => p.classification === cls).length;

    body.innerHTML = '<div class="stat-row">'
      + statCard('重复对数', dup.pairs || 0, '审计脚本记录的关系', 'warn')
      + statCard('高度重叠', pairCount('High Overlap'), '建议优先人工复核', (pairCount('High Overlap') ? 'bad' : 'ok'))
      + statCard('部分重叠', pairCount('Partial Overlap'), '部分职责重叠', 'warn')
      + statCard('互补', pairCount('Complementary'), '互补关系，可保留', 'ok')
      + statCard('相互独立', pairCount('Independent'), '基本无关', '')
      + '</div>'
      + '<div class="toolbar">'
      + `<input id="audit-dup-q" type="search" placeholder="搜索技能名称" autocomplete="off" value="${esc(state.audits.dupQ)}">`
      + '<select id="audit-dup-class"><option value="">全部判定</option>'
      + ['High Overlap', 'Partial Overlap', 'Complementary', 'Independent']
        .map((c) => `<option value="${esc(c)}"${state.audits.dupClass === c ? ' selected' : ''}>${esc(dupLabel(c))}</option>`).join('')
      + '</select><button id="audit-dup-reset" class="ghost">重置</button></div>'
      + '<div class="panel"><table class="table compact" id="audit-dup-table"><thead><tr>'
      + '<th>技能 A</th><th>技能 B</th><th>判定</th></tr></thead><tbody></tbody></table>'
      + '<div class="pager"><span id="audit-dup-count" class="muted"></span></div></div>';

    const renderPairs = () => {
      const ql = state.audits.dupQ.toLowerCase();
      const cls = state.audits.dupClass;
      const filtered = pairs.filter((p) => (!cls || p.classification === cls)
        && (!ql
          || (p.a || '').toLowerCase().includes(ql)
          || (p.b || '').toLowerCase().includes(ql)));
      $('#audit-dup-table tbody').innerHTML = filtered.length
        ? filtered.map((p) => (
          `<tr><td><button class="name-link" data-open-skill="${esc(p.a)}">${esc(p.a)}</button></td>`
          + `<td><button class="name-link" data-open-skill="${esc(p.b)}">${esc(p.b)}</button></td>`
          + `<td>${badge(dupLabel(p.classification), DUP_TONE[p.classification] || '')}</td></tr>`
        )).join('')
        : '<tr><td colspan="3" class="empty">没有匹配的关系</td></tr>';
      $('#audit-dup-count').textContent = `共 ${filtered.length} 对`;
      bindAuditSkillLinks();
    };
    $('#audit-dup-q').addEventListener('input', debounce((e) => {
      state.audits.dupQ = e.target.value.trim();
      renderPairs();
    }, 200));
    $('#audit-dup-class').addEventListener('change', (e) => {
      state.audits.dupClass = e.target.value;
      renderPairs();
    });
    $('#audit-dup-reset').addEventListener('click', () => {
      state.audits.dupQ = '';
      state.audits.dupClass = '';
      $('#audit-dup-q').value = '';
      $('#audit-dup-class').value = '';
      renderPairs();
    });
    renderPairs();
  }

  async function renderAuditHistory() {
    const body = $('#audit-body');
    body.innerHTML = skeleton(8);
    let res;
    try { res = await api('/api/audits/history'); }
    catch (e) {
      body.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
      return;
    }
    const items = res.items || [];
    if (!items.length) {
      body.innerHTML = '<div class="panel card"><div class="muted">暂无历史审计快照。</div></div>';
      return;
    }

    const metric = (snap) => {
      const t = snap.totals || {};
      return {
        avg: (t.quality || {}).average,
        high: ((t.security || {}).levels || {}).High || 0,
        pairs: (t.duplication || {}).pairs || 0,
        unknown: (((t.updates || {}).conclusions) || {})['未知'] || 0,
        total: (snap.scope || {}).total || 0,
      };
    };

    let html = '';
    if (items.length < 2) {
      html += '<div class="panel card"><div class="muted">目前只有一份审计快照（'
        + esc(items[0].audit_date || items[0].name)
        + '）。周度审计产生第二份快照后，这里会显示逐项变化。</div></div>';
    } else {
      const cur = metric(items[0]);
      const prev = metric(items[1]);
      const delta = (a, b, unit) => {
        if (a == null || b == null) return '—';
        const d = Math.round((a - b) * 100) / 100;
        const cls = d > 0 ? 'delta-up' : (d < 0 ? 'delta-down' : 'delta-flat');
        return `<span class="${cls}">${d > 0 ? '+' : ''}${d}${unit || ''}</span>`;
      };
      html += '<div class="stat-row">'
        + statCard('质量均分变化', '', `对比 ${items[1].audit_date || items[1].name}`, '', delta(cur.avg, prev.avg, ' 分'))
        + statCard('安全 High 变化', '', '越低越好', '', delta(cur.high, prev.high, ' 个'))
        + statCard('重复对数变化', '', '越低越好', '', delta(cur.pairs, prev.pairs, ' 对'))
        + statCard('更新未知变化', '', '越低越好', '', delta(cur.unknown, prev.unknown, ' 个'))
        + '</div>';
    }

    html += '<div class="panel"><table class="table compact"><thead><tr>'
      + '<th>审计日期</th><th>生成时间</th><th>基线扫描</th><th>技能数</th>'
      + '<th>质量均分</th><th>安全 High</th><th>重复对数</th><th>更新未知</th><th>只读</th>'
      + '</tr></thead><tbody>';
    for (const snap of items) {
      const t = snap.totals || {};
      const m = metric(snap);
      html += '<tr>'
        + `<td class="nowrap">${esc(snap.audit_date || '—')}</td>`
        + `<td class="nowrap">${esc(fmtTime(snap.generated_at))}</td>`
        + `<td class="nowrap">${esc(fmtTime(snap.baseline_scan_time))}</td>`
        + `<td>${esc(snap.baseline_skill_count == null ? m.total : snap.baseline_skill_count)}</td>`
        + `<td>${esc((t.quality || {}).average == null ? '—' : t.quality.average)}</td>`
        + `<td>${esc(((t.security || {}).levels || {}).High || 0)}</td>`
        + `<td>${esc(((t.duplication || {}).pairs) || 0)}</td>`
        + `<td>${esc((((t.updates || {}).conclusions) || {})['未知'] || 0)}</td>`
        + `<td>${snap.read_only ? badge('是', 'ok') : badge('否', 'bad')}</td>`
        + '</tr>';
    }
    html += '</tbody></table>'
      + '<p class="footnote">审计历史来自 audits/summary-*.json；每次审计生成一份快照。</p></div>';
    body.innerHTML = html;
  }

  function renderAuditReports() {
    const body = $('#audit-body');
    const res = state.loaded.auditsIndex || {};
    const groups = res.reports || {};
    body.innerHTML = '<div class="split">'
      + '<aside class="panel list-pane" id="audit-list"><div class="pad">' + skeleton(4) + '</div></aside>'
      + '<article class="panel doc-pane" id="audit-doc"><div class="muted pad">从左侧选择一份审计报告</div></article>'
      + '</div>';

    let html = '';
    for (const [kind, names] of Object.entries(groups)) {
      if (!names.length) continue;
      html += `<div class="group">${esc(AUDIT_KIND_NAME[kind] || kind)} · ${names.length}</div>`;
      html += names.map((n) => `<button class="item" data-kind="${esc(kind)}" data-name="${esc(n)}">${esc(n)}</button>`).join('');
    }
    const listEl = $('#audit-list');
    listEl.innerHTML = html || '<div class="empty">暂无审计报告</div>';
    $$('#audit-list button.item').forEach((b) => {
      b.addEventListener('click', () => showAudit(b.dataset.kind, b.dataset.name, b));
    });
    const first = $('#audit-list button.item');
    if (first) showAudit(first.dataset.kind, first.dataset.name, first);
  }

  async function showAudit(kind, name, btn) {
    if (btn) $$('#audit-list button.item').forEach((b) => b.classList.toggle('active', b === btn));
    const docEl = $('#audit-doc');
    if (!docEl) return;
    docEl.innerHTML = '<div class="pad">' + skeleton(8) + '</div>';
    try {
      const res = await api('/api/audits/content', { kind, name });
      docEl.innerHTML = `<div class="doc-title">${esc(name)}</div>`
        + `<div class="doc-meta">${esc(AUDIT_KIND_NAME[kind] || kind)}审计 · 只读</div>`
        + `<div class="md">${renderMarkdown(res.content)}</div>`;
      docEl.scrollTop = 0;
    } catch (e) {
      docEl.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
    }
  }

  function bindAudits() {
    const DEFAULT_SORT = {
      quality: ['quality', 'desc'],
      security: ['security', 'desc'],
      updates: ['update', 'desc'],
    };
    $$('#audit-tabs button').forEach((b) => {
      b.addEventListener('click', () => {
        $$('#audit-tabs button').forEach((x) => x.classList.toggle('active', x === b));
        const tab = b.dataset.tab;
        state.audits.tab = tab;
        state.audits.page = 1;
        if (DEFAULT_SORT[tab]) {
          state.audits.sort = DEFAULT_SORT[tab][0];
          state.audits.order = DEFAULT_SORT[tab][1];
        }
        const run = renderAuditTab();
        if (run && run.catch) {
          run.catch((e) => {
            $('#audit-body').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
          });
        }
      });
    });
  }

  /* ---------------- approval center ---------------- */

  const APPROVAL_LABEL = { pending: '待审批', approved: '已批准', rejected: '已拒绝' };
  const APPROVAL_TONE = { pending: 'warn', approved: 'ok', rejected: 'bad' };
  const PRIORITY_TONE = { P0: 'bad', P1: 'warn', P2: 'info' };
  const RESULT_LABEL = {
    applied: ['已应用', 'ok'],
    rolled_back: ['已回滚', 'warn'],
    rollback_failed: ['回滚未完成', 'bad'],
    failed: ['执行失败', 'bad'],
  };
  const PHASE_LABEL = {
    applied: ['已应用', 'ok'],
    rolled_back: ['已回滚', 'warn'],
    applying: ['应用中', 'warn'],
    backing_up: ['备份中', 'warn'],
    mixed: ['状态混合', 'bad'],
    missing: ['目标缺失', 'bad'],
    manual: ['手工备份', 'info'],
    unknown: ['未知', ''],
  };

  function bindApprovals() {
    $$('#approval-tabs button').forEach((b) => {
      b.addEventListener('click', () => selectApprovalTab(b.dataset.tab));
    });
  }

  function selectApprovalTab(tab) {
    state.approvals.tab = tab;
    state.approvals.focus = null;
    $$('#approval-tabs button').forEach((x) => x.classList.toggle('active', x.dataset.tab === tab));
    loadApprovals();
  }

  async function loadApprovals() {
    const body = $('#approval-body');
    if (!body) return;
    body.innerHTML = skeleton(8);
    renderApprovalStats();
    const tab = state.approvals.tab;
    try {
      if (tab === 'changes') return await renderApprovalChanges();
      if (tab === 'logs') return await renderApprovalLogs();
      return await renderApprovalList(tab);
    } catch (e) {
      body.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
    }
  }

  async function renderApprovalStats() {
    const el = $('#approval-stats');
    if (!el) return;
    try {
      const s = await api('/api/approvals/summary');
      const latest = s.latest_change;
      el.innerHTML = `
        <div class="stat ${s.counts.pending ? 'warn' : ''}"><div class="k">待审批</div><div class="v">${s.counts.pending || 0}</div><div class="s">等待你签字的提案</div></div>
        <div class="stat ok"><div class="k">已批准</div><div class="v">${s.counts.approved || 0}</div><div class="s">已签字待执行或已执行</div></div>
        <div class="stat"><div class="k">已拒绝</div><div class="v">${s.counts.rejected || 0}</div><div class="s">未改动任何技能</div></div>
        <div class="stat"><div class="k">变更 / 备份</div><div class="v">${s.changes || 0}</div><div class="s">${latest ? '最近：' + esc(latest.change_id) : '暂无备份'}</div></div>
      `;
    } catch (e) {
      el.innerHTML = `<div class="empty">统计加载失败：${esc(e.message)}</div>`;
    }
  }

  async function renderApprovalList(status) {
    const body = $('#approval-body');
    const res = await api('/api/approvals/proposals');
    const items = (res.items || []).filter((x) => x.status === status);
    const errors = res.errors || [];
    const focus = state.approvals.focus;
    body.innerHTML = `
      ${errors.length ? `<div class="banner warn">提案解析告警：${errors.map(esc).join('；')}</div>` : ''}
      <div class="split approval-split">
        <aside class="panel list-pane" id="approval-list"></aside>
        <article class="panel doc-pane proposal-doc" id="approval-doc"></article>
      </div>`;
    const list = $('#approval-list');
    if (!items.length) {
      list.innerHTML = `<div class="empty">${status === 'pending' ? '当前没有待审批提案。' : '当前没有该类提案。'}</div>`;
      $('#approval-doc').innerHTML = '<div class="empty">提案由命令行或模板生成后出现在这里。</div>';
      return;
    }
    list.innerHTML = `<div class="group">${APPROVAL_LABEL[status]} · ${items.length}</div>` + items.map((p) => `
      <button class="item approval-item" data-id="${esc(p.proposal_id)}">
        <span class="item-title">${esc(p.proposal_id)}</span>
        <span class="item-sub">${esc(p.target_skill || '—')}</span>
        <span class="item-badges">
          ${p.priority ? badge(p.priority, PRIORITY_TONE[p.priority] || '') : ''}
          ${p.result ? badge((RESULT_LABEL[p.result] || [p.result, ''])[0], (RESULT_LABEL[p.result] || ['', ''])[1]) : ''}
          ${p.hash_ok ? badge('哈希一致', 'ok') : badge('哈希不符', 'bad')}
        </span>
      </button>`).join('');
    const buttons = $$('#approval-list .approval-item');
    buttons.forEach((b) => {
      b.addEventListener('click', () => showApprovalProposal(b.dataset.id, b));
    });
    const initial = buttons.find((b) => b.dataset.id === focus) || buttons[0];
    if (initial) showApprovalProposal(initial.dataset.id, initial);
  }

  async function showApprovalProposal(pid, btn) {
    if (btn) $$('#approval-list .approval-item').forEach((b) => b.classList.toggle('active', b === btn));
    const doc = $('#approval-doc');
    if (!doc) return;
    doc.innerHTML = '<div class="pad">' + skeleton(8) + '</div>';
    try {
      const d = await api('/api/approvals/proposals/' + encodeURIComponent(pid));
      state.approvals.current = d;
      state.approvals.currentId = pid;
      doc.innerHTML = renderApprovalDetail(d);
      bindApprovalActions(d);
      doc.scrollTop = 0;
    } catch (e) {
      doc.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
    }
  }

  function listHtml(values) {
    if (!Array.isArray(values) || !values.length) return '<p class="muted">—</p>';
    return '<ul class="plain-list">' + values.map((v) => `<li>${typeof v === 'string' ? inlineMd(v) : esc(JSON.stringify(v))}</li>`).join('') + '</ul>';
  }

  function renderApprovalDetail(d) {
    const f = d.fields || {};
    const s = d.summary || {};
    const status = s.status || f.approval_status || 'pending';
    const result = RESULT_LABEL[f.result];
    const hashes = (d.hash_rows || []).map((h) => `
      <tr>
        <td class="wrap"><code>${esc(h.path)}</code></td>
        <td class="nowrap"><span class="sha" title="${esc(h.baseline)}">${esc(shortHash(h.baseline))}</span></td>
        <td class="nowrap"><span class="sha" title="${esc(h.current || '')}">${esc(shortHash(h.current))}</span></td>
        <td>${h.match ? badge('一致', 'ok') : (d.post_apply && h.exists ? badge('应用后', 'warn') : badge(h.exists ? '已变化' : '文件缺失', 'bad'))}</td>
      </tr>`).join('');
    const lint = (d.lint_errors || []).length
      ? `<ul class="lint-list">${d.lint_errors.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>`
      : '<p class="muted">校验通过：改动差异可应用，哈希与基线一致。</p>';

    let actions = '';
    if (status === 'pending') {
      const blocked = !d.lint_ok || !(s.hash_ok);
      actions = `
        <div class="action-bar">
          <button class="primary" data-act="approve" ${blocked ? 'disabled' : ''}>批准提案</button>
          <button class="ghost" data-act="reject">拒绝…</button>
          ${blocked ? '<span class="action-hint">存在校验问题，修复后才能批准。</span>' : '<span class="action-hint">批准会写入批准语摘要，并把文件移入 proposals/approved/。</span>'}
        </div>`;
    } else if (status === 'approved') {
      if (f.result === 'applied') {
        actions = `<div class="action-bar"><span class="action-hint">该提案已应用。变更编号 <code>${esc(f.change_id || '—')}</code>；如需撤销，请到“变更与备份”执行回滚。</span></div>`;
      } else if (f.result === 'rollback_failed') {
        actions = `<div class="action-bar"><span class="action-hint">上次回滚没有完成，不能重新执行。请先在“变更与备份”对 <code>${esc(f.change_id || '—')}</code> 重试回滚。</span></div>`;
      } else {
        const reapply = f.result === 'rolled_back';
        actions = `
          <div class="action-bar">
            <button class="ghost" data-act="dry">预检（不写入）</button>
            <button class="primary" data-act="apply">正式执行…</button>
            <span class="action-hint">${reapply
              ? `该提案已回滚；再次执行会分配新的变更编号，旧备份继续保留。确认语为“执行 ${esc(f.proposal_id)}”。`
              : `正式执行前服务端会自动再预检一次；确认语为“执行 ${esc(f.proposal_id)}”。`}</span>
          </div>`;
      }
    }

    const approvedInfo = (status !== 'pending') ? `
      <div class="detail-section">
        <h4>批准与执行记录</h4>
        <div class="detail-grid">
          ${cell('批准人', esc(f.approved_by || '—'))}
          ${cell('批准时间', esc(fmtTime(f.approved_at)))}
          ${cell('绑定的改动指纹', f.approved_diff_sha256 ? `<span class="sha" title="${esc(f.approved_diff_sha256)}">${esc(shortHash(f.approved_diff_sha256))}</span>` : '—')}
          ${cell('改动一致性', status === 'approved' ? (d.approved_hash_matches ? badge('匹配', 'ok') : badge('不匹配', 'bad')) : '—')}
          ${cell('变更编号', f.change_id ? `<code>${esc(f.change_id)}</code>` : '—')}
          ${cell('执行结果', result ? badge(result[0], result[1]) : '—')}
        </div>
        ${f.approval_quote ? `<blockquote class="quote">${esc(f.approval_quote)}</blockquote>` : ''}
        ${f.rejection_reason ? `<p class="audit-note"><b>拒绝原因：</b>${esc(f.rejection_reason)}</p>` : ''}
      </div>` : '';

    return `
      <div class="approval-head">
        <div>
          <div class="doc-title">${esc(f.proposal_id)}</div>
          <div class="doc-meta">
            ${badge(APPROVAL_LABEL[status] || status, APPROVAL_TONE[status] || '')}
            ${f.priority ? badge(f.priority, PRIORITY_TONE[f.priority] || '') : ''}
            ${result ? badge(result[0], result[1]) : ''}
            <span>目标：<b>${esc(f.target_skill || '—')}</b></span>
            <span>文件 ${(d.hash_rows || []).length} 个</span>
            <span>${esc(fmtTime(f.created_at))}</span>
          </div>
        </div>
      </div>
      ${d.post_apply ? '<div class="banner info">该提案已应用，哈希显示的是当前文件状态；改动绑定与校验针对批准时的内容。</div>' : ''}
      ${f.result === 'rolled_back' ? '<div class="banner info">该提案已回滚，目标文件已回到批准时的基线；再次执行会分配新的变更编号。</div>' : ''}
      ${f.result === 'rollback_failed' ? '<div class="banner warn">上次回滚未完成，部分文件可能仍处于应用后状态；请先在“变更与备份”重试回滚。</div>' : ''}
      <div class="proposal-sections">
        <section><h4>问题</h4><div class="md">${renderMarkdown(f.problem || '')}</div></section>
        <section><h4>证据</h4>${listHtml(f.evidence)}</section>
        <section><h4>拟改动</h4><div class="md">${renderMarkdown(f.proposed_changes || '')}</div></section>
        <div class="grid-2 proposal-two">
          <section><h4>预期收益</h4><div class="md">${renderMarkdown(f.expected_benefit || '')}</div></section>
          <section><h4>回归风险</h4><div class="md">${renderMarkdown(f.regression_risk || '')}</div></section>
        </div>
        <section><h4>验证计划</h4>${listHtml(f.verification_plan)}</section>
        <section><h4>文件基线</h4>
          <table class="table compact">
            <thead><tr><th>文件</th><th>基线 SHA-256</th><th>当前 SHA-256</th><th>状态</th></tr></thead>
            <tbody>${hashes || '<tr><td colspan="4" class="muted">无基线记录</td></tr>'}</tbody>
          </table>
        </section>
        <section><h4>改动差异（绑定值 ${esc(shortHash(d.diff_sha256))}）</h4>
          ${d.diff ? `<pre class="diff-box"><code>${diffHtml(d.diff)}</code></pre>` : '<p class="muted">没有改动内容</p>'}
        </section>
        <section><h4>结构校验</h4>${lint}</section>
        ${approvedInfo}
        ${actions}
        <div id="apply-output"></div>
      </div>`;
  }

  function cell(k, v) {
    return `<div class="cell"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`;
  }

  function bindApprovalActions(d) {
    const doc = $('#approval-doc');
    const pid = d.fields.proposal_id;
    doc.querySelectorAll('[data-act]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const act = btn.dataset.act;
        if (act === 'approve') openApproveModal(pid);
        else if (act === 'reject') openRejectModal(pid);
        else if (act === 'dry') runApprovalApply(pid, true);
        else if (act === 'apply') openApplyModal(pid);
      });
    });
  }

  function detailErrors(e) {
    const d = e.detail;
    if (d && typeof d === 'object') {
      const parts = [];
      if (d.error) parts.push(d.error);
      if (Array.isArray(d.lint_errors)) parts.push(...d.lint_errors);
      if (parts.length) return parts.join('\n');
    }
    return e.message || String(e);
  }

  function openApproveModal(pid) {
    openModal({
      title: '批准 ' + pid,
      confirmText: '批准并移入 approved/',
      body: `
        <p class="modal-lead">批准语必须包含提案编号与“批准”二字，且不能包含“不批准 / 不予批准 / 拒绝 / 驳回”。它会原样记录在提案与变更日志中。</p>
        <label class="field"><span>批准语</span>
          <textarea id="approve-quote" rows="3">批准 ${esc(pid)}</textarea>
        </label>
        <p class="hint">批准只绑定当前的改动指纹；之后改动内容必须重新提案、重新批准。</p>
        ${modalError()}`,
      onConfirm: async () => {
        const quote = $('#approve-quote').value.trim();
        try {
          await apiPost(`/api/approvals/proposals/${encodeURIComponent(pid)}/approve`, { quote });
        } catch (e) {
          throw new Error(detailErrors(e));
        }
        toast('已批准 ' + pid);
        state.approvals.focus = pid;
        setTimeout(() => selectApprovalTab('approved'), 200);
      },
    });
  }

  function openRejectModal(pid) {
    openModal({
      title: '拒绝 ' + pid,
      confirmText: '拒绝提案',
      danger: true,
      body: `
        <p class="modal-lead">拒绝会把提案移入 proposals/rejected/，不会改动任何技能文件。</p>
        <label class="field"><span>拒绝原因（至少 3 个字符）</span>
          <textarea id="reject-reason" rows="3" placeholder="例如：证据不足，需补充失效引用位置"></textarea>
        </label>
        ${modalError()}`,
      onConfirm: async () => {
        const reason = $('#reject-reason').value.trim();
        if (reason.length < 3) throw new Error('请填写拒绝原因（至少 3 个字符）');
        try {
          await apiPost(`/api/approvals/proposals/${encodeURIComponent(pid)}/reject`, { reason });
        } catch (e) {
          throw new Error(detailErrors(e));
        }
        toast('已拒绝 ' + pid);
        setTimeout(() => selectApprovalTab('rejected'), 200);
      },
    });
  }

  function openApplyModal(pid) {
    openModal({
      title: '正式执行 ' + pid,
      confirmText: '执行变更',
      danger: true,
      body: `
        <p class="modal-lead">将对目标技能文件执行已批准的改动。流程：哈希复核 → 备份 → 应用 → 结构验证；失败自动回滚。</p>
        <label class="field"><span>确认语（必须完全一致）</span>
          <input id="apply-confirm" type="text" placeholder="执行 ${esc(pid)}" autocomplete="off">
        </label>
        <p class="hint">确认语：<code>执行 ${esc(pid)}</code></p>
        ${modalError()}`,
      onConfirm: async () => {
        const confirmation = $('#apply-confirm').value.trim();
        if (confirmation !== `执行 ${pid}`) throw new Error('确认语不一致');
        await runApprovalApply(pid, false, confirmation);
      },
    });
  }

  async function runApprovalApply(pid, dry, confirmation) {
    const out = $('#apply-output');
    if (out) out.innerHTML = `<div class="action-running">${dry ? '预检中…' : '执行中（备份 → 应用 → 验证）…'}</div>`;
    try {
      const res = await apiPost(`/api/approvals/proposals/${encodeURIComponent(pid)}/apply`, {
        dry_run: dry,
        confirmation: confirmation || '',
      });
      renderApplyOutput(res);
      if (res.ok) toast(dry ? '预检通过' : '变更已应用');
      else toast(dry ? '预检未通过' : '执行未完成，请查看输出', 'bad');
      if (!dry && res.ok) {
        state.approvals.focus = pid;
        setTimeout(loadApprovals, 1200);
      }
    } catch (e) {
      if (out) out.innerHTML = `<div class="action-result bad"><b>请求失败</b><pre>${esc(detailErrors(e))}</pre></div>`;
      toast('请求失败', 'bad');
    }
  }

  function renderApplyOutput(res) {
    const out = $('#apply-output');
    if (!out) return;
    const parts = [];
    if (res.message) parts.push(res.message);
    if (res.notice) parts.push(res.notice);
    if (res.preflight) {
      parts.push('— 自动预检 —');
      if (res.preflight.stdout) parts.push(res.preflight.stdout);
      if (res.preflight.stderr) parts.push('[stderr]\n' + res.preflight.stderr);
    }
    if (res.stdout) parts.push(res.stdout);
    if (res.stderr) parts.push('[stderr]\n' + res.stderr);
    out.innerHTML = `<div class="action-result ${res.ok ? 'ok' : 'bad'}">
      <b>${res.ok ? '通过' : '失败'}${res.change_id ? ' · ' + esc(res.change_id) : ''}</b>
      <pre>${esc(parts.join('\n') || '（无输出）')}</pre>
    </div>`;
  }

  async function renderApprovalChanges() {
    const body = $('#approval-body');
    const res = await api('/api/approvals/changes');
    const items = res.items || [];
    body.innerHTML = `
      <div class="panel">
        <table class="table">
          <thead><tr><th>变更编号</th><th>提案</th><th>阶段</th><th>创建时间</th><th>文件</th><th>备份内容</th><th>操作</th></tr></thead>
          <tbody>
            ${items.length ? items.map((c) => {
              const phase = PHASE_LABEL[c.phase] || [c.phase, ''];
              const rolled = c.phase === 'rolled_back';
              const rbNote = rolled
                ? '该变更已回滚，目标文件已恢复'
                : (c.legacy ? '旧格式备份请按 ROLLBACK.md 手工处理' : '');
              return `<tr>
                <td><code>${esc(c.change_id)}</code>${c.dir_name !== c.change_id ? `<div class="muted">目录：${esc(c.dir_name)}</div>` : ''}</td>
                <td>${esc(c.proposal_id || '—')}</td>
                <td>${badge(phase[0], phase[1])}${c.legacy ? ' ' + badge('旧格式', 'info') : ''}</td>
                <td class="nowrap">${esc(fmtTime(c.created_utc))}</td>
                <td>${c.files}</td>
                <td>${c.has_manifest ? 'MANIFEST' : '无 MANIFEST'}${c.has_state ? ' · STATE' : ''}${c.has_rollback ? ' · ROLLBACK' : ''}</td>
                <td class="nowrap">
                  <button class="ghost small" data-change-view="${esc(c.dir_name)}">查看</button>
                  <button class="ghost small" data-change-rollback="${esc(c.dir_name)}" ${rbNote ? `disabled title="${rbNote}"` : ''}>回滚…</button>
                </td>
              </tr>`;
            }).join('') : '<tr><td colspan="7" class="muted pad">还没有备份记录。</td></tr>'}
          </tbody>
        </table>
      </div>
      <p class="footnote">回滚只恢复 MANIFEST 中的 target_files；registry、config.yaml、cron/jobs.json 等受保护文件只做哈希告警，不会恢复。</p>`;
    $$('[data-change-view]').forEach((b) => b.addEventListener('click', () => showChange(b.dataset.changeView)));
    $$('[data-change-rollback]').forEach((b) => {
      if (!b.disabled) b.addEventListener('click', () => openRollbackModal(b.dataset.changeRollback));
    });
  }

  async function showChange(changeId) {
    let d;
    try { d = await api('/api/approvals/changes/' + encodeURIComponent(changeId)); }
    catch (e) { toast(e.message, 'bad'); return; }
    const files = (d.files || []).map((f) => `<tr>
      <td class="wrap"><code>${esc(f.path)}</code></td>
      <td>${f.bytes == null ? '—' : fmtBytes(f.bytes)}</td>
      <td><span class="sha" title="${esc(f.sha256_before || '')}">${esc(shortHash(f.sha256_before))}</span></td>
      <td><span class="sha" title="${esc(f.sha256_after || '')}">${esc(shortHash(f.sha256_after))}</span></td>
    </tr>`).join('');
    openModal({
      title: '备份详情 ' + changeId,
      confirmText: '关闭',
      body: `
        <div class="detail-grid">
          ${cell('提案', esc(d.proposal_id || '—'))}
          ${cell('阶段', esc(d.phase || '—'))}
          ${cell('创建时间', esc(fmtTime(d.created_utc)))}
          ${cell('受保护文件', String((d.protected || []).length))}
        </div>
        <h4>目标文件</h4>
        <table class="table compact"><thead><tr><th>文件</th><th>大小</th><th>变更前</th><th>变更后</th></tr></thead><tbody>${files || '<tr><td colspan="4" class="muted">无记录</td></tr>'}</tbody></table>
        ${d.rollback_md ? `<h4>ROLLBACK.md</h4><pre class="code">${esc(d.rollback_md)}</pre>` : ''}
        ${Object.keys(d.state || {}).length ? `<h4>STATE.json</h4><pre class="code">${esc(JSON.stringify(d.state, null, 2))}</pre>` : ''}`,
      onConfirm: async () => {},
    });
  }

  function openRollbackModal(changeId) {
    openModal({
      title: '回滚 ' + changeId,
      confirmText: '执行回滚',
      danger: true,
      body: `
        <p class="modal-lead">将从备份恢复目标文件并复核 SHA-256。若目标文件在应用后又被修改，回滚会拒绝覆盖。</p>
        <label class="field"><span>确认语（必须完全一致）</span>
          <input id="rollback-confirm" type="text" placeholder="回滚 ${esc(changeId)}" autocomplete="off">
        </label>
        <p class="hint">确认语：<code>回滚 ${esc(changeId)}</code></p>
        ${modalError()}`,
      onConfirm: async () => {
        const confirmation = $('#rollback-confirm').value.trim();
        if (confirmation !== `回滚 ${changeId}`) throw new Error('确认语不一致');
        let res;
        try {
          res = await apiPost(`/api/approvals/changes/${encodeURIComponent(changeId)}/rollback`, { confirmation });
        } catch (e) {
          throw new Error(detailErrors(e));
        }
        toast(res.ok ? '回滚完成' : '回滚失败', res.ok ? '' : 'bad');
        setTimeout(loadApprovals, 400);
        if (!res.ok) {
          throw new Error((res.stdout || '') + (res.stderr ? '\n' + res.stderr : '') || '回滚失败');
        }
      },
    });
  }

  async function renderApprovalLogs() {
    const body = $('#approval-body');
    const kind = state.approvals.log || 'change';
    const names = { change: '变更日志', error: '错误日志', actions: '页面操作' };
    body.innerHTML = `
      <div class="subtabs log-tabs">
        ${Object.keys(names).map((k) => `<button data-log="${k}" class="${k === kind ? 'active' : ''}">${names[k]}</button>`).join('')}
      </div>
      <div class="panel"><div id="log-body" class="log-body">${skeleton(4)}</div></div>`;
    $$('#approval-body [data-log]').forEach((b) => b.addEventListener('click', () => {
      state.approvals.log = b.dataset.log;
      renderApprovalLogs();
    }));
    try {
      const res = await api('/api/approvals/logs', { name: kind });
      const box = $('#log-body');
      box.innerHTML = res.exists
        ? `${res.truncated ? `<div class="banner warn">仅显示最后 ${fmtBytes(res.bytes)} 中的 300 KB。</div>` : ''}<pre class="code log-pre">${esc(res.content || '（空）')}</pre>`
        : '<div class="empty">暂无记录。</div>';
    } catch (e) {
      $('#log-body').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
    }
  }

  /* ---------------- tabs & boot ---------------- */

  const LOADERS = {
    dashboard: loadDashboard,
    skills: loadSkills,
    reports: loadReports,
    audits: loadAudits,
    proposals: loadApprovals,
  };

  function switchView(view) {
    $$('.tabs button').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
    $$('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + view));
    if (view === 'skills') $('#skill-q').focus({ preventScroll: true });
    LOADERS[view]().catch((e) => {
      const el = $('#view-' + view);
      if (el) el.insertAdjacentHTML('beforeend', `<div class="empty">加载失败：${esc(e.message)}</div>`);
    });
  }

  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  let appStarted = false;

  function startApp() {
    if (appStarted) return;
    appStarted = true;
    bindSkills();
    bindReports();
    bindAudits();
    bindApprovals();

    $$('.tabs button').forEach((b) => {
      b.addEventListener('click', () => switchView(b.dataset.view));
    });

    checkHealth().then((h) => { if (h) loadDashboard(); });
  }

  async function boot() {
    bindDrawer();
    bindModal();
    bindAuth();

    let session;
    try {
      session = await authStatus();
    } catch (e) {
      showAuthGate(false);
      return;
    }
    if (session.authenticated) {
      state.csrf = session.csrf_token || '';
      showAuthenticated(session);
      startApp();
    } else {
      showAuthGate(Boolean(session.setup_required));
    }
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
