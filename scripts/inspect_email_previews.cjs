// Local browser QA only. Requires Playwright; never calls an email provider.
const { chromium } = require('playwright');
const path = require('path');
const { pathToFileURL } = require('url');

(async () => {
  const directory = path.resolve(process.argv[2] || 'docs/generated-email-previews');
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  });
  try {
    for (const width of [1000, 360]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } });
      // Previews are deliberately self-contained; no remote requests during QA.
      await page.route('https://**/*', route => route.abort());
      for (const name of ['friendly', 'formal', 'meeting']) {
        await page.goto(pathToFileURL(path.join(directory, `${name}.html`)).href);
        await page.screenshot({ path: path.join(directory, `${name}-${width}.png`), fullPage: true });
        const layout = await page.evaluate(() => ({
          overflow: document.documentElement.scrollWidth > innerWidth,
          cardWidth: document.querySelectorAll('table')[1].getBoundingClientRect().width,
          direction: document.documentElement.dir,
        }));
        if (layout.overflow || layout.cardWidth > 602 || layout.direction !== 'rtl') {
          throw new Error(`Invalid layout: ${name} ${width} ${JSON.stringify(layout)}`);
        }
        console.log(name, width, JSON.stringify(layout));
      }
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
