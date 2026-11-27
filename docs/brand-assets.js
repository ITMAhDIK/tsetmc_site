(() => {
  const loader = document.getElementById('tloomberg-page-loader');
  if (!loader) return;

  const startedAt = Date.now();
  let hideScheduled = false;

  const hideLoader = () => {
    if (hideScheduled) return;
    hideScheduled = true;

    // Keep the animated mark visible briefly, but never block a slow/offline page.
    const minimumDisplayMs = 420;
    const delay = Math.max(0, minimumDisplayMs - (Date.now() - startedAt));

    window.setTimeout(() => {
      loader.classList.add('is-hidden');
      loader.setAttribute('aria-hidden', 'true');
      window.setTimeout(() => loader.remove(), 360);
    }, delay);
  };

  if (document.readyState === 'complete') {
    hideLoader();
  } else {
    window.addEventListener('load', hideLoader, { once: true });
  }

  // A failed external resource must not leave a permanent full-screen overlay.
  window.setTimeout(hideLoader, 10000);
})();
