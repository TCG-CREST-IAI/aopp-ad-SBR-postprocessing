const $ = id => document.getElementById(id);
let report = null;
let timer = null;
let busy = false;
let planValid = false;
let previewVersion = 0;
let previewTimer = null;
const svgNS = 'http://www.w3.org/2000/svg';

function announce(text, error = false) {
  $('announcement').textContent = text;
  $('announcement').classList.toggle('error', error);
}

function chart(rounds, selected = null) {
  const svg = $('confidence');
  svg.replaceChildren();
  const history = rounds.flatMap(r => r.confidence.filter(Number.isFinite));
  const max = Math.max(1, ...history);
  const total = Math.max(1, history.length - 1);
  for (let i = 0; i < 4; i++) {
    const y = 15 + i * 48;
    const line = document.createElementNS(svgNS, 'line');
    Object.entries({x1: 30, x2: 550, y1: y, y2: y, stroke: '#edf1ee'}).forEach(([k,v]) => line.setAttribute(k,v));
    svg.append(line);
    const label = document.createElementNS(svgNS, 'text');
    label.setAttribute('x', 1); label.setAttribute('y', y+3); label.setAttribute('fill', '#8a9c98'); label.setAttribute('font-size', '9');
    label.textContent = (max * (1-i/3)).toFixed(0); svg.append(label);
  }
  let offset = 0;
  rounds.forEach((r, index) => {
    const values = r.confidence.filter(Number.isFinite);
    const path = document.createElementNS(svgNS, 'polyline');
    path.setAttribute('points', values.map((v,i) => `${30+(offset+i)/total*515},${159-v/max*144}`).join(' '));
    path.setAttribute('fill', 'none'); path.setAttribute('stroke', index === selected ? '#d79468' : '#258b79'); path.setAttribute('stroke-width', '2');
    svg.append(path); offset += values.length;
  });
}

function selectStep(value) {
  if (!report) return;
  const step = Number(value);
  $('step').value = step;
  $('step-label').textContent = `${step+1} / ${report.transcript.length}`;
  [...$('events').children].forEach((node, i) => node.classList.toggle('active', i === step));
  const event = report.transcript[step];
  chart(report.alice.rounds, event.kind === 'disclosure' ? event.round : 0);
  if (event.kind === 'syndromes') $('channel-title').textContent = `Exchange ${event.alice_bits}-bit syndromes`;
  if (event.kind === 'disclosure') $('channel-title').textContent = `Disclose ${event.positions.length} bit values each`;
  if (event.kind === 'verification') $('channel-title').textContent = `64-bit verification ${event.matched ? 'matched' : 'failed'}`;
}

function bits(target, values, reference) {
  $(target).replaceChildren();
  values.forEach((value, i) => {
    const bit = document.createElement('span');
    bit.className = `bit ${value ? 'one' : ''} ${reference && value !== reference[i] ? 'mismatch' : ''}`;
    bit.title = `Raw position ${i}: ${value}`;
    $(target).append(bit);
  });
}

function render(data) {
  report = data;
  const a = data.alice;
  $('transport').textContent = data.mode === 'mtls' ? 'LIVE mTLS' : 'SIMULATION';
  $('channel-detail').textContent = data.mode === 'mtls' ? 'Certificate-authenticated HTTPS between servers' : 'In-process transport · no network TLS';
  $('alice-state').textContent = a.status;
  $('bob-state').textContent = data.bob.status;
  $('verified').textContent = data.verified ? 'Matched' : 'Failed';
  $('verified-note').textContent = data.exact_match === false ? 'Synthetic truth check failed' : '64-bit universal hash';
  $('rounds').textContent = a.additional_rounds;
  $('step-note').textContent = `${data.d} bits per disclosure step`;
  $('efficiency').textContent = a.efficiency.toFixed(3);
  $('elapsed').textContent = `${(data.elapsed_ms/1000).toFixed(2)}s`;
  $('code-name').textContent = data.code;
  $('syndrome-bits').textContent = `${a.m} bits`;
  $('padding-bits').textContent = `${a.initial_punctures} / ${a.initial_shortened}`;
  $('disclosed-bits').textContent = `${a.disclosed_per_party} bits`;
  $('leakage-bits').textContent = a.paper_leakage === null ? 'Not applicable' : `${a.paper_leakage} bits`;
  $('leakage-note').textContent = a.paper_bound_applicable ? 'Conditional disclosure estimate: m − p₀ + disclosed values. Verification adds a 64-bit public tag.' : `Random puncturing does not meet the checked untainted condition. Conservative public transcript: ${a.conservative_public_bits} bits, excluding control metadata and public hash seed.`;
  $('backend-note').textContent = a.rounds.some(r => r.backend !== 'ldpc') ? 'Stable sum-product arithmetic was needed for large shortened-bit LLRs. The BP equation is unchanged.' : 'Native sum-product engine · confidence-based stopping.';
  $('key-note').textContent = `${data.input_errors} input errors across ${a.raw_bits} raw bits · observed QBER ${(100*data.observed_qber).toFixed(2)}%. These are synthetic input keys.`;
  bits('alice-bits', data.demo_alice);
  bits('bob-bits', data.demo_bob, data.demo_alice);
  $('events').replaceChildren();
  data.transcript.forEach((event, i) => {
    const li = document.createElement('li');
    const title = document.createElement('strong');
    const detail = document.createElement('span');
    title.textContent = event.kind === 'syndromes' ? '01 / Exchange syndromes' : event.kind === 'verification' ? `${String(i+1).padStart(2,'0')} / Verify corrected raw key` : `${String(i+1).padStart(2,'0')} / Disclosure round ${event.round}`;
    detail.textContent = event.kind === 'disclosure' ? `Positions [${event.positions.join(', ')}] · ${event.alice_bits} bits from each party` : event.kind === 'syndromes' ? `${event.alice_bits} bits from Alice + ${event.bob_bits} bits from Bob` : `Independent Toeplitz hash · ${event.matched ? 'tags agree' : 'tags differ; block rejected'}`;
    li.append(title, detail); li.addEventListener('click', () => selectStep(i)); $('events').append(li);
  });
  $('step').max = data.transcript.length-1;
  $('step').disabled = false;
  $('play').disabled = false;
  $('download').disabled = false;
  selectStep(data.transcript.length-1);
  announce(data.verified ? `Reconciliation verified. ${a.raw_bits} raw bits processed using ${a.additional_rounds} additional communication rounds. This is not a final secret key.` : 'Verification failed. The block must be rejected.', !data.verified);
}

