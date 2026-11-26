(function () {
  var l = LawkittyLoader.show(), hidden = false;
  function hide() { if (hidden) return; hidden = true; l.hide(); }
  window.addEventListener('load', function () { setTimeout(hide, 650); });
  setTimeout(hide, 2400); // safety net

  var cv = document.getElementById('lk-yarn'), yarn = lkYarn(cv);
  function local(e) { var r = cv.getBoundingClientRect(); return [(e.clientX - r.left) * 1440 / r.width, (e.clientY - r.top) * 780 / r.height]; }
  cv.addEventListener('pointermove', function (e) { var p = local(e); cv.style.cursor = yarn.hit(p[0], p[1]) ? 'pointer' : ''; });
  cv.addEventListener('click', function (e) { var p = local(e); yarn.click(p[0], p[1]); });
  var lastY = window.scrollY;
  window.addEventListener('scroll', function () {
    var dy = window.scrollY - lastY; lastY = window.scrollY;
    var r = cv.getBoundingClientRect();
    if (r.bottom > 0 && r.top < window.innerHeight) yarn.nudge(-dy * 2.2);
  }, { passive: true });
})();
