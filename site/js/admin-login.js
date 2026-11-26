(function () {
  var form = document.getElementById('login-form'),
      btn = form.querySelector('button'),
      err = document.getElementById('err'),
      codeStep = document.getElementById('code-step'),
      codeForm = document.getElementById('code-form'),
      codeBtn = codeForm.querySelector('button'),
      codeInput = document.getElementById('code'),
      resend = document.getElementById('resend'),
      emailInput = document.getElementById('email');

  var email = '';

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    email = emailInput.value.trim();
    if (!email) return;
    btn.disabled = true; btn.textContent = 'Sending…'; err.hidden = true;
    fetch('/auth/login/magic', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email, remember: true })
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
      if (r.status !== 200) throw 0;
      return r.json();
    }).then(function () {
      window.location.reload();
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