$('q').addEventListener('input', () => $('q-value').textContent = `${(100*Number($('q').value)).toFixed(1)}%`);
$('mode').addEventListener('change', () => {
  const live = $('mode').value === 'mtls';
  $('token-group').hidden = !live;
  $('mode-hint').textContent = live ? 'Runs separate Alice and Bob services over pinned, certificate-authenticated HTTPS.' : 'One-request demonstration with synthetic inputs.';
});
$('experiment').addEventListener('submit', async event => {
  event.preventDefault(); clearInterval(timer);
  if (!planValid || busy) return;
  busy = true;
  report = null;
  ['verified','rounds','efficiency','elapsed','syndrome-bits','padding-bits','disclosed-bits','leakage-bits'].forEach(id => $(id).textContent = '—');
  ['play','download','step'].forEach(id => $(id).disabled = true);
  ['events','confidence','alice-bits','bob-bits'].forEach(id => $(id).replaceChildren());
  $('alice-state').textContent = 'Decoding…'; $('bob-state').textContent = 'Decoding…';
  $('step-label').textContent = 'Running';
  $('run').disabled = true; $('run').textContent = 'Running experiment…';
  announce('Running independent belief propagation at Alice and Bob…');
  try {
    const payload = {n: Number($('n').value), q: Number($('q').value), q_est: Number($('q-est').value), alpha: Number($('alpha').value), seed: Number($('seed').value), mode: $('mode').value, token: $('token').value};
    const response = await fetch('/api', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'The experiment did not complete');
    render(data);
  } catch(error) { announce(error.message, true); $('alice-state').textContent = 'Aborted'; $('bob-state').textContent = 'Aborted'; $('step-label').textContent = 'No completed run'; }
  finally { busy = false; $('run').disabled = !planValid; $('run').textContent = 'Run reconciliation ↗'; }
});
$('step').addEventListener('input', event => selectStep(event.target.value));
$('play').addEventListener('click', () => {
  clearInterval(timer); let step = 0; selectStep(step);
  timer = setInterval(() => {step++; if (step >= report.transcript.length) clearInterval(timer); else selectStep(step);}, 900);
});
$('download').addEventListener('click', () => {
  const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type:'application/json'}));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'reconciliation-report.json'; anchor.click(); URL.revokeObjectURL(url);
});
fetch('/api').then(response => response.json()).then(data => { $('mode').querySelector('[value="mtls"]').disabled = !data.live_available; }).catch(() => announce('The API is unavailable. Start the Python dashboard server.', true));

async function previewParameters() {
  const version = ++previewVersion;
  planValid = false;
  $('run').disabled = true;
  try {
    const payload = {action:'parameters', n:Number($('n').value), q:Number($('q').value), q_est:Number($('q-est').value), alpha:Number($('alpha').value), seed:Number($('seed').value)};
    const response = await fetch('/api', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const plan = await response.json();
    if (version !== previewVersion) return;
    if (!response.ok) throw new Error(plan.error || 'Check the initial parameters');
    $('plan-code').textContent = `${plan.n}-bit block · ${plan.m} parity checks`;
    $('plan-description').textContent = `${plan.initial_punctures} punctured positions · ${plan.initial_shortened} shortened positions`;
    $('plan-raw').textContent = plan.raw_bits;
    $('plan-step').textContent = plan.d;
    $('plan-status').textContent = 'Configuration ready';
    $('plan-status').classList.remove('invalid');
    planValid = true;
  } catch(error) {
    if (version !== previewVersion) return;
    $('plan-code').textContent = 'Check initial parameters';
    $('plan-description').textContent = error.message;
    $('plan-raw').textContent = '—'; $('plan-step').textContent = '—';
    $('plan-status').textContent = error.message;
    $('plan-status').classList.add('invalid');
  }
  $('run').disabled = busy || !planValid;
}

['n','q','q-est','alpha','seed'].forEach(id => $(id).addEventListener('input', () => {
  previewVersion++;
  planValid = false; $('run').disabled = true;
  clearTimeout(previewTimer); previewTimer = setTimeout(previewParameters, 150);
}));
document.querySelectorAll('[data-q]').forEach(button => button.addEventListener('click', () => {
  $('q').value = button.dataset.q;
  $('q-est').value = button.dataset.q;
  $('q-value').textContent = `${(100*Number(button.dataset.q)).toFixed(1)}%`;
  clearTimeout(previewTimer); previewParameters();
}));
previewParameters();
