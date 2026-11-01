# hrflow_theme: HRflow look for Frappe HR v16

**Deploying it, or building UI that should match it?** See [INTEGRATION.md](INTEGRATION.md).

A small Frappe app that re-skins the whole Desk (and the login page) with the HRflow
brand. **Styling only**: no DocType, no API, no business logic, and nothing in `frappe`,
`erpnext`, `hrms` or `hr_ai` is modified. Uninstall it and the site looks exactly as before.

## What it changes
| Area | How |
|---|---|
| Colours, surfaces, text, borders, status colours | Frappe v16 design tokens (CSS variables) remapped to the HRflow palette, so every module follows |
| Sidebar | deep royal panel, rounded edge, curved bands at the bottom, soft-blue active item, HRflow logo on top |
| Page header | cream, light-blue diagonal bands, bold navy title |
| Buttons / inputs / checkboxes | pill buttons (electric blue primary), warm-white inputs with a blue focus ring |
| Cards, workspace widgets, forms, lists, reports | warm-white cards, 16 px radius, soft shadow, cream table headers, blue row hover |
| Status pills | soft tint plus matching border (pending amber, success green, info blue, error red, failed magenta) |
| HR AI Decisions page (`hr_ai`) | banner, status tabs, decision cards with a left accent bar, matching the reference design (CSS only, its JS is untouched) |
| Login page | cream background, warm-white card, HRflow logo, `app_name` = HR Flow |
| Home page (`/desk` apps screen) | large light-blue diagonal stripes (`home-stripes.svg`) and soft curved bands (`home-arcs.svg`), tiles on warm-white cards |
| Branding | logo (Website/Navbar Settings, splash, website navbar), favicon = the "HR" end of the logo (`hr_ai/public/images/hr-flow-favicon.svg`), app name "HR Flow" (Website + System Settings, English overrides of "Frappe HR", "Framework"...). Re-apply with `bench --site frontend execute hrflow_theme.install.apply_branding` |

The dark theme (user setting) is left as Frappe's own.

## Files
```
hrflow_theme/
├── pyproject.toml
└── hrflow_theme/
    ├── hooks.py             # app_include_css/js, web_include_css, install hooks
    ├── install.py           # sets the logo + app name in Website/Navbar Settings, restores on uninstall
    ├── modules.txt, patches.txt, hrflow_theme/__init__.py   # standard app skeleton
    └── public/
        ├── css/hrflow.css       # Desk theme: brand tokens -> Frappe tokens -> components
        ├── css/hrflow-web.css   # login / website pages
        ├── js/hrflow.js         # inserts the logo at the top of the sidebar (nothing else)
        └── images/
            ├── hrflow-logo.svg          # official logo, artwork unchanged, viewBox trimmed
            ├── hrflow-logo-on-dark.svg  # same + white fill behind the badge, for the blue sidebar
            ├── sidebar-arcs.svg         # decoration
            └── header-stripes.svg       # decoration
```

## Install
Custom apps must be **built into the image** (queue/scheduler containers must see them too).
`platform/build-images.sh` already adds this app as the last image layer, and `scripts/setup_stack.py site`
installs it on the site. By hand, after a rebuild:

```bash
docker compose -p hr -f platform/hr.yml up -d
docker compose -p hr -f platform/hr.yml exec backend bench --site frontend install-app hrflow_theme
docker compose -p hr -f platform/hr.yml exec backend bench --site frontend clear-cache
```
Then hard-refresh the browser (Ctrl+Shift+R).

**Quick try on a dev bench** (no Docker): copy the folder to `apps/`, then
`bench --site <site> install-app hrflow_theme && bench build --app hrflow_theme && bench --site <site> clear-cache`.

## Uninstall
```bash
bench --site frontend uninstall-app hrflow_theme
```
Branding settings go back to their previous values (only if nobody changed them since).

## Notes for the team
- **Font: Inter.** The reference screenshot uses Inter (Frappe v16's default UI font), so it is kept.
  No extra font is loaded.
- **Colours** are written as HEX only once, at the top of `hrflow.css` (`--hf-*` tokens);
  every other colour is derived from them. Change the brand there.
- **Logo:** the SVG paths are copied verbatim from `HRFlow_logo.svg`. Only the viewBox is
  trimmed, and the "on-dark" version adds a white fill behind the badge so it reads on blue.
- **Install side effect:** `install.py` overwrites Website Settings `app_logo` / `app_name` and
  Navbar Settings `app_logo` (the site used `/files/rh-flow-logo.svg` before). Restored on uninstall.
- Selectors were checked against the v16 desk bundle. If a future Frappe update renames a class,
  that part simply falls back to Frappe's normal style.
