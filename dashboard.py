#!/usr/bin/env python3
"""Interactive web dashboard for Stock Screener results.

Usage:
    python dashboard.py                    # Start dashboard on port 5050
    python dashboard.py --port 8080        # Custom port
    python dashboard.py --scan             # Run test scan first, then launch
    python dashboard.py --scan --full      # Run full scan first, then launch
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from threading import Timer

from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

PROJECT_ROOT = Path(__file__).parent.resolve()
SCAN_DIR = PROJECT_ROOT / "data" / "daily_scans"
VENV_PYTHON = PROJECT_ROOT / "venv" / "bin" / "python"
SCAN_LOG_DIR = PROJECT_ROOT / "data" / "logs"
SCAN_STATE_FILE = SCAN_LOG_DIR / "scan_job.json"
SCAN_LOCK_FILE = PROJECT_ROOT / "data" / ".scan.lock"

_scan_process = None
_scan_guard = threading.Lock()


def _read_scan_state():
    if not SCAN_STATE_FILE.exists():
        return {"status": "idle"}
    try:
        return json.loads(SCAN_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "error": "Could not read scan state"}


def _write_scan_state(state):
    SCAN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = SCAN_STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp_file.replace(SCAN_STATE_FILE)


def _process_is_running(process):
    return process is not None and process.poll() is None


def _monitor_scan(process, state, log_file):
    global _scan_process
    return_code = process.wait()
    log_file.close()

    with _scan_guard:
        final_state = dict(state)
        final_state["status"] = "completed" if return_code == 0 else "failed"
        final_state["completed_at"] = datetime.now().astimezone().isoformat()
        final_state["return_code"] = return_code
        if return_code != 0:
            final_state["error"] = "Scan process failed; check the scan log for details"
        _write_scan_state(final_state)
        if _scan_process is process:
            _scan_process = None


def parse_scan_file(filepath):
    """Parse a scan .txt file into structured data."""
    text = Path(filepath).read_text(encoding="utf-8")

    data = {
        "scan_date": "",
        "generated": "",
        "stats": {},
        "spy": {},
        "breadth": {},
        "regime": "",
        "buy_signals": [],
        "sell_signals": [],
    }

    # Header stats
    m = re.search(r"Scan Date:\s*(.+)", text)
    if m:
        data["scan_date"] = m.group(1).strip()

    m = re.search(r"Generated:\s*(.+)", text)
    if m:
        data["generated"] = m.group(1).strip()

    m = re.search(r"Total Universe:\s*([\d,]+)", text)
    if m:
        data["stats"]["total_universe"] = int(m.group(1).replace(",", ""))

    m = re.search(r"Analyzed:\s*([\d,]+)", text)
    if m:
        data["stats"]["analyzed"] = int(m.group(1).replace(",", ""))

    m = re.search(r"Processing Time:\s*([\d.]+)", text)
    if m:
        data["stats"]["processing_minutes"] = float(m.group(1))

    m = re.search(r"Error Rate:\s*([\d.]+)%", text)
    if m:
        data["stats"]["error_rate"] = float(m.group(1))

    m = re.search(r"Buy Signals:\s*(\d+)", text)
    if m:
        data["stats"]["buy_count"] = int(m.group(1))

    m = re.search(r"Sell Signals:\s*(\d+)", text)
    if m:
        data["stats"]["sell_count"] = int(m.group(1))

    # SPY
    m = re.search(r"Phase:\s*(\d)\s*-\s*(.+)", text)
    if m:
        data["spy"]["phase"] = int(m.group(1))
        data["spy"]["phase_name"] = m.group(2).strip()

    m = re.search(r"Trend:\s*(\w+)", text)
    if m:
        data["spy"]["trend"] = m.group(1).strip()

    m = re.search(r"Current Price:\s*\$([\d.]+)", text)
    if m:
        data["spy"]["price"] = float(m.group(1))

    m = re.search(r"Confidence:\s*(\d+)%", text)
    if m:
        data["spy"]["confidence"] = int(m.group(1))

    # Breadth
    for phase_num, label in [(1, "phase_1"), (2, "phase_2"), (3, "phase_3"), (4, "phase_4")]:
        m = re.search(
            rf"Phase {phase_num} \([^)]+\):\s*(\d+)\s*stocks\s*\(([\d.]+)%\)", text
        )
        if m:
            data["breadth"][f"{label}_count"] = int(m.group(1))
            data["breadth"][f"{label}_pct"] = float(m.group(2))

    m = re.search(r"Market Regime:\s*(.+)", text)
    if m:
        data["regime"] = m.group(1).strip()

    # Buy signals — extract each signal as header+details block
    buy_pattern = re.compile(
        r"BUY #(\d+):\s*(\w+)\s*\|\s*Score:\s*([\d.]+)/(\d+)"
        r".*?(?=BUY #\d+:|SELL #\d+:|={50,}.*(?:TOP SELL|END OF|ADDITIONAL)|\Z)",
        re.DOTALL,
    )
    for bm in buy_pattern.finditer(text):
        block = bm.group(0)
        signal = {
            "rank": int(bm.group(1)),
            "ticker": bm.group(2),
            "score": float(bm.group(3)),
            "max_score": int(bm.group(4)),
            "reasons": [],
        }

        m = re.search(r"Phase:\s*(\d)", block)
        if m:
            signal["phase"] = int(m.group(1))

        m = re.search(r"Entry Quality:\s*(\w+)", block)
        if m:
            signal["entry_quality"] = m.group(1)

        m = re.search(r"Stop Loss:\s*\$([\d.]+)", block)
        if m:
            signal["stop_loss"] = float(m.group(1))

        m = re.search(r"Risk/Reward:\s*([\d.]+):1\s*\(Risk \$([\d.]+),\s*Reward \$([\d.]+)\)", block)
        if m:
            signal["rr_ratio"] = float(m.group(1))
            signal["risk"] = float(m.group(2))
            signal["reward"] = float(m.group(3))

        m = re.search(r"RS:\s*([-\d.]+)", block)
        if m:
            signal["rs"] = float(m.group(1))

        m = re.search(r"Volume:\s*([\d.]+)x", block)
        if m:
            signal["volume_ratio"] = float(m.group(1))

        reasons = re.findall(r"[•]\s*(.+)", block)
        signal["reasons"] = [r.strip() for r in reasons[:7]]

        m = re.search(r"Overall Assessment:\s*\n(.+)", block)
        if m:
            signal["assessment"] = m.group(1).strip()

        data["buy_signals"].append(signal)

    # Sell signals
    sell_pattern = re.compile(
        r"SELL #(\d+):\s*(\w+)\s*\|\s*Score:\s*([\d.]+)/(\d+)"
        r".*?(?=SELL #\d+:|={50,}.*(?:END OF|ADDITIONAL)|\Z)",
        re.DOTALL,
    )
    for sm in sell_pattern.finditer(text):
        block = sm.group(0)
        signal = {
            "rank": int(sm.group(1)),
            "ticker": sm.group(2),
            "score": float(sm.group(3)),
            "max_score": int(sm.group(4)),
            "reasons": [],
        }

        m = re.search(r"Phase:\s*(\d)\s*\|\s*.*Severity:\s*(\w+)", block)
        if m:
            signal["phase"] = int(m.group(1))
            signal["severity"] = m.group(2)

        m = re.search(r"Breakdown:\s*\$([\d.]+)", block)
        if m:
            signal["breakdown_level"] = float(m.group(1))

        reasons = re.findall(r"[•]\s*(.+)", block)
        signal["reasons"] = [r.strip() for r in reasons[:5]]

        data["sell_signals"].append(signal)

    return data


def get_scan_files():
    """Get list of available scan files, newest first."""
    if not SCAN_DIR.exists():
        return []
    files = sorted(SCAN_DIR.glob("optimized_scan_*.txt"), reverse=True)
    return [{"name": f.stem, "path": f.name, "date": f.stem.replace("optimized_scan_", "")} for f in files]


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stock Screener Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --border: #2a2d3a;
    --text: #e4e6eb;
    --muted: #8b8fa3;
    --green: #22c55e;
    --red: #ef4444;
    --yellow: #eab308;
    --blue: #3b82f6;
    --purple: #a855f7;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); }

  .header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 16px 32px; display: flex; align-items: center; justify-content: space-between; }
  .header h1 { font-size: 20px; font-weight: 600; }
  .header .meta { color: var(--muted); font-size: 13px; }
  .header .actions { display: flex; gap: 10px; }

  .btn { padding: 8px 16px; border-radius: 6px; border: 1px solid var(--border); background: var(--surface); color: var(--text); cursor: pointer; font-size: 13px; transition: all 0.15s; }
  .btn:hover { border-color: var(--blue); }
  .btn.primary { background: var(--blue); border-color: var(--blue); color: #fff; }
  .btn.primary:hover { opacity: 0.9; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }

  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
  .stat-card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
  .stat-card .value { font-size: 28px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .stat-card.green .value { color: var(--green); }
  .stat-card.red .value { color: var(--red); }
  .stat-card.blue .value { color: var(--blue); }
  .stat-card.yellow .value { color: var(--yellow); }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }

  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
  .card h2 { font-size: 15px; font-weight: 600; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }

  .regime-badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }
  .regime-badge.risk-on { background: rgba(34,197,94,0.15); color: var(--green); }
  .regime-badge.risk-off { background: rgba(239,68,68,0.15); color: var(--red); }
  .regime-badge.transitional { background: rgba(234,179,8,0.15); color: var(--yellow); }

  .chart-wrap { position: relative; height: 250px; }

  .signal-table { width: 100%; border-collapse: collapse; }
  .signal-table th { text-align: left; padding: 10px 12px; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); }
  .signal-table td { padding: 12px; border-bottom: 1px solid var(--border); font-size: 13px; vertical-align: top; }
  .signal-table tr:hover { background: rgba(59,130,246,0.04); }

  .ticker { font-weight: 700; font-size: 14px; color: var(--blue); }
  .score-bar { height: 6px; border-radius: 3px; background: var(--border); overflow: hidden; width: 80px; display: inline-block; vertical-align: middle; margin-left: 6px; }
  .score-bar .fill { height: 100%; border-radius: 3px; }
  .score-num { font-weight: 600; font-size: 13px; }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .badge.good { background: rgba(34,197,94,0.15); color: var(--green); }
  .badge.extended { background: rgba(234,179,8,0.15); color: var(--yellow); }
  .badge.poor { background: rgba(239,68,68,0.15); color: var(--red); }
  .badge.critical { background: rgba(239,68,68,0.25); color: var(--red); }
  .badge.high { background: rgba(239,68,68,0.15); color: var(--red); }
  .badge.medium { background: rgba(234,179,8,0.15); color: var(--yellow); }

  .reasons-list { list-style: none; padding: 0; margin-top: 4px; }
  .reasons-list li { font-size: 11px; color: var(--muted); padding: 1px 0; }

  .expand-btn { background: none; border: none; color: var(--blue); cursor: pointer; font-size: 11px; padding: 2px 0; }
  .expand-btn:hover { text-decoration: underline; }

  .no-data { text-align: center; padding: 60px 20px; color: var(--muted); }
  .no-data h2 { font-size: 18px; margin-bottom: 8px; color: var(--text); }

  .scan-select { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 6px 10px; border-radius: 6px; font-size: 13px; }

  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--blue); border-radius: 50%; animation: spin 0.6s linear infinite; margin-right: 6px; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .toast { position: fixed; bottom: 24px; right: 24px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 20px; font-size: 13px; box-shadow: 0 4px 20px rgba(0,0,0,0.4); z-index: 100; transition: opacity 0.3s; }
  .scan-status { color: var(--muted); font-size: 12px; align-self: center; white-space: nowrap; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>Stock Screener</h1>
    <div class="meta" id="headerMeta">Loading...</div>
  </div>
  <div class="actions">
    <span class="scan-status" id="scanStatus">Idle</span>
    <select class="scan-select" id="scanSelect" onchange="loadScan(this.value)"></select>
    <button class="btn" onclick="runScan(false)" id="testBtn">Test Scan (100)</button>
    <button class="btn primary" onclick="runScan(true)" id="fullBtn">Full Scan</button>
  </div>
</div>

<div class="container" id="content">
  <div class="no-data" id="noData" style="display:none">
    <h2>No scan data yet</h2>
    <p>Run a test scan to get started</p>
  </div>
  <div id="dashboard" style="display:none">

    <div class="stats-row" id="statsRow"></div>

    <div class="grid-2">
      <div class="card">
        <h2>Market Breadth</h2>
        <div class="chart-wrap"><canvas id="breadthChart"></canvas></div>
      </div>
      <div class="card">
        <h2>SPY Regime</h2>
        <div id="spyInfo"></div>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h2 style="color:var(--green)">Buy Signals</h2>
      <div style="overflow-x:auto">
        <table class="signal-table" id="buyTable">
          <thead>
            <tr>
              <th>#</th>
              <th>Ticker</th>
              <th>Score</th>
              <th>Entry</th>
              <th>Stop Loss</th>
              <th>R:R</th>
              <th>RS</th>
              <th>Key Reasons</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <h2 style="color:var(--red)">Sell Signals</h2>
      <div style="overflow-x:auto">
        <table class="signal-table" id="sellTable">
          <thead>
            <tr>
              <th>#</th>
              <th>Ticker</th>
              <th>Score</th>
              <th>Severity</th>
              <th>Breakdown</th>
              <th>Reasons</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>

  </div>
</div>

<script>
let breadthChartInstance = null;
let previousScanStatus = null;

async function loadScanList() {
  const res = await fetch('api/scans');
  const scans = await res.json();
  const sel = document.getElementById('scanSelect');
  sel.innerHTML = '';
  if (scans.length === 0) {
    sel.innerHTML = '<option>No scans</option>';
    document.getElementById('noData').style.display = '';
    document.getElementById('dashboard').style.display = 'none';
    return;
  }
  scans.forEach((s, i) => {
    const opt = document.createElement('option');
    opt.value = s.path;
    opt.textContent = s.date.replace('_', ' ');
    sel.appendChild(opt);
  });
  loadScan(scans[0].path);
}

async function loadScan(path) {
  const res = await fetch('api/scan?path=' + encodeURIComponent(path));
  const data = await res.json();
  if (data.error) {
    document.getElementById('noData').style.display = '';
    document.getElementById('dashboard').style.display = 'none';
    return;
  }
  renderDashboard(data);
}

function renderDashboard(d) {
  document.getElementById('noData').style.display = 'none';
  document.getElementById('dashboard').style.display = '';
  document.getElementById('headerMeta').textContent =
    `Scan: ${d.scan_date} | ${d.stats.analyzed || '?'} stocks analyzed | ${d.regime}`;

  // Stats cards
  const buyCount = d.stats.buy_count || d.buy_signals.length;
  const sellCount = d.stats.sell_count || d.sell_signals.length;
  const topScore = d.buy_signals.length > 0 ? d.buy_signals[0].score : '-';

  document.getElementById('statsRow').innerHTML = `
    <div class="stat-card green">
      <div class="label">Buy Signals</div>
      <div class="value">${buyCount}</div>
      <div class="sub">Phase 2 confirmed uptrends</div>
    </div>
    <div class="stat-card red">
      <div class="label">Sell Signals</div>
      <div class="value">${sellCount}</div>
      <div class="sub">Phase 3/4 breakdowns</div>
    </div>
    <div class="stat-card blue">
      <div class="label">Top Score</div>
      <div class="value">${topScore}<span style="font-size:14px;color:var(--muted)">/125</span></div>
      <div class="sub">${d.buy_signals.length > 0 ? d.buy_signals[0].ticker : '-'}</div>
    </div>
    <div class="stat-card">
      <div class="label">Universe</div>
      <div class="value">${(d.stats.analyzed || 0).toLocaleString()}</div>
      <div class="sub">of ${(d.stats.total_universe || 0).toLocaleString()} | ${d.stats.processing_minutes || '?'} min</div>
    </div>
    <div class="stat-card ${d.spy && d.spy.phase === 2 ? 'green' : d.spy && d.spy.phase === 4 ? 'red' : 'yellow'}">
      <div class="label">SPY Phase</div>
      <div class="value">${d.spy.phase || '?'}</div>
      <div class="sub">${d.spy.trend || ''} @ $${(d.spy.price || 0).toFixed(2)}</div>
    </div>
  `;

  // Breadth chart
  const b = d.breadth;
  if (breadthChartInstance) breadthChartInstance.destroy();
  const ctx = document.getElementById('breadthChart').getContext('2d');
  breadthChartInstance = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Phase 1 (Base)', 'Phase 2 (Uptrend)', 'Phase 3 (Distribution)', 'Phase 4 (Downtrend)'],
      datasets: [{
        data: [b.phase_1_pct || 0, b.phase_2_pct || 0, b.phase_3_pct || 0, b.phase_4_pct || 0],
        backgroundColor: ['#eab308', '#22c55e', '#f97316', '#ef4444'],
        borderColor: '#1a1d27',
        borderWidth: 3
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: 'right', labels: { color: '#8b8fa3', font: { size: 11 }, padding: 12 } },
        tooltip: { callbacks: { label: (c) => `${c.label}: ${c.parsed}% (${b[`phase_${c.dataIndex+1}_count`] || 0} stocks)` } }
      }
    }
  });

  // SPY info
  const regimeClass = d.regime.includes('RISK-ON') ? 'risk-on' : d.regime.includes('RISK-OFF') ? 'risk-off' : 'transitional';
  document.getElementById('spyInfo').innerHTML = `
    <div style="margin-bottom:16px">
      <span class="regime-badge ${regimeClass}">${d.regime}</span>
    </div>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:2px">Phase</div>
        <div style="font-size:20px;font-weight:700">${d.spy.phase} - ${d.spy.phase_name || ''}</div>
      </div>
      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:2px">Confidence</div>
        <div style="font-size:20px;font-weight:700">${d.spy.confidence || '?'}%</div>
      </div>
      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:2px">Price</div>
        <div style="font-size:20px;font-weight:700">$${(d.spy.price || 0).toFixed(2)}</div>
      </div>
      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:2px">Trend</div>
        <div style="font-size:20px;font-weight:700;color:${d.spy.trend === 'Bullish' ? 'var(--green)' : d.spy.trend === 'Bearish' ? 'var(--red)' : 'var(--yellow)'}">${d.spy.trend || '?'}</div>
      </div>
    </div>
    <div style="margin-top:20px;padding:12px;background:var(--bg);border-radius:8px;font-size:12px;color:var(--muted)">
      ${d.regime.includes('RISK-ON')
        ? 'Favorable environment for breakout trades. Focus on Phase 2 breakouts with strong RS.'
        : d.regime.includes('RISK-OFF')
        ? 'Defensive environment — raise cash, tighten stops. Avoid new breakouts.'
        : 'Mixed/transitional market — be selective. Focus on highest quality setups only.'}
    </div>
  `;

  // Buy signals table
  const buyBody = document.querySelector('#buyTable tbody');
  if (d.buy_signals.length === 0) {
    buyBody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:30px">No buy signals</td></tr>';
  } else {
    buyBody.innerHTML = d.buy_signals.map(s => {
      const pct = (s.score / (s.max_score || 125)) * 100;
      const barColor = pct >= 80 ? 'var(--green)' : pct >= 60 ? 'var(--blue)' : 'var(--yellow)';
      const entryClass = (s.entry_quality || '').toLowerCase();
      const topReasons = (s.reasons || []).slice(0, 3).map(r => `<li>${cleanEmoji(r)}</li>`).join('');
      const extraReasons = (s.reasons || []).slice(3);
      const extraHTML = extraReasons.length > 0
        ? `<div class="extra-reasons" style="display:none">${extraReasons.map(r => `<li>${cleanEmoji(r)}</li>`).join('')}</div><button class="expand-btn" onclick="toggleReasons(this)">+${extraReasons.length} more</button>`
        : '';

      return `<tr>
        <td>${s.rank}</td>
        <td><span class="ticker">${s.ticker}</span></td>
        <td>
          <span class="score-num">${s.score}</span>
          <div class="score-bar"><div class="fill" style="width:${pct}%;background:${barColor}"></div></div>
        </td>
        <td><span class="badge ${entryClass}">${s.entry_quality || '-'}</span></td>
        <td>${s.stop_loss ? '$' + s.stop_loss.toFixed(2) : '-'}</td>
        <td style="color:${(s.rr_ratio||0) >= 3 ? 'var(--green)' : (s.rr_ratio||0) >= 2 ? 'var(--text)' : 'var(--yellow)'}">${s.rr_ratio ? s.rr_ratio.toFixed(1) + ':1' : '-'}</td>
        <td style="color:${(s.rs||0) > 0.1 ? 'var(--green)' : (s.rs||0) > 0 ? 'var(--text)' : 'var(--red)'}">${s.rs != null ? s.rs.toFixed(3) : '-'}</td>
        <td><ul class="reasons-list">${topReasons}</ul>${extraHTML}</td>
      </tr>`;
    }).join('');
  }

  // Sell signals table
  const sellBody = document.querySelector('#sellTable tbody');
  if (d.sell_signals.length === 0) {
    sellBody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:30px">No sell signals</td></tr>';
  } else {
    sellBody.innerHTML = d.sell_signals.map(s => {
      const sevClass = (s.severity || 'medium').toLowerCase();
      const reasons = (s.reasons || []).map(r => `<li>${cleanEmoji(r)}</li>`).join('');
      return `<tr>
        <td>${s.rank}</td>
        <td><span class="ticker">${s.ticker}</span></td>
        <td><span class="score-num">${s.score}</span></td>
        <td><span class="badge ${sevClass}">${(s.severity || '?').toUpperCase()}</span></td>
        <td>${s.breakdown_level ? '$' + s.breakdown_level.toFixed(2) : '-'}</td>
        <td><ul class="reasons-list">${reasons}</ul></td>
      </tr>`;
    }).join('');
  }
}

function cleanEmoji(text) {
  return text.replace(/[🟢🔴🟡⭐⚠✓🚨]/g, '').trim();
}

function toggleReasons(btn) {
  const extra = btn.previousElementSibling;
  if (extra.style.display === 'none') {
    extra.style.display = '';
    btn.textContent = 'less';
  } else {
    extra.style.display = 'none';
    btn.textContent = btn.textContent;
  }
}

async function runScan(full) {
  const confirmation = full
    ? 'Confirm Full Scan? This scans the complete US stock universe and usually takes 60-90+ minutes.'
    : 'Confirm Test Scan? This scans 100 stocks and usually takes about 2 minutes.';
  if (!window.confirm(confirmation)) return;

  const btn = full ? document.getElementById('fullBtn') : document.getElementById('testBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Starting...';

  try {
    const res = await fetch('api/run-scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({full: full})
    });
    const result = await res.json();
    if (res.ok && result.success) {
      showToast(full ? 'Full scan started in the background.' : 'Test scan started in the background.');
      previousScanStatus = 'running';
      await refreshScanStatus();
    } else {
      showToast('Could not start scan: ' + (result.error || 'unknown error'));
      await refreshScanStatus();
    }
  } catch (e) {
    showToast('Error: ' + e.message);
    setScanButtons(false);
  }
}

function setScanButtons(running, full = false) {
  const testBtn = document.getElementById('testBtn');
  const fullBtn = document.getElementById('fullBtn');
  testBtn.disabled = running;
  fullBtn.disabled = running;
  testBtn.textContent = running && !full ? 'Test Scanning...' : 'Test Scan (100)';
  fullBtn.textContent = running && full ? 'Full Scanning...' : 'Full Scan';
}

async function refreshScanStatus() {
  try {
    const res = await fetch('api/scan-status');
    const state = await res.json();
    const statusEl = document.getElementById('scanStatus');

    if (state.status === 'running') {
      setScanButtons(true, state.full === true);
      const kind = state.full ? 'Full scan' : 'Test scan';
      statusEl.innerHTML = '<span class="spinner"></span>' + kind + ' running';
    } else {
      setScanButtons(false);
      statusEl.textContent = state.status === 'failed' ? 'Last scan failed' : 'Idle';

      if (previousScanStatus === 'running') {
        if (state.status === 'completed') {
          showToast('Scan complete! Loading results...');
          await loadScanList();
        } else if (state.status === 'failed') {
          showToast('Scan failed. Check the scan log for details.');
        }
      }
    }

    previousScanStatus = state.status;
  } catch (e) {
    document.getElementById('scanStatus').textContent = 'Status unavailable';
  }
}

function showToast(msg) {
  let t = document.getElementById('toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; t.className = 'toast'; document.body.appendChild(t); }
  t.textContent = msg;
  t.style.opacity = '1';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.style.opacity = '0'; }, 5000);
}

loadScanList();
refreshScanStatus();
setInterval(refreshScanStatus, 5000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/scans")
def api_scans():
    return jsonify(get_scan_files())


@app.route("/api/scan")
def api_scan():
    requested_path = request.args.get("path", "")
    if requested_path:
        path = (SCAN_DIR / requested_path).resolve()
        scan_root = SCAN_DIR.resolve()
        if path.parent != scan_root or not path.name.startswith("optimized_scan_") or path.suffix != ".txt":
            return jsonify({"error": "Invalid scan path"}), 400
    else:
        path = (SCAN_DIR / "latest_optimized_scan.txt").resolve()

    if not path.exists():
        return jsonify({"error": "File not found"})

    return jsonify(parse_scan_file(path))


@app.route("/api/run-scan", methods=["POST"])
def api_run_scan():
    global _scan_process
    body = request.json or {}
    full = body.get("full") is True

    with _scan_guard:
        if _process_is_running(_scan_process):
            return jsonify({"success": False, "error": "A scan is already running"}), 409

        lock_check = subprocess.run(
            ["/usr/bin/flock", "-n", str(SCAN_LOCK_FILE), "true"],
            capture_output=True,
        )
        if lock_check.returncode != 0:
            return jsonify({"success": False, "error": "A scheduled or manual scan is already running"}), 409

        python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
        cmd = [
            "/usr/bin/flock",
            "-n",
            str(SCAN_LOCK_FILE),
            python,
            "run_optimized_scan.py",
            "--conservative",
            "--git-storage",
            "--clear-progress",
        ]
        if not full:
            cmd.append("--test-mode")

        SCAN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = SCAN_LOG_DIR / f"web_scan_{timestamp}.log"
        log_file = log_path.open("w", encoding="utf-8")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=str(PROJECT_ROOT),
                start_new_session=True,
            )
        except Exception:
            log_file.close()
            raise

        state = {
            "status": "running",
            "full": full,
            "pid": process.pid,
            "started_at": datetime.now().astimezone().isoformat(),
            "log": log_path.name,
        }
        _scan_process = process
        _write_scan_state(state)

        monitor = threading.Thread(
            target=_monitor_scan,
            args=(process, state, log_file),
            daemon=True,
        )
        monitor.start()

    return jsonify({"success": True, **state}), 202


@app.route("/api/scan-status")
def api_scan_status():
    with _scan_guard:
        state = _read_scan_state()
        if _process_is_running(_scan_process):
            state["status"] = "running"
        return jsonify(state)


def open_browser(port):
    webbrowser.open(f"http://localhost:{port}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stock Screener Dashboard")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--scan", action="store_true", help="Run a test scan before launching")
    parser.add_argument("--full", action="store_true", help="With --scan, run full scan instead of test")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    if args.scan:
        print("Running scan before dashboard launch...")
        python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
        cmd = [python, "run_optimized_scan.py"]
        if not args.full:
            cmd.append("--test-mode")
        subprocess.run(cmd, cwd=str(Path(__file__).parent))

    if not args.no_browser:
        Timer(1.5, open_browser, [args.port]).start()

    print(f"\n  Dashboard running at http://localhost:{args.port}\n")
    app.run(host="0.0.0.0", port=args.port, debug=False)
