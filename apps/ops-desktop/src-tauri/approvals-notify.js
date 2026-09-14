(() => {
  if (location.origin !== 'https://autorisations.example.org' || window !== window.top || window.__opsNotifications) return;
  window.__opsNotifications = true;
  window.dispatchEvent(new Event('ops-native-notifications-ready'));
  const seen = new Set();
  let busy = false;
  async function check() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch('/api/feed', {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      if (!Array.isArray(data.items)) return;
      const fresh = data.items.filter(item => item.status === 'pending' && Number.isSafeInteger(item.id) && item.id > 0 && !seen.has(item.id));
      for (const item of fresh) seen.add(item.id);
      if (seen.size > 1000) { const latest = [...seen].slice(-500); seen.clear(); latest.forEach(id => seen.add(id)); }
      if (fresh.length) window.webkit.messageHandlers.opsApprovalsPending.postMessage(JSON.stringify({pending: fresh.length}));
    } catch (_) { /* No authenticated data is logged or cached. */ }
    finally { busy = false; }
  }
  setInterval(check, 15000);
  check();
})();
