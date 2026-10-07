/**
 * Node.js integration & regression runner for the FP&A engine.
 * Drives the real command-line tool end to end in a throw-away copy of config/ and data/,
 * then runs the Python unit tests. Works on Windows, macOS and Linux (finds python / python3 / py).
 *
 * Run:  node test.js        (optionally set PYTHON=path/to/python)
 */
const { spawnSync } = require('child_process');
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const ROOT = __dirname;
const SEEDED = ['Orchid Bay', 'Sunrise Logistics', 'CloudNimbus', 'Meridian Health', 'Jane Tan', 'jane.tan@', 'ap@cloudnimbus', '088-123456-7', '+65 8123'];

function findPython() {
  const candidates = [process.env.PYTHON, 'python3', 'python', 'py'].filter(Boolean);
  for (const cmd of candidates) {
    const probe = spawnSync(cmd, ['-c', 'import pandas, openpyxl, pptx, requests'], { encoding: 'utf-8' });
    if (probe.status === 0) return cmd;
  }
  throw new Error('No Python with pandas, openpyxl, python-pptx and requests found. Run: pip install -r requirements.txt');
}

const PY = findPython();
if (!fs.existsSync(path.join(ROOT, 'data', 'input', 'samples', 'day3_trial_balance.xlsx'))) {
  console.log('Generating mock data first...');
  spawnSync(PY, [path.join(ROOT, 'data', 'generate_mock_data.py')], { cwd: ROOT, stdio: 'inherit' });
}
const env = { ...process.env, FPA_QUIET: '1', PYTHONIOENCODING: 'utf-8' };
const WS = fs.mkdtempSync(path.join(os.tmpdir(), 'fpa-test-'));
['config', 'data'].forEach((d) => fs.cpSync(path.join(ROOT, d), path.join(WS, d), { recursive: true }));
fs.mkdirSync(path.join(WS, 'outputs'));
const DAY3 = path.join(WS, 'data', 'input', 'samples', 'day3_trial_balance.xlsx');
const DAY4 = path.join(WS, 'data', 'input', 'samples', 'day4_trial_balance.xlsx');

/** Run main.py in the temp workspace; returns {status, json, stderr}. stdout must be clean JSON. */
function cli(args) {
  const run = spawnSync(PY, [path.join(ROOT, 'main.py'), '--base-dir', WS, ...args], { encoding: 'utf-8', env, cwd: ROOT });
  if (run.error) throw new Error(`Failed to start Python: ${run.error.message}`);
  let json;
  try { json = JSON.parse(run.stdout); } catch (e) { throw new Error(`stdout is not clean JSON (${e.message}). stderr: ${run.stderr.slice(0, 400)}`); }
  return { status: run.status, json, stderr: run.stderr };
}

const results = [];
function test(name, fn) {
  try { fn(); results.push([name, true]); console.log(`  \u2714 ${name}`); }
  catch (err) { results.push([name, false]); console.error(`  \u2716 ${name}\n      ${err.message.split('\n')[0]}`); }
}

console.log('====================================================');
console.log(' RUNNING FP&A AUTOMATION SUITE (Node.js Test Runner)');
console.log(`  python: ${PY}   workspace: ${WS}`);
console.log('====================================================\n');

let day3, day4;
console.log('[Workflow end to end]');
test('Day 3 export runs as run 1 with clean JSON on stdout', () => {
  day3 = cli(['--mock', '--tb-file', DAY3]);
  assert.strictEqual(day3.status, 0, day3.json.error);
  assert.strictEqual(day3.json.run_no, 1);
  assert.strictEqual(day3.json.stages.length, 10);
  assert.strictEqual(day3.json.stages[3].status, 'SKIPPED');
});
test('Day 4 export is detected automatically as run 2 and compared with run 1', () => {
  day4 = cli(['--mock', '--tb-file', DAY4]);
  assert.strictEqual(day4.status, 0, day4.json.error);
  assert.strictEqual(day4.json.run_no, 2);
  assert.strictEqual(day4.json.revision_summary.lines_changed, 6);
  assert.strictEqual(day4.json.revision_summary.operating_profit_change, -36500);
});
test('Working capital formulas: DSO, DPO and drag (DSO - DPO) are correct', () => {
  const r = day4.json.ratios;
  assert.ok(Math.abs(r.DSO_Days - 908000 / 608000 * 30) < 0.01);
  assert.ok(Math.abs(r.DPO_Days - 172000 / 199500 * 30) < 0.01);
  assert.ok(Math.abs(r.Working_Capital_Drag_Days - (r.DSO_Days - r.DPO_Days)) < 0.011);
});
test('Controls tie: bridge, balance sheet and cash reconciliation all zero', () => {
  const b = day4.json.bridge;
  assert.strictEqual(b.passes, true);
  assert.strictEqual(b.balance_sheet_difference, 0);
  assert.strictEqual(b.cash_reconciliation_difference, 0);
  assert.ok(day4.json.management_ratios['Adjusted_Gross_Margin_%'] < day4.json.ratios['Gross_Margin_%']);
});
test('KPI scorecard has 14 KPIs with engine-computed statuses', () => {
  assert.strictEqual(day4.json.kpis.length, 14);
  assert.ok(day4.json.kpis.every((k) => ['On target', 'Watch', 'Off target'].includes(k.Status)));
});
test('Adjusted (management-basis) gross margin and opex % are distinct KPIs, both lower than their statutory counterparts', () => {
  const byKpi = Object.fromEntries(day4.json.kpis.map((k) => [k.KPI, k]));
  assert.ok(byKpi['Adjusted_Gross_Margin_%'].Value < byKpi['Gross_Margin_%'].Value, 'adjusted gross margin should be lower than statutory');
  assert.ok(byKpi['Adjusted_Opex_%_of_Revenue'].Value < byKpi['Opex_%_of_Revenue'].Value, 'adjusted opex % should be lower than statutory');
});
test('Materiality thresholds are respected', () => {
  assert.ok(day4.json.material_variances_count > 0);
  const strict = cli(['--mock', '--no-deck', '--abs', '1000000', '--tb-file', DAY4]);
  assert.strictEqual(strict.json.material_variances_count, 0);
});

