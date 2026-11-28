/* lawkitty Squishy theme: everything squishes when pressed, wobbles like jelly when released,
   and throws a burst of bubbles from the pointer. No dependencies. Respects reduced motion.
   Guarded to data-theme="squishy" so it stays inert in every other theme. */
(function () {
  if (window.__lkSquish) return; window.__lkSquish = true;
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  var SEL = 'a, button, input, select, label, li, img, h1, h2, h3, p, section, aside, form, [style*="border"]';
  var COLS = ['#ff7ac8', '#ffb347', '#7ad7ff', '#b0166e', '#9be15d', '#c9a4ff'];
  var pressed = [];
  function active() {
    return document.documentElement.getAttribute('data-theme') === 'squishy';
  }
  function burst(x, y) {
    for (var i = 0; i < 14; i++) {
      var d = document.createElement('span'), s = 6 + Math.random() * 12, star = Math.random() < 0.35;
      d.setAttribute('aria-hidden', 'true');
      d.style.cssText = 'position:fixed;left:' + (x - s / 2) + 'px;top:' + (y - s / 2) + 'px;width:' + s + 'px;height:' + s + 'px;pointer-events:none;z-index:9999;' +
        'background:' + COLS[i % COLS.length] + ';border-radius:' + (star ? '2px' : '50%') + ';box-shadow:inset -2px -2px 0 rgba(0,0,0,.12),inset 2px 2px 0 rgba(255,255,255,.6)';
      if (star) d.style.clipPath = 'polygon(50% 0,61% 35%,98% 35%,68% 57%,79% 91%,50% 70%,21% 91%,32% 57%,2% 35%,39% 35%)';
      document.body.appendChild(d);
      var a = Math.random() * Math.PI * 2, r = 40 + Math.random() * 90;
      var dx = Math.cos(a) * r, dy = Math.sin(a) * r - 30;
      var anim = d.animate([
        { transform: 'translate(0,0) scale(.2) rotate(0deg)', opacity: 1 },
        { transform: 'translate(' + dx * 0.7 + 'px,' + dy * 0.7 + 'px) scale(1.25) rotate(' + (a * 90) + 'deg)', opacity: 1, offset: 0.55 },
        { transform: 'translate(' + dx + 'px,' + (dy + 60) + 'px) scale(.4) rotate(' + (a * 180) + 'deg)', opacity: 0 }
      ], { duration: 700 + Math.random() * 400, easing: 'cubic-bezier(.2,.8,.3,1)' });
      anim.onfinish = (function (el) { return function () { el.remove(); }; })(d);
    }
  }
  document.addEventListener('pointerdown', function (e) {
    if (!active()) return;
    var t = e.target.closest ? e.target.closest(SEL) : null;
    if (!t) return;
    t.classList.remove('lk-boing'); t.classList.add('lk-squish'); pressed.push(t);
    burst(e.clientX, e.clientY);
  }, true);
  function release() {
    pressed.forEach(function (t) {
      t.classList.remove('lk-squish'); void t.offsetWidth; t.classList.add('lk-boing');
      t.addEventListener('animationend', function () { t.classList.remove('lk-boing'); }, { once: true });
    });
    pressed = [];
  }
  document.addEventListener('pointerup', release, true);
  document.addEventListener('pointercancel', release, true);
})();
