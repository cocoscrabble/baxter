# Plan: password reset by email

**Status: not started. Phase 0 is a go/no-go gate** — until we have actually
sent and received a message as `@cocoscrabble.org`, none of the rest is worth
writing. Captured 2026-09-02, at commit `bff7ca37`.

## Goal

A user who has forgotten their password can recover their account without a
director editing the database for them: request a reset by email address,
receive a signed link, set a new password.

Self-service password *change* (for a user who is signed in and knows their
current password) already exists — `ChangePasswordView` in `users/views.py`,
landed in `dd2ec5ef`. It needs no email and is unaffected by any of this.

## Why phase 0 is a gate, not a first step

Every other phase is ordinary Django work with a known shape. The one genuinely
unknown is whether Baxter's Dokku host can put a message in a user's inbox
under the `cocoscrabble.org` name. If it can't, a reset flow is worse than no
reset flow: the request page cheerfully reports "check your email" whether or
not anything was sent (Django deliberately refuses to confirm whether an
address is registered), so undeliverable mail is *invisible* to the user and to
us. They wait; nothing arrives; they email a director anyway.

So phase 0 produces a yes or a no, and phases 1–5 do not begin until it is yes.

## What DNS says today (checked 2026-09-02)

```
MX     cocoscrabble.org  ->  aspmx.l.google.com (Google Workspace)
TXT    cocoscrabble.org  ->  google-site-verification=... only — no SPF record
TXT    google._domainkey.cocoscrabble.org  ->  empty (also default/selector1/
                                                selector2/k1: all empty)
TXT    _dmarc.cocoscrabble.org  ->  "v=DMARC1; p=none"
A      cocoscrabble.org  ->  198.49.23.x / 198.185.159.x (Squarespace)
```

Read that as: **inbound** mail for the domain is Google Workspace, and
**outbound** mail claiming to be from the domain is currently unauthenticated —
there is no SPF record at all, and no DKIM key under the selector Workspace
uses by default. DMARC is published but at `p=none`, so receivers are asked to
do nothing about failures, and with no `rua=` address nobody is collecting
reports either.

Absence of a DKIM record under the common selectors is strong evidence but not
proof that DKIM signing is off; a custom selector would not show up in that
probe. Confirm in the Workspace admin console rather than from DNS.

The practical consequence: a message sent straight from the Dokku host with a
`From:` of `baxter@cocoscrabble.org` is spoofing an unprotected domain. Some
receivers will take it, Gmail will likely spam-folder it, and nothing in the
system will tell us which happened.

## Phase 0 — authenticate the domain, then prove a message gets through

Do this by hand, outside the app. No Django code, no settings changes, no
commits.

**0a. Establish who controls what.** Three separate access questions, and the
answer to each is a person, not a setting:

- Who administers the Google Workspace tenant (can create an account or an app
  password, and can turn on DKIM signing)?
- Who controls `cocoscrabble.org` DNS — Squarespace, or a registrar upstream of
  it? SPF and DKIM records are edited there.
- Is there an existing role mailbox to send from, or does one need creating?

**0b. Pick the sending route.** Two candidates, and phase 0 tests exactly one:

1. **Google Workspace SMTP with an app password.** No new vendor, and the
   domain's own DKIM applies once enabled. Costs a mailbox (or an alias with a
   password) and an app password, which requires 2FA on that account. Google
   throttles hard — a few hundred messages a day — which is far above what
   password resets for a club-scale tournament app will ever need.
2. **A transactional provider (Postmark, Resend, SES).** Gives per-message
   delivery and bounce visibility, which is the thing SMTP will never give us,
   at the cost of a vendor account, an API key, and its own DNS records to
   publish.

Recommendation: **try route 1 first.** It touches the fewest moving parts, and
if deliverability turns out to be bad we will know from 0d and can switch to
route 2 having lost only an afternoon. The application code is identical either
way — both are an `EMAIL_BACKEND` plus credentials — so this choice does not
leak into phases 1–5.

