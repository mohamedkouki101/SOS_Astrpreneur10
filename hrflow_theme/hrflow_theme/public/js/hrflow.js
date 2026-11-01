// HRflow: puts the HRflow logo at the top of the Desk sidebar.
// Visual only. The sidebar is re-rendered by Frappe (module change, collapse...),
// so a MutationObserver keeps exactly one logo block as the sidebar's first child.
// If Frappe's markup changes, the worst case is "no logo"; nothing else is affected.
(function () {
	const LOGO = "/assets/hrflow_theme/images/hrflow-logo-on-dark.svg";

	function ensure_brand() {
		const sidebar = document.querySelector(".body-sidebar");
		if (!sidebar) return;
		let brand = sidebar.querySelector(":scope > .hrflow-brand");
		if (!brand) {
			brand = document.createElement("a");
			brand.className = "hrflow-brand";
			brand.href = "/desk";
			brand.setAttribute("aria-label", "HRflow home");
			const img = document.createElement("img");
			img.src = LOGO;
			img.alt = "HRflow";
			brand.appendChild(img);
		}
		if (sidebar.firstElementChild !== brand) sidebar.prepend(brand);
	}

	let queued = false;
	const observer = new MutationObserver(() => {
		if (queued) return;
		queued = true;
		requestAnimationFrame(() => { queued = false; ensure_brand(); });
	});

	function start() {
		ensure_brand();
		observer.observe(document.body, { childList: true, subtree: true });
	}

	if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
	else start();
})();
