// Tests of the relay (relay/worker.js). A fake fetch stands in for GitHub and records every call,
// so nothing goes over the network; each test loads a fresh copy of the Worker (its own limits,
// label and branch caches, pause and spent budget), as a new isolate would be.
//
//   node --test relay/test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';

const REPO = 'Purple-Palm/arch-update-cactus-reports';
const TOKEN = 'github_pat_11TEST0TOKEN0VALUE0THAT0MUST0NEVER0LEAK';
const ENV = { REPO, GITHUB_TOKEN: TOKEN, PAUSE_MS: '0' };
const IP = '203.0.113.77';
const LOG_PATH = 'logs/2026/10/a1b2c3d4-20261030-174545.log';
const BRANCH = 'logs-a1b2c3d4';
const BASE = '5c47c01d6b0e4f0a9b3c2d1e0f9a8b7c6d5e4f3a';
const LOG = `[17:42:38 СТАРТ] arch-update, режим: автоответы
==> Looking for updates...
gnupg          2.4.9-3  ->  2.4.9-4
:: Proceed with installation? [Y/n]
[17:42:52 АВТО] «:: Proceed with installation? [Y/n]» → y   (pacman proceed)
(2/2) upgrading libjpeg-turbo                    [#####################] 100%
[17:45:45 КОНЕЦ] arch-update завершился, код 0
[17:45:45 ПАКЕТЫ] обновлены: gnupg 2.4.9-3→2.4.9-4, libjpeg-turbo 3.2.0-2→3.2.1-1
`;

let generation = 0;
async function load() {
  return (await import(`./worker.js?copy=${++generation}`)).default;
}

// A report as lib/report.py builds it: the main PC after a clean update.
function sample({ update = {}, device = {}, ...rest } = {}) {
  return {
    schema: 1,
    id: 'a1b2c3d4',
    time: '2026-10-30T17:45:45Z',
    updater: { commit: 'f161b12', date: '30.10.2026' },
    device: {
      vendor: 'Micro-Star International Co., Ltd.', model: 'MS-7C02', model_version: '1.0', family: '',
      chassis: '3', chromebook: false, cpu: 'AMD Ryzen 7 5800X 8-Core Processor', cores: 16, ram_gib: 31.3,
      gpu: [{ id: '10de:2484', name: 'NVIDIA Corporation GA104 [GeForce RTX 3070]', driver: 'nvidia' }],
      nvidia: '580.95.05', arch: 'x86_64', kernel: '7.2.12-arch1-1', distro: 'arch', kernels: ['linux', 'linux-lts'],
      secure_boot: true, boot_readable: true, desktop: 'KDE', session: 'wayland', terminal: 'konsole', lang: 'ru',
      battery: null, network: { type: 'кабель', driver: 'r8169', adapter: 'Realtek RTL8111/8168', speed_mbps: 1000 },
      free_gib: { '/': 120.4, '/boot': 0.7 }, python: '3.14.0', pacman: '7.1.0-1', arch_update: '3.16.1-1',
      aur_helper: 'yay', screen: '136x38', monitors: 3, ...device,
    },
    update: {
      mode: 'auto', verdict: 0, started: true, exit: 0, seconds: 187,
      changed: ['brave-bin', 'gnupg', 'libjpeg-turbo', 'yay-bin', 'yay-bin-debug'], added: [], removed: [],
      problems: [], warnings: [], ok: ['pacman завершился штатно', 'упавших сервисов нет'], ...update,
    },
    log: LOG,
    ...rest,
  };
}

const CHROMEBOOK = {
  vendor: 'Google', model: 'Kevin', model_version: 'rev8', chromebook: true, cpu: 'ARMv8 Processor rev 4 (v8l)',
  cores: 6, ram_gib: 3.8, gpu: [{ id: '', name: '', driver: 'panfrost' }], nvidia: '', arch: 'aarch64',
  battery: { percent: 64, discharging: true }, network: { type: 'Wi-Fi', driver: 'mwifiex_pcie', adapter: '11ab:2b38', signal_dbm: -61 },
  screen: '80x24', monitors: 1,
};

// GitHub, as far as the relay sees it: it remembers the branches made (calls.branches). `answers`
// replaces the answer to a kind of call.
function github(answers = {}) {
  const calls = [];
  const branches = new Set();
  let issues = 40;
  const routes = {
    hour: () => [200, []],
    day: () => [200, []],
    find: () => [200, []],
    label: () => [201, { name: 'x' }],
    ref: (c) => (branches.has(c.path.slice('/git/ref/heads/'.length))
      ? [200, { ref: `refs/${c.path.slice('/git/ref/'.length)}`, object: { sha: BASE } }]
      : [404, { message: 'Not Found' }]),
    base: () => [200, [{ sha: BASE }]],
    branch: (c) => {
      const name = c.body.ref.slice('refs/heads/'.length);
      if (branches.has(name)) return [422, { message: 'Reference already exists' }];
      branches.add(name);
      return [201, { ref: c.body.ref, object: { sha: c.body.sha } }];
    },
    log: (c) => (branches.has(c.body.branch)
      ? [201, { content: { html_url: `https://github.com/${REPO}/blob/${c.body.branch}/${c.path.slice('/contents/'.length)}` } }]
      : [404, { message: `Branch ${c.body.branch} not found` }]),
    issue: () => [201, { number: ++issues }],
    close: () => [200, { state: 'closed' }],
    ...answers,
  };
  globalThis.fetch = async (url, init = {}) => {
    const u = new URL(url);
    const base = `/repos/${REPO}`;
    assert.equal(u.origin, 'https://api.github.com');
    assert.ok(u.pathname.startsWith(base), u.pathname);
    const path = u.pathname.slice(base.length) + u.search;
    const method = init.method || 'GET';
    const kind = method === 'GET'
      ? (path.startsWith('/git/ref/') ? 'ref' : path.startsWith('/commits') ? 'base'
        : path.includes('per_page=1&page=20') ? 'hour' : path.includes('per_page=1&page=60') ? 'day' : 'find')
      : method === 'PUT' ? 'log' : method === 'PATCH' ? 'close'
        : path === '/labels' ? 'label' : path === '/git/refs' ? 'branch' : 'issue';
    const call = { kind, method, path, url: String(url), headers: new Headers(init.headers), raw: init.body || '', at: Date.now() };
    call.body = init.body ? JSON.parse(init.body) : null;
    calls.push(call);
    const answer = routes[kind](call);
    if (answer instanceof Error) throw answer;
    const [status, data, headers = {}] = answer;
    return new Response(data === undefined ? null : JSON.stringify(data),
      { status, headers: { 'content-type': 'application/json; charset=utf-8', ...headers } });
  };
  calls.branches = branches;
  return calls;
}

