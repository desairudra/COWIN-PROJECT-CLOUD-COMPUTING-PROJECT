import http from 'k6/http';
import exec from 'k6/execution';
import { check, fail } from 'k6';
import { Counter } from 'k6/metrics';
import { steps, trendStats } from './steps.js';

const BASE = (__ENV.BASE_URL || 'http://localhost:8000').replace(/\/+$/, '');
const JSON_HDR = { 'Content-Type': 'application/json' };

const signupsOk = new Counter('signups_ok');
const signupsFailed = new Counter('signups_failed');

const RATES = __ENV.RATES || '2,5,10,20,40';

export const options = {
  scenarios: steps('signup', RATES, '60'),
  summaryTrendStats: trendStats,
};

export function setup() {
  const res = http.post(`${BASE}/api/v1/otp/request`, JSON.stringify({ mobile: '9000000000' }), { headers: JSON_HDR });
  if (res.status !== 200 && res.status !== 429) fail(`service not reachable at ${BASE} (status ${res.status})`);
  if (res.status === 200 && !res.json('demo_otp')) fail('service must run with OTP_DELIVERY=demo');
  return { run: String(Math.floor(Math.random() * 10000)).padStart(4, '0') };
}

const hex = (n) => Array.from({ length: n }, () => Math.floor(Math.random() * 16).toString(16)).join('');

function call(method, path, body, headers, name) {
  return http.request(method, `${BASE}${path}`, body ? JSON.stringify(body) : null,
    { headers: Object.assign({}, JSON_HDR, headers), tags: { name }, timeout: '10s' });
}

export function signup(data) {
  const s = exec.scenario;
  const step = RATES.split(',').indexOf(s.name.slice(5)) % 10;
  const mobile = `9${data.run}${step}${String(s.iterationInTest % 10000).padStart(4, '0')}`;
  const ip = { 'X-Forwarded-For': `10.${step}.${Math.floor(s.iterationInTest / 250) % 250}.${(s.iterationInTest % 250) + 1}` };

  const otp = call('POST', '/api/v1/otp/request', { mobile }, ip, 'otp_request');
  if (!check(otp, { 'otp request 200': (r) => r.status === 200 })) return signupsFailed.add(1);

  const ver = call('POST', '/api/v1/otp/verify', { txn_id: otp.json('txn_id'), otp: otp.json('demo_otp') }, ip, 'otp_verify');
  if (!check(ver, { 'verify 200': (r) => r.status === 200 })) return signupsFailed.add(1);
  const auth = Object.assign({ Authorization: `Bearer ${ver.json('token')}` }, ip);

  const add = call('POST', '/api/v1/beneficiaries', {
    full_name: `Load Test ${mobile}`, birth_year: 1950 + (s.iterationInTest % 50), gender: 'FEMALE',
    photo_id_type: 'PAN', photo_id_number: `LT${hex(8).toUpperCase()}`,
  }, Object.assign({ 'Idempotency-Key': `lt-${hex(16)}` }, auth), 'add_beneficiary');
  if (!check(add, { 'add 201': (r) => r.status === 201 })) return signupsFailed.add(1);

  const list = call('GET', '/api/v1/beneficiaries', null, auth, 'list_beneficiaries');
  if (!check(list, { 'list has it': (r) => r.status === 200 && r.json('count') >= 1 })) return signupsFailed.add(1);

  signupsOk.add(1);
}
