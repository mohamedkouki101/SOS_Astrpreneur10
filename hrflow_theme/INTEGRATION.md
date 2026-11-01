# Integrating the HRflow theme

How to deploy `hrflow_theme` on the team's Frappe HR v16 site, and how the other layers
(`hr_ai` pages, future UI work) stay consistent with it.

**In short:** it is a separate Frappe app with no DocTypes and no backend logic. It adds CSS,
one small JS file and images through standard hooks, and sets the logo in two settings.
It does not modify `frappe`, `erpnext`, `hrms` or `hr_ai`, and uninstalling it restores the old look.

---

## 1. What the app plugs into

| Hook (`hooks.py`) | Value | Effect |
|---|---|---|
| `app_include_css` | `/assets/hrflow_theme/css/hrflow.css` | Desk theme, loaded on every `/desk` page |
| `app_include_js` | `/assets/hrflow_theme/js/hrflow.js` | Puts the logo at the top of the sidebar, nothing else |
| `web_include_css` | `/assets/hrflow_theme/css/hrflow-web.css` | Login page and website pages |
| `after_install` | `hrflow_theme.install.apply_branding` | Website Settings `app_logo` and `app_name`, Navbar Settings `app_logo` |
| `before_uninstall` | `hrflow_theme.install.restore_branding` | Puts those three values back |

`required_apps = ["frappe", "hrms"]`. There is no dependency on `hr_ai`: the RH AI page
styles simply do nothing if that page is not installed.

---

## 2. Deploying it (Docker setup, `~/frappe_docker`)

> CLAUDE.md §8: custom apps must be built into the image. The backend, queue and scheduler
> containers all load every installed app's hooks, so an app present in only one container
> breaks the others.

`apps.json` needs one Git repository **per app, with the app at the repository root**.
In our repo, `hrflow_theme/` is a subfolder (like `hr_ai/`). Pick one of the two options below.

### Option A: own repository (recommended, cleanest for the image build)
1. Create a repo, e.g. `github.com/<team>/hrflow_theme`, and copy the **contents** of this
   `hrflow_theme/` folder to its root (so `pyproject.toml` sits at the top).
2. Add it to `apps.json`:
   ```json
   [
     { "url": "https://github.com/frappe/erpnext", "branch": "version-16" },
     { "url": "https://github.com/frappe/hrms", "branch": "version-16" },
     { "url": "https://github.com/<team>/hrflow_theme", "branch": "main" }
   ]
   ```
   Keep any `hr_ai` entry the team already has. Order does not matter for the theme.
3. Rebuild the image as `custom:N` (same command the team used for `custom:16`, about 10–15 min)
   and update the image tag for **every** service in `hr.yml`.
4. Start the stack and install:
   ```bash
   docker compose -p hr -f hr.yml up -d
   docker compose -p hr -f hr.yml exec backend bench --site frontend install-app hrflow_theme
   docker compose -p hr -f hr.yml exec backend bench --site frontend clear-cache
   ```
5. Hard-refresh the browser (Ctrl+Shift+R).

### Option B: same way the team ships `hr_ai`
If `hr_ai` is already deployed from this monorepo (copied into the image, mounted volume, etc.),
do exactly the same for `hrflow_theme/`, then run steps 4–5 above.

### Dev bench (no Docker)
```bash
cp -r hrflow_theme ~/frappe-bench/apps/
cd ~/frappe-bench
./env/bin/pip install -e apps/hrflow_theme
echo hrflow_theme >> sites/apps.txt          # if bench did not add it
bench build --app hrflow_theme
bench --site <site> install-app hrflow_theme
bench --site <site> clear-cache
```

---

## 3. Checking that it works

| Check | Expected |
|---|---|
| `bench --site frontend list-apps` | shows `hrflow_theme` |
| Open `http://<host>/assets/hrflow_theme/css/hrflow.css` | the CSS file (not a 404) |
| `/login` | cream background, warm-white card, HRflow logo, "HRflow" title |
| `/desk` | royal-blue sidebar with the HRflow logo on top, cream page, pill buttons |
| Employee / Job Applicant list | warm-white list, cream header row, blue hover |
| `/desk/rh-ai` (RH AI Decisions) | info banner pill, coloured status tabs, cards with a blue left bar |
| User menu → Toggle theme → Dark | Frappe's normal dark theme (the theme is light-only on purpose) |

---

## 4. For people building UI in `hr_ai` (or any other app)

New pages pick up the theme automatically **if they use Frappe's standard classes and CSS variables**
rather than hard-coded colours.

