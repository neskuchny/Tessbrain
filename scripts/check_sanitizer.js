// Проверка frontend/lib/sanitizeHtml.ts настоящим браузером.
//
// Санитайзер живёт на DOMParser, поэтому проверять его в node без DOM
// бессмысленно — нужен реальный движок. Здесь Chromium открывает пустую
// страницу, в неё вставляется код санитайзера (TypeScript-обвязка снята
// простой заменой — сам код чистый JS), и по нему прогоняются известные
// приёмы обхода: закрытие тега изнутри, обработчики событий, javascript: в
// ссылке, svg/iframe, data-URI, разорванная схема.
//
// Главная проверка не «строка изменилась», а поведенческая: вставляем
// результат в живой DOM и смотрим, не выполнилось ли что-нибудь.
//
// Запуск:  node scripts/check_sanitizer.js
const fs = require('fs');
const path = require('path');
const {execSync} = require('child_process');

function loadChromium() {
  const roots = [];
  try { roots.push(execSync('npm root -g', {encoding: 'utf8'}).trim()); } catch (e) {}
  for (const name of ['playwright-core', 'playwright']) {
    for (const base of [null, ...roots]) {
      try { return require(base ? path.join(base, name) : name).chromium; } catch (e) {}
    }
  }
  console.error('не найден playwright — поставьте: npm i -g playwright');
  process.exit(2);
}
function findChrome() {
  const dir = process.env.PLAYWRIGHT_BROWSERS_PATH || '/opt/pw-browsers';
  if (!fs.existsSync(dir)) return undefined;
  return fs.readdirSync(dir).filter(d => d.startsWith('chromium'))
    .map(d => path.join(dir, d, 'chrome-linux', 'chrome')).find(p => fs.existsSync(p));
}

const SRC = path.join(__dirname, '..', 'frontend', 'lib', 'sanitizeHtml.ts');

// TS → JS: снимаем аннотации типов. Файл написан так, чтобы этого хватало.
function toJs(ts) {
  return ts
    .replace(/^export default sanitizeHtml\s*$/m, '')
    .replace(/^export /gm, '')
    .replace(/const ALLOWED_ATTRS: Record<string, Set<string>>/, 'const ALLOWED_ATTRS')
    .replace(/\(value: string, allowData: boolean\): boolean/, '(value, allowData)')
    .replace(/\(dirty: string \| null \| undefined\): string/, '(dirty)')
    .replace(/let doc: Document/, 'let doc')
    .replace(/\(node: Element\): void/, '(node)');
}

// [название, вход, что не должно случиться]
const CASES = [
  ['голый script', '<script>window.__hit=1</script>жив текст'],
  ['script внутри тега', '<div><script>window.__hit=1</script>x</div>'],
  ['вложенный script', '<scr<script>ipt>window.__hit=1</scr</script>ipt>'],
  ['img onerror', '<img src=x onerror="window.__hit=1">'],
  ['svg onload', '<svg onload="window.__hit=1"></svg>'],
  ['body onload через тег', '<body onload="window.__hit=1">t</body>'],
  ['iframe srcdoc', '<iframe srcdoc="<script>parent.__hit=1</script>"></iframe>'],
  ['iframe javascript:', '<iframe src="javascript:parent.__hit=1"></iframe>'],
  ['ссылка javascript:', '<a href="javascript:window.__hit=1">клик</a>'],
  ['ссылка JaVaScRiPt:', '<a href="JaVaScRiPt:window.__hit=1">клик</a>'],
  ['ссылка с разрывом схемы', '<a href="java\nscript:window.__hit=1">клик</a>'],
  ['ссылка с &#x09 в схеме', '<a href="java&#x09;script:window.__hit=1">клик</a>'],
  ['data:text/html в ссылке', '<a href="data:text/html,<script>1</script>">клик</a>'],
  ['object data', '<object data="javascript:window.__hit=1"></object>'],
  ['style с выражением', '<div style="background:url(javascript:window.__hit=1)">t</div>'],
  ['form action', '<form action="/x"><input name=q></form>'],
  ['meta refresh', '<meta http-equiv="refresh" content="0;url=//evil">'],
  ['base href', '<base href="//evil/">'],
  ['onmouseover на разрешённом теге', '<p onmouseover="window.__hit=1">навести</p>'],
  ['onfocus+autofocus', '<p onfocus="window.__hit=1" autofocus>x</p>'],
];