**0c. Publish SPF and turn on DKIM.** Not a contingency — DNS shows neither
exists today, so this is work that will certainly be needed. Do it before the
send test, so that test measures the configuration we actually intend to ship
rather than a bare-domain baseline we already know is bad. Order matters within
this step.

- [ ] **SPF, one TXT record on the apex.** For route 1 that is
      `v=spf1 include:_spf.google.com ~all`. A domain may publish **exactly
      one** SPF record — two records is a permanent error that fails every
      check, so if route 2 was chosen, or is added later, its include joins
      this same record rather than getting its own. Start with `~all`
      (softfail) rather than `-all`; it can be tightened once phase 5 has
      reporting to confirm nothing legitimate is being caught.
- [ ] **Generate the DKIM key** in the Workspace admin console: Apps → Google
      Workspace → Gmail → Authenticate email. Take the 2048-bit key.
- [ ] **Publish the DKIM key** as TXT at `google._domainkey.cocoscrabble.org`.
      A 2048-bit key exceeds the 255-character limit for a single TXT string,
      so it has to be split into multiple quoted strings within the one
      record. Some DNS editors do this silently, some require it by hand, and
      some cannot do it at all — if the Squarespace editor is the one that
      cannot, that is a reason to move DNS, and it is much better to discover
      it here than in phase 5.
- [ ] **Then** click "Start authentication" in the console. Doing this before
      the record has propagated makes Workspace report the key as missing;
      confirm with `dig +short TXT google._domainkey.cocoscrabble.org` first.
- [ ] **Leave DMARC at `p=none`** for now. Raising it while SPF and DKIM are
      still settling risks discarding real mail. Phase 5 revisits it.

*Verify:* `dig +short TXT cocoscrabble.org` returns the SPF record,
`dig +short TXT google._domainkey.cocoscrabble.org` returns the key, and the
Workspace console shows DKIM as authenticating rather than pending.

**0d. Send a real message from the production host.** Not from a laptop: from
`cocoscrabble.vps.webdock.cloud`, because that is the machine whose IP the
receiver will judge. A one-off `swaks` or a five-line `smtplib` script is
enough — the point is to exercise the host's network path (outbound port 587 is
sometimes blocked by a VPS provider by default, which is exactly the sort of
thing that would derail phase 2 if discovered there).

Send to **three** addresses: a Gmail one, a non-Gmail one (Outlook, Fastmail,
or a work domain), and the director's own address.

**0e. Judge the result — this is the actual gate.** For each of the three:

- Did it arrive at all?
- Inbox or spam? Spam counts as a failure, not a pass.
- In the received message's headers, do `spf=` and `dkim=` say `pass`? Gmail's
  "Show original" reports both. `dkim=pass` with SPF absent still fails DMARC
  alignment reasoning later, so record both values rather than a verdict.

**Go** if all three land in the inbox and authentication passes. **No-go** if
anything lands in spam or fails authentication: 0c is published but not
working, so diagnose it there — a typo or an unsplit key in the DKIM record, an
SPF record that never propagated, a `From:` domain that does not match what was
authorized — and re-run 0d. Only after a clean re-run does phase 1 start.

**0f. Write down the answer.** Update this section of the plan with what was
sent, from where, what the headers said, what records 0c published, and the
date. A later session will otherwise re-derive all of it.

## Phase 1 — make email an identity field

Currently `User` inherits `AbstractUser.email`, which is neither unique nor
required at the database level. `CustomUserCreationForm` requires it, so
form-registered users have one, but anything created by `createsuperuser` or a
script does not. In the dev database 12 of 13 users have an empty email.

Two consequences for reset, both bad and both silent:

- A user with no address on file can never reset, and is told to check their
  inbox anyway.
- Django's `PasswordResetForm.get_users()` matches `email__iexact` and mails
  *every* match, so two accounts sharing an address produce two links that the
  recipient cannot tell apart.

