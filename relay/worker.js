// Arch Update by Cactus: the relay that files update reports in the developer's private repository.
//
// A machine sends a report after an update only if its user said yes (lib/report.py). The program
// is public and cannot hold a GitHub token, so this Cloudflare Worker holds one. For each report:
//   - the log goes to logs/YYYY/MM/<id>-<YYYYMMDD-HHMMSS>.log on the branch logs-<id>, one branch
//     a machine (made from the default branch), so deleting that branch deletes all its logs,
//   - an issue says what happened, on which machine, with the end of the log; the issue of a
//     clean update is closed at once, so the open issues are the ones to look at.
//
// Settings of the Worker: REPO ("owner/name") and the secret GITHUB_TOKEN, a fine-grained token for
// that repository only (Contents and Issues: read and write); PAUSE_MS, the milliseconds between
// two writes to GitHub, is 1000 unless set. Setup in the browser: relay/README.md (its Workers Logs
// must be switched off: Cloudflare would keep every sender's address for days).
// Tests: node --test relay/test.mjs.
//
// Against abuse: strict checks of what comes in, limits per sender network (an IPv4 address or an
// IPv6 /64) and per machine ID, and at most HOURLY reports an hour and DAILY a day in all, counted
// on GitHub itself (a Worker pasted in the dashboard and served from workers.dev has neither the
// Rate Limiting binding nor a working Cache API; a RATE_LIMITER binding is used when a deploy adds
// one). The sender's address is never stored or passed on: the limits keep a keyed hash of it, in
// this isolate's memory only.

const MAX_BODY = 1.5 * 1024 * 1024; // bytes of one request
const MAX_LOG = 1024 * 1024; // bytes of the log in it
const MAX_DEVICE = 32 * 1024; // characters of the device object as JSON
const LISTS = { // update.<list>: [most items, longest item]
  changed: [5000, 256], added: [5000, 256], removed: [5000, 256],
  problems: [200, 4000], warnings: [200, 4000], ok: [200, 4000], notes: [200, 4000],
};
const BODY_MAX = 65000; // characters of an issue body (GitHub takes 65,536)
const TAIL = 200; // lines of the log shown in the issue
const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
const PER_ADDRESS = 6; // requests an hour from one network: well below HOURLY
const PER_ID = 6; // reports an hour from one machine
const HOURLY = 20; // reports an hour in all
const DAILY = 60; // reports a day in all: three machines' full outboxes (20 each) still get through
const API = 'https://api.github.com';
const API_VERSION = '2026-03-10';
const REPO_RE = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}$/;
const JSON_TYPE = /^application\/json\s*(?:;\s*charset\s*=\s*"?utf-8"?\s*)?$/i;
const LABELS = {
  'ок': ['0e8a16', 'Обновление прошло чисто'],
  'предупреждения': ['fbca04', 'Обновление прошло, есть предупреждения'],
  'проблемы': ['d73a4a', 'После обновления есть проблемы'],
  'не начато': ['c5def5', 'Обновление не начиналось, система не менялась'],
  'chromebook': ['1d76db', 'Отчёт с хромбука'],
};
// DMI fields some boards leave unfilled: they name nothing.
const PLACEHOLDER = /^(?:to be filled by o\.?e\.?m\.?|system (?:manufacturer|product name|version)|default string|not (?:applicable|specified|available)|none|n\/a|unknown|o\.?e\.?m\.?)$/i;

const seen = new Map(); // limiter key → times (ms) of its requests in the last hour
const labelsMade = new Set(); // "repo\nlabel" known to exist
const branchesMade = new Set(); // "repo\nbranch" known to exist
let pausedUntil = 0; // GitHub asked to wait until then
let fullUntil = 0; // HOURLY or DAILY reports were filed: no more until then

export default {
  async fetch(request, env, ctx) {
    try {
      return await handle(request, env, ctx);
    } catch (e) {
      console.error(JSON.stringify({ relay: 'unexpected', error: String(e && e.name) }));
      return reply(500, { ok: false, error: 'сбой ретранслятора, попробуй позже' });
    }
  },
};

