app_name = "hrflow_theme"
app_title = "HRflow Theme"
app_publisher = "SOS_Astrpreneur1 team"
app_description = "HRflow visual theme for Frappe HR v16 (styling only)"
app_email = "team@rhflow.example"
app_license = "UNLICENSED"  # TODO: team to choose a licence (Frappe HR is GPL v3)

required_apps = ["frappe", "hrms"]

# Desk (/desk): design tokens + component styles, and a tiny script that puts the logo in the sidebar.
app_include_css = "/assets/hrflow_theme/css/hrflow.css"
app_include_js = "/assets/hrflow_theme/js/hrflow.js"

# Website pages, including /login.
web_include_css = "/assets/hrflow_theme/css/hrflow-web.css"

# Branding stored in Website Settings / Navbar Settings (login page logo, page title).
# Previous values are saved on install and restored on uninstall.
after_install = "hrflow_theme.install.apply_branding"
before_uninstall = "hrflow_theme.install.restore_branding"
