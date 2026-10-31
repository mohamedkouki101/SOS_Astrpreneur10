// RH AI decision page: every action proposed by an agent waits here until a human Allows or Declines it.
// Allow -> AI Action.approve() (signed approval) -> Execution Gateway; Decline -> AI Action.reject(reason).

const KIND_LABELS = {
	email_reply: __("Email reply"), digest: __("Inbox digest"), ranking: __("Candidate ranking"),
	applicant_status: __("Applicant status change"), interview: __("Interview scheduling"),
	leave_decision: __("Leave decision"), onboarding_tasks: __("Onboarding tasks"),
	welcome_email: __("Welcome email"), payroll_reply: __("Payroll answer"),
	review_summary: __("Performance review summary"), grievance_triage: __("Grievance triage"),
};
const TABS = [
	["Proposed", __("Waiting for decision"), "orange"],
	["Approved", __("Allowed"), "green"],
	["Executed", __("Done"), "blue"],
	["Rejected", __("Declined"), "red"],
	["Failed", __("Failed"), "red"],
];
const AGENTS = ["", "recruitment", "comms", "leave", "onboarding", "payroll", "performance", "conflict"];

frappe.pages["rh-ai"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("RH AI: Decisions"), single_column: true });
	wrapper.rh_ai = new RHAIDecisions(page);
};

frappe.pages["rh-ai"].on_page_show = function (wrapper) {
	wrapper.rh_ai && wrapper.rh_ai.refresh();
};

class RHAIDecisions {
	constructor(page) {
		this.page = page;
		this.status = "Proposed";
		this.agent = "";
		this.show_real = false;
		this.page.set_secondary_action(__("Refresh"), () => this.refresh(), "refresh");
		this.agent_field = this.page.add_field({
			fieldname: "agent", label: __("Agent"), fieldtype: "Select",
			options: AGENTS.map((a) => ({ value: a, label: a ? frappe.utils.to_title_case(a) : __("All agents") })),
			change: () => { this.agent = this.agent_field.get_value(); this.refresh(); },
		});
		this.real_field = this.page.add_field({
			fieldname: "show_real", label: __("Show real names"), fieldtype: "Check",
			change: () => { this.show_real = !!this.real_field.get_value(); this.render(); },
		});
		this.$body = $(`<div class="rh-ai"></div>`).appendTo(this.page.main);
		this.$body.html(`
			<style>
				.rh-ai { padding: var(--padding-md) 0; }
				.rh-ai-intro { color: var(--text-muted); margin-bottom: var(--margin-md); }
				.rh-ai-tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: var(--margin-lg); }
				.rh-ai-tab { border: 1px solid var(--border-color); border-radius: var(--border-radius-md); background: var(--card-bg);
					padding: 8px 14px; cursor: pointer; display: flex; gap: 10px; align-items: center; }
				.rh-ai-tab.active { border-color: var(--primary); box-shadow: 0 0 0 1px var(--primary); }
				.rh-ai-tab .count { font-size: var(--text-lg); font-weight: 600; }
				.rh-ai-card { border: 1px solid var(--border-color); border-radius: var(--border-radius-md); background: var(--card-bg);
					padding: var(--padding-md); margin-bottom: var(--margin-md); }
				.rh-ai-card-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }
				.rh-ai-card-head .title { font-weight: 600; font-size: var(--text-md); margin-right: auto; }
				.rh-ai-meta { color: var(--text-muted); font-size: var(--text-sm); margin-bottom: 8px; }
				.rh-ai-summary { white-space: pre-wrap; background: var(--subtle-fg); border-radius: var(--border-radius);
					padding: 10px 12px; font-size: var(--text-sm); max-height: 320px; overflow: auto; margin: 0; }
				.rh-ai-result { margin-top: 8px; font-size: var(--text-sm); }
				.rh-ai-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
				.rh-ai-empty { text-align: center; color: var(--text-muted); padding: 48px 0; }
			</style>
			<div class="rh-ai-intro">${__("Agents only propose. Nothing is sent or changed until a human Allows it here. Names are masked as [PERSON_1], [EMAIL_1]... the AI never sees real personal data.")}</div>
			<div class="rh-ai-tabs"></div>
			<div class="rh-ai-list"></div>`);
		this.$tabs = this.$body.find(".rh-ai-tabs");
		this.$list = this.$body.find(".rh-ai-list");
		this.$tabs.on("click", ".rh-ai-tab", (e) => { this.status = $(e.currentTarget).data("status"); this.refresh(); });
		this.$list.on("click", "[data-action]", (e) => this.act($(e.currentTarget)));
		frappe.realtime.on("list_update", (d) => { if (d && d.doctype === "AI Action") this.refresh(); });
		setInterval(() => { if (frappe.get_route_str() === "rh-ai" && !document.hidden) this.refresh(); }, 15000);
	}

