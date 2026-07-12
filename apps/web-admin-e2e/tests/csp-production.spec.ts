import { expect, test } from '@playwright/test';

test('production shell hydrates without CSP or console violations', async ({ page }) => {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const cspViolations: string[] = [];

  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', event => {
      (window as Window & { __omniCspViolations?: string[] }).__omniCspViolations ??= [];
      (window as Window & { __omniCspViolations?: string[] }).__omniCspViolations?.push(
        `${event.violatedDirective}:${event.blockedURI}`,
      );
    });
  });

  const response = await page.goto('/', { waitUntil: 'networkidle' });
  expect(response?.status()).toBe(200);
  await expect(page.getByRole('heading', { name: /AI 助理/ })).toBeVisible();
  await expect(page.locator('main')).toHaveAttribute('class', /app-shell/);

  const csp = response?.headers()['content-security-policy'] || '';
  expect(csp).toContain("script-src 'self' 'nonce-");
  expect(csp).toContain("style-src 'self' 'nonce-");
  expect(csp).not.toContain("'unsafe-inline'");
  expect(csp).toContain("require-trusted-types-for 'script'");

  cspViolations.push(...await page.evaluate(() => (
    (window as Window & { __omniCspViolations?: string[] }).__omniCspViolations || []
  )));
  expect(pageErrors, `page errors: ${pageErrors.join('\n')}`).toEqual([]);
  expect(consoleErrors, `console errors: ${consoleErrors.join('\n')}`).toEqual([]);
  expect(cspViolations, `CSP violations: ${cspViolations.join('\n')}`).toEqual([]);
});

test('unimplemented Web Admin controls are explicitly disabled', async ({ page }) => {
  await page.goto('/', { waitUntil: 'networkidle' });
  await expect(page.getByRole('button', { name: /搜索 · 未启用/ })).toBeDisabled();
  await expect(page.getByRole('button', { name: /已安排 · 未启用/ })).toBeDisabled();
  await expect(page.getByRole('button', { name: /插件 · 未启用/ })).toBeDisabled();
});
