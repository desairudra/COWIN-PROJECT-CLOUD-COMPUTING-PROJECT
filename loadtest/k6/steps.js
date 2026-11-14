// One constant-arrival-rate scenario per load step, back to back.
// Each scenario is named rate_<N>, so every sample in the output says which step it belongs to.
export function steps(exec, defaultRates, defaultStep) {
  const rates = (__ENV.RATES || defaultRates).split(',').map((r) => parseInt(r, 10));
  const step = parseInt(__ENV.STEP || defaultStep, 10);
  const scenarios = {};
  rates.forEach((rate, i) => {
    scenarios[`rate_${rate}`] = {
      executor: 'constant-arrival-rate',
      exec,
      rate,
      timeUnit: '1s',
      duration: `${step}s`,
      startTime: `${i * step}s`,
      preAllocatedVUs: Math.max(5, Math.ceil(rate / 2)),
      maxVUs: Math.max(20, rate * 3),
      gracefulStop: '5s',
    };
  });
  return scenarios;
}

export const trendStats = ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'];
