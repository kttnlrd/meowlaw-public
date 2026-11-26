# Sign-in: Google, Microsoft and email login codes

LawKitty has three ways to sign in. All three end in the same place: a user row keyed by
lower-cased email and a signed session cookie.

| Method | Offered when | Code |
|---|---|---|
| Email login code / magic link | always | `backend/auth/providers/magic.py` |
| Google (OpenID Connect) | `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set | `backend/auth/providers/sso.py` |
| Microsoft (OpenID Connect) | `MICROSOFT_CLIENT_ID` and `MICROSOFT_CLIENT_SECRET` are set | `backend/auth/providers/sso.py` |

The login page (`site/login.html`, `site/js/login.js`) asks `GET /auth/providers` which
providers are configured and shows a button for each.

## Storage and sessions

- Users and pending login codes live in Postgres (`users`, `magic_tokens`; see
  `backend/auth/store.py` and `backend/db.py`). Configure `LAWKITTY_DATABASE_URL`.
- Accounts are keyed by email. Signing in with Google, Microsoft or a login code for the
  same address reaches the same account, which is why each provider must prove the address
  (below).
- The session is a JWT in an HTTP-only cookie signed with `SESSION_SECRET`. With an
  `https` `APP_BASE_URL` the app refuses to start unless `SESSION_SECRET` is at least 32
  characters.
- Login codes expire after 15 minutes and are single-use. Email is sent through Resend
  (`RESEND_API_KEY`, `RESEND_FROM`).

## Routes

```
GET  /auth/providers               configured SSO providers
GET  /auth/login/{provider}        start Google / Microsoft sign-in (PKCE, state, nonce)
GET  /auth/callback/{provider}     provider redirect target
POST /auth/login/magic             email a login code
POST /auth/login/magic/verify      verify a typed code
GET  /auth/callback/magic          verify an emailed link
GET  /auth/logout
GET  /auth/me
```

## When a provider's sign-in is accepted

- **Google:** the ID token issuer must be Google and `email_verified` must be true.
- **Microsoft, work or school accounts:** the token's issuer must match its tenant (`tid`),
  and the `xms_edov` claim (email domain owner verified) must be true. Microsoft's `email`
  claim alone is not proof of ownership, and in a multi-tenant app any tenant admin could
  otherwise assert any address.
- **Microsoft, personal accounts** (outlook.com, hotmail): tokens come from Microsoft's own
  consumer tenant `9188040d-6c67-4c5b-b112-36a304b66dad`. Microsoft verifies these addresses
  itself and never issues `xms_edov`, so that one check is skipped for this tenant only.
- Setting `MICROSOFT_TENANT` to a tenant GUID restricts sign-in to that single tenant.
  The default, `common`, accepts work, school and personal accounts.

## Setting up Google

1. In Google Cloud, create a project and open **Google Auth Platform**.
2. **Branding:** app name, support email, home page, privacy policy and terms URLs, and your
   domain under Authorised domains. Do not upload a logo unless you intend to go through
   Google's verification review.
3. **Audience:** External.
4. **Clients:** create a *Web application* client with the redirect URI
   `{APP_BASE_URL}/auth/callback/google` (add `http://localhost:8765/auth/callback/google`
   for local development).
5. **Audience → Publish app.** Until it is published, only listed test users can sign in.
   With only the `openid email profile` scopes, publishing does not need verification.
6. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

## Setting up Microsoft

1. In Microsoft Entra, create an app registration. Supported account types: *any
   organisational directory and personal Microsoft accounts* (or narrower if you prefer).
2. Web redirect URI: `{APP_BASE_URL}/auth/callback/microsoft` (plus the localhost one for
   development).
3. **Token configuration:** add the optional ID-token claims `email` and `xms_edov`.
   Without `xms_edov`, every work or school sign-in is refused.
4. **Manifest:** set `requestedAccessTokenVersion` to `2`.
5. **Certificates & secrets:** create a client secret.
6. Set `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET` and optionally `MICROSOFT_TENANT`.

With the Azure CLI, steps 1 to 5 are:

```sh
az ad app create --display-name "LawKitty" \
  --sign-in-audience AzureADandPersonalMicrosoftAccount \
  --web-redirect-uris https://example.com/auth/callback/microsoft \
  --optional-claims '{"idToken":[{"name":"email"},{"name":"xms_edov"}]}'
az rest --method PATCH --uri https://graph.microsoft.com/v1.0/applications/<object-id> \
  --body '{"api":{"requestedAccessTokenVersion":2}}'
az ad app credential reset --id <app-id> --years 2
```

## Admins

Admin rights follow the email address. List admins in `ADMIN_EMAILS` (comma-separated).
It is empty by default, so nobody is an admin until it is set.
