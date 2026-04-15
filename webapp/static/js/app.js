/* ── Config ───────────────────────────────────────────────────────────── */
const API = '';   // same origin — FastAPI serves this file

/* ── Utilities ────────────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);
const fmt  = n  => n == null ? '—' : `$${Number(n).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})}`;
const fmtK = n  => n == null ? '—' : `$${(Number(n)/1000).toFixed(1)}k`;
const fmtPct = n => n == null ? '—' : `${Number(n) >= 0 ? '+' : ''}${Number(n).toFixed(2)}%`;
const sign   = n => Number(n) >= 0 ? 'pos' : 'neg';

async function api(path, opts={}) {
  const r = await fetch(API + path, opts);
  if (!r.ok) { const t = await r.text(); throw new Error(t); }
  return r.json();
}

/* ── Navigation ───────────────────────────────────────────────────────── */
// Per-page auto-refresh. On every navigate() we clear the prior timer and,
// if the destination page has a loader, start a fresh 30s poll so the page
// stays in sync with backend state without a manual click. Backtest is
// deliberately not polled — it's user-triggered and re-running it on a timer
// would re-hit the data API every 30s. See issue #7.
let _pollTimer = null;
const POLL_INTERVAL_MS = 30_000;

function navigate(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  $(`page-${page}`).classList.add('active');
  document.querySelector(`[data-page="${page}"]`).classList.add('active');

  const loaders = {
    portfolio: loadPortfolio,
    positions: loadPositions,
    activity:  loadActivity,
    crypto:    loadCryptoPositions,
    logs:      loadLogs,
  };

  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }

  const loader = loaders[page];
  if (loader) {
    // Call loader once; then set up polling. Loader errors don't break the timer.
    loader().catch(e => console.warn(`Initial load failed for ${page}:`, e));
    _pollTimer = setInterval(() => {
      loader().catch(e => console.warn(`Poll load failed for ${page}:`, e));
    }, POLL_INTERVAL_MS);
  }
}

/* ── Bootstrap ────────────────────────────────────────────────────────── */
// Set today as default end date for backtest, then enter the default page
// via navigate() so the auto-refresh timer gets started for the initial view.
document.addEventListener('DOMContentLoaded', () => {
  const today = new Date().toISOString().slice(0,10);
  if ($('bt-end'))  $('bt-end').value  = today;
  navigate('portfolio');
});