function post(body, { ip = IP, type = 'application/json', path = '/report', method = 'POST', headers = {} } = {}) {
  const raw = typeof body === 'string' || body instanceof Uint8Array || body instanceof ReadableStream ? body : JSON.stringify(body);
  const init = { method, headers: { 'cf-connecting-ip': ip, ...(type ? { 'content-type': type } : {}), ...headers } };
  if (method !== 'GET' && method !== 'HEAD') init.body = raw;
  if (raw instanceof ReadableStream) init.duplex = 'half';
  return new Request(`https://arch-update-reports.example.workers.dev${path}`, init);
}

function context() {
  const waits = [];
  return { waits, waitUntil: (p) => waits.push(p) };
}

async function send(worker, body, opts = {}) {
  const res = await worker.fetch(post(body, opts), opts.env || ENV, opts.ctx || context());
  const text = await res.text();
  return { status: res.status, headers: res.headers, text, data: JSON.parse(text) };
}

function decode(b64) {
  return new TextDecoder().decode(Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)));
}

test('a clean update: count, label, branch, log, issue, then the issue is closed', async () => {
  const worker = await load();
  const calls = github();
  const ctx = context();
  const res = await send(worker, sample(), { ctx });
  assert.equal(res.status, 201);
  assert.deepEqual(res.data, { ok: true, issue: 41 });
  assert.equal(ctx.waits.length, 1, 'the filing is kept alive even if the sender hangs up');
  assert.deepEqual(calls.map((c) => `${c.method} ${c.path}`), [
    'GET /issues?state=all&sort=created&direction=desc&per_page=1&page=20',
    'POST /labels',
    `GET /git/ref/heads/${BRANCH}`,
    'GET /commits?per_page=1',
    'POST /git/refs',
    `PUT /contents/${LOG_PATH}`,
    'POST /issues',
    'PATCH /issues/41',
  ]);
  for (const c of calls) {
    assert.equal(c.headers.get('authorization'), `Bearer ${TOKEN}`);
    assert.equal(c.headers.get('accept'), 'application/vnd.github+json');
    assert.match(c.headers.get('x-github-api-version'), /^\d{4}-\d{2}-\d{2}$/);
    assert.ok(c.headers.get('user-agent'));
  }
  const [, label, , , branch, log, issue, close] = calls;
  assert.equal(label.body.name, 'ок');
  assert.match(label.body.color, /^[0-9a-f]{6}$/);
  assert.deepEqual(branch.body, { ref: `refs/heads/${BRANCH}`, sha: BASE }, 'the machine\'s branch starts at the default branch');
  assert.equal(log.body.message, 'log a1b2c3d4 2026-10-30T17:45:45Z');
  assert.equal(log.body.branch, BRANCH, 'the log goes to the machine\'s own branch');
  assert.equal(decode(log.body.content), LOG, 'the log is stored as it came, in UTF-8');
  assert.equal(issue.body.title, '✓ 30.10.2026 17:45 UTC · Micro-Star International Co., Ltd. MS-7C02 · 5 пакетов · a1b2c3d4');
  assert.deepEqual(issue.body.labels, ['ок']);
  const body = issue.body.body;
  assert.ok(body.startsWith('### ✓ Обновление прошло чисто'));
  assert.ok(body.includes(`(https://github.com/${REPO}/blob/${BRANCH}/${LOG_PATH})`), 'links the stored log');
  for (const shown of ['`AMD Ryzen 7 5800X 8-Core Processor` · 16 ядер', '31,3 ГиБ', '`NVIDIA Corporation GA104 [GeForce RTX 3070]`',
    'NVIDIA `580.95.05`', '`7.2.12-arch1-1` · установлены: `linux`, `linux-lts`', '`KDE` · `wayland`', '`konsole`',
    '`136x38` · мониторов: 3', 'кабель · адаптер `Realtek RTL8111/8168` · драйвер `r8169` · 1000 Мбит/с', '| Батарея | нет |',
    '`/` 120,4 ГиБ · `/boot` 0,7 ГиБ', '`f161b12` · `30.10.2026`', '`3.16.1-1` · pacman `7.1.0-1`', '| AUR-помощник | `yay` |',
    '| Secure Boot | включён |', '| /boot читается | да |', '| Режим | автоответы |', '| Длилось | 3 мин 7 с |',
    '| Код выхода arch-update | 0 |', 'обновлено 5 · новых 0 · удалено 0', '✓ pacman завершился штатно',
    '**Обновлены (5):** `brave-bin`, `gnupg`, `libjpeg-turbo`, `yay-bin`, `yay-bin-debug`', '- **Удалены:** —',
    'Лог целиком: 8 строк', '[17:45:45 КОНЕЦ] arch-update завершился, код 0', '"nvidia": "580.95.05"']) {
    assert.ok(body.includes(shown), `the issue shows ${shown}`);
  }
  assert.deepEqual(close.body, { state: 'closed', state_reason: 'completed' });
});

