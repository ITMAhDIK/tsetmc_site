const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..', '..');
const read = relative => fs.readFileSync(path.join(root, relative), 'utf8');
const inlineScripts = html => [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
  .map(match => match[1].trim())
  .filter(Boolean);

test('login and admin pages contain valid inline JavaScript', () => {
  for (const page of ['docs/saas/index.html', 'docs/saas/admin.html']) {
    const scripts = inlineScripts(read(page));
    assert.ok(scripts.length > 0, page + ' has an inline app script');
    scripts.forEach((script, index) => {
      assert.doesNotThrow(() => new vm.Script(script), page + ' script ' + index);
    });
  }
});

test('browser auth uses same-origin API and never falls back to demo authentication', () => {
  const html = read('docs/saas/index.html');
  assert.match(html, /name="tloomberg-auth-api" content="same-origin"/);
  assert.match(html, /window\.location\.hostname\.endsWith\('\.github\.io'\) \? '' : window\.location\.origin/);
  assert.match(html, /const DEMO_OTP_MODE = false/);
  assert.doesNotMatch(html, /const DEMO_OTP_MODE = true/);
  assert.match(html, /maxlength="6"/);
  assert.match(html, /\/api\/v1\/auth\/phone\/request/);
  assert.match(html, /\/api\/v1\/auth\/phone\/verify/);
  assert.match(html, /PRIVACY_VERSION/);
  assert.match(html, /ثبت‌نام تا زمان انتشار نسخهٔ نهایی/);
});

test('admin console is same-origin, MFA-first and points to real API route families', () => {
  const html = read('docs/saas/admin.html');
  assert.match(html, /name="tloomberg-admin-api" content="same-origin"/);
  assert.match(html, /window\.location\.hostname\.endsWith\('\.github\.io'\) \? '' : window\.location\.origin/);
  assert.match(html, /\/auth\/mfa\/setup/);
  assert.match(html, /\/auth\/mfa\/confirm/);
  assert.match(html, /\/auth\/mfa\/verify/);
  for (const route of [
    '/admin/dashboard', '/admin/users', '/admin/plans',
    '/admin/subscriptions', '/admin/payments/orders',
    '/admin/withdrawals', '/admin/referrals', '/admin/audit-logs',
    '/support/manage/tickets', '/support/manage/faq',
    '/analytics/admin/overview', '/analytics/admin/pages',
    '/analytics/admin/paths',
  ]) {
    assert.ok(html.includes(route), 'admin UI must call ' + route);
  }
  assert.match(html, /credentials:'include'/);
});

test('legal pages are both present and explicitly identify unapproved placeholders', () => {
  const terms = read('docs/saas/terms.html');
  const privacy = read('docs/saas/privacy.html');
  assert.match(terms, /متن نهایی قوانین هنوز/);
  assert.match(privacy, /متن نهایی هنوز/);
  assert.match(privacy, /noindex,nofollow/);
  assert.doesNotMatch(read('docs/saas/index.html'), /href="\.\.\/index\.html"/);
  assert.match(read('docs/saas/index.html'), /https:\/\/itmahdik\.github\.io\/tsetmc_site\//);
});

test('Caddy serves static SaaS pages and proxies only the versioned API prefix', () => {
  const caddy = read('Caddyfile.saas');
  assert.match(caddy, /root \* \/srv\/saas/);
  assert.match(caddy, /handle_path \/api\/v1\/\*/);
  assert.match(caddy, /reverse_proxy saas-api:8000/);
  assert.match(caddy, /@private path \/api\/v1\/internal\/\* \/api\/v1\/notifications\/internal\/\* \/internal\/\*/);
  assert.match(caddy, /handle @private \{\s*respond 404\s*\}/);
  assert.match(caddy, /@identity path \/api\/v1\/auth \/api\/v1\/auth\/\*/);
  assert.match(caddy, /reverse_proxy identity-api:8000/);
  assert.doesNotMatch(caddy, /internal\/metrics/);
});

test('edge image bundles login, admin and both legal pages', () => {
  const docker = read('Dockerfile.saas-edge');
  for (const name of ['index.html', 'admin.html', 'terms.html', 'privacy.html']) {
    assert.ok(docker.includes(name), 'edge image must package ' + name);
  }
});
