// Проверка страниц из docs/share настоящим браузером.
//
// Каждую страницу открываем дважды:
//   1) как файл с диска — так её откроет тот, кому её переслали;
//   2) через сервер, который НАМЕРЕННО врёт: отдаёт заголовок
//      «charset=windows-1251». Так ведут себя почтовые клиенты и
//      файлообменники, и именно на этом обычно ломается русский текст.
//
// Требуем от каждой: режим CSS1Compat (не старый BackCompat),
// кодировка UTF-8, язык ru, ноль кракозябр, ноль ошибок js.
// Контрольный опыт: те же страницы без обёртки (docs/*.html) через
// врущий сервер читаются как windows-1251 и рассыпаются в «РњРѕР·Рі» —
// значит проверка не пустая, она ловит реальную поломку.
//
// Запуск:  node scripts/check_share_pages.js
const http = require('http');
const fs = require('fs');
const path = require('path');
const {execSync} = require('child_process');

// playwright ставят то локально, то глобально — ищем оба варианта,
// чтобы скрипт запускался из любой папки.
function loadChromium() {
  const roots = [];
  try { roots.push(execSync('npm root -g', {encoding: 'utf8'}).trim()); } catch (e) {}
  for (const name of ['playwright-core', 'playwright']) {
    for (const base of [null, ...roots]) {
      try {
        return require(base ? path.join(base, name) : name).chromium;
      } catch (e) { /* пробуем следующий */ }
    }
  }
  console.error('не найден playwright — поставьте: npm i -g playwright');
  process.exit(2);
}
const chromium = loadChromium();

// браузер лежит в /opt/pw-browsers, версия в имени папки меняется
function findChrome() {
  const dir = process.env.PLAYWRIGHT_BROWSERS_PATH || '/opt/pw-browsers';
  if (!fs.existsSync(dir)) return undefined;
  const hit = fs.readdirSync(dir)
    .filter(d => d.startsWith('chromium'))
    .map(d => path.join(dir, d, 'chrome-linux', 'chrome'))
    .find(p => fs.existsSync(p));
  return hit;  // undefined → playwright возьмёт свой по умолчанию
}

const ROOT = '/home/user/tessent-test_new/tessent_brain/docs/share/';
const LANGS = ['ru', 'en'];
const files = ['index', 'tessent_capabilities', 'tessent_article', 'tessent_map', 'tessent_cost'];
// проверяем оба языка: набор двуязычный
const MOJI = /Ð[-¿]|Ñ[-¿]|[Ð-Ñ][-¿]/;

// Враждебный сервер: намеренно врёт про кодировку.
const server = http.createServer((req, res) => {
  const name = req.url.replace(/^\//, '') || 'index.html';
  const p = path.join(ROOT, name);
  if (!fs.existsSync(p)) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, {'Content-Type': 'text/html; charset=windows-1251'});
  res.end(fs.readFileSync(p));
});

const probe = () => ({
  mode: document.compatMode,
  charset: document.characterSet,
  lang: document.documentElement.lang || '(нет)',
  title: document.title.slice(0, 40),
  moji: /[\u00C0-\u00FF][\u0080-\u00BF]/.test(document.body.innerText) || /[\u0402-\u040F\u0452-\u045F]/.test(document.body.innerText),
  sample: document.body.innerText.replace(/\s+/g, ' ').slice(0, 44),
  errs: window.__errs || [],
});

(async () => {
  await new Promise(r => server.listen(8731, r));
  const b = await chromium.launch({
    executablePath: findChrome(),
    args: ['--no-sandbox']});
  let bad = 0;

  for (const mode of ['file', 'http-lying']) {
    console.log('\n### ' + (mode === 'file'
      ? 'открыт двойным кликом (file://)'
      : 'отдан сервером, который врёт: charset=windows-1251'));
    for (const lang of LANGS) {
    for (const f of files) {
      const p = await b.newPage({viewport: {width: 1100, height: 800}});
      await p.addInitScript(() => {
        window.__errs = [];
        window.addEventListener('error', e => window.__errs.push(String(e.message)));
      });
      const url = mode === 'file'
        ? 'file://' + ROOT + lang + '/' + f + '.html'
        : 'http://127.0.0.1:8731/' + lang + '/' + f + '.html';
      await p.goto(url);
      await p.waitForTimeout(600);
      const r = await p.evaluate(probe);
      const ok = r.mode === 'CSS1Compat' && r.charset === 'UTF-8'
        && r.lang === lang && !r.moji && r.errs.length === 0;
      if (!ok) bad++;
      console.log((ok ? '  ok  ' : '  ПЛОХО ') + (lang + '/' + f).padEnd(28),
        r.mode, r.charset, '| lang:' + r.lang,
        '| кракозябры:' + (r.moji ? 'ДА' : 'нет'),
        '| ошибок js:' + r.errs.length, '|', r.sample);
      await p.close();
    }
    }
  }
  await b.close();
  server.close();
  console.log(bad === 0 ? '\nВСЁ ЧИСТО' : `\nПРОБЛЕМ: ${bad}`);
  process.exit(bad === 0 ? 0 : 1);
})();