test('each machine\'s logs go to a branch of its own, made once', async () => {
  let worker = await load();
  const calls = github();
  const kinds = () => calls.splice(0).map((c) => c.kind);
  const later = sample({ time: '2026-10-31T08:00:00Z', update: { verdict: 1, warnings: ['w'] } });
  assert.equal((await send(worker, sample())).status, 201);
  assert.deepEqual(kinds(), ['hour', 'label', 'ref', 'base', 'branch', 'log', 'issue', 'close']);
  assert.equal((await send(worker, later, { ip: '203.0.113.81' })).status, 201);
  assert.deepEqual(kinds(), ['hour', 'label', 'log', 'issue'], 'a branch known to be there is not looked for');

  worker = await load(); // another isolate: it looks, and finds the branch
  assert.equal((await send(worker, sample({ time: '2026-10-31T09:00:00Z' }))).status, 201);
  assert.deepEqual(kinds(), ['hour', 'label', 'ref', 'log', 'issue', 'close']);

  // The owner deleted the machine's branch (its reports were to go): the log cannot be written there,
  // and the next try makes the branch again instead of trusting what it knew.
  calls.branches.delete(BRANCH);
  assert.equal((await send(worker, sample({ time: '2026-10-31T10:00:00Z' }), { ip: '203.0.113.82' })).status, 502);
  assert.deepEqual(kinds(), ['hour', 'log']);
  assert.equal((await send(worker, sample({ time: '2026-10-31T10:00:00Z' }), { ip: '203.0.113.83' })).status, 201);
  assert.deepEqual(kinds(), ['hour', 'ref', 'base', 'branch', 'log', 'issue', 'close']);
  assert.ok(calls.branches.has(BRANCH));

  // Another report of the same machine made the branch first.
  worker = await load();
  const raced = github({ ref: () => [404, { message: 'Not Found' }], branch: () => [422, { message: 'Reference already exists' }],
    log: () => [201, {}] });
  assert.equal((await send(worker, sample())).status, 201);
  assert.deepEqual(raced.map((c) => c.kind), ['hour', 'label', 'ref', 'base', 'branch', 'log', 'issue', 'close']);
});

test('an empty repository is named as the trouble, and nothing is written', async () => {
  for (const answers of [{ ref: () => [409, { message: 'Git Repository is empty.' }] },
    { base: () => [409, { message: 'Git Repository is empty.' }] }]) {
    const worker = await load();
    const calls = github(answers);
    const res = await send(worker, sample());
    assert.equal(res.status, 502);
    assert.match(res.data.error, /пустой.*README/);
    assert.ok(!calls.some((c) => c.kind === 'log' || c.kind === 'issue'));
  }
  const worker = await load();
  const calls = github();
  const res = await send(worker, sample({ log: '' }));
  assert.equal(res.status, 201);
  assert.deepEqual(calls.map((c) => c.kind), ['hour', 'label', 'issue', 'close'], 'an empty log needs no branch');
  assert.ok(calls.find((c) => c.kind === 'issue').body.body.includes('лог пустой'));
});

test('warnings and problems stay open with their labels; a Chromebook is marked', async () => {
  const worker = await load();
  const calls = github({
    label: (c) => (c.body.name === 'chromebook'
      ? [422, { message: 'Validation Failed', errors: [{ resource: 'Label', code: 'already_exists', field: 'name' }] }]
      : [201, {}]),
  });
  const warned = sample({ device: CHROMEBOOK, update: { verdict: 1, warnings: ['есть пакеты-сироты — не удалял, посмотреть: pacman -Qdtq'] } });
  let res = await send(worker, warned);
  assert.equal(res.status, 201);
  let issue = calls.find((c) => c.kind === 'issue');
  assert.equal(issue.body.title, '⚠ 30.10.2026 17:45 UTC · Google Kevin · 5 пакетов · a1b2c3d4');
  assert.deepEqual(issue.body.labels, ['предупреждения', 'chromebook']);
  assert.ok(issue.body.body.includes('⚠ есть пакеты-сироты'));
  assert.ok(issue.body.body.includes('| Батарея | 64 % · разряжается |'));
  assert.ok(issue.body.body.includes('Wi-Fi · адаптер `11ab:2b38` · драйвер `mwifiex_pcie` · сигнал -61 дБм'));
  assert.ok(issue.body.body.includes('`Google Kevin` · `rev8` · хромбук'));
  assert.equal(calls.filter((c) => c.kind === 'label').length, 2);
  assert.ok(!calls.some((c) => c.kind === 'close'), 'an update with warnings stays open');

  calls.length = 0;
  const broken = sample({ id: 'b2c3d4e5', device: CHROMEBOOK, update: { verdict: 2, problems: ['нет /boot/vmlinuz-linux — НЕ перезагружайся'] } });
  res = await send(worker, broken, { ip: '203.0.113.78' });
  assert.equal(res.status, 201);
  issue = calls.find((c) => c.kind === 'issue');
  assert.ok(issue.body.title.startsWith('✗ '));
  assert.deepEqual(issue.body.labels, ['проблемы', 'chromebook']);
  assert.ok(issue.body.body.startsWith('### ✗ После обновления есть проблемы'));
  assert.deepEqual(calls.filter((c) => c.kind === 'label').map((c) => c.body.name), ['проблемы'],
    'labels made once are not made again');
  assert.ok(!calls.some((c) => c.kind === 'close'));
});

