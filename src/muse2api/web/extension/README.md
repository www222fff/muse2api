# muse2api Cookie Import — Chrome Extension

A Manifest V3 Chrome extension that reads muse.ai login cookies (including
httpOnly ones) from your browser and posts them to your muse2api gateway in one
click.

## Why this is needed

The core muse.ai cookies (`hatch_sess`, `hatch_native_auth_device`, plus
`hatch_gw` / `hatch_vml` when muse.ai sets them) are all `httpOnly`, which means regular web-page
JavaScript cannot read them. Only a browser extension with the `cookies`
permission can access them.

## Install (unpacked / developer mode)

1. Download `muse2api-cookie-extension.zip` from the muse2api dashboard
   (Accounts page → **Download extension** button) and unzip it.
2. Open `chrome://extensions` in Chrome.
3. Enable **Developer mode** (top-right toggle).
4. Click **Load unpacked** and select the unzipped folder.
5. The extension icon appears in the toolbar.

## Usage

1. Log in to [https://muse.ai/](https://muse.ai/) in this browser until you can
   see the chat interface.
2. Click the extension icon in the toolbar.
3. Fill in:
   - **Gateway URL**: the base URL of your muse2api instance, e.g.
     `http://your-server:18610`. You can copy it from the address bar when you
     have the dashboard open.
   - **Admin Key**: the bearer token for `/admin/*` — `MUSE2API_ADMIN_KEY`, or the
     generated API key (`m2a-…`, stored in `data/api_key`) when that is unset.
   - **Account label** (optional): a name for the account.
4. Click **Read and import**.

The extension reads the muse.ai cookies from your browser and posts them to
`POST {gateway}/admin/accounts`. The response shows the new account ID and
expiry.

## Permissions

- `cookies` — read muse.ai cookies.
- `storage` — persist the gateway URL and key between sessions (never the
  cookies themselves).
- `https://muse.ai/*`, `https://*.muse.ai/*` — read cookies for this domain.
- Optional: your gateway URL (requested at save time so the extension can POST
  to it). Only the specific origin you configure is requested.

## Cookie lifetime

muse.ai ends a login 30 days after sign-in, and nothing the gateway does extends
it: the session endpoint re-issues only `hatch_vml` (2 days, renewed every
keepalive), and the `hatch_sess` copies muse.ai re-issues later carry a fresh
30-day expiry that the server does not honour. The Accounts page therefore shows
the sign-in expiry of `hatch_sess` / `hatch_native_auth_device` (whichever is
earlier). Sign in again and re-import (one click) before it runs out.

## Languages

The popup is available in English, Tiếng Việt, and 中文. Select in the popup.
