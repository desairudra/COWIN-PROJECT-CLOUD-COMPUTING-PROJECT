import http from 'k6/http';
import { check } from 'k6';

const BASE = (__ENV.BASE_URL || 'http://localhost:8080').replace(/\/+$/, '');
const PEAK = parseInt(__ENV.PEAK || '300', 10);
const LOW = parseInt(__ENV.LOW || '20', 10);

export const options = {
  scenarios: {
    hpa: {
      executor: 'ramping-arrival-rate',
      startRate: LOW,
      timeUnit: '1s',
      preAllocatedVUs: 50,
      maxVUs: PEAK * 4,
      stages: [
        { target: LOW, duration: '30s' },
        { target: PEAK, duration: '60s' },
        { target: PEAK, duration: __ENV.HOLD || '180s' },
        { target: LOW, duration: '20s' },
        { target: LOW, duration: __ENV.COOL || '180s' },
      ],
    },
  },
  summaryTrendStats: ['avg', 'med', 'p(95)', 'p(99)', 'max'],
};

const CITIES = ['Mumbai', 'Hyderabad'];

export default function () {
  const r = Math.random();
  const url = r < 0.7
    ? `${BASE}/centers?city=${CITIES[Math.floor(Math.random() * 2)]}`
    : `${BASE}/centers/${Math.random() < 0.5 ? 'MUM' : 'HYD'}-00${1 + Math.floor(Math.random() * 9)}`;
  const res = http.get(url, { timeout: '10s', tags: { name: r < 0.7 ? 'by_city' : 'by_id' } });
  check(res, { 'status 200': (x) => x.status === 200 });
}
