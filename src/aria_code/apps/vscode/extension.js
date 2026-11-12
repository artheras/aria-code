// Aria Code VS Code Extension — CommonJS, no build step required
'use strict';

const vscode = require('vscode');
const fs = require('fs');
const path = require('path');
const os = require('os');

const DEFAULT_API_BASE = 'http://127.0.0.1:8000';
const HEALTH_POLL_MS = 30_000;

// ── Status bar ──────────────────────────────────────────────────────────────
let statusBarItem;
let healthTimer;
let webviewViewRef = null;
let apiBase = DEFAULT_API_BASE;

function resolveDiscoveredUrl(fileName) {
  try {
    const ariaHome = process.env.ARIA_HOME || path.join(os.homedir(), '.aria-code');
    const legacyHome = path.join(os.homedir(), '.arthera');
    for (const h of [ariaHome, legacyHome]) {
      const p = path.join(h, fileName);
      if (fs.existsSync(p)) {
        const data = JSON.parse(fs.readFileSync(p, 'utf8'));
        if (data && data.url) {
          return String(data.url).replace(/\/+$/, '');
        }
      }
    }
  } catch (_) {}
  return null;
}

function getConfiguredApiBase() {
  const discovered = resolveDiscoveredUrl('runtime.json');
  if (discovered) return discovered;
  const cfg = vscode.workspace.getConfiguration('ariaCode');
  const value = cfg.get('apiBase', DEFAULT_API_BASE);
  return String(value || DEFAULT_API_BASE).replace(/\/+$/, '');
}

function getApiOrigin() {
  try {
    return new URL(apiBase).origin;
  } catch (_) {
    return new URL(DEFAULT_API_BASE).origin;
  }
}

async function checkHealth() {
  try {
    const res = await fetch(apiBase + '/health', { signal: AbortSignal.timeout(4000) });
    if (res.ok) {
      statusBarItem.text = '$(pulse) Aria Online';
      statusBarItem.backgroundColor = undefined;
      return true;
    }
  } catch (_) { /* offline */ }
  statusBarItem.text = '$(warning) Aria Offline';
  statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
  return false;
}