	refresh() {
		frappe.call({
			method: "hr_ai.api.review_queue", type: "GET",
			args: { status: this.status, agent: this.agent || null },
		}).then((r) => { this.data = r.message; this.render(); });
	}

	render() {
		if (!this.data) return;
		const { rows, counts, can_decide } = this.data;
		this.real_field.$wrapper.toggle(!!can_decide);
		this.$tabs.html(TABS.map(([s, label, color]) => `
			<div class="rh-ai-tab ${s === this.status ? "active" : ""}" data-status="${s}">
				<span class="indicator-pill ${color}">${label}</span><span class="count">${counts[s] || 0}</span>
			</div>`).join(""));
		if (!rows.length) {
			this.$list.html(`<div class="rh-ai-empty">${this.status === "Proposed"
				? __("Nothing waiting for a decision. Proposals from the agents will appear here.")
				: __("No actions in this list.")}</div>`);
			return;
		}
		this.$list.html(rows.map((r) => this.card(r, can_decide)).join(""));
	}

	text(r, value) {
		let t = value || "";
		if (this.show_real && r.token_map) {
			for (const [token, real] of Object.entries(r.token_map)) t = t.split(token).join(real);
		}
		return frappe.utils.escape_html(t);
	}

	card(r, can_decide) {
		const tab = TABS.find((t) => t[0] === r.status) || [r.status, r.status, "gray"];
		const ref = r.reference_doctype && r.reference_name
			? ` · <a href="/desk/${frappe.router.slug(r.reference_doctype)}/${encodeURIComponent(r.reference_name)}">${__(r.reference_doctype)}</a>` : "";
		const decided = r.approver
			? `<div class="rh-ai-meta">${r.status === "Rejected" ? __("Declined") : __("Allowed")} ${__("by")} ${frappe.utils.escape_html(r.approver)} · ${frappe.datetime.comment_when(r.approved_on)}${r.decision_note ? " · " + frappe.utils.escape_html(r.decision_note) : ""}</div>` : "";
		const result = r.execution_result
			? `<div class="rh-ai-result"><span class="indicator-pill ${r.status === "Failed" ? "red" : "blue"}">${__("Result")}</span> ${frappe.utils.escape_html(r.execution_result)}</div>` : "";
		const buttons = r.status === "Proposed" && can_decide
			? `<button class="btn btn-primary btn-sm" data-action="allow" data-name="${r.name}">${__("Allow")}</button>
			   <button class="btn btn-danger btn-sm" data-action="decline" data-name="${r.name}">${__("Decline")}</button>` : "";
		return `
			<div class="rh-ai-card">
				<div class="rh-ai-card-head">
					<span class="title">${KIND_LABELS[r.kind] || r.kind}</span>
					<span class="indicator-pill gray">${__("Agent")}: ${frappe.utils.to_title_case(r.agent || "")}</span>
					<span class="indicator-pill ${tab[2]}">${tab[1]}</span>
				</div>
				<div class="rh-ai-meta">${r.name} · ${__("proposed")} ${frappe.datetime.comment_when(r.creation)}${ref}</div>
				<pre class="rh-ai-summary">${this.text(r, r.summary)}</pre>
				${decided}${result}
				<div class="rh-ai-actions">
					${buttons}
					<a class="btn btn-default btn-sm" href="/desk/ai-action/${r.name}">${__("Details / edit")}</a>
				</div>
			</div>`;
	}

	act($btn) {
		const name = $btn.data("name");
		const run = (method, args = {}) =>
			frappe.call({ method: "run_doc_method", args: { dt: "AI Action", dn: name, method, args }, freeze: true })
				.then(() => {
					frappe.show_alert({ message: method === "approve" ? __("Allowed: {0}", [name]) : __("Declined: {0}", [name]),
						indicator: method === "approve" ? "green" : "red" });
					this.refresh();
				});
		if ($btn.data("action") === "allow") {
			frappe.confirm(__("Allow {0}? The agent's proposal will be carried out exactly as shown.", [name]), () => run("approve"));
		} else {
			frappe.prompt({ fieldname: "note", fieldtype: "Small Text", label: __("Why are you declining?"), reqd: 1 },
				(v) => run("reject", { note: v.note }), __("Decline {0}", [name]), __("Decline"));
		}
	}
}