test('the notes are shown after the rest, and checked like the other lists', async () => {
  const worker = await load();
  const calls = github();
  const res = await send(worker, sample({ update: { verdict: 0, notes: ['сервисы ждут перезапуска: netctl@<instance>'] } }));
  assert.equal(res.status, 201);
  const body = calls.find((c) => c.kind === 'issue').body.body;
  assert.ok(body.includes('✓ упавших сервисов нет\n· сервисы ждут перезапуска: netctl@<instance>'), body);
  for (const notes of ['x', [1], Array(201).fill('n'), ['n'.repeat(4001)]]) {
    assert.equal((await send(worker, sample({ update: { notes } }), { ip: '203.0.113.90' })).status, 400, String(notes));
  }
  assert.equal((await send(worker, sample({ update: { notes: undefined } }), { ip: '203.0.113.91' })).status, 201,
    'a report from before there were notes');
});

test('a run that never started is labelled so, and counts its packages in Russian', async () => {
  const worker = await load();
  const calls = github();
  const cases = [
    [{ verdict: 1, started: false, changed: [], warnings: ['обновление отменено — система не менялась'] }, '·', ['не начато'], '0 пакетов'],
    [{ verdict: 2, started: false, changed: ['a'], problems: ['/boot не смонтирован'] }, '✗', ['проблемы', 'не начато'], '1 пакет'],
    [{ verdict: null, changed: ['a', 'b'], added: ['c'] }, '·', [], '3 пакета'],
    [{ verdict: 1, changed: Array.from({ length: 11 }, (_, i) => `p${i}`) }, '⚠', ['предупреждения'], '11 пакетов'],
    [{ verdict: 1, changed: Array.from({ length: 21 }, (_, i) => `p${i}`) }, '⚠', ['предупреждения'], '21 пакет'],
  ];
  for (const [i, [update, sym, labels, count]] of cases.entries()) {
    calls.length = 0;
    const res = await send(worker, sample({ id: `0000000${i}`, update }), { ip: `198.51.100.${i}` });
    assert.equal(res.status, 201);
    const issue = calls.find((c) => c.kind === 'issue');
    assert.ok(issue.body.title.startsWith(`${sym} `), issue.body.title);
    assert.ok(issue.body.title.includes(` · ${count} · `), issue.body.title);
    assert.deepEqual(issue.body.labels, labels);
    assert.ok(!calls.some((c) => c.kind === 'close'));
  }
});

test('what is not a report is refused before GitHub hears of it', async () => {
  const worker = await load();
  const calls = github();
  const big = 'x'.repeat(1024 * 1024 + 1);
  const cases = [
    [405, sample(), { method: 'GET' }],
    [405, sample(), { method: 'PUT' }],
    [404, sample(), { path: '/issues' }],
    [400, sample(), { type: 'text/plain' }],
    [400, sample(), { type: '' }],
    [400, 'not json'],
    [400, new Uint8Array([0x7b, 0xff, 0xfe, 0x7d])],
    [400, []],
    [400, sample({ schema: 2 })],
    [400, sample({ id: 'A1B2C3D4' })],
    [400, sample({ id: 'a1b2c3' })],
    [400, sample({ id: 12345678 })],
    [400, sample({ time: '2026-10-30 17:45:45' })],
    [400, sample({ time: '2026-02-30T10:00:00Z' })],
    [400, sample({ time: '2026-10-30T17:45:45+03:00' })],
    [400, { ...sample(), device: [] }],
    [400, { ...sample(), device: undefined }],
    [400, { ...sample(), update: undefined }],
    [400, sample({ update: { verdict: 3 } })],
    [400, sample({ update: { verdict: '0' } })],
    [400, sample({ update: { verdict: undefined } })],
    [400, sample({ update: { started: 'yes' } })],
    [400, sample({ update: { seconds: -1 } })],
    [400, sample({ update: { exit: 1.5 } })],
    [400, sample({ update: { changed: 'gnupg' } })],
    [400, sample({ update: { changed: [1] } })],
    [400, sample({ update: { changed: Array(5001).fill('p') } })],
    [400, sample({ update: { warnings: ['x'.repeat(4001)] } })],
    [400, { ...sample(), log: undefined }],
    [400, sample({ log: 123 })],
    [413, sample({ log: big })],
    [413, JSON.stringify(sample()), { headers: { 'content-length': String(2 * 1024 * 1024) } }],
    [413, JSON.stringify(sample({ log: 'y'.repeat(1600 * 1024) }))],
  ];
  for (const [i, [status, body, opts = {}]] of cases.entries()) {
    const res = await send(worker, body, { ip: `192.0.2.${i}`, ...opts });
    assert.equal(res.status, status, `case ${i}: ${res.text}`);
    assert.equal(res.data.ok, false);
    assert.ok(res.data.error.length > 3, `case ${i} says why`);
    if (status === 405) assert.equal(res.headers.get('allow'), 'POST');
  }
  assert.equal(calls.length, 0);
});

