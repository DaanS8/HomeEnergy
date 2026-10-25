// Offline checks of exported workflow logic; n8n globals are supplied as fixtures.
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';

const dateTime = {
  fromISO(date) {
    const parsed = new Date(`${date}T12:00:00Z`);
    return {
      isValid: !Number.isNaN(+parsed) && parsed.toISOString().slice(0, 10) === date,
      toFormat: () => date.split('-').reverse().join('-'),
      toISODate: () => date,
      startOf(unit) { assert.equal(unit, 'day'); return this; },
      plus({ days }) {
        const shifted = new Date(parsed);
        shifted.setUTCDate(shifted.getUTCDate() + days);
        return dateTime.fromISO(shifted.toISOString().slice(0, 10));
      },
      minus({ days }) { return this.plus({ days: -days }); },
    };
  },
};
const nowAt = instant => ({
  setZone(zone) {
    assert.equal(zone, 'Europe/Brussels');
    const localDate = new Intl.DateTimeFormat('en-CA', { timeZone: zone }).format(new Date(instant));
    return dateTime.fromISO(localDate);
  },
});
const now = nowAt('2026-10-12T12:00:00Z');
const allIds = new Set();
for (const [file, offset, endpoint, status] of [
  ['n8n-day-ahead-admin.json', 1, '/api/v1/reports/day-ahead', 'warning'],
  ['n8n-ev-usage.json', -2, '/api/v1/reports/ev/daily', 'complete'],
]) {
  const path = new URL(`../deploy/n8n/${file}`, import.meta.url);
  if (!existsSync(path) && file === 'n8n-day-ahead-admin.json') {
    console.log(`SKIP ${file}: workflow removed from workspace`);
    continue;
  }
  const workflow = JSON.parse(readFileSync(path, 'utf8'));
  const ev = file === 'n8n-ev-usage.json';
  const nodes = Object.fromEntries(workflow.nodes.map(node => [node.name, node]));
  assert.equal(workflow.active, false);
  assert.equal(workflow.settings.timezone, 'Europe/Brussels');
  for (const node of workflow.nodes) {
    assert(!allIds.has(node.id), `Duplicate node ID: ${node.id}`);
    allIds.add(node.id);
  }
  for (const [source, outputs] of Object.entries(workflow.connections)) {
    assert(source in nodes);
    for (const edge of outputs.main.flat()) assert(edge.node in nodes);
  }
  const settings = Object.fromEntries(nodes.Settings.parameters.assignments.assignments.map(a => [a.name, a.value]));
  assert.equal(settings.test_date, '');
  if (ev) {
    assert(!('admin_channel_id' in settings));
    assert(!nodes.Settings.notes.includes('1532856135546044607'));
    assert.equal(settings.start_date, '2026-09-01');
  } else {
    assert.equal(settings.date_offset_days, offset);
  }
  for (const node of workflow.nodes.filter(n => n.type === 'n8n-nodes-base.discord')) {
    assert.equal(node.parameters.channelId.value, ev ? '1532856135546044607' : '={{ $json.admin_channel_id }}');
    assert.equal(node.parameters.operation, 'send');
  }
  const run = (name, json, context = json, binary = {}, clock = now) => {
    const lookup = () => ({ item: { json: context } });
    return new Function('$json', '$now', 'DateTime', '$', '$binary', '$input', nodes[name].parameters.jsCode)(
      json, clock, dateTime, lookup, binary, { all: () => [{ json }] },
    );
  };
  const selectDate = json => ev ? run('Report dates', json)[0].json : json;
  const expression = (source, json) => new Function('$json', `return (${source.slice(3, -2)});`)(json);
  // Follow the exported IF conditions and connections. A correct evaluation
  // is useful only if the workflow actually routes it to delivery or retry.
  const route = json => {
    let name = 'Report ready';
    const visited = [];
    while (name) {
      assert(visited.length < 20, 'Unexpected workflow loop');
      visited.push(name);
      if (name === 'Request context' || name === 'Loop over days') break;
      const node = nodes[name];
      let output = 0;
      if (node.type === 'n8n-nodes-base.if') {
        const condition = node.parameters.conditions.conditions[0];
        assert.equal(condition.operator.type, 'boolean');
        const actual = expression(condition.leftValue, json);
        const operation = condition.operator.operation;
        assert(['true', 'false'].includes(operation));
        output = actual === (operation === 'true') ? 0 : 1;
      }
      const edges = workflow.connections[name]?.main[output] ?? [];
      assert(edges.length <= 1, 'Unexpected extra delivery branch');
      name = edges[0]?.node;
    }
    return visited;
  };
  const scheduled = run('Request context', selectDate(settings)).json;
  assert.equal(scheduled.date, offset === 1 ? '2026-10-13' : '2026-10-10');
  assert.equal(scheduled.attempt, 1);
  const override = run('Request context', selectDate({
    ...settings, test_date: '2026-09-02', base_url: 'http://home-energy.test/',
    guild_id: 'test-guild', admin_channel_id: 'test-admin',
  })).json;
  assert.equal(override.date, '2026-09-02');
  assert.equal(nodes['Prepare report'].parameters.method, 'POST');
  assert.equal(expression(nodes['Prepare report'].parameters.url, override), `http://home-energy.test${endpoint}`);
  assert.equal(expression(nodes['Prepare report'].parameters.jsonBody, override).date, override.date);
  // Even if the default offset changes on a retry, the originally selected date stays fixed.
  const retry = run('Request context', { ...override, date_offset_days: -3 }).json;
  assert.equal(retry.date, override.date);
  assert.equal(retry.attempt, 2);
  assert.throws(() => run('Request context', selectDate({ ...settings, test_date: '2026-02-30' })));
  const png = {
    date: override.date, status, image: 'iVBORw0KGgoAAA', image_filename: 'report.png',
    image_mime_type: 'image/png', total_kwh: '42.750', ev_total_kwh: '0', ev_blocks: [], warning: 'van 18:00 tot 19:00',
  };
  const complete = run('Evaluate response', { statusCode: 200, body: png }, override).json;
  assert(complete.complete);
  assert.deepEqual(route(complete), ['Report ready', 'Decode PNG', 'Report and attachment', 'Send image to admin',
    ...(ev ? ['Loop over days'] : [])]);
  assert(!run('Evaluate response', { statusCode: 200, body: { ...png, date: '2026-09-03' } }, override).json.complete);
  assert(!run('Evaluate response', { statusCode: 200, body: { ...png, image: 'corrupt' } }, override).json.complete);
  const failure = { statusCode: 503, body: { status: 'error', retryable: true, code: 'database_unavailable' } };
  assert(run('Evaluate response', failure, { ...override, attempt: 5 }).json.can_retry);
  assert(!run('Evaluate response', failure, { ...override, attempt: 6 }).json.can_retry);
  assert.deepEqual(route(run('Evaluate response', failure, { ...override, attempt: 5 }).json),
    ['Report ready', 'Retry allowed', 'Wait 10 minutes', 'Request context']);
  assert.deepEqual(route(run('Evaluate response', failure, { ...override, attempt: 6 }).json),
    ['Report ready', 'Retry allowed', 'Send admin alert', 'Report failed']);
  assert(!run('Evaluate response', { statusCode: 422, body: { retryable: false } }, override).json.can_retry);
  assert(run('Evaluate response', {}, override).json.can_retry);
  const binary = { image_file: { id: 'binary-file' } };
  const attachment = run('Report and attachment', {}, { body: png, ...override }, binary);
  assert.equal(attachment.binary, binary);
  assert.equal(attachment.json.admin_channel_id, ev ? undefined : 'test-admin');
  assert.equal(attachment.json.guild_id, 'test-guild');
  assert.equal(ev ? nodes['Send image to admin'].parameters.channelId.value
    : expression(nodes['Send image to admin'].parameters.channelId.value, attachment.json),
  ev ? '1532856135546044607' : 'test-admin');
  assert(!('image' in attachment.json));
  assert.deepEqual(nodes['Send image to admin'].parameters.files.values, [{ inputFieldName: 'image_file' }]);
  if (ev) {
    const next = (source, output = 0) => workflow.connections[source].main[output].map(edge => edge.node);
    assert.deepEqual(next('Run manually'), ['Backfill mode']);
    assert.deepEqual(next('Backfill mode'), ['Settings']);
    assert.equal(nodes.Settings.parameters.includeOtherFields, true);
    assert.deepEqual(next('Daily schedule'), ['Settings']);
    assert.deepEqual(next('Settings'), ['Report dates']);
    assert.deepEqual(next('Report dates'), ['Loop over days']);
    assert.equal(nodes['Loop over days'].type, 'n8n-nodes-base.splitInBatches');
    assert.equal(nodes['Loop over days'].typeVersion, 3);
    assert.equal(nodes['Loop over days'].parameters.batchSize, 1);
    assert.equal(nodes['Loop over days'].parameters.options.reset, false);
    assert.deepEqual(next('Loop over days', 0), []);
    assert.deepEqual(next('Loop over days', 1), ['Request context']);
    const manual = { ...run('Backfill mode', {}).json, ...settings };
    const dates = run('Report dates', manual);
    assert.equal(dates.length, 40);
    assert.equal(dates[0].json.date, '2026-09-01');
    assert.equal(dates.at(-1).json.date, '2026-10-10');
    assert.equal(new Set(dates.map(item => item.json.date)).size, 40);
    // Every date gets its own first attempt; a failed report retries that date
    // before returning to the outer loop after successful delivery.
    for (const item of dates) {
      const first = run('Request context', item.json).json;
      assert.equal(first.attempt, 1);
      assert.deepEqual(item.pairedItem, { item: 0 });
      const failed = run('Evaluate response', failure, first).json;
      assert.equal(route(failed).at(-1), 'Request context');
      const retry = run('Request context', failed).json;
      assert.equal(retry.date, first.date);
      assert.equal(retry.attempt, 2);
      const sent = run('Evaluate response', { statusCode: 200, body: { ...png, date: retry.date } }, retry).json;
      assert.equal(route(sent).at(-1), 'Loop over days');
    }
    assert.equal(run('Report dates', { ...manual, test_date: '2026-09-02' }).length, 1);
    assert.throws(() => run('Report dates', { ...manual, test_date: '2026-10-11' }));
    assert.throws(() => run('Report dates', { ...manual, start_date: '2026-09-31' }));
    assert.deepEqual(run('Report dates', { ...manual, start_date: '2026-10-11' }), []);
    // Brussels has already crossed midnight although UTC is still October 12.
    const midnight = nowAt('2026-10-12T22:30:00Z');
    assert.equal(run('Report dates', settings, settings, {}, midnight)[0].json.date, '2026-10-11');
    const dstDates = run('Report dates', { ...manual, start_date: '2026-10-24' }, manual, {},
      nowAt('2026-10-29T12:00:00Z')).map(item => item.json.date);
    assert.deepEqual(dstDates, ['2026-10-24', '2026-10-25', '2026-10-26', '2026-10-27']);
    console.log('PASS EV backfill: 40 ordered days, daily-only schedule, per-day retries, inclusive bounds, Brussels midnight and DST');
  }
  // Check syntax of every exported expression, including message templates and request bodies.
  function check(value) {
    if (typeof value === 'string' && value.startsWith('=')) {
      for (const [, expression] of value.matchAll(/{{([\s\S]*?)}}/g)) {
        new Function('$json', 'DateTime', '$', `return (${expression});`);
      }
    } else if (value && typeof value === 'object') {
      Object.values(value).forEach(check);
    }
  }
  workflow.nodes.forEach(check);
  console.log(`PASS ${file}: connections, admin destination, dates, bounded retries, PNG validation and attachments`);
}
