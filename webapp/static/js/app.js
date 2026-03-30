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
function navigate(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  $(`page-${page}`).classList.add('active');
  document.querySelector(`[data-page="${page}"]`).classList.add('active');

  const loaders = { portfolio: loadPortfolio, positions: loadPositions, activity: loadActivity, crypto: loadCryptoPositions };
  if (loaders[page]) loaders[page]();
}

/* ── Bootstrap ────────────────────────────────────────────────────────── */
// Set today as default end date for backtest and hyperopt
document.addEventListener('DOMContentLoaded', () => {
  const today = new Date().toISOString().slice(0,10);
  if ($('bt-end'))  $('bt-end').value  = today;
  if ($('ho-end'))  $('ho-end').value  = today;
  if ($('cbt-end')) $('cbt-end').value = today;
  if ($('cho-end')) $('cho-end').value = today;
  loadPortfolio();
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

    Plotly.react(container, [{
      x: xs, y: ys, type: 'scatter', mode: 'lines',
      fill: 'tozeroy', fillcolor: fillColor,
      line: { color: lineColor, width: 2 },
      hovertemplate: '%{x}<br><b>$%{y:,.2f}</b><extra></extra>',
    }], plotLayout(), { displayModeBar: false, responsive: true });

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
  const tbody = $('activity-body');
  try {
    const orders = await api('/api/orders');
    if (!orders.length) {
      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--text-3);padding:40px">No orders yet</td></tr>`;
      return;
    }
    tbody.innerHTML = orders.map(o => {
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
  } catch(e) { tbody.innerHTML = `<tr><td colspan="7" style="color:var(--red);padding:20px">${e.message}</td></tr>`; }
}

/* ══════════════════════════════════════════════════════════════════════
   SCREENER PAGE
══════════════════════════════════════════════════════════════════════ */
async function runScreener() {
  const universe = $('screener-universe').value;
  $('screener-loading').style.display = 'flex';
  $('screener-card').style.display    = 'none';
  try {
    const results = await api(`/api/screener?universe=${universe}&max_candidates=20`);
    $('screener-loading').style.display = 'none';
    if (!results.length) {
      $('screener-body').innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--text-3);padding:40px">No setups found right now</td></tr>`;
    } else {
      $('screener-body').innerHTML = results.map(r => {
        const score    = r.score || r.Score || 0;
        const pct      = Math.min(score * 200, 100);
        const strength = score < 0.25 ? 'strong' : score < 0.45 ? 'medium' : 'weak';
        const scoreColor = score < 0.25 ? 'var(--green)' : score < 0.45 ? '#f0a500' : 'var(--text-3)';
        return `
          <tr>
            <td><strong>${r.symbol}</strong></td>
            <td>${fmt(r.close)}</td>
            <td>${fmt(r.bb_lower)}</td>
            <td style="color:var(--green)">${Number(r.rsi || r.RSI).toFixed(1)}</td>
            <td>${Number(r.vol_ratio || r['Vol Ratio'] || 0).toFixed(2)}x</td>
            <td>
              <div class="score-bar">
                <div class="score-track">
                  <div class="score-fill ${strength}" style="width:${pct}%"></div>
                </div>
                <span class="score-num" style="color:${scoreColor}">${score.toFixed(3)}</span>
              </div>
            </td>
          </tr>`;
      }).join('');
    }
    $('screener-card').style.display = 'block';
  } catch(e) {
    $('screener-loading').style.display = 'none';
    $('screener-body').innerHTML = `<tr><td colspan="6" style="color:var(--red);padding:20px">${e.message}</td></tr>`;
    $('screener-card').style.display = 'block';
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
   HYPEROPT PAGE
══════════════════════════════════════════════════════════════════════ */
async function runHyperopt() {
  const nEvals = parseInt($('ho-evals').value);
  $('ho-loading-label').textContent = nEvals;
  $('ho-results').style.display     = 'none';
  $('ho-placeholder').style.display = 'none';
  $('ho-loading').style.display     = 'flex';

  const payload = {
    symbol:     $('ho-symbol').value.trim().toUpperCase() || 'AMZN',
    strategy:   $('ho-strategy').value,
    resolution: $('ho-resolution').value,
    start:      $('ho-start').value,
    end:        $('ho-end').value,
    max_evals:  nEvals,
    train_pct:  parseFloat($('ho-trainpct').value),
    objective:  $('ho-objective').value,
  };

  try {
    const data = await api('/api/hyperopt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    $('ho-loading').style.display = 'none';
    renderHyperoptResults(data);
  } catch(e) {
    $('ho-loading').style.display     = 'none';
    $('ho-placeholder').style.display = 'flex';
    $('ho-placeholder').innerHTML = `<div class="empty-icon">⚠</div><div style="color:var(--red)">${e.message}</div>`;
  }
}

function renderHyperoptResults(data) {
  // Objective label
  const objLabels = { sharpe_ratio: 'Sharpe Ratio', total_return: 'Total Return (%)', profit_factor: 'Profit Factor' };
  $('ho-obj-label').textContent = objLabels[data.objective] || data.objective;

  // Convergence chart — best loss so far converted to score (negate loss back to metric)
  const conv = data.convergence.filter(p => p.best !== null);
  const trials = conv.map(p => p.trial);
  const scores = conv.map(p => -p.best);  // loss = -metric, so negate back

  const trialLosses = data.convergence.map(p => p.loss !== null ? -p.loss : null);

  const layout = plotLayout(260);
  layout.yaxis.tickprefix = '';
  layout.yaxis.title = { text: objLabels[data.objective] || data.objective, font: { size: 10 } };
  layout.xaxis.title = { text: 'Trial', font: { size: 10 } };
  layout.showlegend = true;

  Plotly.react('chart-hyperopt', [
    {
      x: data.convergence.map((_,i) => i+1),
      y: trialLosses,
      type: 'scatter', mode: 'markers', name: 'Trial score',
      marker: { color: 'rgba(139,148,158,0.4)', size: 5 },
      hovertemplate: 'Trial %{x}<br>Score: <b>%{y:.4f}</b><extra></extra>',
    },
    {
      x: trials,
      y: scores,
      type: 'scatter', mode: 'lines', name: 'Best so far',
      line: { color: '#00d4aa', width: 2.5 },
      fill: 'tozeroy', fillcolor: 'rgba(0,212,170,0.06)',
      hovertemplate: 'Trial %{x}<br>Best: <b>%{y:.4f}</b><extra></extra>',
    },
  ], layout, { displayModeBar: false, responsive: true });

  // In-sample vs out-of-sample stats
  const ins = data.in_sample;
  const oos = data.out_of_sample;
  const statDef = [
    { key: 'total_return', label: 'Total Return', fmt: v => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`, color: v => v >= 0 ? 'var(--green)' : 'var(--red)' },
    { key: 'sharpe_ratio', label: 'Sharpe',       fmt: v => v.toFixed(3) },
    { key: 'max_drawdown', label: 'Max Drawdown', fmt: v => `${v.toFixed(1)}%`, color: () => 'var(--red)' },
    { key: 'win_rate',     label: 'Win Rate',     fmt: v => `${v.toFixed(1)}%` },
    { key: 'n_trades',     label: 'Trades',       fmt: v => v },
  ];

  $('ho-stats-row').innerHTML = statDef.map(s => {
    const inV  = ins[s.key];
    const ooV  = oos[s.key];
    const inCol  = s.color ? s.color(inV)  : 'var(--text)';
    const ooCol  = s.color ? s.color(ooV)  : 'var(--text)';
    return `
      <div class="bt-stat glass">
        <div class="bt-stat-label">${s.label}</div>
        <div class="ho-split-vals">
          <div>
            <div class="ho-split-tag">In-sample</div>
            <div class="bt-stat-value" style="color:${inCol};font-size:15px">${s.fmt(inV)}</div>
          </div>
          <div class="ho-split-div"></div>
          <div>
            <div class="ho-split-tag">Out-of-sample</div>
            <div class="bt-stat-value" style="color:${ooCol};font-size:15px">${s.fmt(ooV)}</div>
          </div>
        </div>
      </div>`;
  }).join('');

  // Best params grid
  const paramNames = {
    bb_window: 'BB Window', bb_std: 'BB Std Dev', rsi_window: 'RSI Window',
    buy_rsi: 'Buy RSI', sell_rsi: 'Sell RSI', entry_pct_b_max: 'Entry %B Max',
    stop_loss_pct: 'Stop Loss %', take_profit_pct: 'Take Profit %',
  };
  $('ho-params-grid').innerHTML = Object.entries(data.best_params).map(([k, v]) => {
    const label = paramNames[k] || k;
    const display = typeof v === 'number' && !Number.isInteger(v) ? v.toFixed(4) : v;
    return `
      <div class="ho-param-item">
        <div class="ho-param-label">${label}</div>
        <div class="ho-param-value">${display}</div>
      </div>`;
  }).join('');

  $('ho-results').style.display = 'block';
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

async function runCryptoScreener() {
  $('crypto-screener-loading').style.display = 'flex';
  $('crypto-screener-table').style.display   = 'none';
  $('crypto-screener-empty').style.display   = 'none';
  try {
    const results = await api('/api/crypto/screener');
    $('crypto-screener-loading').style.display = 'none';
    if (!results.length) {
      $('crypto-screener-empty').style.display = 'block';
      return;
    }
    $('crypto-screener-body').innerHTML = results.map(r => {
      const score      = r.score || 0;
      const pct        = Math.min(score * 200, 100);
      const strength   = score < 0.25 ? 'strong' : score < 0.45 ? 'medium' : 'weak';
      const scoreColor = score < 0.25 ? 'var(--green)' : score < 0.45 ? '#f0a500' : 'var(--text-3)';
      return `
        <tr>
          <td><strong>${r.symbol}</strong></td>
          <td>${Number(r.close).toPrecision(6)}</td>
          <td>${Number(r.bb_lower).toPrecision(6)}</td>
          <td style="color:var(--green)">${Number(r.rsi).toFixed(1)}</td>
          <td>${Number(r.vol_ratio).toFixed(2)}x</td>
          <td>
            <div class="score-bar">
              <div class="score-track">
                <div class="score-fill ${strength}" style="width:${pct}%"></div>
              </div>
              <span class="score-num" style="color:${scoreColor}">${score.toFixed(3)}</span>
            </div>
          </td>
        </tr>`;
    }).join('');
    $('crypto-screener-table').style.display = 'table';
  } catch(e) {
    $('crypto-screener-loading').style.display = 'none';
    $('crypto-screener-empty').textContent = `Error: ${e.message}`;
    $('crypto-screener-empty').style.display = 'block';
  }
}

async function runCryptoBacktest() {
  const payload = {
    symbol:     ($('cbt-symbol').value.trim() || 'BTC/USD').toUpperCase(),
    strategy:   $('cbt-strategy').value,
    resolution: $('cbt-resolution').value,
    start:      $('cbt-start').value,
    end:        $('cbt-end').value,
  };

  $('cbt-results').style.display = 'none';
  $('cbt-loading').style.display = 'flex';

  try {
    const data = await api('/api/crypto/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    $('cbt-loading').style.display = 'none';

    const s     = data.stats;
    const stRet = (s.total_return_pct * 100).toFixed(2);
    const bhRet = data.bh_ret.toFixed(2);
    const vs    = (s.total_return_pct * 100 - data.bh_ret).toFixed(2);

    $('cbt-stats-row').innerHTML = `
      <div class="bt-stat glass">
        <div class="bt-stat-label">Strategy Return</div>
        <div class="bt-stat-value" style="color:${Number(stRet)>=0?'var(--green)':'var(--red)'}">
          ${Number(stRet)>=0?'+':''}${stRet}%
        </div>
      </div>
      <div class="bt-stat glass">
        <div class="bt-stat-label">vs Buy &amp; Hold</div>
        <div class="bt-stat-value" style="color:${Number(vs)>=0?'var(--green)':'var(--red)'}">
          ${Number(vs)>=0?'+':''}${vs}pp
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
        <div class="bt-stat-label">Trades</div>
        <div class="bt-stat-value">${s.num_trades}</div>
      </div>`;

    if (data.equity_curve && data.equity_curve.length) {
      const xs   = data.equity_curve.map(p => p.t);
      const ys   = data.equity_curve.map(p => p.v);
      const isPos = s.total_return_pct >= 0;
      const lc   = isPos ? '#00d4aa' : '#ff5b5b';
      const fc   = isPos ? 'rgba(0,212,170,0.08)' : 'rgba(255,91,91,0.08)';
      const initial = ys[0];
      const bhYs    = ys.map((_, i) => initial * (1 + (data.bh_ret/100) * (i / (ys.length-1))));

      Plotly.react('chart-crypto-backtest', [
        { x: xs, y: ys, type: 'scatter', mode: 'lines', name: 'Strategy',
          fill: 'tozeroy', fillcolor: fc, line: { color: lc, width: 2 },
          hovertemplate: '%{x}<br><b>$%{y:,.0f}</b><extra>Strategy</extra>' },
        { x: xs, y: bhYs, type: 'scatter', mode: 'lines', name: 'Buy & Hold',
          line: { color: 'rgba(255,255,255,0.2)', width: 1.5, dash: 'dash' },
          hovertemplate: '%{x}<br><b>$%{y:,.0f}</b><extra>Buy & Hold</extra>' },
      ], plotLayout(300), { displayModeBar: false, responsive: true });
    }

    $('cbt-results').style.display  = 'block';
    $('cbt-compare').style.display  = 'none';
  } catch(e) {
    $('cbt-loading').style.display = 'none';
    alert(`Crypto backtest error: ${e.message}`);
  }
}

async function runCryptoCompare() {
  const payload = {
    strategy:   $('cbt-strategy').value,
    start:      $('cbt-start').value,
    end:        $('cbt-end').value,
    resolution: $('cbt-resolution').value,
  };

  $('cbt-results').style.display  = 'none';
  $('cbt-compare').style.display  = 'none';
  $('cbt-loading').style.display  = 'flex';
  $('cbt-loading-msg').textContent = 'Running strategy on all 12 pairs…';

  try {
    const data = await api('/api/crypto/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    $('cbt-loading').style.display = 'none';

    const stratLabels = {
      crypto_mean_reversion: 'Mean Reversion',
      crypto_trend_following: 'Trend Following',
      crypto_breakout: 'Breakout',
    };
    const rows = data.results;
    if (!rows || !rows.length) {
      alert('No results returned.');
      return;
    }
    $('cbt-compare-body').innerHTML = rows.map((r, i) => {
      const rank = i + 1;
      const sharpeColor = r.sharpe >= 0.8 ? 'var(--green)' : r.sharpe >= 0.5 ? '#f0a500' : 'var(--red)';
      const retColor    = r.return_pct >= 0 ? 'var(--green)' : 'var(--red)';
      const vs          = (r.return_pct - r.bh_ret).toFixed(1);
      const vsColor     = Number(vs) >= 0 ? 'var(--green)' : 'var(--red)';
      return `
        <tr>
          <td><strong>#${rank} ${r.symbol}</strong></td>
          <td style="color:${sharpeColor};font-weight:700">${r.sharpe.toFixed(3)}</td>
          <td style="color:${retColor}">${r.return_pct >= 0 ? '+' : ''}${r.return_pct.toFixed(1)}%</td>
          <td style="color:${vsColor}">${Number(vs) >= 0 ? '+' : ''}${vs}pp</td>
          <td style="color:var(--red)">${r.max_drawdown.toFixed(1)}%</td>
          <td>${r.win_rate.toFixed(1)}%</td>
          <td>${r.n_trades}</td>
        </tr>`;
    }).join('');
    $('cbt-compare').style.display = 'block';
  } catch(e) {
    $('cbt-loading').style.display = 'none';
    alert(`Compare error: ${e.message}`);
  }
}

async function runCryptoHyperopt() {
  const nEvals = parseInt($('cho-evals').value);
  $('cho-loading-label').textContent = nEvals;
  $('cho-results').style.display     = 'none';
  $('cho-placeholder').style.display = 'none';
  $('cho-loading').style.display     = 'flex';

  const payload = {
    symbol:     ($('cho-symbol').value.trim() || 'BTC/USD').toUpperCase(),
    strategy:   $('cho-strategy').value,
    resolution: '60',
    start:      $('cho-start').value,
    end:        $('cho-end').value,
    max_evals:  nEvals,
    train_pct:  parseFloat($('cho-trainpct').value),
    objective:  'sharpe_ratio',
  };

  try {
    const data = await api('/api/hyperopt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    $('cho-loading').style.display = 'none';
    renderCryptoHyperoptResults(data);
  } catch(e) {
    $('cho-loading').style.display     = 'none';
    $('cho-placeholder').style.display = 'flex';
    $('cho-placeholder').innerHTML = `<div class="empty-icon">⚠</div><div style="color:var(--red)">${e.message}</div>`;
  }
}

function renderCryptoHyperoptResults(data) {
  $('cho-obj-label').textContent = 'Sharpe Ratio';

  const conv = data.convergence.filter(p => p.best !== null);
  const trialLosses = data.convergence.map(p => p.loss !== null ? -p.loss : null);
  const scores      = conv.map(p => -p.best);
  const trials      = conv.map(p => p.trial);

  const layout = plotLayout(260);
  layout.yaxis.tickprefix = '';
  layout.yaxis.title = { text: 'Sharpe Ratio', font: { size: 10 } };
  layout.xaxis.title = { text: 'Trial', font: { size: 10 } };
  layout.showlegend  = true;

  Plotly.react('chart-crypto-hyperopt', [
    { x: data.convergence.map((_,i) => i+1), y: trialLosses, type: 'scatter', mode: 'markers',
      name: 'Trial score', marker: { color: 'rgba(139,148,158,0.4)', size: 5 },
      hovertemplate: 'Trial %{x}<br>Score: <b>%{y:.4f}</b><extra></extra>' },
    { x: trials, y: scores, type: 'scatter', mode: 'lines', name: 'Best so far',
      line: { color: '#00d4aa', width: 2.5 }, fill: 'tozeroy', fillcolor: 'rgba(0,212,170,0.06)',
      hovertemplate: 'Trial %{x}<br>Best: <b>%{y:.4f}</b><extra></extra>' },
  ], layout, { displayModeBar: false, responsive: true });

  const ins = data.in_sample;
  const oos = data.out_of_sample;
  const statDef = [
    { key: 'total_return', label: 'Total Return', fmt: v => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`, color: v => v >= 0 ? 'var(--green)' : 'var(--red)' },
    { key: 'sharpe_ratio', label: 'Sharpe',       fmt: v => v.toFixed(3) },
    { key: 'max_drawdown', label: 'Max Drawdown', fmt: v => `${v.toFixed(1)}%`, color: () => 'var(--red)' },
    { key: 'win_rate',     label: 'Win Rate',     fmt: v => `${v.toFixed(1)}%` },
    { key: 'n_trades',     label: 'Trades',       fmt: v => v },
  ];
  $('cho-stats-row').innerHTML = statDef.map(s => {
    const inV = ins[s.key], ooV = oos[s.key];
    const inCol = s.color ? s.color(inV) : 'var(--text)';
    const ooCol = s.color ? s.color(ooV) : 'var(--text)';
    return `
      <div class="bt-stat glass">
        <div class="bt-stat-label">${s.label}</div>
        <div class="ho-split-vals">
          <div><div class="ho-split-tag">In-sample</div><div class="bt-stat-value" style="color:${inCol};font-size:15px">${s.fmt(inV)}</div></div>
          <div class="ho-split-div"></div>
          <div><div class="ho-split-tag">Out-of-sample</div><div class="bt-stat-value" style="color:${ooCol};font-size:15px">${s.fmt(ooV)}</div></div>
        </div>
      </div>`;
  }).join('');

  $('cho-params-grid').innerHTML = Object.entries(data.best_params).map(([k, v]) => {
    const display = typeof v === 'number' && !Number.isInteger(v) ? v.toFixed(4) : v;
    return `<div class="ho-param-item"><div class="ho-param-label">${k}</div><div class="ho-param-value">${display}</div></div>`;
  }).join('');

  $('cho-results').style.display = 'block';
}
