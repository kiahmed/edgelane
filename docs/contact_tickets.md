# Contact / support tickets — backend contract

The market UI (`market/ui/index.html`) Contact dialog submits a support ticket.
The **frontend is done**; this documents the endpoint the backend session must build.

> **Status: implemented.** `POST /contact` lives in
> `market/backend/app/routes/contact.py`. It validates → stores any attachment in
> the private `contact-attachments` Storage bucket → inserts a row into
> `public.contact_tickets` (migration `0008`) via the service_role key → emails
> `SUPPORT_EMAIL` (`app/emailer.py`). The email is **best-effort**: the saved
> ticket is the source of truth, so a send failure still returns 2xx.
>
> **Email transport is pluggable** (`EMAIL_PROVIDER=auto|smtp|resend`): SMTP when
> `SMTP_HOST` is set (e.g. Google Workspace — `smtp.gmail.com:587` + App Password,
> or the `smtp-relay.gmail.com` relay; stdlib `smtplib`, no deps), otherwise Resend
> (HTTP API). Config keys in `edgelane_market.config`: `SUPPORT_EMAIL`,
> `CONTACT_FROM_EMAIL`, `CONTACT_ATTACHMENT_MAX_BYTES`, `CONTACT_BUCKET`,
> `EMAIL_PROVIDER`, SMTP: `SMTP_HOST/PORT/USER/PASSWORD/STARTTLS/USE_SSL`, or
> `RESEND_API_KEY`.
>
> **Why backend-side, not a Supabase trigger:** the backend already holds the
> uploaded file bytes (so it can attach them directly); a DB-webhook→Edge-Function
> would only get the row JSON and have to re-fetch from Storage, across a separate
> Deno deploy — and Supabase has no native "send email" anyway (its mailer is
> auth-only). One codebase, attachment in hand, synchronous status.

## Endpoint

```
POST {API_BASE}/contact
Content-Type: multipart/form-data
```

Auth header is attached via `_authHeaders()`:
- signed-in user → `Authorization: Bearer <supabase JWT>`
- teaser/anon    → `X-EdgeLane-Session: <anon token>`
- (dev-bypass    → no auth header)

Accept the request whether or not it's authenticated (the form is reachable from
the gated teaser too). If a JWT is present, capture the `sub` (user id) on the row.

## Form fields

| field        | type   | required | notes                                  |
|--------------|--------|----------|----------------------------------------|
| `name`       | text   | yes      | ≤120 chars                             |
| `email`      | text   | yes      | validated client-side, re-validate     |
| `message`    | text   | yes      | ≤5000 chars                            |
| `attachment` | file   | no       | **≤5 MB** (enforced client-side; enforce server-side too) |

## Behavior

- Insert a row into a `contact_tickets` table (suggested cols: `id`, `user_id nullable`,
  `name`, `email`, `message`, `attachment_path nullable`, `attachment_name`,
  `attachment_size`, `created_at`, `status default 'open'`).
- Store the attachment in Supabase Storage (private bucket) or DB blob; keep the
  path/name/size on the row.
- Enforce the 5 MB cap and a sane MIME allowlist (client sends
  `image/*,.pdf,.txt,.csv,.log,.json,.zip`).
- Rate-limit by IP / session to prevent abuse (reuse the Turnstile/anon-session
  gating already in front of `/session/anon`).

## Responses the frontend understands

- **2xx** → success ("Thanks — your ticket was submitted."), form resets, dialog closes.
  Body: `{"ok": true, "ticket_id": "<uuid>"}`.
- **non-2xx** → frontend shows `json.detail` or `json.error` if present, else
  `Submission failed (HTTP <status>)`. Return a JSON body with `detail` on errors.

## Frontend wiring notes (as built)

The endpoint is **live** — the UI can call it directly, no further backend work.

- **Don't set `Content-Type` manually.** Build a `FormData` and pass it as the
  fetch `body`; the browser sets `multipart/form-data` + the boundary itself.
  Setting it by hand breaks multipart parsing.
- **Server-enforced limits** (mirror client-side for nice UX, but the server is
  the source of truth): `name` ≤120, `message` ≤5000, attachment ≤5 MB.
- **Attachment extension allowlist** (server rejects anything else with 415):
  `.png .jpg .jpeg .gif .webp .bmp .svg .pdf .txt .csv .log .json .zip`.
- **Error status codes:** 422 (bad/missing field), 413 (attachment too big),
  415 (extension not allowed), 502 (couldn't save). All carry `detail`.
- **CORS:** the calling origin must match `CORS_ALLOW_ORIGINS` /
  `CORS_ALLOW_ORIGIN_REGEX` in `edgelane_market.config`. The deployed Vercel
  origin already matches the `edgelane*.vercel.app` regex; add any custom domain.
- **Auth is optional** — send the usual `_authHeaders()`; a signed-in user's id
  is captured on the ticket, anon/teaser submissions are accepted too.

Example:

```js
const fd = new FormData();
fd.append("name", name);
fd.append("email", email);
fd.append("message", message);
if (file) fd.append("attachment", file);   // optional
const r = await fetch(`${API_BASE}/contact`, {
  method: "POST",
  headers: _authHeaders(),                  // NO Content-Type here
  body: fd,
});
const j = await r.json().catch(() => ({}));
if (r.ok) { /* j.ticket_id */ } else { /* show j.detail */ }
```
