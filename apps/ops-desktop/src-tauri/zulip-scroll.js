// WebKitGTK 2.52.5 can trap wheel input over Zulip's clipped message headers
// when the document uses overscroll-behavior:none. Native-wheel regression:
// scrollY remained 826 without this override and moved to 138 with it.
// Keep nested scroll areas and modal overflow locks under Zulip's control.
(() => {
  if (location.origin !== "https://zulip.ops.local:8443") return;
  const apply = () => document.documentElement.style.setProperty("overscroll-behavior", "auto", "important");
  if (document.documentElement) apply();
  else document.addEventListener("DOMContentLoaded", apply, {once: true});
})();