/* ══════════════════════════════════════════════════════════════════════
   PORTFOLIO PAGE
══════════════════════════════════════════════════════════════════════ */
async function loadPortfolio() {
  try {
    const [acct, pos] = await Promise.all([api('/api/account'), api('/api/positions')]);

    // Hero equity
    $('equity-value').textContent = fmt(acct.equity);
    const ch = $('equity-change');
    ch.textContent = `${fmtPct(acct.daily_pl_pct)}  ${fmt(acct.daily_pl)} today`;
    ch.className = `equity-change ${sign(acct.daily_pl)}`;
    $('equity-meta').textContent = `Last close equity: ${fmt(acct.last_equity)}`;

    // Stats
    $('stat-invested').textContent  = fmt(acct.long_mkt);
    $('stat-cash').textContent      = fmt(acct.cash);
    $('stat-bp').textContent        = fmt(acct.buying_power);
    $('stat-positions').textContent = pos.length;

    // Mode badge
    const badge = $('mode-badge');
    badge.textContent = acct.paper ? 'Paper' : 'Live';
    badge.className   = 'mode-badge' + (acct.paper ? '' : ' live');

    // Holdings list
    renderHoldings(pos);

    // Timestamp
    $('last-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();

  } catch(e) { console.error('Portfolio load error:', e); }

  loadEquityLog(7);
}

function renderHoldings(positions) {
  const el = $('holdings-list');
  if (!positions.length) {
    el.innerHTML = '<div class="empty-holdings">No open positions</div>';
    return;
  }
  el.innerHTML = positions.map(p => {
    const pl    = p.unrealized_pl;
    const plPct = p.unrealized_plpc;
    const cls   = pl >= 0 ? 'pos' : 'neg';
    return `
      <div class="holding-row" onclick="navigate('positions')">
        <div class="holding-symbol">${p.symbol}</div>
        <div class="holding-meta">
          <div class="holding-name">${p.qty} shares</div>
          <div class="holding-shares">Avg ${fmt(p.entry)}</div>
        </div>
        <div class="holding-right">
          <div class="holding-value">${fmt(p.market_value)}</div>
          <div class="holding-pl ${cls}">${fmtPct(plPct)}  ${pl >= 0 ? '+' : ''}${fmt(pl)}</div>
        </div>
      </div>`;
  }).join('');
}

/* ── Equity log chart ─────────────────────────────────────────────────── */
async function loadEquityLog(days, tabEl=null) {
  if (tabEl) {
    document.querySelectorAll('.range-tab').forEach(t => t.classList.remove('active'));
    tabEl.classList.add('active');
  }
  try {
    const data = await api(`/api/equity-log?days=${days}`);
    const container = $('chart-equity');
    const empty     = $('chart-equity-empty');

    if (!data.points || !data.points.length) {
      container.style.display = 'none';
      empty.style.display = 'block';
      return;
    }
    container.style.display = 'block';
    empty.style.display = 'none';

    const xs = data.points.map(p => p.t);
    const ys = data.points.map(p => p.v);
    const isPos = ys[ys.length-1] >= ys[0];
    const lineColor = isPos ? '#00d4aa' : '#ff5b5b';
    const fillColor = isPos ? 'rgba(0,212,170,0.08)' : 'rgba(255,91,91,0.08)';

    // Scale y-axis — floor at 85% of min value to show fluctuations with context
    const yMin = Math.min(...ys);
    const yMax = Math.max(...ys);
    const yFloor = Math.floor(yMin * 0.85 / 5000) * 5000;  // round down to nearest $5k
    const yPad = (yMax - yMin) * 0.05 || yMax * 0.02;
    const layout = plotLayout();
    layout.yaxis.range = [yFloor, yMax + yPad];

    // Invisible baseline trace at yFloor, then fill between it and equity line
    const baseline = { x: xs, y: xs.map(() => yFloor), type: 'scatter', mode: 'lines',
      line: { color: 'transparent', width: 0 }, showlegend: false, hoverinfo: 'skip' };
    Plotly.react(container, [baseline, {
      x: xs, y: ys, type: 'scatter', mode: 'lines',
      fill: 'tonexty', fillcolor: fillColor,
      line: { color: lineColor, width: 2 },
      hovertemplate: '%{x}<br><b>$%{y:,.2f}</b><extra></extra>',
    }], layout, { displayModeBar: false, responsive: true });

  } catch(e) { console.warn('No equity log:', e); }
}

/* ══════════════════════════════════════════════════════════════════════
   POSITIONS PAGE
══════════════════════════════════════════════════════════════════════ */
async function loadPositions() {
  const grid  = $('positions-grid');
  const empty = $('positions-empty');
  try {
    const pos = await api('/api/positions');
    if (!pos.length) {
      grid.innerHTML = '';
      grid.style.display  = 'none';
      empty.style.display = 'block';
      return;
    }
    grid.style.display  = 'grid';
    empty.style.display = 'none';

    grid.innerHTML = pos.map(p => {
      const pl    = p.unrealized_pl;
      const plPct = p.unrealized_plpc;
      const cls   = pl >= 0 ? 'pos' : 'neg';
      const sign  = pl >= 0 ? '+' : '';
      return `
        <div class="position-card glass ${cls}">
          <div class="pos-symbol">${p.symbol}</div>
          <div class="pos-shares">${p.qty} shares  ·  ${fmt(p.market_value)} value</div>
          <div class="pos-price-row">
            <div>
              <div class="pos-price-label">Entry</div>
              <div class="pos-price-val">${fmt(p.entry)}</div>
            </div>
            <div style="text-align:right">
              <div class="pos-price-label">Current</div>
              <div class="pos-price-val">${fmt(p.current)}</div>
            </div>
          </div>
          <div class="pos-divider"></div>
          <div class="pos-pl-row">
            <div>
              <div class="pos-price-label">Unrealized P&amp;L</div>
              <div class="pos-pl-amount ${cls}">${sign}${fmt(pl)}</div>
            </div>
            <div class="pos-pl-pct ${cls}">${fmtPct(plPct)}</div>
          </div>
        </div>`;
    }).join('');
  } catch(e) { grid.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠</div>${e.message}</div>`; }
}

/* ══════════════════════════════════════════════════════════════════════
   ACTIVITY PAGE
══════════════════════════════════════════════════════════════════════ */
async function loadActivity() {
  // Load closed trades and recent orders in parallel
  try {
    const [trades, orders] = await Promise.all([api('/api/trades'), api('/api/orders')]);

    // Closed trades table
    const tBody = $('trades-body');
    if (!trades.length) {
      tBody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--text-3);padding:40px">No closed trades yet</td></tr>`;
    } else {
      tBody.innerHTML = trades.map(t => {
        const cls = t.pnl >= 0 ? 'pos' : 'neg';
        const color = t.pnl >= 0 ? 'var(--green)' : 'var(--red)';
        const sgn = t.pnl >= 0 ? '+' : '';
        const entryTime = new Date(t.entry_time).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
        const exitTime  = new Date(t.exit_time).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
        const qtyFmt = t.symbol.includes('/') ? Number(t.qty).toFixed(4).replace(/\.?0+$/, '') : t.qty;
        return `
          <tr>
            <td style="color:var(--text-2);font-size:12px">${entryTime}</td>
            <td style="color:var(--text-2);font-size:12px">${exitTime}</td>
            <td><strong>${t.symbol}</strong></td>
            <td>${qtyFmt}</td>
            <td>${fmt(t.buy_price)}</td>
            <td>${fmt(t.sell_price)}</td>
            <td style="color:${color};font-weight:600">${sgn}${fmt(t.pnl)}</td>
            <td style="color:${color};font-weight:600">${sgn}${t.pnl_pct}%</td>
          </tr>`;
      }).join('');
    }

    // Recent orders table
    const oBody = $('activity-body');
    if (!orders.length) {
      oBody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--text-3);padding:40px">No orders yet</td></tr>`;
    } else {
      oBody.innerHTML = orders.map(o => {
        const time  = o.filled_at ? new Date(o.filled_at).toLocaleString() : (o.created_at ? new Date(o.created_at).toLocaleString() : '—');
        const price = o.fill_price ? fmt(o.fill_price) : '—';
        const side  = o.side.toUpperCase();
        return `
          <tr>
            <td style="color:var(--text-2);font-size:12px">${time}</td>
            <td><strong>${o.symbol}</strong></td>
            <td><span class="badge badge-${o.side}">${side}</span></td>
            <td>${o.qty}</td>
            <td>${price}</td>
            <td style="color:var(--text-2)">${o.type}</td>
            <td style="color:var(--text-2)">${o.status}</td>
          </tr>`;
      }).join('');
    }
  } catch(e) {
    const msg = `<tr><td colspan="8" style="color:var(--red);padding:20px">${e.message}</td></tr>`;
    $('trades-body').innerHTML = msg;
    $('activity-body').innerHTML = msg;
  }
}

