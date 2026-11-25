/* Panel "Look" form fields (templates/_look_fields.html): the style, the picture position
   (only the positions that style can do) and the dropdown placeholder box. */
(function () {
  var HINTS = {
    embed: 'Outside: a normal embed with the buttons or dropdown in a row under it. The picture can go on the right (small) or at the bottom (big).',
    box: 'Inside: everything (text, picture, buttons or dropdown) sits in one box. It is not an embed, so the colour becomes the side bar of the box. The picture can go up, down or right (small).'
  };

  function sync(wrap, wanted) {
    var style = wrap.querySelector('.lk-style').value;
    var pos = wrap.querySelector('.lk-pos');
    var positions = {};
    try { positions = JSON.parse(wrap.getAttribute('data-positions') || '{}'); } catch (e) {}
    var keep = wanted || pos.value;
    pos.textContent = '';
    (positions[style] || []).forEach(function (kv) {
      var o = document.createElement('option');
      o.value = kv[0];
      o.textContent = kv[1];
      pos.appendChild(o);
    });
    if (Array.prototype.some.call(pos.options, function (o) { return o.value === keep; })) pos.value = keep;
    wrap.querySelector('.lk-hint').textContent = HINTS[style] || '';
    var ctl = wrap.querySelector('.lk-control');
    var ph = wrap.querySelector('.lk-ph');
    if (ctl && ph) ph.style.display = ctl.value === 'dropdown' ? '' : 'none';
  }

  function init(wrap) {
    wrap.querySelectorAll('.lk-style, .lk-control').forEach(function (el) {
      el.addEventListener('change', function () { sync(wrap); });
    });
    sync(wrap, wrap.getAttribute('data-position') || '');
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.lk-wrap').forEach(init);
  });
})();
