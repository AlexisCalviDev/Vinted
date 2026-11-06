// Reads Vinted catalog pages in headless Chromium (Vinted's anti-bot refuses
// plain HTTP clients) and prints the listings found, as JSON, on stdout.
//   node vinted_fetch.js queries.json   ->  {"<query text>": [items...], "_errors": {...}}
// queries.json: [{"text": "barbour", "catalog": 5}, ...]
const fs = require('fs');
const { chromium } = require('playwright');

const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const queries = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const out = { _errors: {} };
  const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
  const ctx = await browser.newContext({ locale: 'fr-FR', userAgent: UA, viewport: { width: 1366, height: 900 } });
  // Only the HTML is needed: skip fonts, media and third-party assets.
  await ctx.route('**/*', (route) => {
    const t = route.request().resourceType();
    const u = route.request().url();
    if (['image', 'media', 'font'].includes(t) || !/^https:\/\/www\.vinted\.fr\//.test(u) && t !== 'document') return route.abort();
    return route.continue();
  });
  const page = await ctx.newPage();
  for (const q of queries) {
    const url = 'https://www.vinted.fr/catalog?order=newest_first&search_text=' + encodeURIComponent(q.text) +
      '&catalog[]=' + encodeURIComponent(q.catalog);
    try {
      // The first page of a session often gets Cloudflare's challenge, which
      // clears by itself: retry the same page a couple of times.
      let ok = false;
      for (let attempt = 0; attempt < 3 && !ok; attempt++) {
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
        ok = await page.waitForSelector('a[data-testid$="--overlay-link"]', { state: 'attached', timeout: 15000 })
          .then(() => true, () => false);
        if (!ok) await sleep(3000);
      }
      if (!ok) throw new Error('no listings on page (anti-bot challenge or empty search)');
      out[q.text] = await page.$$eval('a[data-testid$="--overlay-link"]', (links) => links.map((a) => {
        const m = (a.getAttribute('data-testid') || '').match(/product-item-id-(\d+)/);
        const box = a.parentElement;
        const img = box && box.querySelector('img');
        return {
          id: m ? m[1] : null,
          href: a.href,
          label: a.getAttribute('title') || '',
          img: img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '',
          alt: img ? (img.getAttribute('alt') || '') : '',
        };
      }).filter((x) => x.id));
    } catch (e) {
      out._errors[q.text] = String(e && e.message || e).split('\n')[0];
    }
    await sleep(1200 + Math.random() * 1500);
  }
  await browser.close();
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