/* ══════════════════════════════════════════════════════════════════════
   BACKTEST PAGE
══════════════════════════════════════════════════════════════════════ */
async function runBacktest() {
  const payload = {
    symbol:          $('bt-symbol').value.trim().toUpperCase() || 'AMZN',
    strategy:        $('bt-strategy').value,
    resolution:      $('bt-resolution').value,
    start:           $('bt-start').value,
    end:             $('bt-end').value,
    initial_capital: parseFloat($('bt-capital').value),
    risk_pct:        parseFloat($('bt-risk').value),
  };

  $('bt-results').style.display     = 'none';
  $('bt-placeholder').style.display = 'none';
  $('bt-loading').style.display     = 'flex';

  try {
    const data = await api('/api/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    $('bt-loading').style.display = 'none';
    renderBacktestResults(data, payload);
  } catch(e) {
    $('bt-loading').style.display     = 'none';
    $('bt-placeholder').style.display = 'flex';
    $('bt-placeholder').innerHTML = `<div class="empty-icon">⚠</div><div style="color:var(--red)">${e.message}</div>`;
  }
}

function renderBacktestResults(data, req) {
  const s      = data.stats;
  const stRet  = (s.total_return_pct * 100).toFixed(2);
  const bhRet  = data.bh_ret.toFixed(2);
  const vs     = (s.total_return_pct * 100 - data.bh_ret).toFixed(2);
  const vsSign = Number(vs) >= 0;

  $('bt-stats-row').innerHTML = `
    <div class="bt-stat glass">
      <div class="bt-stat-label">Strategy Return</div>
      <div class="bt-stat-value" style="color:${Number(stRet)>=0?'var(--green)':'var(--red)'}">
        ${Number(stRet)>=0?'+':''}${stRet}%
      </div>
    </div>
    <div class="bt-stat glass">
      <div class="bt-stat-label">vs Buy &amp; Hold</div>
      <div class="bt-stat-value" style="color:${vsSign?'var(--green)':'var(--red)'}">
        ${vsSign?'+':''}${vs}pp
      </div>
      <div class="bt-stat-sub">B&amp;H: ${Number(bhRet)>=0?'+':''}${bhRet}%</div>
    </div>
    <div class="bt-stat glass">
      <div class="bt-stat-label">Sharpe Ratio</div>
      <div class="bt-stat-value">${s.sharpe_ratio.toFixed(2)}</div>
    </div>
    <div class="bt-stat glass">
      <div class="bt-stat-label">Max Drawdown</div>
      <div class="bt-stat-value" style="color:var(--red)">${(s.max_drawdown_pct*100).toFixed(1)}%</div>
    </div>
    <div class="bt-stat glass">
      <div class="bt-stat-label">Win Rate</div>
      <div class="bt-stat-value">${(s.win_rate*100).toFixed(0)}%</div>
      <div class="bt-stat-sub">${s.num_trades} trades</div>
    </div>`;

  // Chart
  if (data.equity_curve && data.equity_curve.length) {
    const xs = data.equity_curve.map(p => p.t);
    const ys = data.equity_curve.map(p => p.v);
    const isPos = s.total_return_pct >= 0;
    const lc = isPos ? '#00d4aa' : '#ff5b5b';
    const fc = isPos ? 'rgba(0,212,170,0.08)' : 'rgba(255,91,91,0.08)';

    // Build B&H curve at same points
    const initial = req.initial_capital;
    const bhStart = data.equity_curve[0].v; // rough proxy
    const bhYs    = ys.map((_, i) => initial * (1 + (data.bh_ret/100) * (i / (ys.length-1))));

    Plotly.react('chart-backtest', [
      { x: xs, y: ys, type: 'scatter', mode: 'lines', name: 'Strategy',
        fill: 'tozeroy', fillcolor: fc, line: { color: lc, width: 2 },
        hovertemplate: '%{x}<br><b>$%{y:,.0f}</b><extra>Strategy</extra>' },
      { x: xs, y: bhYs, type: 'scatter', mode: 'lines', name: 'Buy & Hold',
        line: { color: 'rgba(255,255,255,0.2)', width: 1.5, dash: 'dash' },
        hovertemplate: '%{x}<br><b>$%{y:,.0f}</b><extra>Buy & Hold</extra>' },
    ], plotLayout(350), { displayModeBar: false, responsive: true });
  }

  // Trades
  const tbody = $('bt-trades-body');
  if (!data.trades || !data.trades.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-3);padding:24px">No trades in this period</td></tr>';
  } else {
    tbody.innerHTML = data.trades.map(t => {
      const pl  = t.pnl;
      const cls = pl >= 0 ? 'var(--green)' : 'var(--red)';
      return `
        <tr>
          <td style="font-size:12px;color:var(--text-2)">${t.entry_date}</td>
          <td style="font-size:12px;color:var(--text-2)">${t.exit_date}</td>
          <td>${fmt(t.entry_price)}</td>
          <td>${fmt(t.exit_price)}</td>
          <td style="color:${cls};font-weight:600">${pl>=0?'+':''}${fmt(pl)}</td>
          <td style="color:${cls}">${t.return_pct>=0?'+':''}${t.return_pct}%</td>
        </tr>`;
    }).join('');
  }

  $('bt-results').style.display = 'block';
}