async function handle(request, env, ctx) {
  const { pathname } = new URL(request.url);
  if (pathname !== '/' && pathname !== '/report') {
    return reply(404, { ok: false, error: 'нет такого адреса: отчёты принимает /report' });
  }
  if (request.method !== 'POST') {
    return reply(405, { ok: false, error: 'сюда отчёты отправляют методом POST' }, { Allow: 'POST' });
  }
  const repo = String(env.REPO || '').trim();
  const token = String(env.GITHUB_TOKEN || '').trim();
  if (!REPO_RE.test(repo) || !token) {
    return reply(503, { ok: false, error: 'ретранслятор не настроен: нужны REPO и GITHUB_TOKEN' });
  }
  if (!JSON_TYPE.test(request.headers.get('content-type') || '')) {
    return reply(400, { ok: false, error: 'нужен Content-Type: application/json' });
  }
  const length = request.headers.get('content-length');
  if (length !== null && !/^\s*\d+\s*$/.test(length)) {
    return reply(400, { ok: false, error: 'неверный Content-Length' });
  }
  if (length !== null && Number(length) > MAX_BODY) return reply(413, { ok: false, error: 'отчёт больше 1,5 МиБ' });

  const now = Date.now();
  const address = await keyed(token, 'address', network(request.headers.get('cf-connecting-ip') || ''));
  let wait = limit(`a:${address}`, PER_ADDRESS, now) || await bound(env, `a:${address}`);
  if (wait) return tooMany(wait);
  // Known already that no report gets through now: said before a byte of this one is read.
  if (now < fullUntil) return tooMany(secs(fullUntil - now));
  if (now < pausedUntil) {
    return reply(503, { ok: false, error: 'GitHub просит подождать' }, { 'Retry-After': String(secs(pausedUntil - now)) });
  }

  const bytes = await readBody(request, MAX_BODY);
  if (!bytes) return reply(413, { ok: false, error: 'отчёт больше 1,5 МиБ' });
  let data;
  try {
    data = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  } catch {
    return reply(400, { ok: false, error: 'это не JSON в UTF-8' });
  }
  const checked = validate(data);
  if (checked.error) return reply(checked.status, { ok: false, error: checked.error });

  wait = limit(`i:${data.id}`, PER_ID, now) || await bound(env, `i:${data.id}`);
  if (wait) return tooMany(wait);
  const gh = client(repo, token, Number(env.PAUSE_MS ?? 1000));
  // Filed to the end even if the sender hangs up meanwhile (a slow link, a closed lid).
  const work = fileReport(gh, checked.report, checked.logBytes, now).catch(failed);
  ctx?.waitUntil?.(work);
  return work;
}

// ───────────────────────────── what comes in ─────────────────────────────

async function readBody(request, max) {
  if (!request.body) return new Uint8Array(0);
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > max) {
      await reader.cancel().catch(() => {});
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let at = 0;
  for (const c of chunks) {
    bytes.set(c, at);
    at += c.byteLength;
  }
  return bytes;
}

function isObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function parseTime(s) {
  const m = typeof s === 'string' && /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$/.exec(s);
  if (!m) return null;
  const [y, mo, d, h, mi, se] = m.slice(1).map(Number);
  const t = new Date(Date.UTC(y, mo - 1, d, h, mi, se));
  if (y < 2020 || y > 2099 || t.getUTCMonth() !== mo - 1 || t.getUTCDate() !== d || h > 23 || mi > 59 || se > 59) {
    return null;
  }
  const [, Y, MO, D, H, MI, SE] = m;
  return { Y, MO, D, H, MI, SE };
}