// Что обязано ВЫЖИТЬ — санитайзер не должен ломать нормальные документы.
const KEEP = [
  ['заголовок', '<h2>Регламент</h2>', 'h2'],
  ['абзац и жирный', '<p>текст <strong>важно</strong></p>', 'strong'],
  ['список', '<ul><li>раз</li><li>два</li></ul>', 'li'],
  ['таблица', '<table><tr><td colspan="2">я</td></tr></table>', 'td'],
  ['цитата', '<blockquote>цитата</blockquote>', 'blockquote'],
  ['обычная ссылка', '<a href="https://example.com">сайт</a>', 'a'],
  ['относительная ссылка', '<a href="/docs/1">док</a>', 'a'],
  ['картинка data-URI', '<img src="data:image/png;base64,iVBORw0KGgo=">', 'img'],
  ['класс вёрстки', '<div class="prose">т</div>', 'div'],
];

(async () => {
  const chromium = loadChromium();
  const js = toJs(fs.readFileSync(SRC, 'utf8'));
  const b = await chromium.launch({executablePath: findChrome(), args: ['--no-sandbox']});
  const p = await b.newPage();
  await p.goto('about:blank');
  await p.addScriptTag({content: js});

  let bad = 0;
  console.log('приёмы обхода — ничего не должно выполниться:');
  for (const [name, dirty] of CASES) {
    const r = await p.evaluate(({dirty}) => {
      window.__hit = 0;
      const out = sanitizeHtml(dirty);
      const host = document.createElement('div');
      document.body.appendChild(host);
      host.innerHTML = out;                       // вставляем В ЖИВОЙ DOM
      const evt = host.querySelector('*');
      if (evt) { evt.dispatchEvent(new Event('mouseover')); evt.dispatchEvent(new Event('focus')); }
      const res = {hit: window.__hit, out, tags: [...host.querySelectorAll('*')].map(e => e.tagName.toLowerCase())};
      host.remove();
      return res;
    }, {dirty});
    const danger = ['script', 'iframe', 'object', 'embed', 'svg', 'form', 'meta', 'base', 'link'];
    const leaked = r.tags.filter(t => danger.includes(t));
    const jsUrl = /javascript\s*:/i.test(r.out.replace(/&#x?[0-9a-f]+;?/gi, ''));
    const onAttr = /\son[a-z]+\s*=/i.test(r.out);
    const ok = !r.hit && leaked.length === 0 && !jsUrl && !onAttr;
    if (!ok) bad++;
    console.log((ok ? '  ok   ' : '  ПЛОХО ') + name.padEnd(28) +
      (ok ? '' : ` hit=${r.hit} теги=${leaked} → ${r.out.slice(0, 90)}`));
  }

  console.log('\nобычная разметка — должна уцелеть:');
  for (const [name, clean, mustHave] of KEEP) {
    const r = await p.evaluate(({clean, mustHave}) => {
      const out = sanitizeHtml(clean);
      const host = document.createElement('div');
      host.innerHTML = out;
      const has = !!host.querySelector(mustHave);
      const text = host.textContent.trim();
      return {out, has, text};
    }, {clean, mustHave});
    // у картинки текста нет по определению — требуем его только там, где он был
    const ok = r.has && (mustHave === 'img' || r.text.length > 0);
    if (!ok) bad++;
    console.log((ok ? '  ok   ' : '  ПЛОХО ') + name.padEnd(28) + (ok ? '' : `→ ${r.out}`));
  }

  await b.close();
  console.log(bad === 0 ? '\nВСЁ ЧИСТО' : `\nПРОБЛЕМ: ${bad}`);
  process.exit(bad === 0 ? 0 : 1);
})();
