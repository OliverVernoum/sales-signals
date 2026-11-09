# Sales Signals

A sales-intelligence monitor for selling iplicit. It watches competitor
resellers, competitor vendors, finance-software news sites and their YouTube
channels every 30 minutes, flags stories that signal a sales opportunity
(end of support, acquisitions, new FD/CFO, price rises, migrations, case
studies, international expansion, new features), and emails each person the
signal with its relevance and suggested next step.

- **Page**: `docs/index.html`, hosted free on GitHub Pages. People sign in with
  just their email (a code, no password), tick the sources and triggers they
  want, choose instant / daily / weekly emails, and see their latest signals.
- **Sources & triggers**: `docs/presets.json`, built from the research brief.
  Edit it to add sources or tune trigger words; the page and checker both read it.
- **Database + email sign-in**: Supabase (free tier)
- **Checker**: `monitor.py`, run on a schedule by GitHub Actions

## Social media
- **YouTube**: checked automatically for every source that has a channel.
- **LinkedIn, X, Facebook, Instagram**: these sites don't allow automatic
  reading, and scraping them breaks their terms, so the page lists them as
  links only. To monitor one, use a feed service that turns a social profile
  into an RSS feed (e.g. RSS.app, paid; check its terms) and paste the feed
  address into "Your own sites or feeds".

## Editing sources and triggers
Each source in `docs/presets.json` has one or more `urls`. `kind` is
`news`, `case_studies` (every new item counts as a "New customer case study")
or `youtube`. Each trigger has `all`: a list of word groups. A story matches
when it contains at least one word from **every** group. `retire*` matches
any ending; ALL-CAPS words (`CFO`, `US`, `AI`) only match in capitals.
After changing it, just push — no database change needed.

## Setup (about 25 minutes)

### 1. Gmail app password
Turn on 2-step verification for the Gmail account that will send emails, then
create an app password at myaccount.google.com/apppasswords. You'll use it twice.

### 2. Supabase
1. Create a free project at supabase.com.
2. **SQL Editor** → New query → paste all of `supabase.sql` → Run.
3. **Authentication → Emails → SMTP Settings**: turn on custom SMTP.
   Host `smtp.gmail.com`, port `587`, username = your Gmail address,
   password = the app password, sender email = the same Gmail address.
   (Supabase's built-in email only sends to your own team, so this step is needed.)
4. **Authentication → Emails → Templates**: in both **Confirm signup** and
   **Magic Link**, replace the body with:
   ```html
   <h2>Your Sales Signals code</h2>
   <p>Enter this code: <b style="font-size:22px;letter-spacing:3px">{{ .Token }}</b></p>
   <p>Or <a href="{{ .ConfirmationURL }}">tap here to sign in</a>.</p>
   ```
5. **Authentication → URL Configuration**: set Site URL to your GitHub Pages
   URL (step 3 below), e.g. `https://<username>.github.io/<repo>/`.
6. **Project Settings → API Keys**: note the **Project URL**, the
   **publishable** key (or legacy `anon`) and the **secret** key (or legacy
   `service_role`).

### 3. The page
1. Put the Project URL and **publishable** key in `docs/config.js`.
   (That key is meant to be public. The database only lets each person see
   and change their own alerts.)
2. Push the whole folder to a new **public** GitHub repo.
3. Repo → Settings → Pages → Deploy from a branch → `main`, folder `/docs`.

### 4. Secrets for the checker
Repo → Settings → Secrets and variables → Actions → add:

| Secret                 | Value                                            |
|------------------------|--------------------------------------------------|
| `SUPABASE_URL`         | Project URL                                      |
| `SUPABASE_SERVICE_KEY` | the **secret** key (never put this in the page!) |
| `SITE_URL`             | your GitHub Pages URL                            |
| `SMTP_HOST`            | `smtp.gmail.com`                                 |
| `SMTP_PORT`            | `587`                                            |
| `SMTP_USER`            | the Gmail address                                |
| `SMTP_PASS`            | the app password                                 |

### 5. Go
Open your page, sign in, save some alerts, then Actions tab → Story monitor →
Run workflow.

## Notes
- Signing in proves the person owns the email, so nobody can sign someone
  else up.
- The first time a site is checked, its existing stories are just recorded,
  so people aren't flooded with old news; only newer stories are sent.
- Every email has "All findings & settings" and one-click "Unsubscribe" links.
- Limits: 10 of your own sites and 30 keywords per person; any number of ready-made sources.
- Signals are kept for 90 days and shown on the page; each can be dismissed.
- Daily and weekly digests go out from 8am UK time (weekly on Mondays).
- Supabase rate-limits sign-in emails (about 30 an hour by default; change
  in Authentication → Rate Limits). Gmail allows about 500 emails a day.
- The checker refuses internal/private addresses, so a sign-up can't make it
  fetch anything except public websites.
- Sites that build their headlines with JavaScript can't be read; point at
  their RSS feed instead (often `/feed`, `/rss` or `rss.xml`).
- Change how often it runs with the `cron` line in
  `.github/workflows/monitor.yml` (times are UTC).