// ── Webview HTML ─────────────────────────────────────────────────────────────
function buildWebviewHtml() {
  const apiOrigin = getApiOrigin();
  // The webview script uses plain functions (no template literals) to avoid
  // nesting backticks inside the outer template literal.
  const css = `
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg:        var(--vscode-editor-background, #1e1e2e);
      --fg:        var(--vscode-editor-foreground, #cdd6f4);
      --accent:    var(--vscode-button-background, #7c3aed);
      --accent-fg: var(--vscode-button-foreground, #ffffff);
      --border:    var(--vscode-panel-border, #313244);
      --input-bg:  var(--vscode-input-background, #181825);
      --input-fg:  var(--vscode-input-foreground, #cdd6f4);
      --hover:     var(--vscode-list-hoverBackground, #2a2a3e);
      --error:     #f38ba8;
      --green:     #a6e3a1;
      --red:       #f38ba8;
      --yellow:    #f9e2af;
      --muted:     var(--vscode-descriptionForeground, #6c7086);
      --radius:    6px;
      --font:      var(--vscode-font-family, 'Segoe UI', system-ui, sans-serif);
      --mono:      var(--vscode-editor-font-family, 'Cascadia Code', 'Courier New', monospace);
    }
    body { background: var(--bg); color: var(--fg); font-family: var(--font); font-size: 13px; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
    .tab-bar { display: flex; border-bottom: 1px solid var(--border); background: var(--bg); flex-shrink: 0; }
    .tab { flex: 1; padding: 8px 4px; text-align: center; cursor: pointer; font-size: 11px; font-weight: 500; color: var(--muted); border-bottom: 2px solid transparent; transition: color .15s, border-color .15s; letter-spacing: .3px; user-select: none; }
    .tab:hover { color: var(--fg); background: var(--hover); }
    .tab.active { color: var(--fg); border-bottom-color: var(--accent); }
    .panel { display: none; flex: 1; overflow-y: auto; padding: 10px; flex-direction: column; gap: 10px; }
    .panel.active { display: flex; }
    .offline-banner { display: none; align-items: center; gap: 6px; padding: 6px 10px; background: rgba(243,139,168,.12); border: 1px solid rgba(243,139,168,.4); border-radius: var(--radius); color: var(--error); font-size: 11px; flex-shrink: 0; }
    .offline-banner.visible { display: flex; }
    .offline-banner button { margin-left: auto; background: rgba(243,139,168,.2); border: 1px solid rgba(243,139,168,.4); color: var(--error); border-radius: 4px; padding: 2px 8px; cursor: pointer; font-size: 11px; }
    .offline-banner button:hover { background: rgba(243,139,168,.35); }
    .card { background: var(--input-bg); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px; }
    .card-title { font-size: 10px; font-weight: 600; letter-spacing: .8px; text-transform: uppercase; color: var(--muted); margin-bottom: 8px; }
    .index-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .index-card { background: var(--input-bg); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px; }
    .index-name { font-size: 11px; color: var(--muted); margin-bottom: 4px; }
    .index-value { font-size: 17px; font-weight: 700; font-family: var(--mono); letter-spacing: -.3px; }
    .index-change { font-size: 11px; margin-top: 3px; font-family: var(--mono); }
    .up { color: var(--green); } .down { color: var(--red); } .flat { color: var(--muted); }
    .kv-row { display: flex; justify-content: space-between; align-items: center; padding: 5px 0; border-bottom: 1px solid var(--border); }
    .kv-row:last-child { border-bottom: none; }
    .kv-key { color: var(--muted); font-size: 12px; } .kv-val { font-family: var(--mono); font-size: 12px; font-weight: 600; }
    .chat-messages { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; padding-bottom: 4px; min-height: 0; }
    .msg { padding: 8px 10px; border-radius: var(--radius); font-size: 12.5px; line-height: 1.55; max-width: 100%; word-break: break-word; }
    .msg.user { background: rgba(124,58,237,.18); border: 1px solid rgba(124,58,237,.35); align-self: flex-end; color: var(--fg); }
    .msg.aria { background: var(--input-bg); border: 1px solid var(--border); align-self: flex-start; color: var(--fg); }
    .msg.aria .msg-label { font-size: 10px; color: var(--accent); font-weight: 700; margin-bottom: 4px; letter-spacing: .5px; }
    .msg.aria code { background: rgba(255,255,255,.07); border-radius: 3px; padding: 1px 4px; font-family: var(--mono); font-size: 11px; }
    .msg.aria pre { background: rgba(0,0,0,.3); border-radius: 4px; padding: 8px; overflow-x: auto; margin: 4px 0; }
    .msg.aria pre code { background: none; padding: 0; }
    .msg.typing { color: var(--muted); font-style: italic; }
    .chat-input-row { display: flex; gap: 6px; flex-shrink: 0; margin-top: 4px; }
    .chat-input { flex: 1; background: var(--input-bg); border: 1px solid var(--border); border-radius: var(--radius); color: var(--input-fg); padding: 7px 10px; font-size: 12.5px; font-family: var(--font); resize: none; outline: none; min-height: 36px; max-height: 100px; }
    .chat-input:focus { border-color: var(--accent); }
    .send-btn { background: var(--accent); color: var(--accent-fg); border: none; border-radius: var(--radius); padding: 7px 12px; cursor: pointer; font-size: 13px; font-weight: 600; align-self: flex-end; transition: opacity .15s; white-space: nowrap; }
    .send-btn:hover { opacity: .85; } .send-btn:disabled { opacity: .45; cursor: not-allowed; }
    .alert-item { display: flex; align-items: center; gap: 8px; padding: 8px; background: var(--input-bg); border: 1px solid var(--border); border-radius: var(--radius); font-size: 12px; }
    .alert-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--yellow); flex-shrink: 0; }
    .alert-dot.triggered { background: var(--green); }
    .alert-meta { flex: 1; } .alert-symbol { font-weight: 700; font-family: var(--mono); } .alert-cond { color: var(--muted); font-size: 11px; margin-top: 1px; }
    .alert-add-row { display: flex; gap: 6px; flex-wrap: wrap; }
    .alert-add-row input { flex: 1; min-width: 80px; background: var(--input-bg); border: 1px solid var(--border); border-radius: var(--radius); color: var(--input-fg); padding: 6px 8px; font-size: 12px; outline: none; }
    .alert-add-row input:focus { border-color: var(--accent); }
    .btn { background: var(--accent); color: var(--accent-fg); border: none; border-radius: var(--radius); padding: 6px 12px; cursor: pointer; font-size: 12px; font-weight: 600; transition: opacity .15s; }
    .btn:hover { opacity: .85; }
    .btn.secondary { background: transparent; border: 1px solid var(--border); color: var(--fg); }
    .btn.secondary:hover { background: var(--hover); }
    .skeleton { background: linear-gradient(90deg, var(--border) 25%, var(--hover) 50%, var(--border) 75%); background-size: 200% 100%; animation: shimmer 1.4s infinite; border-radius: 4px; height: 14px; }
    @keyframes shimmer { 0%{background-position:200% 0} 100%{background-position:-200% 0} }
    .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
    .section-title { font-size: 11px; font-weight: 700; letter-spacing: .5px; text-transform: uppercase; color: var(--muted); }
    .refresh-btn { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 13px; padding: 2px 4px; border-radius: 4px; transition: color .15s, background .15s; }
    .refresh-btn:hover { color: var(--fg); background: var(--hover); }
    .spinning { animation: spin .7s linear infinite; display: inline-block; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .footer { flex-shrink: 0; border-top: 1px solid var(--border); padding: 5px 10px; font-size: 10px; color: var(--muted); display: flex; justify-content: space-between; align-items: center; background: var(--bg); }
    .status-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--muted); display: inline-block; margin-right: 5px; }
    .status-dot.online { background: var(--green); }
    .empty-state { color: var(--muted); text-align: center; padding: 24px 10px; font-size: 12px; }
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 10px; }
  `;

  // Note: all JS inside the webview HTML uses string concatenation (no backtick template
  // literals) so it can safely sit inside the outer JS template literal.
  const webviewScript = `
(function() {
  var vscode = acquireVsCodeApi();
  var API = ${JSON.stringify(apiBase)};
  var BT3 = /\\x60\\x60\\x60([\\s\\S]*?)\\x60\\x60\\x60/g;
  var BT1 = /\\x60([^\\x60]+)\\x60/g;

  // ── helpers ──────────────────────────────────────────────────────────────
  function apiFetch(path, opts) {
    var ctrl = new AbortController();
    var tid = setTimeout(function() { ctrl.abort(); }, 8000);
    return fetch(API + path, Object.assign({}, opts || {}, { signal: ctrl.signal }))
      .then(function(r) {
        clearTimeout(tid);
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .catch(function(e) { clearTimeout(tid); throw e; });
  }

  function setOffline(id, offline) {
    var el = document.getElementById(id);
    if (el) el.classList.toggle('visible', offline);
  }

  function setFooterStatus(online) {
    var dot = document.getElementById('footer-dot');
    if (dot) dot.classList.toggle('online', online);
  }

  function fmt(n, decimals) {
    if (n == null) return '\\u2014';
    decimals = decimals == null ? 2 : decimals;
    return Number(n).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  }

  function changeClass(v) { return v > 0 ? 'up' : v < 0 ? 'down' : 'flat'; }
  function changeSign(v) { return v > 0 ? '+' : ''; }

  function renderMarkdown(text) {
    return text
      .replace(BT3, '<pre><code>$1</code></pre>')
      .replace(BT1, '<code>$1</code>')
      .replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>')
      .replace(/\\*([^*]+)\\*/g, '<em>$1</em>')
      .replace(/^## (.+)$/gm, '<strong style="display:block;margin:6px 0 3px;font-size:12px">$1</strong>')
      .replace(/^# (.+)$/gm, '<strong style="display:block;margin:6px 0 3px">$1</strong>')
      .replace(/^- (.+)$/gm, '\\u2022 $1')
      .replace(/\\\\n/g, '<br>')
      .replace(/\\n/g, '<br>');
  }

  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ── tab switching ─────────────────────────────────────────────────────────
  document.querySelectorAll('.tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
      document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
      document.querySelectorAll('.panel').forEach(function(p) { p.classList.remove('active'); });
      tab.classList.add('active');
      var panel = document.getElementById('panel-' + tab.dataset.tab);
      if (panel) panel.classList.add('active');
      if (tab.dataset.tab === 'portfolio' && !tab.dataset.loaded) {
        tab.dataset.loaded = '1'; loadPortfolio();
      }
      if (tab.dataset.tab === 'alerts' && !tab.dataset.loaded) {
        tab.dataset.loaded = '1'; loadAlerts();
      }
    });
  });

  // ── MARKET ───────────────────────────────────────────────────────────────
  function renderIndexCard(name, val, chg) {
    var cls = changeClass(chg);
    return '<div class="index-card">'
      + '<div class="index-name">' + escHtml(name) + '</div>'
      + '<div class="index-value ' + cls + '">' + (val != null ? fmt(val) : '\\u2014') + '</div>'
      + '<div class="index-change ' + cls + '">' + changeSign(chg) + fmt(chg, 2) + '%</div>'
      + '</div>';
  }

  function renderOfflineIndices() {
    var names = ['S&P 500', 'Nasdaq', '\\u6caa\\u6df3300', 'BTC'];
    var grid = document.getElementById('index-grid');
    if (!grid) return;
    grid.innerHTML = names.map(function(n) {
      return '<div class="index-card">'
        + '<div class="index-name">' + n + '</div>'
        + '<div class="index-value flat">\\u2014</div>'
        + '<div class="index-change flat">Offline</div>'
        + '</div>';
    }).join('');
  }

  function renderIndices(data) {
    var grid = document.getElementById('index-grid');
    if (!grid) return;
    var items = Array.isArray(data) ? data
      : (data.indices || data.data || Object.values(data));
    if (!items.length) { renderOfflineIndices(); return; }
    grid.innerHTML = items.slice(0, 4).map(function(item) {
      var name = item.name || item.symbol || item.code || '\\u2014';
      var val = item.price || item.close || item.last || item.value;
      var chg = item.change_pct || item.change_percent || item.pct_change || item.chg_pct || 0;
      return renderIndexCard(name, val, chg);
    }).join('');
  }

  function renderQuoteFallback(data) {
    var LABELS = { SPY: 'S&P 500', QQQ: 'Nasdaq', 'BTC-USD': 'Bitcoin' };
    var grid = document.getElementById('index-grid');
    if (!grid) return;
    var items = Array.isArray(data) ? data : (data.quotes || data.data || Object.values(data));
    grid.innerHTML = items.slice(0, 4).map(function(q) {
      var sym = q.symbol || q.ticker || '?';
      var price = q.price || q.last || q.close;
      var chg = q.change_percent || q.change_pct || q.chg || 0;
      return renderIndexCard(LABELS[sym] || sym, price, chg);
    }).join('');
  }

  window.loadMarket = function() {
    var btn = document.getElementById('market-refresh-btn');
    if (btn) btn.innerHTML = '<span class="spinning">\\u21bb</span>';

    apiFetch('/api/v1/institution/market/indices')
      .then(function(data) {
        setOffline('market-offline', false);
        setFooterStatus(true);
        renderIndices(data);
      })
      .catch(function() {
        return apiFetch('/api/v2/market/quote?symbols=SPY,QQQ,BTC-USD')
          .then(function(data) {
            setOffline('market-offline', false);
            setFooterStatus(true);
            renderQuoteFallback(data);
          })
          .catch(function() {
            setOffline('market-offline', true);
            setFooterStatus(false);
            renderOfflineIndices();
          });
      })
      .finally(function() {
        if (btn) btn.innerHTML = '\\u21bb';
      });

    apiFetch('/api/v1/institution/analysis/morning-brief')
      .then(function(brief) {
        var briefCard = document.getElementById('brief-card');
        if (!briefCard) return;
        var text = typeof brief === 'string' ? brief
          : (brief.brief || brief.content || brief.text || JSON.stringify(brief, null, 2));
        briefCard.style.color = '';
        briefCard.innerHTML = renderMarkdown(escHtml(text.slice(0, 800) + (text.length > 800 ? '...' : '')));
      })
      .catch(function() {
        var briefCard = document.getElementById('brief-card');
        if (briefCard) briefCard.textContent = 'Morning brief unavailable.';
      });
  };

  // ── PORTFOLIO ─────────────────────────────────────────────────────────────
  window.loadPortfolio = function() {
    var card = document.getElementById('portfolio-card');
    if (!card) return;
    card.innerHTML = '<div class="skeleton" style="width:100%;margin-bottom:8px;height:12px"></div>'
      + '<div class="skeleton" style="width:75%;margin-bottom:8px;height:12px"></div>'
      + '<div class="skeleton" style="width:60%;height:12px"></div>';
    apiFetch('/api/v1/institution/portfolio/overview')
      .then(function(data) {
        setOffline('portfolio-offline', false);
        setFooterStatus(true);
        var d = data.data || data;
        var rows = [];
        function addRow(label, value, cls) {
          if (value == null) return;
          rows.push('<div class="kv-row">'
            + '<span class="kv-key">' + escHtml(label) + '</span>'
            + '<span class="kv-val ' + (cls || '') + '">' + escHtml(String(value)) + '</span>'
            + '</div>');
        }
        addRow('Total Value', d.total_value != null ? '$' + fmt(d.total_value) : null);
        addRow('Cash', d.cash != null ? '$' + fmt(d.cash) : null);
        addRow('Day P&L', d.day_pnl != null
          ? (d.day_pnl >= 0 ? '+$' : '-$') + fmt(Math.abs(d.day_pnl)) : null,
          d.day_pnl >= 0 ? 'up' : 'down');
        addRow('Total P&L', d.total_pnl != null
          ? (d.total_pnl >= 0 ? '+$' : '-$') + fmt(Math.abs(d.total_pnl)) : null,
          d.total_pnl >= 0 ? 'up' : 'down');
        addRow('Positions', d.position_count != null ? d.position_count
          : (d.positions ? d.positions.length : null));
        addRow('Return', d.total_return_pct != null ? fmt(d.total_return_pct, 2) + '%' : null,
          d.total_return_pct >= 0 ? 'up' : 'down');
        if (!rows.length) {
          card.innerHTML = '<pre style="font-size:11px;font-family:var(--mono);white-space:pre-wrap;color:var(--muted)">'
            + escHtml(JSON.stringify(d, null, 2).slice(0, 600)) + '</pre>';
        } else {
          card.innerHTML = rows.join('');
        }
      })
      .catch(function() {
        setOffline('portfolio-offline', true);
        setFooterStatus(false);
        card.innerHTML = '<div class="empty-state">Portfolio data unavailable</div>';
      });
  };

  // ── CHAT ─────────────────────────────────────────────────────────────────
  function appendMsg(role, text, isHtml) {
    var msgs = document.getElementById('chat-messages');
    var div = document.createElement('div');
    div.className = 'msg ' + role;
    if (role === 'aria') {
      div.innerHTML = '<div class="msg-label">ARIA</div>' + (isHtml ? text : escHtml(text));
    } else {
      div.textContent = text;
    }
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
    return div;
  }

  window.sendMessage = function(overrideText) {
    var input = document.getElementById('chat-input');
    var btn = document.getElementById('send-btn');
    var text = (overrideText || input.value).trim();
    if (!text) return;
    if (!overrideText) input.value = '';
    btn.disabled = true;
    appendMsg('user', text, false);
    var typingEl = appendMsg('aria', 'Thinking…', false);
    typingEl.classList.add('typing');

    apiFetch('/api/v2/ai/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text })
    }).then(function(res) {
      setOffline('chat-offline', false);
      setFooterStatus(true);
      var reply = res.response || res.content || res.message || res.answer
        || (res.data && res.data.response) || JSON.stringify(res);
      typingEl.classList.remove('typing');
      typingEl.innerHTML = '<div class="msg-label">ARIA</div>' + renderMarkdown(escHtml(reply));
      vscode.postMessage({ type: 'chatResponse', text: reply });
    }).catch(function() {
      setOffline('chat-offline', true);
      setFooterStatus(false);
      typingEl.classList.remove('typing');
      typingEl.innerHTML = '<div class="msg-label">ARIA</div>'
        + '<span style="color:var(--error)">Could not reach API. Is the backend running?</span>';
    }).finally(function() {
      btn.disabled = false;
      var msgs = document.getElementById('chat-messages');
      if (msgs) msgs.scrollTop = msgs.scrollHeight;
    });
  };

  document.getElementById('chat-input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  // ── ALERTS ────────────────────────────────────────────────────────────────
  window.loadAlerts = function() {
    var list = document.getElementById('alerts-list');
    if (!list) return;
    list.innerHTML = '<div class="empty-state">Loading…</div>';
    apiFetch('/api/v2/alert/list')
      .then(function(data) {
        setOffline('alerts-offline', false);
        setFooterStatus(true);
        var alerts = Array.isArray(data) ? data : (data.alerts || data.data || []);
        if (!alerts.length) {
          list.innerHTML = '<div class="empty-state">No active alerts</div>';
          return;
        }
        list.innerHTML = alerts.map(function(a) {
          var sym = a.symbol || a.ticker || '\\u2014';
          var price = a.target_price || a.price || '\\u2014';
          var dir = a.direction || a.condition || '';
          var triggered = a.triggered || a.status === 'triggered';
          var alertId = a.id || a.alert_id || '';
          return '<div class="alert-item">'
            + '<div class="alert-dot' + (triggered ? ' triggered' : '') + '"></div>'
            + '<div class="alert-meta">'
            + '<div class="alert-symbol">' + escHtml(sym) + '</div>'
            + '<div class="alert-cond">' + escHtml(dir) + ' ' + escHtml(String(price)) + '</div>'
            + '</div>'
            + '<button class="btn secondary" style="font-size:11px;padding:3px 8px" '
            + 'onclick="deleteAlert(' + JSON.stringify(alertId) + ')">✕</button>'
            + '</div>';
        }).join('');
      })
      .catch(function() {
        setOffline('alerts-offline', true);
        setFooterStatus(false);
        list.innerHTML = '<div class="empty-state">Could not load alerts</div>';
      });
  };

  window.addAlert = function() {
    var sym = document.getElementById('alert-symbol').value.trim().toUpperCase();
    var price = document.getElementById('alert-price').value.trim();
    var dir = document.getElementById('alert-dir').value;
    if (!sym || !price) return;
    apiFetch('/api/v2/alert/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: sym, target_price: parseFloat(price), direction: dir })
    }).then(function() {
      document.getElementById('alert-symbol').value = '';
      document.getElementById('alert-price').value = '';
      loadAlerts();
    }).catch(function() { setOffline('alerts-offline', true); });
  };

  window.deleteAlert = function(id) {
    if (!id) return;
    apiFetch('/api/v2/alert/' + id, { method: 'DELETE' })
      .then(function() { loadAlerts(); })
      .catch(function() {});
  };

  // ── messages from extension host ─────────────────────────────────────────
  window.addEventListener('message', function(e) {
    var msg = e.data;
    if (msg.type === 'analyzeText') {
      document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
      document.querySelectorAll('.panel').forEach(function(p) { p.classList.remove('active'); });
      document.querySelector('[data-tab="chat"]').classList.add('active');
      document.getElementById('panel-chat').classList.add('active');
      sendMessage(msg.text);
    }
    if (msg.type === 'setOnline') { setFooterStatus(msg.online); }
  });

  // auto-refresh market every 60 s
  loadMarket();
  setInterval(loadMarket, 60000);
})();
  `;

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src ${apiOrigin};">
<title>Aria Financial</title>
<style>${css}</style>
</head>
<body>

<div class="tab-bar">
  <div class="tab active" data-tab="market">&#x1F4C8; Market</div>
  <div class="tab" data-tab="portfolio">&#x1F4BC; Portfolio</div>
  <div class="tab" data-tab="chat">&#x1F4AC; Chat</div>
  <div class="tab" data-tab="alerts">&#x1F514; Alerts</div>
</div>

<!-- MARKET -->
<div class="panel active" id="panel-market">
  <div class="offline-banner" id="market-offline">&#x26A1; API Offline &#x2014; backend not reachable
    <button onclick="loadMarket()">Retry</button>
  </div>
  <div class="section-header">
    <span class="section-title">Major Indices</span>
    <button class="refresh-btn" id="market-refresh-btn" title="Refresh" onclick="loadMarket()">&#x21BB;</button>
  </div>
  <div class="index-grid" id="index-grid">
    <div class="index-card"><div class="skeleton" style="width:60%;margin-bottom:8px"></div><div class="skeleton" style="width:80%"></div></div>
    <div class="index-card"><div class="skeleton" style="width:60%;margin-bottom:8px"></div><div class="skeleton" style="width:80%"></div></div>
    <div class="index-card"><div class="skeleton" style="width:60%;margin-bottom:8px"></div><div class="skeleton" style="width:80%"></div></div>
    <div class="index-card"><div class="skeleton" style="width:60%;margin-bottom:8px"></div><div class="skeleton" style="width:80%"></div></div>
  </div>
  <div class="section-header" style="margin-top:4px">
    <span class="section-title">Morning Brief</span>
  </div>
  <div class="card" id="brief-card" style="font-size:12px;line-height:1.6;color:var(--muted)">Loading morning brief&#x2026;</div>
</div>

<!-- PORTFOLIO -->
<div class="panel" id="panel-portfolio">
  <div class="offline-banner" id="portfolio-offline">&#x26A1; API Offline &#x2014; backend not reachable
    <button onclick="loadPortfolio()">Retry</button>
  </div>
  <div class="section-header">
    <span class="section-title">Portfolio Overview</span>
    <button class="refresh-btn" onclick="loadPortfolio()" title="Refresh">&#x21BB;</button>
  </div>
  <div class="card" id="portfolio-card">
    <div class="skeleton" style="width:100%;margin-bottom:8px;height:12px"></div>
    <div class="skeleton" style="width:75%;margin-bottom:8px;height:12px"></div>
    <div class="skeleton" style="width:60%;height:12px"></div>
  </div>
</div>

<!-- CHAT -->
<div class="panel" id="panel-chat" style="flex-direction:column;gap:6px">
  <div class="offline-banner" id="chat-offline">&#x26A1; API Offline &#x2014; backend not reachable
    <button onclick="sendMessage()">Retry</button>
  </div>
  <div class="chat-messages" id="chat-messages">
    <div class="msg aria">
      <div class="msg-label">ARIA</div>
      Hello! I&#x2019;m Aria, your AI financial assistant. Ask me anything about markets, stocks, or your portfolio.
    </div>
  </div>
  <div class="chat-input-row">
    <textarea class="chat-input" id="chat-input" placeholder="Ask Aria anything&#x2026;" rows="1"></textarea>
    <button class="send-btn" id="send-btn" onclick="sendMessage()">Send</button>
  </div>
</div>

<!-- ALERTS -->
<div class="panel" id="panel-alerts">
  <div class="offline-banner" id="alerts-offline">&#x26A1; API Offline &#x2014; backend not reachable
    <button onclick="loadAlerts()">Retry</button>
  </div>
  <div class="section-header">
    <span class="section-title">Price Alerts</span>
    <button class="refresh-btn" onclick="loadAlerts()" title="Refresh">&#x21BB;</button>
  </div>
  <div class="card" style="padding:10px">
    <div class="card-title">Add New Alert</div>
    <div class="alert-add-row">
      <input type="text" id="alert-symbol" placeholder="Symbol (e.g. AAPL)" />
      <input type="text" id="alert-price" placeholder="Target price" />
      <select id="alert-dir" style="background:var(--input-bg);border:1px solid var(--border);border-radius:var(--radius);color:var(--input-fg);padding:6px 8px;font-size:12px;outline:none;cursor:pointer">
        <option value="above">Above &#x2191;</option>
        <option value="below">Below &#x2193;</option>
      </select>
      <button class="btn" onclick="addAlert()">+ Add</button>
    </div>
  </div>
  <div class="section-header" style="margin-top:4px">
    <span class="section-title">Active Alerts</span>
  </div>
  <div id="alerts-list">
    <div class="empty-state">Loading alerts&#x2026;</div>
  </div>
</div>

<div class="footer">
  <span><span class="status-dot" id="footer-dot"></span>Aria Code v1.0</span>
  <span>${apiOrigin}</span>
</div>

<script>${webviewScript}</script>
</body>
</html>`;
}

// ── WebviewViewProvider ──────────────────────────────────────────────────────
class AriaWebviewProvider {
  constructor(extensionUri) {
    this._extensionUri = extensionUri;
  }

  resolveWebviewView(webviewView, _context, _token) {
    webviewViewRef = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this._extensionUri],
    };
    webviewView.webview.html = buildWebviewHtml();
    webviewView.webview.onDidReceiveMessage(_msg => { /* handle future msg types */ });
  }
}

// ── Activate ─────────────────────────────────────────────────────────────────
function activate(context) {
  apiBase = getConfiguredApiBase();

  // Status bar
  statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBarItem.text = '$(pulse) Aria …';
  statusBarItem.tooltip = 'Aria Code — click to open dashboard';
  statusBarItem.command = 'aria-code.openPanel';
  statusBarItem.show();
  context.subscriptions.push(statusBarItem);

  // Health polling
  checkHealth();
  healthTimer = setInterval(async () => {
    const online = await checkHealth();
    if (webviewViewRef && webviewViewRef.visible) {
      webviewViewRef.webview.postMessage({ type: 'setOnline', online });
    }
  }, HEALTH_POLL_MS);

  // Register sidebar webview provider
  const provider = new AriaWebviewProvider(context.extensionUri);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider('aria-code.sidebar', provider, {
      webviewOptions: { retainContextWhenHidden: true }
    })
  );

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration(event => {
      if (event.affectsConfiguration('ariaCode.apiBase')) {
        apiBase = getConfiguredApiBase();
        checkHealth();
        if (webviewViewRef) {
          webviewViewRef.webview.html = buildWebviewHtml();
        }
      }
    })
  );

  // Command: open/focus the sidebar panel
  context.subscriptions.push(
    vscode.commands.registerCommand('aria-code.openPanel', async () => {
      await vscode.commands.executeCommand('aria-code.sidebar.focus');
    })
  );

  // Command: analyze selected text with AI
  context.subscriptions.push(
    vscode.commands.registerCommand('aria-code.analyzeSelection', async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showWarningMessage('Aria: No active editor');
        return;
      }
      const text = editor.document.getText(editor.selection).trim();
      if (!text) {
        vscode.window.showWarningMessage('Aria: No text selected');
        return;
      }

      // Reveal the sidebar first
      await vscode.commands.executeCommand('aria-code.sidebar.focus');

      if (webviewViewRef) {
        webviewViewRef.webview.postMessage({
          type: 'analyzeText',
          text: 'Analyze this for me:\n\n' + text
        });
      } else {
        // Fallback: direct API call → notification
        try {
          const res = await fetch(API_BASE + '/api/v2/ai/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: 'Analyze this: ' + text.slice(0, 500) }),
            signal: AbortSignal.timeout(10000),
          });
          if (res.ok) {
            const data = await res.json();
            const reply = data.response || data.content || data.message || JSON.stringify(data);
            vscode.window.showInformationMessage('Aria: ' + reply.slice(0, 200));
          }
        } catch (_) {
          vscode.window.showErrorMessage('Aria: Backend not reachable at ' + apiBase);
        }
      }
    })
  );
}

// ── Deactivate ────────────────────────────────────────────────────────────────
function deactivate() {
  if (healthTimer) clearInterval(healthTimer);
  if (statusBarItem) statusBarItem.dispose();
}

module.exports = { activate, deactivate };