// A report as lib/report.py builds it (schema 1). Unknown fields are ignored; known ones must be right.
function validate(r) {
  const bad = (error, status = 400) => ({ error, status });
  if (!isObject(r)) return bad('нужен объект JSON');
  if (r.schema !== 1) return bad('schema: поддерживается только 1');
  if (typeof r.id !== 'string' || !/^[0-9a-f]{8}$/.test(r.id)) return bad('id: нужны 8 шестнадцатеричных цифр');
  const when = parseTime(r.time);
  if (!when) return bad('time: нужно время UTC вида 2026-10-30T17:45:45Z');
  if (!isObject(r.device)) return bad('device: нужен объект');
  let deviceSize;
  try {
    deviceSize = JSON.stringify(r.device).length;
  } catch {
    deviceSize = Infinity; // nested too deep to write back
  }
  if (deviceSize > MAX_DEVICE) return bad('device: слишком большой');
  if (r.updater != null && !isObject(r.updater)) return bad('updater: нужен объект');
  const u = r.update;
  if (!isObject(u)) return bad('update: нужен объект');
  if (![0, 1, 2, null].includes(u.verdict)) return bad('update.verdict: 0, 1, 2 или null');
  if (u.started !== undefined && typeof u.started !== 'boolean') return bad('update.started: true или false');
  if (u.mode != null && (typeof u.mode !== 'string' || u.mode.length > 20)) return bad('update.mode: короткая строка');
  if (u.exit != null && !(Number.isInteger(u.exit) && Math.abs(u.exit) < 1e6)) {
    return bad('update.exit: целое число или null');
  }
  if (u.seconds != null && !(typeof u.seconds === 'number' && u.seconds >= 0 && u.seconds <= 30 * 86400)) {
    return bad('update.seconds: число секунд или null');
  }
  const lists = {};
  for (const [key, [items, chars]] of Object.entries(LISTS)) {
    const v = u[key] ?? [];
    if (!Array.isArray(v) || v.length > items || !v.every((s) => typeof s === 'string' && s.length <= chars)) {
      return bad(`update.${key}: нужен список строк (до ${items} строк по ${chars} символов)`);
    }
    lists[key] = v;
  }
  if (typeof r.log !== 'string') return bad('log: нужна строка');
  const logBytes = new TextEncoder().encode(r.log);
  if (logBytes.length > MAX_LOG) return bad('log: больше 1 МиБ', 413);
  const update = {
    mode: u.mode ?? null, verdict: u.verdict, started: u.started !== false, exit: u.exit ?? null,
    seconds: u.seconds ?? null, ...lists,
  };
  const report = { id: r.id, time: r.time, when, device: r.device, updater: r.updater || {}, update, log: r.log };
  return { report, logBytes };
}

// ───────────────────────────── limits ─────────────────────────────

function secs(ms) {
  return Math.max(1, Math.ceil(ms / 1000));
}

function tooMany(wait) {
  return reply(429, { ok: false, error: 'слишком много отчётов, попробуй позже' }, { 'Retry-After': String(wait) });
}

// What the limits count by: an IPv4 address, or the /64 network of an IPv6 one (a home or a
// server is given a whole /64, so a sender could take a fresh address for every request).
function network(ip) {
  const v4 = /(?:^|:)(\d{1,3}(?:\.\d{1,3}){3})$/.exec(ip);
  if (v4) return v4[1]; // also an IPv4 address written as IPv6 (::ffff:192.0.2.1)
  if (!ip.includes(':')) return ip;
  const [head, tail] = ip.toLowerCase().split('::');
  const left = head ? head.split(':') : [];
  const right = tail ? tail.split(':') : [];
  const groups = tail === undefined ? left
    : [...left, ...Array(Math.max(0, 8 - left.length - right.length)).fill('0'), ...right];
  return `${groups.slice(0, 4).map((g) => g.replace(/^0+(?=.)/, '')).join(':')}::/64`;
}

// A keyed hash: the same address gives the same key, and the key gives nothing back.
async function keyed(secret, what, value) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const mac = new Uint8Array(await crypto.subtle.sign('HMAC', key, enc.encode(`${what}:${value}`)));
  return Array.from(mac.subarray(0, 12), (b) => b.toString(16).padStart(2, '0')).join('');
}

// Seconds to wait if `key` already had `max` requests in the last hour, else 0 (and this one counts).
function limit(key, max, now) {
  const recent = (seen.get(key) || []).filter((t) => t > now - HOUR);
  if (recent.length >= max) {
    seen.set(key, recent);
    return secs(recent[0] + HOUR - now);
  }
  recent.push(now);
  seen.set(key, recent);
  if (seen.size > 5000) {
    for (const [k, times] of seen) if (times[times.length - 1] <= now - HOUR) seen.delete(k);
    for (const k of seen.keys()) { // still crowded: forget the oldest keys, not the memory
      if (seen.size <= 5000) break;
      seen.delete(k);
    }
  }
  return 0;
}

// The Workers Rate Limiting binding, if this deploy has one (its limit and period are its own).
async function bound(env, key) {
  if (typeof env.RATE_LIMITER?.limit !== 'function') return 0;
  try {
    const { success } = await env.RATE_LIMITER.limit({ key });
    return success ? 0 : 60;
  } catch {
    return 0;
  }
}

// ───────────────────────────── GitHub ─────────────────────────────

