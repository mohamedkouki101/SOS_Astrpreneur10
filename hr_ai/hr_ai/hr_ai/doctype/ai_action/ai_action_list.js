frappe.listview_settings["AI Action"] = {
	add_fields: ["status", "kind", "agent"],
	get_indicator(doc) {
		const colors = { Proposed: "orange", Approved: "green", Rejected: "red", Executed: "blue", Failed: "red" };
		return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
	},
};