/* ── Plotly shared layout ─────────────────────────────────────────────── */
function plotLayout(height=220) {
  return {
    height,
    paper_bgcolor: 'transparent',
    plot_bgcolor:  'transparent',
    font: { family: 'Inter, sans-serif', color: '#8b949e', size: 11 },
    margin: { l: 52, r: 16, t: 8, b: 36 },
    xaxis: {
      gridcolor: 'rgba(255,255,255,0.05)',
      linecolor:  'rgba(255,255,255,0.08)',
      tickcolor:  'rgba(255,255,255,0.08)',
      showgrid: true,
    },
    yaxis: {
      gridcolor: 'rgba(255,255,255,0.05)',
      linecolor:  'rgba(255,255,255,0.08)',
      tickcolor:  'rgba(255,255,255,0.08)',
      tickprefix: '$',
      showgrid: true,
    },
    legend: {
      bgcolor: 'transparent',
      font: { size: 11, color: '#8b949e' },
    },
    hoverlabel: {
      bgcolor:     'rgba(13,18,32,0.95)',
      bordercolor: 'rgba(255,255,255,0.1)',
      font:        { family: 'Inter, sans-serif', size: 12, color: '#f0f2f5' },
    },
  };
}

/* ══════════════════════════════════════════════════════════════════════
   CRYPTO PAGE
══════════════════════════════════════════════════════════════════════ */
async function loadCryptoPositions() {
  try {
    const pos = await api('/api/crypto/positions');

    // Stats
    const totalValue = pos.reduce((s, p) => s + p.market_value, 0);
    const totalPL    = pos.reduce((s, p) => s + p.unrealized_pl, 0);
    $('crypto-stat-value').textContent = fmt(totalValue);
    $('crypto-stat-pl').textContent    = (totalPL >= 0 ? '+' : '') + fmt(totalPL);
    $('crypto-stat-pl').style.color    = totalPL >= 0 ? 'var(--green)' : 'var(--red)';
    $('crypto-stat-count').textContent = pos.length;

    renderCryptoHoldings(pos);
  } catch(e) { console.error('Crypto positions load error:', e); }
  // Also load arbitrator status on crypto page load
  loadArbitratorStatus();
}

