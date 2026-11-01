// Sign-in step in front of each tile of the apps screen (/desk). Server side: hr_ai/app_gate.py.
// Clicking a tile opens a sign-in dialog instead of the app; the server checks the credentials and
// the account type (chat user or admin), switches the session, then the app opens.
(function () {
	// Tiles are recognised by their logo (Frappe renders no app id on the tile).
	const TILES = {
		"/assets/hr_ai/images/hr-chat-logo.svg": { app: "rh_chat", kind: "user" },
		"/assets/hr_ai/images/hr-flow-favicon.svg": { app: "hrms", kind: "admin" },
		"/assets/hr_ai/images/hr-ai-logo.svg": { app: "hr_ai", kind: "admin" },
		"/assets/hr_ai/images/hr-flow-admin.svg": { app: "frappe", kind: "admin" },
	};

	function tile_for(el) {
		if (!document.body.classList.contains("desktop-page")) return null;
		const icon = el.closest(".desktop-icon");
		if (!icon || icon.closest(".modal")) return null;
		const img = icon.querySelector("img.app-icon");
		if (!img) return null;
		const gate = TILES[new URL(img.getAttribute("src"), location.origin).pathname];
		if (!gate) return null;
		const app = (frappe.boot.app_data || []).find((a) => a.app_name === gate.app) || {};
		return { ...gate, title: (icon.querySelector(".icon-title") || {}).textContent || app.app_title,
			route: app.app_route || "/desk", logo: img.getAttribute("src") };
	}

	function open_dialog(t) {
		const admin = t.kind === "admin";
		const d = new frappe.ui.Dialog({
			title: __("Sign in to {0}", [t.title]),
			fields: [
				{ fieldtype: "HTML", fieldname: "intro", options: `
					<div class="hf-gate-intro">
						<img src="${frappe.utils.escape_html(t.logo)}" alt="">
						<div>${admin ? __("This app needs an <b>administrator</b> account.")
							: __("This app needs a <b>user</b> account (employee or applicant).")}</div>
					</div>` },
				{ fieldtype: "Data", fieldname: "usr", label: __("Login ID"), reqd: 1,
					placeholder: admin ? "Administrator" : "Utilisateur" },
				{ fieldtype: "Password", fieldname: "pwd", label: __("Password"), reqd: 1 },
				{ fieldtype: "HTML", fieldname: "error", options: '<div class="hf-gate-error"></div>' },
			],
			primary_action_label: __("Sign in"),
			async primary_action(values) {
				const $err = d.fields_dict.error.$wrapper.find(".hf-gate-error").text("");
				const $btn = d.get_primary_btn().prop("disabled", true);
				try {
					const r = await fetch("/api/method/hr_ai.app_gate.app_login", {
						method: "POST",
						headers: { "Content-Type": "application/json", Accept: "application/json",
							"X-Frappe-CSRF-Token": frappe.csrf_token },
						body: JSON.stringify({ app: t.app, usr: values.usr, pwd: values.pwd }),
					});
					const data = await r.json().catch(() => ({}));
					if (r.ok && data.message) {
						window.location.href = t.route;   // full page load with the new session
						return;
					}
					let msg = r.status === 429 ? __("Too many attempts. Wait a minute and try again.")
						: __("Sign-in failed.");
					try { msg = JSON.parse(JSON.parse(data._server_messages)[0]).message || msg; } catch (e) {
						if (typeof data.message === "string") msg = data.message;
					}
					$err.text(msg);
				} catch (e) {
					$err.text(__("Network error. Try again."));
				}
				d.set_value("pwd", "");
				$btn.prop("disabled", false);
			},
		});
		d.$wrapper.addClass("hf-gate-dialog");
		d.show();
		setTimeout(() => d.get_field("usr").$input.trigger("focus"), 200);
	}

	// Capture phase: runs before the tile's own click handler, which would open the app.
	document.addEventListener("click", (ev) => {
		const t = tile_for(ev.target);
		if (!t) return;
		ev.preventDefault();
		ev.stopImmediatePropagation();
		open_dialog(t);
	}, true);
})();