test('a body that grows past the limit while it streams is refused', async () => {
  const worker = await load();
  const calls = github();
  const chunk = new Uint8Array(256 * 1024).fill(0x20);
  let sent = 0;
  const stream = new ReadableStream({
    pull(controller) {
      if (sent++ < 8) controller.enqueue(chunk);
      else controller.close();
    },
  });
  const res = await send(worker, stream);
  assert.equal(res.status, 413);
  assert.ok(sent < 9, 'reading stops at the limit');
  assert.equal(calls.length, 0);
});

test('POST / works as well as POST /report', async () => {
  const worker = await load();
  github();
  assert.equal((await send(worker, sample(), { path: '/' })).status, 201);
});

test('a relay without its settings says so', async () => {
  const worker = await load();
  const calls = github();
  for (const env of [{ REPO }, { GITHUB_TOKEN: TOKEN }, { REPO: 'not a repo', GITHUB_TOKEN: TOKEN }, { REPO: `${REPO}/x`, GITHUB_TOKEN: TOKEN }]) {
    const res = await send(worker, sample(), { env });
    assert.equal(res.status, 503);
  }
  assert.equal(calls.length, 0);
});

test('GitHub failing gives 502, and never the token', async () => {
  const made = ['hour', 'label', 'ref', 'base', 'branch'];
  const cases = [
    [{ log: () => [500, { message: 'Server Error' }] }, [...made, 'log']],
    [{ issue: () => [422, { message: 'Validation Failed' }] }, [...made, 'log', 'issue']],
    [{ hour: () => [401, { message: 'Bad credentials' }] }, ['hour']],
    [{ log: () => new TypeError('fetch failed') }, [...made, 'log']],
    [{ ref: () => [500, { message: 'Server Error' }] }, ['hour', 'label', 'ref']],
    [{ base: () => [200, [{ sha: 'not a sha' }]] }, ['hour', 'label', 'ref', 'base']],
    [{ branch: () => [403, { message: 'Resource not accessible by personal access token' }] }, made],
  ];
  for (const [answers, kinds] of cases) {
    const worker = await load();
    const calls = github(answers);
    const res = await send(worker, sample());
    assert.equal(res.status, 502);
    assert.ok(!res.text.includes(TOKEN) && !res.text.includes('github_pat'));
    assert.deepEqual(calls.map((c) => c.kind), kinds);
  }
});

test('an issue that could not be closed is filed all the same', async () => {
  const worker = await load();
  github({ close: () => [500, {}] });
  const res = await send(worker, sample());
  assert.equal(res.status, 201);
  assert.equal(res.data.issue, 41);
});

test('a rate-limited GitHub is left alone for a while', async () => {
  const worker = await load();
  const calls = github({ log: () => [403, { message: 'You have exceeded a secondary rate limit.' }, { 'retry-after': '120' }] });
  let res = await send(worker, sample());
  assert.equal(res.status, 502);
  const before = calls.length;
  res = await send(worker, sample({ id: 'c3d4e5f6' }), { ip: '203.0.113.79' });
  assert.equal(res.status, 503);
  const wait = Number(res.headers.get('retry-after'));
  assert.ok(wait > 100 && wait <= 120, `retry after ${wait}`);
  assert.equal(calls.length, before, 'no call to GitHub while it asked to wait');
});

