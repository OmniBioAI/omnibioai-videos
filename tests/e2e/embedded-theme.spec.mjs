import fs from 'node:fs';
import { expect, test } from '@playwright/test';

const BASE = 'http://127.0.0.1:4178/';
const CATALOG = [
  { filename: 'tutorial.mp4', title: 'RNA-seq workflow tutorial', desc: 'Build and run a reproducible analysis.', tag: 'tutorial', category: 'Workflows', featured: true, duration: 245, order: 1 },
  { filename: 'platform.mp4', title: 'Explore OmniBioAI', desc: 'Learn the platform fundamentals.', tag: 'intro', category: 'Getting Started', duration: 125, order: 2 },
];

async function openEmbedded(page, width, theme, accent) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.route('**/videos.json', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify(CATALOG) }));
  await page.route('**/*.mp4*', route => route.abort());
  await page.goto(BASE);
  await page.setContent(`<iframe title="Video Tutorials" src="${BASE}?obai_theme=${theme}&obai_accent=${accent}&obai_density=comfortable&obai_motion=reduce" style="position:fixed;inset:0;width:100%;height:100%;border:0"></iframe>`);
  const frame = page.frameLocator('iframe');
  await expect(frame.locator('.hero-title')).toHaveText('RNA-seq workflow tutorial');
  return frame;
}

async function contrast(frame, foregroundSelector, backgroundSelector) {
  return frame.locator(foregroundSelector).evaluate((node, bgSelector) => {
    const parse = value => (value.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    const luminance = channels => channels.map(value => {
      const c = value / 255;
      return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    }).reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
    const fg = parse(getComputedStyle(node).color);
    const bg = parse(getComputedStyle(document.querySelector(bgSelector)).backgroundColor);
    const [lighter, darker] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
    return (lighter + 0.05) / (darker + 0.05);
  }, backgroundSelector);
}

for (const appearance of [
  { theme: 'light', accent: 'purple', expected: 'rgb(112, 48, 160)' },
  { theme: 'dark', accent: 'teal', expected: 'rgb(0, 229, 160)' },
]) {
  for (const width of [1440, 390]) {
    test(`embedded ${appearance.theme}+${appearance.accent} at ${width}px is AA and themed`, async ({ page }) => {
      const frame = await openEmbedded(page, width, appearance.theme, appearance.accent);
      const root = frame.locator('html');
      await expect(root).toHaveAttribute('data-embedded', 'true');
      await expect(root).toHaveAttribute('data-theme', appearance.theme);
      await expect(root).toHaveAttribute('data-accent', appearance.accent);
      await expect(frame.locator('.topbar')).toBeHidden();

      for (const [foreground, background] of [
        ['.page-heading h1', 'body'],
        ['.page-heading p', 'body'],
        ['.hero-title', 'body'],
        ['.hero-desc', 'body'],
      ]) {
        expect(await contrast(frame, foreground, background), `${foreground} contrast`).toBeGreaterThanOrEqual(4.5);
      }

      const all = frame.getByRole('button', { name: 'All', exact: true });
      await all.click();
      const selected = await all.evaluate(node => ({
        background: getComputedStyle(node).backgroundColor,
        border: getComputedStyle(node).borderTopColor,
      }));
      expect(selected.background).toBe(appearance.expected);
      expect(selected.border).toBe(appearance.expected);
      expect(await contrast(frame, '.filter-btn.active', '.filter-btn.active')).toBeGreaterThanOrEqual(4.5);

      fs.mkdirSync('/tmp/n11a-videos-screenshots', { recursive: true });
      await page.locator('iframe').screenshot({ path: `/tmp/n11a-videos-screenshots/videos-${appearance.theme}-${appearance.accent}-${width}.png` });
    });
  }
}

test('standalone portal keeps its top bar', async ({ page }) => {
  await page.route('**/videos.json', route => route.fulfill({ contentType: 'application/json', body: '[]' }));
  await page.goto(`${BASE}?obai_theme=light&obai_accent=purple`);
  await expect(page.locator('html')).toHaveAttribute('data-embedded', 'false');
  await expect(page.locator('.topbar')).toBeVisible();
});
