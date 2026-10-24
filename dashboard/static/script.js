// Nocturne — dashboard interactions. Pure progressive enhancement:
// the pages work identically if this file fails to load.
(function () {
  'use strict';
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var d = document, root = d.documentElement;

  function ready(fn) { d.readyState !== 'loading' ? fn() : d.addEventListener('DOMContentLoaded', fn); }

  ready(function () {
    // animated light strip across the top
    var strip = d.createElement('div');
    strip.className = 'nx-strip';
    d.body.appendChild(strip);

    // stagger the sidebar entrance
    d.querySelectorAll('.sidebar .nav-item').forEach(function (el, i) { el.style.setProperty('--i', i); });

    if (reduce) return;

    // click ripple on any button
    d.addEventListener('pointerdown', function (e) {
      var b = e.target.closest && e.target.closest('button, .btn, .btn-xs');
      if (!b || b.disabled) return;
      var r = b.getBoundingClientRect(), s = Math.max(r.width, r.height) * 2;
      var rp = d.createElement('span');
      rp.className = 'nx-ripple';
      rp.style.cssText = 'width:' + s + 'px;height:' + s + 'px;left:' + (e.clientX - r.left - s / 2) + 'px;top:' + (e.clientY - r.top - s / 2) + 'px';
      b.appendChild(rp);
      setTimeout(function () { rp.remove(); }, 700);
    });

    // reveal cards as they scroll into view
    if ('IntersectionObserver' in window) {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (en.isIntersecting) { en.target.classList.add('nx-in'); io.unobserve(en.target); }
        });
      }, { threshold: 0.08, rootMargin: '0px 0px -30px 0px' });
      d.querySelectorAll('.t-card, .grid2 > *, .p-table, .perm-group, .act-grid > *, .proof-grid > *, .stat').forEach(function (el, i) {
        // only elements below the fold; the rest already animate on load
        if (el.getBoundingClientRect().top > window.innerHeight) {
          el.classList.add('nx-reveal');
          el.style.setProperty('--d', (i % 4) * 60 + 'ms');
          io.observe(el);
        }
      });
    }
  });
})();

// Mobile: keep the active nav item visible in the scrollable sidebar strip.
document.addEventListener('DOMContentLoaded', function () {
  var a = document.querySelector('.sidebar .nav-item.active');
  if (a && a.scrollIntoView) a.scrollIntoView({ inline: 'center', block: 'nearest' });
});

// Gradient headings use a transparent text fill, which turns emoji into flat white
// shapes. Wrap emoji in a span that opts out of the fill so they keep their colors.
document.addEventListener('DOMContentLoaded', function () {
  var emoji = /(\p{Extended_Pictographic}(?:\uFE0F|\u20E3|\p{Emoji_Modifier}|\u200D\p{Extended_Pictographic})*)/gu;
  document.querySelectorAll('.page-title, .sidebar h2').forEach(function (el) {
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT), nodes = [], n;
    while ((n = walker.nextNode())) nodes.push(n);
    nodes.forEach(function (node) {
      if (!emoji.test(node.nodeValue)) { emoji.lastIndex = 0; return; }
      emoji.lastIndex = 0;
      var frag = document.createDocumentFragment();
      node.nodeValue.split(emoji).forEach(function (part, i) {
        if (i % 2) { var s = document.createElement('span'); s.className = 'emo'; s.textContent = part; frag.appendChild(s); }
        else if (part) frag.appendChild(document.createTextNode(part));
      });
      node.parentNode.replaceChild(frag, node);
    });
  });
});