class GitHubError extends Error {
  constructor(res, hint = '') {
    super(`GitHub ${res.status} (${res.step})`);
    this.step = res.step;
    this.status = res.status;
    this.retryAfter = limitedFor(res);
    this.hint = hint; // what the owner can fix, said in the answer instead of the usual words
  }
}

// Seconds GitHub wants us to keep away when this answer is a rate limit, else 0.
function limitedFor(res) {
  const limited = res.status === 429 || (res.status === 403 && (res.headers.get('x-ratelimit-remaining') === '0'
    || /rate limit/i.test(String(res.data?.message || ''))));
  if (!limited) return 0;
  const after = Number(res.headers.get('retry-after'));
  const reset = Number(res.headers.get('x-ratelimit-reset'));
  const wait = after > 0 ? after : reset > 0 ? reset - Date.now() / 1000 : 60;
  return Math.min(3600, Math.max(60, Math.ceil(wait)));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Calls to the repository's API. Writes are spaced out (GitHub asks for a second between them).
function client(repo, token, pause) {
  let lastWrite = 0;
  const call = async (step, method, path, body) => {
    const write = method !== 'GET';
    if (write && lastWrite && pause > 0) {
      const wait = lastWrite + pause - Date.now();
      if (wait > 0) await sleep(wait);
    }
    const res = await fetch(`${API}/repos/${repo}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': API_VERSION,
        'User-Agent': 'arch-update-cactus-relay',
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: body === undefined || typeof body === 'string' ? body : JSON.stringify(body),
      signal: AbortSignal.timeout(20000),
    });
    if (write) lastWrite = Date.now();
    let data = null;
    try {
      data = await res.json();
    } catch {
      // an empty or non-JSON answer
    }
    return { step, status: res.status, ok: res.ok, data, headers: res.headers };
  };
  call.repo = repo;
  return call;
}

function failed(e) {
  if (e instanceof GitHubError) {
    if (e.retryAfter) pausedUntil = Math.max(pausedUntil, Date.now() + e.retryAfter * 1000);
    console.error(JSON.stringify({ relay: 'github', step: e.step, status: e.status }));
  } else {
    console.error(JSON.stringify({ relay: 'github', step: 'other', error: String(e && e.name) }));
  }
  return reply(502, { ok: false, error: e?.hint || 'GitHub не принял отчёт, попробуй позже' });
}

async function fileReport(gh, r, logBytes, now) {
  const full = await budgetSpent(gh, now);
  if (full) {
    fullUntil = Math.max(fullUntil, now + full * 1000);
    return tooMany(full);
  }
  const verdict = outcome(r.update, r.device.chromebook === true);
  await makeLabels(gh, verdict.labels);
  const { Y, MO, D, H, MI, SE } = r.when;
  const branch = `logs-${r.id}`;
  const path = `logs/${Y}/${MO}/${r.id}-${Y}${MO}${D}-${H}${MI}${SE}.log`;
  let logUrl = `https://github.com/${gh.repo}/blob/${branch}/${path}`;
  if (logBytes.length) {
    await makeBranch(gh, branch);
    const log = await putLog(gh, branch, path, logBytes, r);
    if (log.duplicate) return reply(200, { ok: true, issue: log.duplicate, duplicate: true });
    logUrl = log.url || logUrl;
  }
  const res = await gh('issue', 'POST', '/issues', {
    title: issueTitle(r, verdict), body: issueBody(r, verdict, logBytes.length ? logUrl : null), labels: verdict.labels,
  });
  if (res.status !== 201 || !Number.isInteger(res.data?.number)) throw new GitHubError(res);
  const number = res.data.number;
  if (verdict.close) {
    const closed = await gh('close', 'PATCH', `/issues/${number}`, { state: 'closed', state_reason: 'completed' });
    if (!closed.ok) {
      // Filed all the same: an issue left open is only one more to look at.
      const e = new GitHubError(closed);
      if (e.retryAfter) pausedUntil = Math.max(pausedUntil, Date.now() + e.retryAfter * 1000);
      console.error(JSON.stringify({ relay: 'github', step: e.step, status: e.status }));
    }
  }
  return reply(201, { ok: true, issue: number });
}

// Seconds until a report may be filed if HOURLY issues were opened in the last hour or DAILY in the
// last day, else 0: the HOURLY-th (DAILY-th) newest issue tells, whatever isolate or location this
// request runs in.
async function budgetSpent(gh, now) {
  for (const [nth, span, step] of [[HOURLY, HOUR, 'hour'], [DAILY, DAY, 'day']]) {
    const res = await gh(step, 'GET', `/issues?state=all&sort=created&direction=desc&per_page=1&page=${nth}`);
    if (!res.ok) throw new GitHubError(res);
    const created = Date.parse(Array.isArray(res.data) && isObject(res.data[0]) ? res.data[0].created_at : '');
    if (created > now - span) return secs(created + span - now);
    if (!(created > now - DAY)) return 0; // none, or older than a day: the DAILY-th newest is older still
  }
  return 0;
}

// A branch of the repository, made from the newest commit of the default branch if it is not there
// yet. A repository with no commit at all has nothing to make it from: the owner adds a README.
async function makeBranch(gh, branch) {
  const key = `${gh.repo}\n${branch}`;
  if (branchesMade.has(key)) return;
  const empty = (res) => res.status === 409 && /empty/i.test(String(res.data?.message || ''));
  const hint = 'репозиторий отчётов пустой: создай в нём README (relay/README.md, шаг 1)';
  const ref = await gh('ref', 'GET', `/git/ref/heads/${branch}`);
  if (empty(ref)) throw new GitHubError(ref, hint);
  if (ref.status === 404) {
    const base = await gh('base', 'GET', '/commits?per_page=1');
    if (empty(base)) throw new GitHubError(base, hint);
    const sha = base.ok && Array.isArray(base.data) && isObject(base.data[0]) ? base.data[0].sha : null;
    if (typeof sha !== 'string' || !/^[0-9a-f]{40,64}$/.test(sha)) throw new GitHubError(base);
    const made = await gh('branch', 'POST', '/git/refs', { ref: `refs/heads/${branch}`, sha });
    const there = made.status === 422 && /already exists/i.test(String(made.data?.message || '')); // made meanwhile
    if (made.status !== 201 && !there) throw new GitHubError(made);
  } else if (!ref.ok) {
    throw new GitHubError(ref);
  }
  branchesMade.add(key);
}

async function makeLabels(gh, names) {
  for (const name of names) {
    const key = `${gh.repo}\n${name}`;
    if (labelsMade.has(key)) continue;
    const [color, description] = LABELS[name];
    const res = await gh('label', 'POST', '/labels', { name, color, description });
    const exists = res.status === 422 && Array.isArray(res.data?.errors)
      && res.data.errors.some((e) => e?.code === 'already_exists');
    if (res.status === 201 || exists) labelsMade.add(key);
    else if (limitedFor(res)) throw new GitHubError(res);
    // Anything else is tried again with the next report; the issue is filed meanwhile.
  }
}

function base64(bytes) {
  if (typeof bytes.toBase64 === 'function') return bytes.toBase64();
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

async function putLog(gh, branch, path, bytes, r) {
  // Written by hand: base64 needs no escaping, and JSON.stringify would scan a megabyte for nothing
  // (CPU time is what the free plan counts: 10 ms a request).
  const message = JSON.stringify(`log ${r.id} ${r.time}`);
  const body = `{"message":${message},"branch":${JSON.stringify(branch)},"content":"${base64(bytes)}"}`;
  let res = await gh('log', 'PUT', `/contents/${path}`, body);
  if (res.status === 409) res = await gh('log', 'PUT', `/contents/${path}`, body); // another commit came first
  if (res.status === 201 || res.status === 200) {
    const url = res.data?.content?.html_url;
    return { url: typeof url === 'string' && /^https:\/\/github\.com\/[\w.%/-]+$/.test(url) ? url : null };
  }
  if (res.status === 422 && /sha/i.test(String(res.data?.message || ''))) {
    // The log is there already: the same report again, after the answer to an earlier try was lost.
    return { duplicate: await issueFor(gh, path) };
  }
  branchesMade.delete(`${gh.repo}\n${branch}`); // deleted meanwhile, perhaps: the next try looks again
  throw new GitHubError(res);
}

// The issue already filed for this log, if a recent one links to it.
async function issueFor(gh, path) {
  const res = await gh('find', 'GET', '/issues?state=all&sort=created&direction=desc&per_page=10');
  if (!res.ok || !Array.isArray(res.data)) return null;
  const hit = res.data.find((i) => isObject(i) && typeof i.body === 'string' && i.body.includes(path)
    && Number.isInteger(i.number));
  return hit ? hit.number : null;
}

// ───────────────────────────── the issue ─────────────────────────────

function outcome(u, chromebook) {
  const kind = u.verdict === 2 ? 'bad' : !u.started ? 'none' : u.verdict === 1 ? 'warn' : u.verdict === 0 ? 'good' : 'none';
  const [sym, headline, label] = {
    good: ['✓', 'Обновление прошло чисто', 'ок'],
    warn: ['⚠', 'Обновление прошло, есть предупреждения', 'предупреждения'],
    bad: ['✗', u.started ? 'После обновления есть проблемы' : 'Обновление не начиналось: есть проблема', 'проблемы'],
    none: ['·', u.started ? 'Итога проверки нет' : 'Обновление не начиналось', null],
  }[kind];
  const labels = [label, u.started ? null : 'не начато', chromebook ? 'chromebook' : null].filter(Boolean);
  return { sym, headline: `${sym} ${headline}`, labels, close: u.verdict === 0 };
}

function plural(n, one, few, many) {
  const m10 = n % 10;
  const m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  return m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
}

// Control characters, terminal colour codes and direction marks would garble what GitHub shows.
// An OSC sequence (a window title, a link) goes only with its terminator, within its line: a lone
// ESC ] loses just those two characters, not the rest of the log.
const ANSI = /\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b\n]*(?:\x07|\x1b\\)|[@-Z\\-_])/g;
const CONTROL = /[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g;

function clean(s) {
  return String(s).replace(/\r\n?/g, '\n').replace(ANSI, '').replace(CONTROL, '');
}

function cut(s, max) {
  return s.length > max ? `${s.slice(0, max - 1).replace(/[\ud800-\udbff]$/, '')}…` : s;
}

function longestRun(s) {
  let best = 0;
  for (const m of s.matchAll(/`+/g)) best = Math.max(best, m[0].length);
  return best;
}

// Text from a report is shown only inside code spans and code blocks: there GitHub renders nothing
// (no links, images, mentions, issue references or HTML). The fence is longer than any run of
// backticks in the text, so the text cannot close it.
function code(s, max = 120) {
  const t = cut(clean(s).replace(/\s+/g, ' ').trim(), max);
  if (!t) return '—';
  const fence = '`'.repeat(longestRun(t) + 1);
  const pad = t.startsWith('`') || t.endsWith('`') ? ' ' : '';
  return `${fence}${pad}${t}${pad}${fence}`;
}

function block(text, info) {
  const t = text.endsWith('\n') ? text : `${text}\n`;
  const fence = '`'.repeat(Math.max(3, longestRun(t) + 1));
  return `${fence}${info}\n${t}${fence}`;
}

function details(summary, inner) {
  return `<details>\n<summary>${summary}</summary>\n\n${inner}\n\n</details>`;
}

// A table cell: pipes in code spans are escaped for the table, GitHub shows them as they were.
function table(title, rows) {
  const lines = [`| ${title} | |`, '|:--|:--|'];
  for (const [label, value] of rows) lines.push(`| ${label} | ${String(value || '—').replace(/\|/g, '\\|')} |`);
  return lines.join('\n');
}

const str = (v) => (typeof v === 'string' && v.trim() ? v : null);
const int = (v) => (Number.isInteger(v) ? v : null);
const real = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const join = (...parts) => parts.filter(Boolean).join(' · ') || null;

function num(x, digits = 1) {
  return String(Math.round(x * 10 ** digits) / 10 ** digits).replace('.', ',');
}

function duration(s) {
  if (real(s) === null) return null;
  const t = Math.round(s);
  if (t < 60) return `${t} с`;
  if (t < 3600) return `${Math.floor(t / 60)} мин ${t % 60} с`;
  return `${Math.floor(t / 3600)} ч ${Math.floor((t % 3600) / 60)} мин`;
}

function deviceName(d) {
  const part = (v) => {
    const t = typeof v === 'string' ? clean(v).replace(/[^\p{L}\p{N} .,+()_-]+/gu, ' ').replace(/\s+/g, ' ').trim() : '';
    return t && !PLACEHOLDER.test(t) ? t : '';
  };
  const vendor = part(d.vendor);
  const model = part(d.model);
  const name = model.toLowerCase().startsWith(vendor.toLowerCase()) ? model : `${vendor} ${model}`.trim();
  return cut(name, 60) || 'неизвестное устройство';
}

function issueTitle(r, verdict) {
  const { Y, MO, D, H, MI } = r.when;
  const u = r.update;
  const n = u.changed.length + u.added.length + u.removed.length;
  return `${verdict.sym} ${D}.${MO}.${Y} ${H}:${MI} UTC · ${deviceName(r.device)} · ${n} `
    + `${plural(n, 'пакет', 'пакета', 'пакетов')} · ${r.id}`;
}

// A Map, not an object: a mode such as "constructor" must not find something of Object's.
const MODES = new Map([['auto', 'автоответы'], ['manual', 'ручной']]);

function updateRows(u) {
  const mode = MODES.get(u.mode) || (str(u.mode) && code(u.mode, 20));
  return [
    ['Режим', mode],
    ['Длилось', duration(u.seconds)],
    ['Код выхода arch-update', int(u.exit) !== null ? String(u.exit) : null],
    ['Пакеты', `обновлено ${u.changed.length} · новых ${u.added.length} · удалено ${u.removed.length}`],
  ];
}

// Problems, warnings, what is fine and the notes (what needs nothing now; they never change the verdict), one a
// line, as the summary screen shows them.
function findings(u) {
  const lines = [];
  let size = 0;
  let more = 0;
  for (const [mark, list] of [['✗', u.problems], ['⚠', u.warnings], ['✓', u.ok], ['·', u.notes]]) {
    for (const item of list) {
      const line = `${mark} ${cut(clean(item).replace(/\s+/g, ' ').trim(), 500)}`;
      if (size + line.length > 12000) {
        more++;
        continue;
      }
      lines.push(line);
      size += line.length + 1;
    }
  }
  if (more) lines.push(`… и ещё ${more}`);
  return lines.length ? block(lines.join('\n'), 'text') : '';
}

function deviceRows(d, updater) {
  const c = (v, max) => (str(v) ? code(v, max) : null);
  const gpus = (Array.isArray(d.gpu) ? d.gpu : []).filter(isObject).slice(0, 4)
    .map((g) => join(c(g.name) || c(g.id, 20), str(g.driver) && `драйвер ${code(g.driver, 40)}`)).filter(Boolean);
  const kernels = (Array.isArray(d.kernels) ? d.kernels : []).filter(str).slice(0, 8);
  const net = isObject(d.network) ? d.network : {};
  const netType = ['Wi-Fi', 'кабель', 'другое', 'нет сети'].includes(net.type) ? net.type : c(net.type, 30);
  const bat = isObject(d.battery) ? d.battery : null;
  const free = isObject(d.free_gib) ? d.free_gib : {};
  return [
    ['Модель', join(c([d.vendor, d.model].filter(str).join(' ')), c(d.model_version, 60),
      d.chromebook === true && 'хромбук')],
    ['Процессор', join(c(d.cpu), int(d.cores) && `${d.cores} ${plural(d.cores, 'ядро', 'ядра', 'ядер')}`, c(d.arch, 20))],
    ['Память', real(d.ram_gib) !== null && `${num(d.ram_gib)} ГиБ`],
    ['Видеокарта', join(gpus.join('; '), str(d.nvidia) && `NVIDIA ${code(d.nvidia, 30)}`)],
    ['Ядро', join(c(d.kernel, 60), kernels.length && `установлены: ${kernels.map((k) => code(k, 40)).join(', ')}`)],
    ['Дистрибутив', c(d.distro, 40)],
    ['Рабочий стол', join(c(d.desktop, 60), c(d.session, 20))],
    ['Терминал', c(d.terminal, 60)],
    ['Экран', join(c(d.screen, 20), int(d.monitors) && `мониторов: ${d.monitors}`)],
    ['Сеть', join(netType, str(net.adapter) && `адаптер ${code(net.adapter)}`, str(net.driver) && `драйвер ${code(net.driver, 40)}`,
      real(net.signal_dbm) !== null && `сигнал ${num(net.signal_dbm, 0)} дБм`,
      real(net.speed_mbps) !== null && `${num(net.speed_mbps, 0)} Мбит/с`)],
    ['Батарея', d.battery === null ? 'нет' : bat && real(bat.percent) !== null
      && join(`${num(bat.percent, 0)} %`, bat.discharging === true ? 'разряжается' : bat.discharging === false && 'от сети')],
    ['Свободно', join(...['/', '/boot'].map((p) => real(free[p]) !== null && `${code(p)} ${num(free[p])} ГиБ`))],
    ['Версия апдейтера', join(c(updater.commit, 40), c(updater.date, 20))],
    ['arch-update', join(c(d.arch_update, 40), str(d.pacman) && `pacman ${code(d.pacman, 40)}`)],
    ['AUR-помощник', str(d.aur_helper) ? code(d.aur_helper, 20) : d.aur_helper === '' && 'нет'],
    ['Secure Boot', d.secure_boot === true ? 'включён' : d.secure_boot === false && 'выключен'],
    ['/boot читается', d.boot_readable === true ? 'да' : d.boot_readable === false && 'нет'],
  ];
}

function packageList(label, list) {
  if (!list.length) return `- **${label}:** —`;
  let out = '';
  let shown = 0;
  for (const p of list) {
    const piece = `${shown ? ', ' : ''}${code(p, 100)}`;
    if (out.length + piece.length > 6000) break;
    out += piece;
    shown++;
  }
  const more = list.length - shown;
  return `- **${label} (${list.length}):** ${out}${more ? ` … и ещё ${more}` : ''}`;
}

// Index where the last `n` lines of `text` begin.
function lastLines(text, n) {
  let at = text.length;
  for (let i = 0; i < n; i++) {
    const nl = at > 0 ? text.lastIndexOf('\n', at - 1) : -1;
    if (nl < 0) return 0;
    at = nl;
  }
  return at + 1;
}

function countLines(text) {
  let n = 1;
  for (let i = text.indexOf('\n'); i >= 0 && i < text.length - 1; i = text.indexOf('\n', i + 1)) n++;
  return n;
}

// The end of the log in a collapsed block of at most `room` characters.
function logTail(log, room) {
  const text = log.endsWith('\n') ? log.slice(0, -1) : log;
  if (!text) return '';
  const fit = room - 300; // the block's own lines
  if (fit < 500) return '_Конец лога сюда не поместился — он весь в файле по ссылке выше._';
  const total = countLines(text);
  let tail = clean(text.slice(lastLines(text, TAIL)));
  let first = '';
  if (tail.length > fit) {
    const nl = tail.indexOf('\n', tail.length - fit);
    if (nl >= 0) tail = tail.slice(nl + 1);
    else {
      tail = tail.slice(-fit).replace(/^[\udc00-\udfff]/, '');
      first = ', начало строки обрезано';
    }
  }
  const lines = countLines(tail);
  const what = lines < total ? `Конец лога: последние ${lines} ${plural(lines, 'строка', 'строки', 'строк')} из ${total}`
    : `Лог целиком: ${total} ${plural(total, 'строка', 'строки', 'строк')}`;
  return details(`${what}${first}`, block(tail, 'text'));
}

function issueBody(r, verdict, logUrl) {
  const u = r.update;
  const { Y, MO, D, H, MI, SE } = r.when;
  const top = [
    `### ${verdict.headline}`,
    `Отчёт ${code(r.id)} · ${D}.${MO}.${Y} ${H}:${MI}:${SE} UTC · ${logUrl ? `[лог целиком](${logUrl})` : 'лог пустой'}`,
    table('Обновление', updateRows(u)),
    findings(u),
    table('Устройство', deviceRows(r.device, isObject(r.updater) ? r.updater : {})),
    ['#### Пакеты', '', packageList('Обновлены', u.changed), packageList('Новые', u.added),
      packageList('Удалены', u.removed)].join('\n'),
  ].filter(Boolean).join('\n\n');
  // Backticks and invisible characters only occur inside JSON strings: written as \uXXXX escapes
  // they keep the fence short and the text plain.
  const escape = (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`;
  const json = JSON.stringify(r.device, null, 1).replace(CONTROL, escape).replace(/`/g, escape);
  const end = details('Устройство целиком (JSON)', block(json.length > 12000 ? `${cut(json, 12000)}\n(обрезано)` : json, 'json'));
  // Everything else is short by construction; the log's end takes the room that is left.
  let room = BODY_MAX - top.length - end.length - 8;
  for (let i = 0; i < 4; i++) {
    const body = [top, logTail(r.log, room), end].filter(Boolean).join('\n\n') + '\n';
    if (body.length <= BODY_MAX) return body;
    room -= body.length - BODY_MAX + 200;
  }
  return `${top}\n\n${end}\n`;
}

function reply(status, data, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...headers },
  });
}
