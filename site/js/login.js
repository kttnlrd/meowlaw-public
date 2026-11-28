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
  // Styled to each provider's published sign-in button guidelines (logo, wording, colours).
  var SSO_LOGOS = {
    google: '<svg viewBox="0 0 48 48" aria-hidden="true"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>',
    microsoft: '<svg viewBox="0 0 21 21" aria-hidden="true"><rect x="1" y="1" width="9" height="9" fill="#f25022"/><rect x="1" y="11" width="9" height="9" fill="#00a4ef"/><rect x="11" y="1" width="9" height="9" fill="#7fba00"/><rect x="11" y="11" width="9" height="9" fill="#ffb900"/></svg>'
  };
  var sso = document.getElementById('sso'),
      ssoButtons = document.getElementById('sso-buttons'),
      ssoReady = false;
  fetch('/auth/providers')
    .then(function (r) { return r.status === 200 ? r.json() : { providers: [] }; })
    .then(function (j) {
      (j.providers || []).forEach(function (p) {
        var a = document.createElement('a');
        a.className = 'sso-btn sso-' + p.id;
        if (SSO_LOGOS[p.id]) a.innerHTML = SSO_LOGOS[p.id];  // static markup only
        var label = document.createElement('span');
        label.textContent = 'Sign in with ' + p.label;
        a.appendChild(label);
        a.href = '/auth/login/' + encodeURIComponent(p.id);
        a.addEventListener('click', function () {
          // Read "Keep me signed in" at click time.
          a.href = '/auth/login/' + encodeURIComponent(p.id) + '?next=' + encodeURIComponent(nextUrl) +
            (document.getElementById('remember').checked ? '&remember=1' : '');
        });
        ssoButtons.appendChild(a);
        ssoReady = true;
        sso.hidden = form.hidden;
        document.getElementById('auth-sub').textContent =
          'Use your Google or Microsoft account, or we’ll email you a 6-digit login code.';
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
      form.hidden = true; sso.hidden = true; codeStep.hidden = false; codeInput.focus();
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
    codeStep.hidden = true; form.hidden = false; sso.hidden = !ssoReady; err.hidden = true;
    btn.disabled = false; btn.textContent = 'Email me a login code';
  });
})();
