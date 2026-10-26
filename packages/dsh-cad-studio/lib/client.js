window.__ModuleLoader__.load({
	id: "dsh-cad-studio",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let react = require("react");
		const React = react;
		// cad-studio Client 半件：把 Host 的 presentationMeta 投影成工具卡片。
		//
		// 挂载点：`tool.call.toolview`（keyed by tool name，来自 client-ui-tool 的
		// conversation.chat.node → tool-call 声明树）。Host 插件在 output.presentationMeta
		// 里给出 { kind, ok, error, images, preview, metrics, ... }，Client 侧
		// ToolResultNode.meta 原样携带。
		//
		// 仅用 React.createElement，无 JSX / import / TS。
		const el = React.createElement;

		function parseArgs(raw) {
			try { return JSON.parse(raw || "{}"); } catch { return {}; }
		}

		function numberFmt(n) {
			const v = Number(n);
			return Number.isFinite(v) ? (Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(3)) : String(n ?? "-");
		}

		function MetricLine({ m }) {
			if (!m || typeof m !== "object") return null;
			const bits = [];
			if (m.volume_mm3 !== undefined) bits.push(`体积 ${numberFmt(m.volume_mm3)} mm³`);
			if (m.volume !== undefined) bits.push(`体积 ${numberFmt(m.volume)} mm³`);
			if (m.bbox_mm && Array.isArray(m.bbox_mm)) bits.push(`边界 ${m.bbox_mm.map((x) => numberFmt(x)).join(" × ")} mm`);
			if (m.bbox && Array.isArray(m.bbox) && !m.bbox_mm) bits.push(`边界 ${m.bbox.map((x) => numberFmt(x)).join(", ")}`);
			if (m.faces !== undefined) bits.push(`${m.faces} 面`);
			if (m.edges !== undefined) bits.push(`${m.edges} 边`);
			if (m.solids !== undefined) bits.push(`${m.solids} 实体`);
			if (m.valid === false) bits.push("B-Rep 无效");
			if (bits.length === 0) return null;
			return el("div", { style: { font: "var(--dsw-font-xs-13)", color: "var(--dsw-alias-label-secondary)", lineHeight: "18px" } }, bits.join(" · "));
		}

		function Chip({ tone, children }) {
			const color = tone === "error" ? "var(--dsw-alias-state-error-primary)"
				: tone === "warn" ? "var(--dsw-alias-state-warning-primary, #f59e0b)"
				: "var(--dsw-alias-state-success-primary, #22c55e)";
			return el("span", {
				style: {
					font: "var(--dsw-font-xs-13)",
					lineHeight: "20px",
					padding: "0 8px",
					borderRadius: "999px",
					border: "1px solid " + color,
					color,
					background: "var(--dsw-alias-bg-base)",
					flex: "none",
				},
			}, children);
		}

		function MetaChips({ meta }) {
			const chips = [];
			if (!meta) return null;
			if (meta.kind === "cad-mates" && meta.overall) {
				const tone = meta.overall === "PASS" ? "ok" : meta.overall === "FAIL" ? "error" : "warn";
				chips.push(el(Chip, { key: "mates", tone }, `mates ${meta.overall}`));
			}
			if (meta.kind === "cad-run") {
				if (meta.warnings) chips.push(el(Chip, { key: "w", tone: "warn" }, `${meta.warnings} warnings`));
				if (meta.errors) chips.push(el(Chip, { key: "e", tone: "error" }, `${meta.errors} errors`));
				if (meta.native) chips.push(el(Chip, { key: "n", tone: "warn" }, `${meta.native} native`));
				if (meta.step) chips.push(el(Chip, { key: "s", tone: "ok" }, "STEP"));
				if (meta.runId) chips.push(el(Chip, { key: "rid", tone: "ok" }, String(meta.runId)));
			}
			if (meta.kind === "cad-commit" && meta.hash) chips.push(el(Chip, { key: "h", tone: "ok" }, String(meta.hash)));
			if (meta.kind === "cad-restore" && meta.target) chips.push(el(Chip, { key: "t", tone: "ok" }, `→ ${meta.target}`));
			if (meta.kind === "cad-status") chips.push(el(Chip, { key: "c", tone: meta.clean ? "ok" : "warn" }, meta.clean ? "clean" : "dirty"));
			if (meta.kind === "cad-log") chips.push(el(Chip, { key: "l", tone: "ok" }, `${meta.count ?? 0} commits`));
			if (meta.kind === "cad-diff") chips.push(el(Chip, { key: "d", tone: "ok" }, `${meta.files ?? 0} files`));
			if (meta.kind === "cad-inspect" && meta.format === "code") chips.push(el(Chip, { key: "fmt", tone: "ok" }, "code 视图"));
			if (chips.length === 0) return null;
			return el("div", { style: { display: "flex", flexWrap: "wrap", gap: "4px", marginTop: "4px" } }, chips);
		}

		function PreviewGrid({ preview }) {
			const imgs = (Array.isArray(preview) ? preview : []).filter((p) => p && p.dataUrl);
			if (imgs.length === 0) return null;
			return el(
				"div",
				{
					style: {
						display: "grid",
						gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
						gap: "6px",
						marginTop: "6px",
					},
				},
				imgs.map((p) =>
					el("figure", { key: p.path, style: { margin: 0 } },
						el("img", {
							src: p.dataUrl,
							alt: p.label || p.path || "",
							style: {
								width: "100%",
								maxHeight: "200px",
								objectFit: "contain",
								borderRadius: "6px",
								border: "1px solid var(--dsw-alias-border-l1)",
								background: "var(--dsw-alias-bg-base)",
							},
						}),
						el("figcaption", { style: { font: "var(--dsw-font-xs-13)", color: "var(--dsw-alias-label-caption)", textAlign: "center", marginTop: "2px" } }, p.label || ""),
					),
				),
			);
		}

		function PathList({ images }) {
			const paths = (Array.isArray(images) ? images : []).filter((i) => i && i.path && !i.dataUrl);
			if (paths.length === 0) return null;
			return el(
				"div",
				{ style: { font: "var(--dsw-font-xs-13)", color: "var(--dsw-alias-label-tertiary)", marginTop: "4px", wordBreak: "break-all" } },
				paths.map((p) => el("div", { key: p.path }, String(p.view || p.label || "") + " → " + String(p.path))),
			);
		}

		function ErrorBox({ error, isError, content }) {
			if (!isError && (!error || !error.message)) return null;
			const message = (error && error.message) || "";
			const code = error && error.code ? " [" + error.code + "]" : "";
			const hint = error && error.hint ? "\n提示: " + error.hint : "";
			return el(
				"div",
				{
					style: {
						marginTop: "6px",
						padding: "6px 8px",
						borderRadius: "6px",
						border: "1px solid var(--dsw-alias-state-error-primary)",
						color: "var(--dsw-alias-state-error-primary)",
						font: "var(--dsw-font-xs-13)",
						whiteSpace: "pre-wrap",
						wordBreak: "break-word",
					},
				},
				message + code + hint,
			);
		}

		function StateDot({ state }) {
			const color = state === "ok" ? "var(--dsw-alias-state-success-primary, #22c55e)"
				: state === "error" ? "var(--dsw-alias-state-error-primary)"
				: state === "running" ? "var(--dsw-static-deepseek-500, #4d6bfe)"
				: "var(--dsw-alias-label-tertiary)";
			return el("span", {
				style: { display: "inline-block", width: "8px", height: "8px", borderRadius: "999px", background: color, flex: "none", marginRight: "6px", marginTop: "7px" },
			});
		}

		function summaryOf(toolName, args, meta) {
			if (toolName === "cad3_catalog") {
				const action = args.action || "search";
				const id = args.identifier || (Array.isArray(args.identifiers) ? (args.identifiers || []).join(", ") : "") || args.query || "";
				return String(action) + (id ? " · " + id : "");
			}
			if (args.project) return String(args.project);
			if (args.script) return String(args.script);
			if (args.path) return String(args.path);
			if (args.revision) return String(args.revision);
			if (args.message) return String(args.message);
			if (meta && meta.kind) return String(meta.kind);
			return "";
		}

		function CadRow({ toolName, block, cwd, openFile, inspect, callId }) {
			const [open, setOpen] = React.useState(false);
			const done = block !== null && typeof block === "object" && ("kind" in block);
			const meta = done && block.meta && typeof block.meta === "object" ? block.meta : null;
			const state = !done ? "running" : done && block.isError ? "error" : "ok";
			const argsRaw = done ? (block.call && block.call.argsRaw) || "" : block.argsRaw || "";
			const args = parseArgs(argsRaw);
			const summary = summaryOf(toolName, args, meta) || callId || "";
			const script = args.script || null;

			const head = el(
				"div",
				{
					role: "button",
					tabIndex: 0,
					onClick: () => setOpen(!open),
					onKeyDown: (e) => { if (e.key === "Enter" || e.key === " ") { setOpen(!open); } },
					style: { display: "flex", alignItems: "flex-start", minWidth: 0, cursor: "pointer", padding: "2px 0" },
				},
				el(StateDot, { state }),
				el("span", { style: { font: "var(--dsw-font-s-14, 14px)", color: "var(--dsw-alias-label-secondary)", flex: "none", lineHeight: "22px" } }, toolName || "cad3"),
				el("span", { style: { margin: "0 8px", flex: "none", color: "var(--dsw-alias-label-caption)", lineHeight: "22px" } }, "·"),
				el("span", { style: { font: "var(--dsw-font-s-14, 14px)", color: state === "error" ? "var(--dsw-alias-state-error-primary)" : "var(--dsw-alias-label-tertiary)", textOverflow: "ellipsis", whiteSpace: "nowrap", overflow: "hidden", minWidth: 0, flex: "auto", lineHeight: "22px" } }, summary || " "),
			);

			let body = null;
			if (open || state === "error") {
				body = el(
					"div",
					{ style: { display: "flex", flexDirection: "column", gap: "4px", margin: "2px 0 4px 14px" } },
					script && openFile ? el("button", { type: "button", onClick: () => openFile(String(script)), style: { alignSelf: "flex-start", background: "transparent", border: "none", color: "var(--dsw-static-deepseek-500)", cursor: "pointer", padding: 0, font: "var(--dsw-font-xs-13)" } }, "open script") : null,
					done && meta ? el(MetaChips, { meta }) : null,
					done && meta ? el(MetricLine, { m: meta.metrics }) : null,
					done && meta ? el(PreviewGrid, { preview: meta.preview || meta.images }) : null,
					done && meta ? el(PathList, { images: (meta.images || []).concat(meta.preview || []) }) : null,
					done ? el(ErrorBox, { error: meta && meta.error, isError: !!block.isError, content: block.content }) : null,
				);
			}

			return el(
				"div",
				{
					style: {
						border: "1px solid " + (state === "error" ? "var(--dsw-alias-state-error-primary)" : "var(--dsw-alias-border-l1)"),
						background: "var(--dsw-alias-bg-base)",
						borderRadius: "8px",
						padding: "4px 10px",
						minWidth: 0,
					},
					"data-cad-toolview": toolName,
				},
				head,
				body,
				inspect ? el("button", { type: "button", onClick: () => inspect(), style: { position: "absolute", opacity: 0, pointerEvents: "none", width: 0, height: 0 } }) : null,
			);
		}

		const cadClientPlugin = {
			name: "cad-studio-client",
			inject: ["slots"],
			apply(ctx) {
				const slots = ctx.get("slots");
				if (slots === undefined) return;
				const keys = [
					"cad3_doctor", "cad3_init", "cad3_run", "cad3_inspect", "cad3_render",
					"cad3_mates", "cad3_catalog", "cad3_status", "cad3_log", "cad3_diff",
					"cad3_commit", "cad3_restore", "cad3_cli",
				];
				slots.inject("tool.call.toolview", function* () {
					for (const key of keys) {
						yield slots.register({ name: "tool.call.toolview", key }, CadRow);
					}
				});
				console.log("[cad-studio-client] registered", keys.length, "toolviews");
			},
		};
		exports.apply = cadClientPlugin.apply;
		exports.inject = cadClientPlugin.inject;
		return module.exports;
	},
});
