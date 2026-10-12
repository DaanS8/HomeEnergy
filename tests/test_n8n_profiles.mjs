// Offline checks of exported workflow logic; n8n globals are supplied as fixtures.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const dateTime = {
  fromISO(date) {
    const parsed = new Date(`${date}T12:00:00Z`);
    return {
      isValid: !Number.isNaN(+parsed) && parsed.toISOString().slice(0, 10) === date,
      toFormat: () => date.split('-').reverse().join('-'),
    };
  },
};
const now = {
  setZone(zone) {
    assert.equal(zone, 'Europe/Brussels');
    return {
      plus({ days }) {
        const date = new Date('2026-10-12T12:00:00Z');
        date.setUTCDate(date.getUTCDate() + days);
        return { toISODate: () => date.toISOString().slice(0, 10) };
      },
    };
  },
};
const allIds = new Set();
for (const [file, offset, endpoint, status] of [
  ['n8n-day-ahead-admin.json', 1, '/api/v1/reports/day-ahead', 'warning'],
  ['n8n-ev-usage.json', -2, '/api/v1/reports/ev/daily', 'complete'],
]) {
  const workflow = JSON.parse(readFileSync(new URL(`../deploy/n8n/${file}`, import.meta.url), 'utf8'));
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
  assert.equal(settings.date_offset_days, offset);
  for (const node of workflow.nodes.filter(n => n.type === 'n8n-nodes-base.discord')) {
    assert.equal(node.parameters.channelId.value, '={{ $json.admin_channel_id }}');
    assert.equal(node.parameters.operation, 'send');
  }
  const run = (name, json, context = json, binary = {}) => {
    const lookup = () => ({ item: { json: context } });
    return new Function('$json', '$now', 'DateTime', '$', '$binary', nodes[name].parameters.jsCode)(
      json, now, dateTime, lookup, binary,
    );
  };
  const expression = (source, json) => new Function('$json', `return (${source.slice(3, -2)});`)(json);
  // Follow the exported IF conditions and connections. A correct evaluation
  // is useful only if the workflow actually routes it to delivery or retry.
  const route = json => {
    let name = 'Report ready';
    const visited = [];
    while (name) {
      assert(visited.length < 20, 'Unexpected workflow loop');
      visited.push(name);
      if (name === 'Request context') break;
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
  const scheduled = run('Request context', settings).json;
  assert.equal(scheduled.date, offset === 1 ? '2026-10-13' : '2026-10-10');
  assert.equal(scheduled.attempt, 1);
  const override = run('Request context', {
    ...settings, test_date: '2026-09-02', base_url: 'http://home-energy.test/',
    guild_id: 'test-guild', admin_channel_id: 'test-admin',
  }).json;
  assert.equal(override.date, '2026-09-02');
  assert.equal(nodes['Prepare report'].parameters.method, 'POST');
  assert.equal(expression(nodes['Prepare report'].parameters.url, override), `http://home-energy.test${endpoint}`);
  assert.equal(expression(nodes['Prepare report'].parameters.jsonBody, override).date, override.date);
  // Even if the default offset changes on a retry, the originally selected date stays fixed.
  const retry = run('Request context', { ...override, date_offset_days: -3 }).json;
  assert.equal(retry.date, override.date);
  assert.equal(retry.attempt, 2);
  assert.throws(() => run('Request context', { ...settings, test_date: '2026-02-30' }));
  const png = {
    date: override.date, status, image: 'iVBORw0KGgoAAA', image_filename: 'report.png',
    image_mime_type: 'image/png', total_kwh: '42.750', ev_total_kwh: '0', ev_blocks: [], warning: 'van 18:00 tot 19:00',
  };
  const complete = run('Evaluate response', { statusCode: 200, body: png }, override).json;
  assert(complete.complete);
  assert.deepEqual(route(complete), ['Report ready', 'Decode PNG', 'Report and attachment', 'Send image to admin']);
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
  assert.equal(attachment.json.admin_channel_id, 'test-admin');
  assert.equal(attachment.json.guild_id, 'test-guild');
  assert.equal(expression(nodes['Send image to admin'].parameters.channelId.value, attachment.json), 'test-admin');
  assert(!('image' in attachment.json));
  assert.deepEqual(nodes['Send image to admin'].parameters.files.values, [{ inputFieldName: 'image_file' }]);
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