test('a spent hourly quota keeps the relay away until GitHub resets it', async () => {
  const worker = await load();
  const reset = Math.floor(Date.now() / 1000) + 900;
  const calls = github({
    log: () => [403, { message: 'API rate limit exceeded for user ID 1.' }, { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': String(reset) }],
  });
  assert.equal((await send(worker, sample())).status, 502);
  const before = calls.length;
  const res = await send(worker, sample({ id: 'c3d4e5f6' }), { ip: '203.0.113.79' });
  assert.equal(res.status, 503);
  const wait = Number(res.headers.get('retry-after'));
  assert.ok(wait > 850 && wait <= 901, `retry after ${wait}`);
  assert.equal(calls.length, before);
});

test('a rate-limited label stops the report, and the relay waits', async () => {
  const worker = await load();
  const calls = github({ label: () => [429, { message: 'Too many requests' }, { 'retry-after': '90' }] });
  assert.equal((await send(worker, sample())).status, 502);
  assert.deepEqual(calls.map((c) => c.kind), ['hour', 'label'], 'nothing is written after it');
  const res = await send(worker, sample({ id: 'c3d4e5f6' }), { ip: '203.0.113.79' });
  assert.equal(res.status, 503);
  assert.ok(Number(res.headers.get('retry-after')) > 60);
  assert.equal(calls.length, 2);
});

test('a report sent again after a lost answer is not filed twice', async () => {
  const exists = () => [422, { message: 'Invalid request.\n\n"sha" wasn\'t supplied.' }];
  let worker = await load();
  let calls = github({
    log: exists,
    find: () => [200, [{ number: 11, body: 'other' }, { number: 12, body: `[лог целиком](https://github.com/${REPO}/blob/main/${LOG_PATH})` }]],
  });
  let res = await send(worker, sample());
  assert.equal(res.status, 200);
  assert.deepEqual(res.data, { ok: true, issue: 12, duplicate: true });
  assert.deepEqual(calls.map((c) => c.kind), ['hour', 'label', 'ref', 'base', 'branch', 'log', 'find']);

  // The log was stored but no issue made it: the issue is filed now, linking the stored log.
  worker = await load();
  calls = github({ log: exists });
  res = await send(worker, sample());
  assert.equal(res.status, 201);
  assert.deepEqual(calls.map((c) => c.kind), ['hour', 'label', 'ref', 'base', 'branch', 'log', 'find', 'issue', 'close']);
  assert.ok(calls.find((c) => c.kind === 'issue').body.body.includes(`(https://github.com/${REPO}/blob/${BRANCH}/${LOG_PATH})`));
});

test('a log GitHub took for another commit\'s is written again, once', async () => {
  let worker = await load();
  let tries = 0;
  let calls = github({ log: () => (++tries === 1 ? [409, { message: 'is at 1a2b but expected 3c4d' }] : [201, {}]) });
  let res = await send(worker, sample());
  assert.equal(res.status, 201);
  assert.deepEqual(calls.filter((c) => c.kind === 'log').length, 2);
  assert.equal(calls[calls.length - 1].kind, 'close');

  worker = await load();
  calls = github({ log: () => [409, { message: 'is at 1a2b but expected 3c4d' }] });
  res = await send(worker, sample());
  assert.equal(res.status, 502);
  assert.deepEqual(calls.map((c) => c.kind).slice(-2), ['log', 'log'], 'tried twice, not more');
  assert.ok(!calls.some((c) => c.kind === 'issue'));
});

test('writes to GitHub are spaced out by PAUSE_MS, reads are not', async () => {
  const worker = await load();
  const calls = github();
  const res = await send(worker, sample({ device: CHROMEBOOK }), { env: { ...ENV, PAUSE_MS: '50' } });
  assert.equal(res.status, 201);
  const writes = calls.filter((c) => c.method !== 'GET');
  assert.deepEqual(writes.map((c) => c.kind), ['label', 'label', 'branch', 'log', 'issue', 'close']);
  for (let i = 1; i < writes.length; i++) {
    const gap = writes[i].at - writes[i - 1].at;
    assert.ok(gap >= 49, `${writes[i - 1].kind} → ${writes[i].kind}: ${gap} ms`);
  }
  const ref = calls.find((c) => c.kind === 'ref');
  const label = calls.filter((c) => c.kind === 'label').pop();
  assert.ok(ref.at - label.at < 40, 'a read right after a write does not wait');
});

test('the issue stays under GitHub\'s limit and keeps the end of the log', async () => {
  const worker = await load();
  const calls = github();
  const lines = Array.from({ length: 3000 }, (_, i) => `строка ${i}: ${'x'.repeat(150)}`);
  const log = `первая строка лога\n${lines.join('\n')}\nпоследняя строка лога\n`;
  const update = {
    verdict: 2,
    changed: Array.from({ length: 5000 }, (_, i) => `package-number-${i}`),
    added: Array.from({ length: 3000 }, (_, i) => `added-${i}`),
    problems: Array.from({ length: 200 }, (_, i) => `проблема ${i} ${'п'.repeat(300)}`),
    warnings: Array.from({ length: 100 }, () => 'w'.repeat(1000)),
  };
  const res = await send(worker, sample({ update, log }));
  assert.equal(res.status, 201);
  const put = calls.find((c) => c.kind === 'log');
  assert.equal(decode(put.body.content), log, 'the stored log is whole');
  const body = calls.find((c) => c.kind === 'issue').body.body;
  assert.ok(body.length <= 65000, `body of ${body.length} characters`);
  assert.ok(body.includes('последняя строка лога'));
  assert.ok(!body.includes('первая строка лога'));
  assert.match(body, /Конец лога: последние \d+ строк[аи]? из 3002/);
  assert.match(body, /\*\*Обновлены \(5000\):\*\* .* … и ещё \d+/);
  assert.match(body, /… и ещё \d+\n/, 'the list of findings is cut too');
  assert.ok(body.includes('"nvidia": "580.95.05"'), 'the device block is still there');

  // One endless line: its end is kept.
  calls.length = 0;
  const line = `${'z'.repeat(900 * 1024)}КОНЕЦ`;
  await send(worker, sample({ id: 'd4e5f6a7', log: line }), { ip: '203.0.113.80' });
  const one = calls.find((c) => c.kind === 'issue').body.body;
  assert.ok(one.length <= 65000);
  assert.ok(one.includes('zzzКОНЕЦ\n'));
  assert.ok(one.includes('начало строки обрезано'));
});

// What is left of the Markdown once code blocks and code spans are taken out (by the CommonMark
// rules): the only place where GitHub would render links, mentions, references or HTML.
function outsideCode(md) {
  const kept = [];
  let fence = 0;
  for (const line of md.split('\n')) {
    const run = /^ {0,3}(`{3,})(.*)$/.exec(line);
    if (fence) {
      if (run && run[1].length >= fence && !run[2].trim()) fence = 0;
      continue;
    }
    if (run && !run[2].includes('`')) {
      fence = run[1].length;
      continue;
    }
    kept.push(line);
  }
  const text = kept.join('\n');
  let out = '';
  let i = 0;
  while (i < text.length) {
    const start = text.indexOf('`', i);
    if (start < 0) {
      out += text.slice(i);
      break;
    }
    out += text.slice(i, start);
    let n = 0;
    while (text[start + n] === '`') n++;
    let close = -1;
    for (let j = text.indexOf('`', start + n); j >= 0;) {
      let m = 0;
      while (text[j + m] === '`') m++;
      if (m === n) {
        close = j;
        break;
      }
      j = text.indexOf('`', j + m);
    }
    if (close < 0) {
      out += '`'.repeat(n);
      i = start + n;
    } else {
      out += ' ';
      i = close + n;
    }
  }
  return out;
}

test('nothing from a report can become a link, a mention, a reference or HTML', async () => {
  const worker = await load();
  const calls = github();
  const evil = '@octocat [click](https://evil.example/x) ![p](https://evil.example/p.png) <img src=x onerror=alert(1)> '
    + '#1 owner/repo#2 www.evil.example ~~s~~ **b** <details> </details> | a | b |';
  const report = sample({
    device: {
      vendor: evil, model: '`` ` ```', cpu: `${evil}\n| x | y |`, kernel: '``````````', desktop: '\\', terminal: 'a\\|b',
      gpu: [{ name: evil, driver: '`' }], network: { type: evil, adapter: evil, driver: evil },
      screen: '<script>alert(1)</script>', kernels: [evil],
    },
    updater: { commit: evil, date: evil },
    update: { verdict: 2, mode: '@octocat', problems: [evil, 'line\nbreak'], warnings: [evil], ok: [evil], changed: [evil, 'x`y', '|'], added: [evil] },
    log: `${evil}\n\`\`\`\n</details>\n@octocat\n\`\`\`\`\n${evil}\n`,
  });
  const res = await send(worker, report);
  assert.equal(res.status, 201);
  const { title, body } = calls.find((c) => c.kind === 'issue').body;
  for (const bad of ['@', '#', '`', '<', '[', '://', '*']) assert.ok(!title.includes(bad), `title has no ${bad}: ${title}`);

  const outside = outsideCode(body);
  assert.doesNotMatch(outside, /@\w/, 'no mention');
  assert.doesNotMatch(outside, /#\d/, 'no issue reference');
  assert.ok(!outside.includes('evil.example'), 'no link');
  assert.doesNotMatch(outside, /~~|\*\*b\*\*/, 'no formatting from the report');
  const ours = outside.replace(/<\/?(?:details|summary)>/g, '');
  assert.ok(!ours.includes('<'), 'no HTML but our own blocks');
  assert.equal(outside.match(/<details>/g).length, 2);
  assert.equal(outside.match(/<\/details>/g).length, 2);
  const links = outside.match(/\]\(/g) || [];
  assert.equal(links.length, 1, 'one link: to the stored log');
  for (const line of body.split('\n').filter((l) => l.startsWith('| '))) {
    assert.equal(line.match(/(?<!\\)\|/g).length, 3, `a table row keeps its two cells: ${line}`);
  }
});

test('colour codes, control characters and direction marks are taken out of what is shown', async () => {
  const worker = await load();
  const calls = github();
  const esc = String.fromCharCode(0x1b);
  const rlo = String.fromCharCode(0x202e);
  const bell = String.fromCharCode(7);
  const log = `${esc}[1;31mкрасная${esc}[0m строка${bell}\r\nвторая${rlo}строка\n`;
  const res = await send(worker, sample({ device: { cpu: `CPU${rlo}evil${esc}[2J` }, update: { verdict: 1, warnings: [`w${rlo}x`] }, log }));
  assert.equal(res.status, 201);
  const { title, body } = calls.find((c) => c.kind === 'issue').body;
  for (const c of [esc, rlo, bell, '\r']) {
    assert.ok(!body.includes(c) && !title.includes(c), `no ${JSON.stringify(c)}`);
  }
  assert.ok(body.includes('красная строка\nвтораястрока\n'));
  assert.ok(body.includes('`CPUevil`'));
  assert.equal(decode(calls.find((c) => c.kind === 'log').body.content), log, 'the stored log keeps everything');
});

test('a window title or a link is taken out whole, and a broken one takes nothing else with it', async () => {
  const worker = await load();
  const calls = github();
  const esc = String.fromCharCode(0x1b);
  const bell = String.fromCharCode(7);
  const alm = String.fromCharCode(0x61c); // the Arabic letter mark, a direction mark too
  const log = `line one\nline two ${esc}]0;window title\nline three\n${esc}]0;title${bell}titled `
    + `${esc}]8;;https://evil.example/${esc}\\link${esc}]8;;${esc}\\ done\nline four: the error at the end\n`;
  const res = await send(worker, sample({ device: { cpu: `CPU${alm}x` }, log }));
  assert.equal(res.status, 201);
  const { body } = calls.find((c) => c.kind === 'issue').body;
  assert.ok(body.includes('line two 0;window title\nline three\ntitled link done\nline four: the error at the end\n'), body);
  assert.match(body, /Лог целиком: 5 строк/);
  assert.ok(!body.includes('evil.example') && !body.includes(esc) && !body.includes(alm));
  assert.ok(body.includes('`CPUx`'));
});

test('a mode named like something of Object\'s is shown as text, in code', async () => {
  for (const [mode, shown] of [['constructor', '`constructor`'], ['__proto__', '`__proto__`'], ['toString', '`toString`'],
    ['manual', 'ручной']]) {
    const worker = await load();
    const calls = github();
    assert.equal((await send(worker, sample({ update: { mode } }))).status, 201);
    const { body } = calls.find((c) => c.kind === 'issue').body;
    assert.ok(body.includes(`| Режим | ${shown} |`), `${mode}: ${body.split('\n').find((l) => l.startsWith('| Режим'))}`);
    assert.ok(!body.includes('native code') && !body.includes('[object Object]'));
  }
});

test('too many requests from one address wait', async () => {
  const worker = await load();
  const calls = github();
  for (let i = 0; i < 6; i++) assert.equal((await send(worker, 'not json')).status, 400);
  const res = await send(worker, 'not json');
  assert.equal(res.status, 429);
  assert.ok(Number(res.headers.get('retry-after')) > 3000);
  assert.equal((await send(worker, 'not json', { ip: '203.0.113.200' })).status, 400, 'another address is not held up');
  assert.equal(calls.length, 0);
});

test('an IPv6 sender is counted by its /64, however it writes its address', async () => {
  const worker = await load();
  github();
  const same = ['2001:db8:1:2::1', '2001:db8:1:2::2', '2001:0db8:0001:0002:ffff:0:0:3', '2001:DB8:1:2:a:b:c:d',
    '2001:db8:1:2:0:0:0:5', '2001:db8:1:2::abcd'];
  for (const ip of same) assert.equal((await send(worker, 'not json', { ip })).status, 400, ip);
  assert.equal((await send(worker, 'not json', { ip: '2001:db8:1:2:dead:beef::7' })).status, 429, 'a fresh address of the same /64');
  assert.equal((await send(worker, 'not json', { ip: '2001:db8:1:3::1' })).status, 400, 'the next /64 is someone else');
  assert.equal((await send(worker, 'not json', { ip: '::ffff:198.51.100.200' })).status, 400);
  for (let i = 0; i < 5; i++) await send(worker, 'not json', { ip: '198.51.100.200' });
  assert.equal((await send(worker, 'not json', { ip: '::ffff:198.51.100.200' })).status, 429, 'an IPv4 address written as IPv6 is that address');
});

test('too many reports from one machine wait', async () => {
  const worker = await load();
  github();
  for (let i = 0; i < 6; i++) assert.equal((await send(worker, sample(), { ip: `198.51.100.${i}` })).status, 201);
  const res = await send(worker, sample(), { ip: '198.51.100.99' });
  assert.equal(res.status, 429);
  assert.equal((await send(worker, sample({ id: 'e5f6a7b8' }), { ip: '198.51.100.98' })).status, 201);
});

const ago = (minutes) => new Date(Date.now() - minutes * 60 * 1000).toISOString().replace(/\.\d+Z$/, 'Z');

test('no more than 20 reports an hour in all, counted on GitHub', async () => {
  const worker = await load();
  const calls = github({ hour: () => [200, [{ number: 300, created_at: ago(10) }]] });
  const res = await send(worker, sample());
  assert.equal(res.status, 429);
  const wait = Number(res.headers.get('retry-after'));
  assert.ok(wait > 2900 && wait <= 3000, `retry after ${wait}`);
  assert.deepEqual(calls.map((c) => c.kind), ['hour']);
});

test('no more than 60 reports a day in all, counted on GitHub', async () => {
  let worker = await load();
  let calls = github({ hour: () => [200, [{ number: 300, created_at: ago(120) }]], day: () => [200, [{ number: 260, created_at: ago(600) }]] });
  const res = await send(worker, sample());
  assert.equal(res.status, 429);
  const wait = Number(res.headers.get('retry-after'));
  assert.ok(wait > 14 * 3600 - 100 && wait <= 14 * 3600, `retry after ${wait}`);
  assert.deepEqual(calls.map((c) => c.kind), ['hour', 'day']);

  worker = await load();
  calls = github({ hour: () => [200, [{ number: 300, created_at: ago(120) }]], day: () => [200, [{ number: 260, created_at: ago(1500) }]] });
  assert.equal((await send(worker, sample())).status, 201, 'the 60th newest is more than a day old');
  assert.deepEqual(calls.map((c) => c.kind).slice(0, 3), ['hour', 'day', 'label']);

  worker = await load();
  calls = github({ hour: () => [200, [{ number: 300, created_at: ago(1500) }]] });
  assert.equal((await send(worker, sample())).status, 201);
  assert.ok(!calls.some((c) => c.kind === 'day'), 'the 20th newest is a day old: the 60th is older, no need to ask');
});

test('once the budget is spent, the next senders hear so without a call to GitHub', async () => {
  const worker = await load();
  const calls = github({ hour: () => [200, [{ number: 300, created_at: ago(10) }]] });
  assert.equal((await send(worker, sample())).status, 429);
  assert.equal(calls.length, 1);
  for (const [i, body] of [sample({ id: 'f6a7b8c9' }), 'not json', JSON.stringify(sample({ log: 'y'.repeat(1400 * 1024) }))].entries()) {
    const res = await send(worker, body, { ip: `203.0.113.${90 + i}` });
    assert.equal(res.status, 429);
    const wait = Number(res.headers.get('retry-after'));
    assert.ok(wait > 2900 && wait <= 3000, `retry after ${wait}`);
  }
  assert.equal(calls.length, 1, 'GitHub was asked once');
});

test('a Rate Limiting binding is used when the deploy has one, with no address in its keys', async () => {
  const worker = await load();
  const calls = github();
  const keys = [];
  const env = { ...ENV, RATE_LIMITER: { limit: async ({ key }) => (keys.push(key), { success: false }) } };
  const res = await send(worker, sample(), { env });
  assert.equal(res.status, 429);
  assert.equal(keys.length, 1);
  assert.ok(!keys[0].includes(IP) && !keys[0].includes('203.0.113'));
  assert.equal(calls.length, 0);
});

test('the sender\'s address never reaches GitHub', async () => {
  const worker = await load();
  const calls = github();
  const res = await send(worker, sample({ update: { verdict: 1, warnings: ['w'] } }));
  assert.equal(res.status, 201);
  assert.ok(!res.text.includes(IP));
  for (const c of calls) {
    assert.ok(!c.url.includes(IP) && !c.raw.includes(IP));
    for (const [, value] of c.headers) assert.ok(!value.includes(IP));
  }
});
