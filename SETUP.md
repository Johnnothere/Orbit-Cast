# OrbitCast AI — deploy (no editing required)

Both files are complete drop-in replacements. You don't paste or edit anything.

## Replace two files

1. **templates/index.html** -> replace your current one. (This is your exact file with
   the AI changes already merged in — your terminal design, scraper-driven views, and
   everything else are untouched. Diff it against your local copy if you want to see
   precisely what moved; it's only the AI panel, plus the Pro modal.)
2. **app.py** -> replace your current one. All changes are in the AI section, plus the
   new storage/billing helpers and routes. Your SOURCES scraper wiring is unchanged.

## Add a few lines

To **requirements.txt**:
    pdfplumber
    python-docx
    psycopg2-binary
    stripe

To **.gitignore**:
    uploads_store.jsonl
    premium_store.json

Then deploy as usual: git add -A && git commit && git push --force ...

## What changed

- Honest matching. The old prompt forced "exactly 5" inflated recommendations and
  invented generic "revelations" (Acceleration Window, etc.). Now it classifies the
  upload first (refusing to invent a profile if it isn't a CV), recommends only genuine
  matches above a 65 score, returns an honest empty state when nothing fits, and names
  real gaps. The threshold is enforced in code, not just asked for in the prompt.
- Server-side CV reading with pdfplumber / python-docx — replaces the old client-side
  regex that missed most PDF text.
- AI is the landing view, and the logo returns there.
- Storage: every analysis is saved (extracted text + result). Raw files are NOT kept by
  default (lighter on GDPR); set STORE_RAW_FILES=true to keep them.
- Revenue: 3 free analyses, then a Pro upgrade prompt (Stripe). The "Pro" header button
  and the paywall both open it. Free for everyone on the core experience.

## Environment variables (Railway -> Variables)

  ANTHROPIC_API_KEY                      Honest CV analysis        (you already have it)
  DATABASE_URL                           Durable storage           (recommended, see note)
  STRIPE_SECRET_KEY + STRIPE_PRICE_ID    Paid tier                 (only for billing)
  STRIPE_WEBHOOK_SECRET                  Verify payments           (with billing)
  PUBLIC_URL                             Stripe redirects          (with billing)
  STORE_RAW_FILES                        Keep original CV files    (optional, default off)
  FREE_ANALYSES                          Free analyses before pay  (optional, default 3)

Storage note: with no DATABASE_URL, uploads write to local JSON files — fine, but
Railway's filesystem resets on each redeploy, so stored CVs won't survive a deploy. For
durable storage add a Railway Postgres (+ New -> Database -> PostgreSQL); it sets
DATABASE_URL automatically and the app creates its tables on boot. Since you're storing
CV text, add a short privacy policy — the "Delete my CV data" link already works.

## Billing (when ready)

1. Stripe -> create a Product with a recurring price; copy its Price ID (price_...).
2. Add STRIPE_SECRET_KEY, STRIPE_PRICE_ID, PUBLIC_URL to Railway.
3. Stripe -> Webhooks -> endpoint https://<your-url>/api/stripe-webhook, event
   checkout.session.completed, put its secret in STRIPE_WEBHOOK_SECRET.

Until those keys exist, the upgrade button just collects emails — safe to ship now.