So: audit production for blank and duplicate addresses, backfill or clear them,
then make the field `unique=True, blank=False` with a migration. Note that
`unique` cannot coexist with multiple `""` values, so the backfill genuinely has
to come first — and per the SQLite-vs-Postgres gotcha, a migration that passes
locally is not proof it passes on the prod Postgres.

Decide deliberately what happens to an account that ends up with no address:
the honest options are "a director sets one in admin" or "that account cannot
self-serve", and either is fine as long as it is chosen rather than discovered.

*Verify:* migration applies to a copy of the production database; a duplicate
address is rejected at the model level.

## Phase 2 — transport configuration

Settings, following the existing `django-environ` pattern in
`baxter/settings.py`:

```python
EMAIL_BACKEND = env("EMAIL_BACKEND",
                    default="django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="baxter@cocoscrabble.org")
```

The console backend as default means dev needs no configuration and prints the
reset link to the terminal — which is also how phase 3's manual testing works.
Add the variables to `.env.example`.

Production config goes through Ansible, **not** `dokku config:set` by hand:
`baxter_extra_config` in `../vps/ansible/group_vars/all/production.yml`, with
the password as a vault variable in `secrets.yml`, exactly as
`ROSTER_API_TOKEN` already is.

*Verify:* `send_mail` from a production shell reaches a real inbox — the same
check as 0c, now going through Django's settings rather than a bare script.

## Phase 3 — the reset flow

Django's four views (`PasswordResetView`, `PasswordResetDoneView`,
`PasswordResetConfirmView`, `PasswordResetCompleteView`) wired into
`users/urls.py`. The URL names `password_reset_confirm` and
`password_reset_complete` must be spelled exactly that way — Django's own code
reverses them.

Six templates alongside the existing ones in `users/templates/users/`, kept as
thin as `password_change.html`: the request form, "check your mail", the
new-password form, "done", plus `password_reset_email.html` and
`password_reset_subject.txt`. A "Forgot your password?" link on `login.html`.

One trap that is already handled: the absolute URL in the email comes from
`request.get_host()` when `django.contrib.sites` is not installed, and it is
not; `SECURE_PROXY_SSL_HEADER` in the `not DEBUG` block of settings already
makes `request.is_secure()` true behind the dokku nginx proxy. So links come out
as `https://<host>/...` with no sites framework and no extra setting.

No command or event-log wiring: the completeness guard in
`tournaments/tests/test_event_completeness.py` only walks views whose module
starts with `tournaments`, and this is the `users` app.

*Verify:* tests in `users/tests/test_views.py` using the `locmem` backend —
assert on `mail.outbox`, pull the token out of the body, walk the confirm URL,
check the password changed. Plus the two silent cases: an unknown address still
reports success and sends nothing, and (after phase 1) every account that can
be looked up has an address to send to.

## Phase 4 — rate limiting

The request endpoint is unauthenticated and causes the server to send mail on
demand. Django ships no throttle. A per-IP and per-address counter in the cache
is proportionate at this scale; `django-ratelimit` or `django-axes` if a
dependency is preferred over ten lines. Worth having before the flow is public
rather than after.

*Verify:* a test asserting the Nth request in a window is refused.

## Phase 5 — rollout

Ansible-deploy the config, run through a real reset against production with a
throwaway account, and confirm the message lands in an inbox rather than spam
from the production host specifically — the 0c result was for a bare script and
does not automatically carry over.

Consider afterwards: raise DMARC from `p=none` to `p=quarantine` and add an
`rua=` reporting address, so that future breakage is visible. That is a domain
decision beyond Baxter, so it belongs to whoever owns the DNS rather than to
this plan.

## Open questions

- Who administers the Workspace tenant and the domain's DNS? (0a — blocks
  everything.)
- Is DKIM actually off, or on under a non-standard selector? (Check the console,
  not DNS.)
- Is outbound SMTP from the webdock VPS blocked? (0c answers this incidentally.)
- What happens to accounts with no email address after phase 1 — director sets
  one, or no self-service for them?
