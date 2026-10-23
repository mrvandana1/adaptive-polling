// k6 load scenarios. MUST mirror analysis/profiles.py (ground-truth profiles).
// SCENARIO = steady | step | ramp | accel   (accel = quadratic ramp, d2Q/dt2 > 0)
import http from 'k6/http';

const SCENARIO = __ENV.SCENARIO || 'step';
const TARGET = __ENV.TARGET || 'http://demo-app.autoscale-lab.svc:8000';
const CONTROLLER = __ENV.CONTROLLER || 'http://dfc-controller.autoscale-lab.svc:8080';
const PATH = __ENV.PATH_ || '/work';
const BASE = +(__ENV.BASE_RPS || 10);
const PEAK = +(__ENV.PEAK_RPS || 150);
const WARM = +(__ENV.WARMUP_S || 60);
const RAMP = +(__ENV.RAMP_S || 60);
const HOLD = +(__ENV.HOLD_S || 120);
const COOL = +(__ENV.COOL_S || 45);
const ACCEL_STEP = 5;

function points() {
  if (SCENARIO === 'steady') return { pts: [[0, BASE], [WARM + HOLD + COOL, BASE]], marks: {} };
  const pts = [[0, BASE], [WARM, BASE]];
  let end;
  if (SCENARIO === 'step') {
    pts.push([WARM + 1, PEAK], [WARM + HOLD, PEAK]);
    end = WARM + HOLD;
  } else if (SCENARIO === 'ramp') {
    pts.push([WARM + RAMP, PEAK], [WARM + RAMP + HOLD, PEAK]);
    end = WARM + RAMP + HOLD;
  } else if (SCENARIO === 'accel') {
    const n = Math.floor(RAMP / ACCEL_STEP);
    for (let i = 1; i <= n; i++) pts.push([WARM + ACCEL_STEP * i, BASE + (PEAK - BASE) * Math.pow(i / n, 2)]);
    pts.push([WARM + RAMP + HOLD, PEAK]);
    end = WARM + RAMP + HOLD;
  } else {
    throw new Error('unknown SCENARIO ' + SCENARIO);
  }
  pts.push([end + 1, BASE], [end + COOL, BASE]);
  return { pts, marks: { burst_start: WARM, burst_end: end } };
}

const { pts, marks } = points();
const stages = [];
for (let i = 1; i < pts.length; i++) {
  const d = pts[i][0] - pts[i - 1][0];
  if (d > 0) stages.push({ target: Math.round(pts[i][1]), duration: d + 's' });
}

const scenarios = {
  load: {
    executor: 'ramping-arrival-rate',
    startRate: BASE,
    timeUnit: '1s',
    preAllocatedVUs: 50,
    maxVUs: 600,
    stages,
    exec: 'hit',
  },
};
for (const [name, t] of Object.entries(marks)) {
  scenarios['mark_' + name] = {
    executor: 'per-vu-iterations', vus: 1, iterations: 1, startTime: t + 's', exec: 'mark',
    env: { MARK: name },
  };
}
export const options = { scenarios, discardResponseBodies: true };

export function hit() {
  http.get(TARGET + PATH, { tags: { name: 'work' } });
}

// Ground-truth marker: tells the controller exactly when the burst starts/ends
// (aligned with the load timeline) so detection latency can be measured.
export function mark() {
  http.post(CONTROLLER + '/mark', JSON.stringify({ name: __ENV.MARK }),
    { headers: { 'Content-Type': 'application/json' } });
}

export function handleSummary(data) {
  return { stdout: 'K6_SUMMARY ' + JSON.stringify({ scenario: SCENARIO, metrics: data.metrics }) + '\n' };
}
