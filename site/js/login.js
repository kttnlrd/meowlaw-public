(function () {
  var form = document.getElementById('login-form'),
      btn = form.querySelector('button'),
      err = document.getElementById('login-err'),
      codeStep = document.getElementById('code-step'),
      codeForm = document.getElementById('code-form'),
      codeBtn = codeForm.querySelector('button'),
      codeInput = document.getElementById('code'),
      resend = document.getElementById('resend'),
      emailInput = document.getElementById('email');

  var email = '';

  // Already signed in on this device? Go straight to the app.
  // Same-site relative paths only (mirrors backend/auth _safe_next): never navigate to a
  // caller-supplied absolute, protocol-relative or javascript: URL.
  // Check what the browser will actually navigate to: it strips tab/CR/LF and treats "\" as
  // "/", so "/\t/evil.com" resolves off-site even though it starts with a single "/".
  function sameSite(n) {
    if (typeof n !== 'string' || n.charAt(0) !== '/' || /[\x00-\x20\x7f\\]/.test(n)) return false;
    try { return new URL(n, window.location.href).origin === window.location.origin; } catch (e) { return false; }
  }
  var nextUrl = new URLSearchParams(window.location.search).get('next') || '';
  if (!sameSite(nextUrl)) nextUrl = '/app';
  fetch('/auth/me', { credentials: 'same-origin' })
    .then(function (r) { return r.status === 200 ? r.json() : null; })
    .then(function (j) { if (j && j.authenticated) window.location.replace(nextUrl); })
    .catch(function () {});

  // Google / Microsoft buttons, only for the providers this instance has configured.
  var sso = document.getElementById('sso');
  fetch('/auth/providers')
    .then(function (r) { return r.status === 200 ? r.json() : { providers: [] }; })
    .then(function (j) {
      (j.providers || []).forEach(function (p) {
        var a = document.createElement('a');
        a.className = 'btn';
        a.textContent = 'Continue with ' + p.label;
        a.href = '/auth/login/' + encodeURIComponent(p.id);
        a.addEventListener('click', function () {
          // Read "Keep me signed in" at click time.
          a.href = '/auth/login/' + encodeURIComponent(p.id) + '?next=' + encodeURIComponent(nextUrl) +
            (document.getElementById('remember').checked ? '&remember=1' : '');
        });
        sso.appendChild(a);
        sso.hidden = false;
      });
    })
    .catch(function () {});

  var ssoError = new URLSearchParams(window.location.search).get('error');
  if (ssoError === 'sso_failed' || ssoError === 'sso_unavailable') {
    err.textContent = ssoError === 'sso_failed'
      ? "We couldn't sign you in with that account. Try again, or use an email login code."
      : "That sign-in option isn't available right now. Use an email login code instead.";
    err.hidden = false;
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    email = emailInput.value.trim();
    if (!email) return;
    btn.disabled = true; btn.textContent = 'Sending…'; err.hidden = true;
    fetch('/auth/login/magic', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email, remember: document.getElementById('remember').checked, next: nextUrl })
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.status === 202, sent: j.sent === true }; });
    }).then(function (x) {
      if (!x.ok) throw 0;
      form.hidden = true; codeStep.hidden = false; codeInput.focus();
    }).catch(function () {
      err.textContent = "We couldn't send the email right now. Please try again in a minute.";
      err.hidden = false; btn.disabled = false; btn.textContent = 'Email me a login code';
    });
  });

  codeForm.addEventListener('submit', function (e) {
    e.preventDefault();
    var code = codeInput.value.trim();
    if (!code) return;
    codeBtn.disabled = true; codeBtn.textContent = 'Signing in…'; err.hidden = true;
    fetch('/auth/login/magic/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email, code: code })
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.status === 200, next: j.next || '/app' }; });
    }).then(function (x) {
      if (!x.ok) throw 0;
      window.location.href = sameSite(x.next) ? x.next : '/app';
    }).catch(function () {
      err.textContent = "That code didn't work. Check it and try again.";
      err.hidden = false; codeBtn.disabled = false; codeBtn.textContent = 'Sign in';
    });
  });

  resend.addEventListener('click', function (e) {
    e.preventDefault();
    codeStep.hidden = true; form.hidden = false; err.hidden = true;
    btn.disabled = false; btn.textContent = 'Email me a login code';
  });
})();