function renderCryptoHoldings(positions) {
  const grid  = $('crypto-positions-grid');
  const empty = $('crypto-positions-empty');

  if (!positions.length) {
    grid.innerHTML  = '';
    grid.style.display   = 'none';
    empty.style.display  = 'block';
    return;
  }
  grid.style.display  = 'grid';
  empty.style.display = 'none';

  grid.innerHTML = positions.map(p => {
    const pl    = p.unrealized_pl;
    const plPct = p.unrealized_plpc;
    const cls   = pl >= 0 ? 'pos' : 'neg';
    const sgn   = pl >= 0 ? '+' : '';
    // Crypto qty formatted to 6 decimal places
    const qtyFmt = Number(p.qty).toFixed(6).replace(/\.?0+$/, '');
    return `
      <div class="position-card glass ${cls}">
        <div class="pos-symbol">${p.symbol}</div>
        <div class="pos-shares">${qtyFmt}  ·  ${fmt(p.market_value)} value</div>
        <div class="pos-price-row">
          <div>
            <div class="pos-price-label">Entry</div>
            <div class="pos-price-val">${fmt(p.entry)}</div>
          </div>
          <div style="text-align:right">
            <div class="pos-price-label">Current</div>
            <div class="pos-price-val">${fmt(p.current)}</div>
          </div>
        </div>
        <div class="pos-divider"></div>
        <div class="pos-pl-row">
          <div>
            <div class="pos-price-label">Unrealized P&amp;L</div>
            <div class="pos-pl-amount ${cls}">${sgn}${fmt(pl)}</div>
          </div>
          <div class="pos-pl-pct ${cls}">${fmtPct(plPct)}</div>
        </div>
      </div>`;
  }).join('');
}

