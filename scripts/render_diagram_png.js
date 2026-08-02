// render_diagram_png.js — load a MoiraiCore diagram in the live dashboard and screenshot its canvas.
// Usage: node render_diagram_png.js <name-substring> <outfile.png>
const puppeteer = require('puppeteer');

(async () => {
  const nameSub = process.argv[2] || 'BYOD';
  const out = process.argv[3] || '/tmp/diagram.png';
  const base = process.env.MOIRAI_BASE || 'http://localhost:7878';

  const browser = await puppeteer.launch({
    headless: 'shell',
    executablePath: process.env.PUPPETEER_EXEC || undefined,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 2 });
  await page.goto(base + '/', { waitUntil: 'networkidle2', timeout: 60000 });

  // Login as diag_qa
  await page.waitForSelector('#auth-username', { timeout: 15000 });
  await page.type('#auth-username', 'diag_qa');
  await page.type('#auth-password', 'diag_temp_123');
  await page.click('button[onclick="authDoLogin()"]');
  await page.waitForFunction(() => typeof window.go === 'function' || document.querySelector('[data-view="diagrams"]'), { timeout: 15000 });

  // Open Diagrams view via nav click
  await page.evaluate(() => {
    const el = document.querySelector('[data-view="diagrams"]');
    if (el) el.click();
  });
  await page.waitForSelector('#diagram-select', { timeout: 15000 });

  // Select the matching diagram
  const ok = await page.evaluate((sub) => {
    const sel = document.getElementById('diagram-select');
    const opt = Array.from(sel.options).find(o => o.textContent.includes(sub));
    if (!opt) return false;
    sel.value = opt.value;
    if (typeof loadSelectedDiagram === 'function') loadSelectedDiagram();
    return true;
  }, nameSub);
  if (!ok) { console.error('NO_MATCH:' + nameSub); await browser.close(); process.exit(2); }

  // Wait for Excalidraw canvas to paint
  await page.waitForFunction(() => {
    const c = document.querySelector('#excalidraw-mount canvas');
    if (!c) return false;
    return c.width > 100 && c.height > 100;
  }, { timeout: 20000 });

  await new Promise(r => setTimeout(r, 1500));
  const el = await page.$('#excalidraw-mount');
  await el.screenshot({ path: out });
  console.log('OK:' + out);
  await browser.close();
})().catch(e => { console.error('ERR:' + e.message); process.exit(1); });