console.log('\n[Disclosure controls]');
test('Investor output is fully redacted; Management keeps identifiers', () => {
  const investor = JSON.stringify([day4.json.narratives.Investor, day4.json.slides.Investor]);
  SEEDED.forEach((s) => assert.ok(!investor.includes(s), `leaked: ${s}`));
  assert.ok(day4.json.narratives.Management.includes('Orchid Bay'));
  const counts = day4.json.protection_log.Investor.redaction_counts;
  assert.ok(Object.values(counts).reduce((a, b) => a + b, 0) > 0);
});
test('Investor pack: budget, QoQ, YoY and totals only; no forecast, MoM, cash or business units', () => {
  const pack = fs.readFileSync(path.join(WS, 'outputs', day4.json.version_id, 'packs', 'investor_pack.md'), 'utf-8');
  ['BvA_Month', 'QoQ', 'YoY', 'KPI scorecard'].forEach((s) => assert.ok(pack.includes(s), `missing ${s}`));
  ['RFvA_', 'MoM', 'Cash balance', 'Platform', 'Enterprise', 'SMB', 'adjusted gross margin'].forEach((s) => assert.ok(!pack.toLowerCase().includes(s.toLowerCase()), `should not contain ${s}`));
});
test('Revision log stays internal (not in any pack)', () => {
  ['management', 'board', 'investor'].forEach((a) => {
    const pack = fs.readFileSync(path.join(WS, 'outputs', day4.json.version_id, 'packs', `${a}_pack.md`), 'utf-8');
    assert.ok(!pack.includes('ADJ-01') && !pack.includes('Prepared_By'));
  });
  assert.ok(fs.existsSync(path.join(WS, 'outputs', day4.json.version_id, 'revision_log_INTERNAL.csv')));
});
test('Automated checks pass and three decks are built and checked', () => {
  assert.ok(Object.values(day4.json.checks).every((c) => c.passed));
  ['Management', 'Board', 'Investor'].forEach((a) => {
    assert.ok(fs.existsSync(day4.json.decks[a]), `deck missing for ${a}`);
    assert.strictEqual(day4.json.deck_checks[a].passed, true);
  });
});

console.log('\n[Robustness]');
test('Live AI failure is clearly labelled as fallback (never silent)', () => {
  const r = cli(['--no-deck', '--ollama-url', 'http://127.0.0.1:9', '--tb-file', DAY4]);
  assert.strictEqual(r.status, 0);
  Object.values(r.json.narrative_modes).forEach((m) => assert.strictEqual(m, 'MOCK_FALLBACK'));
  assert.ok(r.json.narratives.Board.startsWith('[FALLBACK'));
});
test('Bad input returns exit code 2 with a JSON error (no traceback)', () => {
  const r = cli(['--mock', '--no-deck', '--period', '2026-05', '--tb-file', DAY3]);
  assert.strictEqual(r.status, 2);
  assert.ok(['FAILED', 'BLOCKED'].includes(r.json.status) && r.json.error.length > 0);
});
test('Google Sheets workbook inputs give the same results as CSV inputs', () => {
  const sheets = cli(['--mock', '--no-deck', '--sheets-workbook', path.join(WS, 'data', 'input', 'sheets_inputs.xlsx'), '--tb-file', DAY4]);
  assert.strictEqual(sheets.status, 0, sheets.json.error);
  assert.deepStrictEqual(sheets.json.ratios, day4.json.ratios);
  assert.deepStrictEqual(sheets.json.narratives, day4.json.narratives);
});

console.log('\n[Sign-off and roll-forward]');
test('One-click approvals release all audiences and lock June automatically', () => {
  const r = cli(['--mock', '--no-deck', '--demo-approve', '--tb-file', DAY4]);
  assert.strictEqual(r.status, 0, r.json.error);
  assert.strictEqual(r.json.signoff_demo.lock.status, 'LOCKED');
  assert.strictEqual(r.json.signoff_demo.lock.rows_appended, 29);
});
test('A locked month cannot be re-run', () => {
  const r = cli(['--mock', '--no-deck', '--tb-file', DAY4]);
  assert.strictEqual(r.status, 2);
  assert.ok(r.json.error.includes('locked'));
});

console.log('\n[Python unit tests]');
test('Python regression suite passes', () => {
  const r = spawnSync(PY, ['-m', 'unittest', 'discover', '-s', 'tests'], { encoding: 'utf-8', env, cwd: ROOT });
  assert.strictEqual(r.status, 0, (r.stderr || '').split('\n').slice(-6).join(' | '));
});

fs.rmSync(WS, { recursive: true, force: true });
const passed = results.filter((r) => r[1]).length;
console.log('\n====================================================');
if (passed === results.length) {
  console.log(` ALL INTEGRATION & REGRESSION TESTS PASSED (${passed}/${results.length})`);
  console.log('====================================================');
} else {
  console.error(` TEST SUITE FAILED: ${passed}/${results.length} passed`);
  console.error('====================================================');
  process.exit(1);
}
