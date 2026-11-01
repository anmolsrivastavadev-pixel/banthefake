// Result window: shows one scan job from storage.session. Builds DOM nodes with text only, never HTML strings.
import { describeError, describeReport, helpUrl } from './lib.js';

const api = globalThis.browser ?? globalThis.chrome;
const id = location.hash.slice(1);
const verdictEl = document.getElementById('verdict');
const signalsEl = document.getElementById('signals');
const sectionsEl = document.getElementById('sections');
const statusEl = document.getElementById('status');
const privacyEl = document.getElementById('privacy');
const helpEl = document.getElementById('help');

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null && c !== '') node.append(c);
  return node;
}

function setVerdict(cls, title, ...lines) {
  verdictEl.className = `verdict ${cls}`;
  verdictEl.replaceChildren(el('span', { class: 'dot' }), el('div', {}, el('b', {}, title), ...lines.filter(Boolean).map(t => el('span', { class: 'muted' }, t))));
  statusEl.textContent = [title, ...lines].filter(Boolean).map(t => (/[.!?…]$/.test(t) ? t : `${t}.`)).join(' ');
}

function render(job) {
  if (job.server) {
    helpEl.href = helpUrl(job.server);
    privacyEl.textContent = `Images are sent only to ${new URL(job.server).host} (your configured server), and only when you `
      + 'choose "Check with BanTheFake". The extension sends nothing else and collects no analytics.';
  }
  if (!job.server) helpEl.hidden = true;
  signalsEl.replaceChildren();
  sectionsEl.replaceChildren();
  if (job.status === 'loading') {
    setVerdict('loading', 'Scanning…', job.imageHost ? `Image from ${job.imageHost}` : '');
    return;
  }
  if (job.status === 'error') {
    const { title, detail } = describeError(job.code, job.message);
    setVerdict('caution', title, detail);
    return;
  }
  const view = describeReport(job.report);
  if (!view) {
    const { title, detail } = describeError('bad_response');
    setVerdict('caution', title, detail);
    return;
  }
  setVerdict(view.cls, view.title, view.detail, view.caveat);
  signalsEl.replaceChildren(...view.signals.map(s => el('li', {}, el('span', { class: `tag ${s.tag}` }, s.label), el('span', {}, s.text))));
  for (const section of view.sections) {
    const dl = el('dl');
    for (const [k, v] of section.rows) dl.append(el('dt', {}, k), el('dd', {}, v));
    sectionsEl.append(el('section', { class: 'block' }, el('h2', {}, section.title), dl, el('p', { class: 'note' }, section.note)));
  }
}

let seen = false;

async function load() {
  const data = await api.storage.session.get(id);
  if (data[id]) {
    seen = true;
    render(data[id]);
    return;
  }
  // The job may still be on its way from the background worker; only give up after a moment.
  setTimeout(() => {
    if (!seen) setVerdict('caution', 'Result not available', 'This result has expired. Right-click the image and choose "Check with BanTheFake" again.');
  }, 3000);
}

api.storage.onChanged.addListener((changes, area) => {
  if (area === 'session' && changes[id]?.newValue) {
    seen = true;
    render(changes[id].newValue);
  }
});
load();
