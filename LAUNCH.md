# Going public

Everything that has to change when OrbitCast moves off
`orbitcast.up.railway.app` onto a domain you own, in the order it has to
happen. Written as a checklist because the failure mode here is not a crash —
it is sign-in quietly breaking for everyone, or a week of digest emails going
out with dead links, and neither announces itself.

Placeholder below: `example.com`. Substitute the real one.

---

## 0. Before anything

**Buy the domain.** This is yours to do — I cannot make purchases.

Worth knowing before you pick a registrar: you want one that gives you DNS
with CNAME-flattening or ALIAS records at the apex (Cloudflare and Porkbun
both do, free). Without it you cannot point `example.com` — as opposed to
`www.example.com` — at Railway, and you end up redirecting the apex through a
third party.

---

## 1. Railway

1. Service → Settings → Networking → **Custom Domain** → add `example.com`.
2. Railway returns a CNAME target like `xxxx.up.railway.app`. Add it at your
   registrar:
   - `www` → CNAME → `xxxx.up.railway.app`
   - apex → ALIAS/ANAME/flattened-CNAME → the same target
3. Wait for the certificate. Railway issues it automatically once DNS
   resolves; it is normally minutes, occasionally an hour.
4. **Do not remove the `*.up.railway.app` domain yet.** It is the way back in
   if something below is wrong.

---

## 2. Environment variables

| Variable | New value | What breaks if you forget |
|---|---|---|
| `PUBLIC_URL` | `https://example.com` | **Every link in every email.** `mailer.PUBLIC_URL` builds the unsubscribe and settings URLs, and a stale value means a digest whose unsubscribe link 404s — which is both infuriating and, for unsubscribe specifically, a compliance problem |

Nothing else is domain-dependent. `PUBLIC_URL` has a hardcoded default
pointing at the Railway host, so forgetting it fails silently rather than
loudly — check it first.

---

## 3. Google OAuth

In the Google Cloud console, the OAuth 2.0 Client for OrbitCast:

- **Authorised JavaScript origins** → add `https://example.com`
- **Authorised redirect URIs** → add `https://example.com/auth/callback`

Keep the Railway entries alongside them until the cut-over is proven. A
missing redirect URI gives `redirect_uri_mismatch` on Google's own page,
after the user has already clicked through — the one failure here that users
will see and you will not.

Check `GOOGLE_REDIRECT_URI` in `auth.py` resolves to the new host.

---

## 4. Cookies and CSP

- Cookies are set with `path="/"` and no explicit domain, so they follow the
  host automatically. You only need `SESSION_COOKIE_DOMAIN` if you want one
  session shared across the apex **and** `www`, and the simpler answer is to
  redirect one to the other (next section) and not have the problem.
- `security.py` sends HSTS only over HTTPS, so it starts applying on its own.
- Once you are certain every subresource is HTTPS, add
  `upgrade-insecure-requests` to the CSP. Everything the app loads today
  (Google Fonts, Turnstile) is already HTTPS, so this is cheap.
- **Do not submit to the HSTS preload list** until you are sure no subdomain
  will ever need plain HTTP. Preload is effectively irreversible.

---

## 5. One canonical origin

Pick `https://example.com` or `https://www.example.com` and redirect the other
to it at the DNS/registrar level. Two live origins means two cookie jars, two
sets of CSP behaviour and split SEO, and the symptom is "I was signed in a
minute ago".

Redirect `*.up.railway.app` to the canonical host too, once the new domain is
proven.

---

## 6. Prove it before telling anyone

In order, on the new domain:

1. Load `/` — check for CSP violations in the console (there should be none).
2. Sign in with Google. Sign out. Sign in again.
3. Request an email sign-in link and follow it.
4. Open the account panel → Digest → **Send it**. When the email arrives,
   **click every link in it**: the event links, "Change how often you get
   this", and Unsubscribe. This is the single best test of `PUBLIC_URL`.
5. Run an analysis end to end.
6. `curl -sI https://example.com/ | grep -i "strict-transport\|content-security"`

Only then announce anything.

---

## 7. Aikido

`aikido.dev` is a scanning service, not a certification body — there is no
badge to earn, and nothing here should be described as an audit.

1. Create the account and connect `Johnnothere/Orbit-Cast`.
2. Run it **after** the security and digest work has landed, so you are not
   triaging things already fixed. The 18 dependency CVEs that pass found in
   `pypdf` and `Authlib` would otherwise be its loudest first finding.
3. Expect it to flag, and here is the honest position on each:
   - **Secrets in git history.** If `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or
     `ADMIN_SECRET` was ever committed, **rotate the key**. Rewriting history
     does not help — assume anything ever pushed is public.
   - **CSP `'unsafe-inline'` on scripts.** Known and accepted: the app is one
     large inline `<script>`, and removing it means extracting ~3,000 lines
     to an external file with a nonce. Document it as accepted rather than
     pretending it is fixed.
   - **Admin auth is a single static header secret.** Improved, not solved.
     Rotation is supported; a real login is not built.
4. Make it a required check on PRs only once the baseline is green. A gate
   that is red on arrival gets ignored, and then it is not a gate.

### The CI workflow is not in the repository yet

`.github/workflows/security.yml` exists on disk but is deliberately **not
committed**. GitHub refuses a push that adds or edits a workflow file unless
the token carries `workflow` scope on top of `repo` — it will not let a
code-push token quietly add something that runs on their infrastructure,
which is the right call.

To add it, either:

- push it with a token that has **`repo` + `workflow`** scope, or
- paste the file in through the GitHub web UI (Actions → New workflow), which
  needs no extra token scope at all.

Once it is in, `pip-audit`, `bandit` and `semgrep` run on every PR and weekly,
so the dependency half of the Aikido work is covered whether or not Aikido
itself is connected. Until then, those checks only run when someone runs them
by hand.

---

## Still outstanding at launch

Honest list of what is not done, so nothing here is a surprise later:

- **Redis for rate limiting.** `RATELIMIT_STORAGE_URI` still defaults to
  `memory://`, so counters reset on every deploy and the daily AI spend
  ceilings are per-process rather than global. This is the one item on this
  list that costs real money if it bites.
- **Turnstile keys.** The code ships inert; bot protection is off until
  `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY` are set.
- **Migrations 0022 and 0023** have not been run.
- **The app runs one Gunicorn worker** and several things depend on that —
  the in-memory catalogue, the scrape lock, the AI semaphore and the digest
  scheduler. Concurrency comes from 64 threads, not workers. Raising
  `--workers` without addressing those will send every digest twice. See the
  risks section of the implementation plan.
