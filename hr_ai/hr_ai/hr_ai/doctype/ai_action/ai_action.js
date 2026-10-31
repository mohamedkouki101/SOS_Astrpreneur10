frappe.ui.form.on("AI Action", {
	refresh(frm) {
		const colors = { Proposed: "orange", Approved: "green", Rejected: "red", Executed: "blue", Failed: "red" };
		frm.page.set_indicator(__(frm.doc.status), colors[frm.doc.status] || "gray");
		const intro = {
			Proposed: [__("Proposed by the {0} agent. Nothing has been sent or changed. Review, edit if needed, then Allow or Decline.", [frm.doc.agent]), "orange"],
			Approved: [frm.doc.execution_result ? __("Allowed. {0}", [frm.doc.execution_result]) : __("Allowed. The Execution Gateway is running it now: refresh in a few seconds."), "green"],
			Executed: [__("Executed: {0}", [frm.doc.execution_result || ""]), "blue"],
			Failed: [__("Execution failed: {0}", [frm.doc.execution_result || ""]), "red"],
		}[frm.doc.status];
		frm.set_intro(intro ? intro[0] : "", intro ? intro[1] : "");
		if (frm.doc.result_doctype && frm.doc.result_name) {
			frm.add_custom_button(__("Open {0}", [__(frm.doc.result_doctype)]), () =>
				frappe.set_route("Form", frm.doc.result_doctype, frm.doc.result_name)
			);
		}
		if (frm.doc.status === "Approved" && !frm.doc.execution_result) setTimeout(() => frm.doc.status === "Approved" && frm.reload_doc(), 4000);
		frm.add_custom_button(__("All decisions"), () => frappe.set_route("rh-ai"));
		if (frm.doc.status !== "Proposed" || frm.is_dirty()) return;
		if (!frappe.user.has_role(["HR Manager", "System Manager"])) return;

		frm.add_custom_button(__("Allow"), () => {
			frappe.confirm(__("Allow this {0} proposal?", [frm.doc.kind]), () =>
				frm.call("approve").then(() => frm.reload_doc())
			);
		}).addClass("btn-primary");

		frm.add_custom_button(__("Decline"), () => {
			frappe.prompt(
				{ fieldname: "note", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
				(v) => frm.call("reject", { note: v.note }).then(() => frm.reload_doc()),
				__("Decline proposal")
			);
		});
	},
});