async function loadArbitratorStatus() {
  try {
    const data = await api('/api/crypto/arbitrator');
    // Universe
    if (data.universe && data.universe.length) {
      const raw = data.universe[0];
      const match = raw.match(/Universe:\s*\[(.+)\]/);
      $('arb-universe-list').textContent = match ? match[1] : raw;
    } else {
      $('arb-universe-list').textContent = 'Not available — scanner not running';
    }
    // Decisions
    if (!data.decisions || !data.decisions.length) {
      $('arb-decisions-empty').style.display = 'block';
      $('arb-decisions-table').style.display = 'none';
      return;
    }
    $('arb-decisions-empty').style.display = 'none';
    $('arb-decisions-body').innerHTML = data.decisions.map(line => {
      // Parse log lines like: "2026-04-07 12:00:00  INFO  ▶ ENTER BTC/USD  strategy=crypto_trend_following  conviction=0.85..."
      const timeMatch = line.match(/^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})/);
      const time = timeMatch ? timeMatch[1].split(' ')[1] : '—';
      const isEnter = line.includes('ENTER');
      const action = isEnter ? 'ENTER' : 'EXIT';
      const actionColor = isEnter ? 'var(--green)' : 'var(--red)';
      const pairMatch = line.match(isEnter ? /ENTER\s+(\S+)/ : /EXIT\s+(\S+)/);
      const pair = pairMatch ? pairMatch[1] : '—';
      const stratMatch = line.match(/strategy=(\S+)/);
      const strat = stratMatch ? stratMatch[1] : '—';
      const convMatch = line.match(/conviction=([\d.]+)/);
      const conv = convMatch ? parseFloat(convMatch[1]) : 0;
      const convColor = conv >= 0.7 ? 'var(--green)' : conv >= 0.4 ? '#f0a500' : 'var(--text-muted)';
      // Everything after conviction as details
      const detailMatch = line.match(/conviction=[\d.]+\s+(.*)/);
      const details = detailMatch ? detailMatch[1].substring(0, 60) : '';
      return `<tr>
        <td style="font-family:monospace;font-size:0.8rem">${time}</td>
        <td style="color:${actionColor};font-weight:600">${action}</td>
        <td><strong>${pair}</strong></td>
        <td style="font-size:0.8rem">${strat.replace('crypto_','')}</td>
        <td style="color:${convColor};font-weight:600">${conv.toFixed(2)}</td>
        <td style="font-size:0.8rem;color:var(--text-muted)">${details}</td>
      </tr>`;
    }).join('');
    $('arb-decisions-table').style.display = 'table';
  } catch(e) {
    $('arb-decisions-empty').textContent = `Error: ${e.message}`;
    $('arb-decisions-empty').style.display = 'block';
  }
}

/* ══════════════════════════════════════════════════════════════════════
   LOGS PAGE
══════════════════════════════════════════════════════════════════════ */
async function loadLogs() {
  try {
    const data = await api('/api/logs');
    const eqLog = data.equity || [];
    const crLog = data.crypto || [];

    const eqOut = $('equity-log-output');
    const crOut = $('crypto-log-output');

    eqOut.textContent = eqLog.length ? eqLog.join('') : 'No logs available';
    crOut.textContent = crLog.length ? crLog.join('') : 'No logs available';

    // Update line-count badges
    const eqBadge = $('equity-log-badge');
    const crBadge = $('crypto-log-badge');
    if (eqBadge) eqBadge.textContent = `${eqLog.length} lines`;
    if (crBadge) crBadge.textContent = `${crLog.length} lines`;

    // Auto-scroll to bottom
    eqOut.scrollTop = eqOut.scrollHeight;
    crOut.scrollTop = crOut.scrollHeight;
  } catch(e) {
    console.error('Logs load error:', e);
    $('equity-log-output').textContent = `Error: ${e.message}`;
    $('crypto-log-output').textContent = `Error: ${e.message}`;
  }
}

