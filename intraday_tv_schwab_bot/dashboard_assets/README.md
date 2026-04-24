# Dashboard themes

This directory ships the static assets served by the local dashboard and the theme plugin tree. Themes live at:

```
dashboard_assets/
├── dashboard.html        # base desktop template
├── dashboard.css         # base layout + structure (no theme tokens)
├── dashboard.js
├── mobile.html  mobile.css  mobile.js
├── images.json           # brand images embedded into the page at serve time
└── themes/
    ├── default/theme.css     # blue-tinted dark (original)
    ├── dark/theme.css        # pure black glass
    ├── light/theme.css       # white background
    ├── nexus/theme.css       # near-black with mint/teal accent
    ├── solstice/theme.css    # near-black with warm amber accent
    ├── nebula/theme.css      # near-black with violet accent
    └── example_custom/       # starter for a fully custom dashboard
```

At serve time the server injects two tags into the base template:

```html
<link rel="stylesheet" href="/themes/{THEME}/theme.css" />      <!-- after dashboard.css -->
<script src="/themes/{THEME}/theme.js" defer onerror="this.remove()"></script>
```

So anything in `theme.css` overrides the base via normal CSS cascade, and `theme.js` runs after `dashboard.js` with graceful self-removal if the file doesn't exist.

---

## Using a theme

Set the folder name in your config:

```yaml
dashboard:
  theme: dark            # or default, light, example_custom, or your own
```

If the name is malformed (not matching `^[a-z0-9_-]{1,40}$`) or the folder is missing, the server logs a warning and falls back to `default`.

---

## Creating a theme

### 1. Color-only tweak (the common case)

Copy one of the shipped themes and edit the tokens:

```bash
cp -r themes/dark themes/my_theme
# edit themes/my_theme/theme.css
```

Then set `dashboard.theme: my_theme` in your config. That's it — no code changes, no restart of anything outside the bot.

#### Required tokens

The base `dashboard.css` reads every one of these through `var(--name)`. If your `theme.css` doesn't define one, the dashboard will render that region with the CSS default (usually transparent or black). **Define all of them.**

| Token                            | Purpose                                                                     |
|----------------------------------|-----------------------------------------------------------------------------|
| `--bg`                           | Page background fallback color                                              |
| `--body-bg`                      | Full `background:` layer (image / gradient / color) for `<body>`            |
| `--body-glow-1`, `--body-glow-2` | Radial glow gradients layered on top of `--body-bg` (use `none` to disable) |
| `--text`                         | Primary foreground text color                                               |
| `--muted`                        | Secondary labels, metadata                                                  |
| `--soft`                         | Tertiary hints                                                              |
| `--panel`                        | Solid panel fallback color                                                  |
| `--panel-bg`                     | Full `background:` for panels (gradient/color)                              |
| `--panel-glow`                   | Top-edge radial glow on panels (or `none`)                                  |
| `--panel3`                       | Deeper panel tone used for inset cards                                      |
| `--line`                         | Hairline border color                                                       |
| `--line-strong`                  | Border for emphasized edges                                                 |
| `--shadow`                       | `box-shadow` for elevated panels                                            |
| `--chip`                         | Background tint for small chips/pills                                       |
| `--accent`                       | Primary brand / active state                                                |
| `--good`                         | Positive PnL, long bias                                                     |
| `--bad`                          | Negative PnL, short bias                                                    |
| `--warn`                         | Warnings, drawdown, near-limit                                              |
| `--chart-bg`                     | Chart panel background                                                      |
| `--chart-grid`                   | Layered `background-image` for chart gridlines                              |
| `--gauge-inner`                  | Interior disc color of the risk gauges                                      |

#### Optional tokens

Shipped themes also define `--accent-2`, `--bg2`, and `--panel2` as convenience aliases. The base CSS doesn't currently read them, but they're a good place to stash variant shades your own rules reuse.

#### Layout tokens (don't override in theme.css)

These live in the base `dashboard.css` under `:root` and are intentionally **shared across themes**. Override only if you know what you're changing:

```
--radius  --top-h  --dock-h  --dock-hover-h  --gap  --left-w  --right-w  --expanded-panel-inset
```

### 2. Beyond colors: add a JS hook

Drop a `theme.js` next to your `theme.css`. It's loaded after `dashboard.js` with `defer`, so all globals `dashboard.js` sets are ready when yours runs. Missing file is silently fine — the `<script>` tag self-removes on 404.

```js
// themes/my_theme/theme.js
document.addEventListener('DOMContentLoaded', () => {
  console.log('theme loaded', document.documentElement.dataset.theme);
});
```

### 3. Beyond JS: a fully custom dashboard

Drop an `index.html` in your theme folder. When present, the server serves **your** HTML instead of the base `dashboard.html`. You own the whole page — the base CSS is not loaded unless you opt in.

The backend contract is just the existing JSON API:

| Endpoint                                                             | Purpose                                                            |
|----------------------------------------------------------------------|--------------------------------------------------------------------|
| `GET /api/state`                                                     | Full dashboard state snapshot (same shape `dashboard.js` consumes) |
| `GET /api/chart?symbol=XYZ&bars=120&timeframe=1m` or `timeframe=htf` | OHLC + indicator payload for the requested symbol                  |
| `GET /health`                                                        | `{"ok": true}` liveness probe                                      |

Four template substitutions are applied to your `index.html` at serve time:

| Token             | Replaced with                                 |
|-------------------|-----------------------------------------------|
| `__REFRESH_MS__`  | Integer poll interval from config             |
| `__IMAGES__`      | JSON object of brand image data URIs          |
| `__BRAND_BADGE__` | The brand badge as a `data:` URI              |
| `__THEME__`       | Your folder name (use it to build asset URLs) |

See `themes/example_custom/index.html` for a minimal working skeleton — it shows all four substitutions, a `fetch('/api/state')` poll loop, and links to its own `theme.css` + `theme.js`.

You can also ship a `mobile.html` in the same folder to override the `/mobile` route.

### 4. Theme-owned images & fonts

Drop files under `themes/<name>/assets/`. Serve them in your CSS or HTML via:

```
/themes/<name>/assets/<path>
```

Extensions are whitelisted — only these are served:

```
png jpg jpeg webp gif svg ico
woff woff2 ttf otf
css js json map
mp3 wav
```

Anything else returns `415 Unsupported Media Type`. Path traversal attempts return `400` or `404`.

---

## Quick reference: what can a theme folder contain?

```
themes/<name>/
├── theme.css      ← required (color/visual tokens)
├── theme.js       ← optional (JS hook, runs after dashboard.js)
├── index.html     ← optional (full desktop template override)
├── mobile.html    ← optional (full mobile template override)
└── assets/        ← optional (theme-owned images, fonts, extra css/js)
    └── ...
```

## Constraints & safety

- Folder name must match `^[a-z0-9_-]{1,40}$` (lowercase, digits, `_`, `-`; max 40 chars).
- All asset paths are validated against path traversal (`..`, `%2E%2E`, symlinks out of tree).
- Requests return `400` for bad names, `404` for missing files, `415` for disallowed extensions.
- All responses set `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- Asset contents are cached in-process once read; **restart the bot** after editing theme files.