**Do**
- Use Frappe components: `.btn-primary`, `.btn-default`, `.indicator-pill <color>`, `.frappe-card`,
  `.form-control`, `frappe.ui.Dialog`, `page.add_field`, and so on.
- Use Frappe variables for colours: `var(--text-color)`, `var(--text-muted)`, `var(--card-bg)`,
  `var(--border-color)`, `var(--primary)`, `var(--bg-blue)` / `var(--text-on-blue)`, etc.
  They are remapped to HRflow colours.
- When you need a brand colour directly, use the HRflow tokens (defined in `hrflow.css`),
  always with a fallback so the page still works without the theme:

  | Token | Colour | Use |
  |---|---|---|
  | `--hf-royal` | `#073B9B` | brand, strong accents |
  | `--hf-electric` | `#1455E8` | primary actions, active states |
  | `--hf-soft` | `#76A9FF` | secondary accents, hovers |
  | `--hf-cream` | `#F8F0DF` | page background |
  | `--hf-white` | `#FFFDF8` | cards, panels |
  | `--hf-ink` | `#14264D` | text, headings |
  | `--hf-pending` `--hf-success` `--hf-info` `--hf-error` `--hf-failed` | status | |

  ```css
  .my-widget { color: var(--hf-ink, var(--text-color)); border-color: var(--hf-electric, var(--primary)); }
  ```

**Don't**
- Hard-code HEX colours or `#fff`/`#000` backgrounds (they will clash with the cream palette).
- Load another font. The theme uses Inter, Frappe's default.
- Override `--hf-*` tokens in your app. Change the brand in one place: the top of `hrflow.css`.

### The RH AI Decisions page contract
`hrflow.css` (section 4) styles the existing markup of `hr_ai/.../page/rh_ai/rh_ai.js`. It depends on
these class names and attributes. **If you rename them, tell the theme owner**; nothing breaks,
the page just falls back to the plain style.

| Selector | Styled as |
|---|---|
| `.rh-ai` | page root (all rules are scoped under it) |
| `.rh-ai-intro` | info banner pill with a sparkle |
| `.rh-ai-tabs`, `.rh-ai-tab`, `.rh-ai-tab.active`, `.rh-ai-tab .count` | status tabs, the selected one solid blue |
| `.rh-ai-tab[data-status="Proposed"\|"Approved"\|"Executed"\|"Rejected"\|"Failed"]` | amber, green, blue, red, magenta tint |
| `.rh-ai-card`, `.rh-ai-card-head .title` | decision card with left accent bar |
| `.rh-ai-meta`, `.rh-ai-summary`, `.rh-ai-result`, `.rh-ai-actions`, `.rh-ai-empty` | meta line, proposal text, result, buttons, empty state |

The page's own inline `<style>` can stay: the theme's rules are more specific and win.

---

## 5. Things that could affect the existing setup

| Point | Detail |
|---|---|
| Branding settings | Install overwrites Website Settings `app_logo` / `app_name` and Navbar Settings `app_logo` (currently `/files/rh-flow-logo.svg`). The old values are saved and restored on uninstall, unless someone changed them in between. |
| Global look | All Desk pages change colour, radius and shadows. Behaviour, permissions and data are not affected. |
| Dark theme | Not themed; users who pick Dark get Frappe's dark theme. |
| Frappe updates | Selectors were checked against the v16 desk bundle. If an update renames a class, that element falls back to Frappe's default style. |
| Merging into the repo | The app only adds the `hrflow_theme/` folder, so merging it cannot overwrite other code. `apps.json` / `hr.yml` changes are made by whoever owns the Docker setup. |

---

## 6. Rollback

```bash
docker compose -p hr -f hr.yml exec backend bench --site frontend uninstall-app hrflow_theme
docker compose -p hr -f hr.yml exec backend bench --site frontend clear-cache
```
To remove it from the image, delete its `apps.json` entry and rebuild.

---

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| Nothing changed | `bench --site frontend clear-cache`, then Ctrl+Shift+R. Check that `list-apps` shows `hrflow_theme`. |
| `/assets/hrflow_theme/...` returns 404 | Assets were not built or the frontend container uses an old image: run `bench build --app hrflow_theme`, or rebuild the image and update **all** tags in `hr.yml`. |
| `ModuleNotFoundError: hrflow_theme` in queue/scheduler logs | The app is installed in only one container. Build it into the image (Option A or B). |
| Logo missing in the sidebar | Check the browser console for `hrflow.js` errors, and that `/assets/hrflow_theme/images/hrflow-logo-on-dark.svg` loads. |
| Login page still shows the old logo | Website Settings → App Logo was changed after install, or `after_install` did not run. Set it to `/assets/hrflow_theme/images/hrflow-logo.svg`. |
