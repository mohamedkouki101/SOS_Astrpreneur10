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
	["__security", __("Security checks"), "purple"],
];
const VERDICT_COLORS = { clean: "green", suspicious: "orange", quarantined: "red", released: "purple", rejected: "gray", error: "gray" };
const SOURCE_LABELS = { upload_cv: __("CV upload"), email_body: __("Email"), email_attachment: __("Email attachment") };
const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v)); // report text is hostile: always plain text
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
		this.page.add_inner_button(__("Sync inbox & CVs"), () =>
			frappe.call({ method: "hr_ai.ingest.security_sync", freeze: true, freeze_message: __("Inspecting new emails and CVs...") })
				.then((r) => {
					const m = r.message || {};
					frappe.show_alert({ message: __("{0} new email(s), {1} CV(s) inspected", [m.inbox?.new || 0, m.cvs?.processed || 0]), indicator: "green" });
					this.refresh();
				}));
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
		this.$list.on("click", "[data-sec]", (e) => this.security_action($(e.currentTarget)));
		frappe.realtime.on("list_update", (d) => { if (d && d.doctype === "AI Action") this.refresh(); });
		setInterval(() => { if (frappe.get_route_str() === "rh-ai" && !document.hidden) this.refresh(); }, 15000);
	}

	refresh() {
		this.agent_field.$wrapper.toggle(this.status !== "__security");
		if (this.status === "__security") {
			frappe.call({ method: "hr_ai.ingest.security_reports", type: "GET" })
				.then((r) => { this.reports = r.message || []; this.render(); });
			return;
		}
		frappe.call({
			method: "hr_ai.api.review_queue", type: "GET",
			args: { status: this.status, agent: this.agent || null },
		}).then((r) => { this.data = r.message; this.render(); });
	}

	render() {
		if (!this.data) return;
		const { rows, counts, can_decide } = this.data;
		if (this.data.security_pending != null) counts.__security = this.data.security_pending;
		this.real_field.$wrapper.toggle(!!can_decide);
		this.$tabs.html(TABS.map(([s, label, color]) => `
			<div class="rh-ai-tab ${s === this.status ? "active" : ""}" data-status="${s}">
				<span class="indicator-pill ${color}">${label}</span><span class="count">${counts[s] || 0}</span>
			</div>`).join(""));
		if (this.status === "__security") {
			this.render_security(can_decide);
			return;
		}
		if (!rows.length) {
			this.$list.html(`<div class="rh-ai-empty">${this.status === "Proposed"
				? __("Nothing waiting for a decision. Proposals from the agents will appear here.")
				: __("No actions in this list.")}</div>`);
			return;
		}
		this.$list.html(rows.map((r) => this.card(r, can_decide)).join(""));
	}

	render_security(can_decide) {
		const rows = this.reports || [];
		const intro = `<div class="rh-ai-meta">${__("Every CV and email from outside is inspected by the input security layer before any agent can read it: hidden text removed, personal data masked, prompt injections scored. Quarantined documents never reach the AI unless HR releases them.")}</div>`;
		if (!rows.length) {
			this.$list.html(intro + `<div class="rh-ai-empty">${__("No inspected documents yet. Use Sync inbox & CVs.")}</div>`);
			return;
		}
		const order = { quarantined: 0, suspicious: 1 };
		rows.sort((a, b) => (order[a.verdict] ?? 2) - (order[b.verdict] ?? 2));
		this.$list.html(intro + rows.map((r) => {
			const v = r.decision || (r.status === "accepted" ? r.verdict : "rejected") || "error";
			const link = r.linked ? ` · <a href="/desk/${frappe.router.slug(r.linked[0])}/${encodeURIComponent(r.linked[1])}">${esc(__(r.linked[0]))}</a>` : "";
			const can_release = can_decide && r.verdict === "quarantined" && !r.decision;
			const can_reject = can_decide && r.status === "accepted" && !r.decision;
			return `
			<div class="rh-ai-card">
				<div class="rh-ai-card-head">
					<span class="title">${esc(SOURCE_LABELS[r.source_type] || r.source_type)}${r.filename ? " · " + esc(r.filename) : ""}</span>
					<span class="indicator-pill gray">${__("Risk")} ${Math.round((r.risk_score_bp || 0) / 100)}%</span>
					<span class="indicator-pill ${VERDICT_COLORS[v] || "gray"}">${esc(__(v))}</span>
				</div>
				<div class="rh-ai-meta">${esc(r.received_at)} · ${r.released_to_ai ? __("sent to the AI (masked)") : __("held back from the AI")}${r.rejection ? " · " + esc(r.rejection) : ""}${link}</div>
				<div class="rh-ai-actions">
					<button class="btn btn-default btn-sm" data-sec="inspect" data-id="${esc(r.report_id)}">${__("Inspect")}</button>
					${can_release ? `<button class="btn btn-warning btn-sm" data-sec="release" data-id="${esc(r.report_id)}">${__("Release to AI")}</button>` : ""}
					${can_reject ? `<button class="btn btn-danger btn-sm" data-sec="reject" data-id="${esc(r.report_id)}">${__("Reject")}</button>` : ""}
				</div>
			</div>`;
		}).join(""));
	}

	security_action($btn) {
		const id = $btn.data("id"), action = $btn.data("sec");
		if (action === "inspect") return this.inspect(id);
		frappe.prompt({ fieldname: "reason", fieldtype: "Small Text", reqd: 1,
			label: action === "release" ? __("Why is it safe to release this document to the AI?") : __("Why are you rejecting this document?") },
			(v) => frappe.call({ method: "hr_ai.ingest.security_decide", args: { report_id: id, action, reason: v.reason }, freeze: true })
				.then(() => { frappe.show_alert({ message: action === "release" ? __("Released to the AI, flagged as suspicious") : __("Rejected"), indicator: "green" }); this.refresh(); }),
			action === "release" ? __("Release to AI") : __("Reject document"), action === "release" ? __("Release") : __("Reject"));
	}

	inspect(id) {
		frappe.call({ method: "hr_ai.ingest.security_report", type: "GET", args: { report_id: id } }).then((r) => {
			const rep = r.message;
			const pill = { pass: "green", warn: "orange", fail: "red", skipped: "gray" };
			const section = (title, body) => body ? `<h5 style="margin-top:16px">${title}</h5>${body}` : "";
			const list = (items, fn) => items && items.length ? `<ul>${items.map(fn).join("")}</ul>` : "";
			const pre = (t) => `<pre class="rh-ai-summary">${esc(t)}</pre>`;
			const html = `
				<div class="rh-ai-meta">${esc(SOURCE_LABELS[rep.source.type] || rep.source.type)} · ${esc(rep.source.file_type)} · ${__("risk")} ${Math.round(rep.risk_score_bp / 100)}% · ${esc(rep.verdict || rep.status)}</div>
				${section(__("Pipeline"), list(rep.stages, (s) => `<li><span class="indicator-pill ${pill[s.status] || "gray"}">${esc(s.status)}</span> <b>${esc(s.stage)}</b>: ${esc(s.detail)}</li>`))}
				${section(__("Why this score"), list(rep.reasons, (x) => `<li>${esc(x.label)} <span class="text-muted">(+${Math.round(x.points / 100)}%)</span></li>`))}
				${section(__("Hidden content removed"), list(rep.removed, (x) => `<li>${esc(x.label)} · ${esc(x.location)}${pre(x.text)}</li>`))}
				${section(__("Injection attempts"), list(rep.injection_matches, (x) => `<li>${esc(x.label)} (${esc(x.where)})${pre(x.text)}</li>`))}
				${section(__("Personal data protected"), list(rep.masked, (x) => `<li><code>${esc(x.token)}</code> = ${esc(x.original)}</li>`) + list(rep.protected_removed, (x) => `<li>${esc(x.attribute)} ${__("removed")}</li>`))}
				${section(__("What the AI receives"), rep.text.sent ? pre(rep.text.sent) : `<div class="text-muted">${__("Nothing: held back from the AI.")}</div>`)}
				${section(__("Original text (as a naive extractor would read it)"), pre(rep.text.original))}
				${rep.decision ? section(__("HR decision"), `<div>${esc(rep.decision.action)} · ${esc(rep.decision.decided_at)} · ${esc(rep.decision.reason)}</div>`) : ""}`;
			const d = new frappe.ui.Dialog({ title: __("Inspection report"), size: "extra-large",
				fields: [{ fieldtype: "HTML", fieldname: "body" }] });
			d.fields_dict.body.$wrapper.html(html);
			d.show();
		});
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
