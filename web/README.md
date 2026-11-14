# Web frontend

A static page (HTML, CSS, plain JavaScript, no build step) for the registration
service. It walks through the four API operations: enter mobile, confirm OTP,
then add and list up to four people.

The service serves this folder at `/`, so one URL hosts both the page and the
API (`WEB_DIR` in `service/README.md`). To host it elsewhere, set
`window.COWIN_API_BASE` in `config.js` and add the page's origin to the
service's `CORS_ORIGINS`.

What it shows on purpose, for the demo and viva:
- **Four slots** for the 4-per-mobile rule (Sec 9.2). They fill as people are added.
- **Safe retries.** Each "Add person" gets an `Idempotency-Key`. The key is kept
  after a network error or a 5xx response, so pressing the button again replays the
  same request instead of creating a duplicate. Editing the form starts a new key.
  A replayed request shows "already added".
- **Which replica answered**, in the footer (`X-Served-By`), so the load
  balancer spreading requests is visible during E2/E3.
- A banner saying this is a course project, not the official CoWIN, and asking
  for made-up details only.

The typeface is Atkinson Hyperlegible, designed for low-vision readers, chosen
for the case study's inclusive-access principle (Sec 3.1). The page is usable
down to phone width, works with the keyboard, and respects reduced-motion settings.
