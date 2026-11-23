// Pixel Bot dashboard interactions. Pure progressive enhancement:
// every page works the same if this file fails to load.
(function () {
  'use strict';
  var d = document, root = d.documentElement;
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function ready(fn) { d.readyState !== 'loading' ? fn() : d.addEventListener('DOMContentLoaded', fn); }

  // ---- light / dark switch (remembered in localStorage, applied early by theme-init.js)
  var KEY = 'pixel-theme';
  function isLight() { return root.getAttribute('data-theme') === 'light'; }
  function save(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }
  var SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square" aria-hidden="true"><rect x="8" y="8" width="8" height="8"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2"/></svg>';
  var MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="miter" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>';

  ready(function () {
    var b = d.createElement('button');
    b.type = 'button'; b.className = 'theme-toggle';
    function paint() {
      b.innerHTML = isLight() ? MOON : SUN;
      b.setAttribute('aria-label', isLight() ? 'Switch to dark mode' : 'Switch to light mode');
      b.title = isLight() ? 'Dark mode' : 'Light mode';
    }
    b.addEventListener('click', function () {
      if (isLight()) root.removeAttribute('data-theme'); else root.setAttribute('data-theme', 'light');
      save(isLight() ? 'light' : 'dark'); paint();
    });
    paint();
    var side = d.querySelector('.sidebar'), top = d.querySelector('.topbar');
    if (side) { var lg = side.querySelector('.legal-links'); side.insertBefore(b, lg || null); }
    else if (top) { top.insertBefore(b, top.lastElementChild && top.children.length > 1 ? top.lastElementChild : null); }
    else { b.classList.add('floating'); d.body.appendChild(b); }

    // keep the active nav item visible in the scrollable strip on mobile
    var a = d.querySelector('.sidebar .nav-item.active');
    if (a && a.scrollIntoView) a.scrollIntoView({ inline: 'center', block: 'nearest' });

    // reveal cards below the fold as they scroll into view
    if (reduce || !('IntersectionObserver' in window)) return;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) { en.target.classList.add('nx-in'); io.unobserve(en.target); } });
    }, { threshold: 0.05, rootMargin: '0px 0px -30px 0px' });
    d.querySelectorAll('.t-card, .grid2 > *, .p-table, .perm-group, .act-grid > *, .proof-grid > *, .stat').forEach(function (el, i) {
      if (el.getBoundingClientRect().top > window.innerHeight) {
        el.classList.add('nx-reveal');
        el.style.setProperty('--d', (i % 4) * 50 + 'ms');
        io.observe(el);
      }
    });
  });
})();
