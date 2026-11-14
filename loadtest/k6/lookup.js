import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';
import { steps, trendStats } from './steps.js';

const BASE = (__ENV.BASE_URL || 'http://localhost:8000').replace(/\/+$/, '');
const CITIES = ['Mumbai', 'Hyderabad'];
const PINCODES = ['400005', '400050', '400076', '500016', '500034', '500081'];
const IDS = [];
for (let i = 1; i <= 10; i++) {
  const n = String(i).padStart(3, '0');
  IDS.push(`MUM-${n}`, `HYD-${n}`);
}

const zoneA = new Counter('zone_a');
const zoneB = new Counter('zone_b');

export const options = {
  scenarios: steps('lookup', '25,50,100,200,400', '60'),
  summaryTrendStats: trendStats,
  noConnectionReuse: false,
};

const pick = (list) => list[Math.floor(Math.random() * list.length)];

export function lookup() {
  const r = Math.random();
  let res;
  if (r < 0.5) {
    res = http.get(`${BASE}/centers?city=${pick(CITIES)}`, { tags: { name: 'by_city' }, timeout: '10s' });
  } else if (r < 0.8) {
    res = http.get(`${BASE}/centers/${pick(IDS)}`, { tags: { name: 'by_id' }, timeout: '10s' });
  } else {
    res = http.get(`${BASE}/centers?pincode=${pick(PINCODES)}`, { tags: { name: 'by_pincode' }, timeout: '10s' });
  }
  check(res, {
    'status 200': (x) => x.status === 200,
    'has served_by': (x) => x.status === 200 && x.body.includes('served_by'),
  });
  const az = res.headers['X-Availability-Zone'] || '';
  if (az.endsWith('a')) zoneA.add(1);
  else if (az.endsWith('b')) zoneB.add(1);
}
