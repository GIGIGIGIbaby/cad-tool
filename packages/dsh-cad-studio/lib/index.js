// cad-studio —— CAD 工场：FreeCAD 原生 V3 的 DSH 插件。
//
// 本文件只做编排与展示，几何真源是内嵌的 cad3 Python CLI（FreeCAD 在自己的进程里跑）：
//   - 解释器解析、ctx.subprocess 子进程、ctx.sandbox 会话策略约束、超时/取消；
//   - 13 个 cad3_* 工具：doctor / init / run / inspect / render / mates / catalog /
//     status / log / diff / commit / restore，以及原样转发 argv 的 cad3_cli；
//   - 工具结果为 canonical JSON；模型可见文本走 output.render，浏览器卡片走
//     output.presentationMeta（渲染图 dataUrl、metrics、错误）。
//
// 与 V2 插件的关系：沿用 V2 的 Host 约定（inject、tools.register、subprocess/sandbox
// 用法与惰性 sandboxPolicy），但 CLI 从 build123d 的 cad 换成 FreeCAD 的 cad3，不再
// 需要 venv bootstrap。
import { existsSync, readFileSync, statSync } from "node:fs";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url));
const PACKAGE_DIR = resolve(PLUGIN_DIR, "..");
const PY_ENTRY = join(PACKAGE_DIR, "python", "cad3.py");

const STDOUT_CAP = 8 * 1024 * 1024; // 单次命令 stdout 内存保留上限（尾部）
const STDERR_CAP = 128 * 1024; // stderr 保留上限（尾部）
const SPILL_CAP = 32 * 1024 * 1024; // 溢出时完整流落盘上限
const GRACE_MS = 10000; // SIGTERM → SIGKILL 升级窗口

const text = (v, fb = "") => (typeof v === "string" ? v.trim() : fb);
const num = (v, fb = 0) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : fb;
};
const list = (v) => (Array.isArray(v) ? v : []);
const short = (s, n = 240) => {
  s = String(s ?? "");
  return s.length > n ? s.slice(-n) : s;
};
const clip = (s, n = 12000) => {
  s = String(s ?? "");
  return s.length > n ? `${s.slice(0, n)}\n… (${s.length - n} chars truncated)` : s;
};

/** canonical 输出禁 null：递归去掉 null/undefined 键（数组保留非空项）。 */
function clean(v) {
  if (Array.isArray(v)) {
    const out = [];
    for (const item of v) {
      const cleaned = clean(item);
      if (cleaned !== undefined) out.push(cleaned);
    }
    return out;
  }
  if (v && typeof v === "object") {
    const out = {};
    for (const [k, x] of Object.entries(v)) {
      const cleaned = clean(x);
      if (cleaned !== undefined) out[k] = cleaned;
    }
    return out;
  }
  return v === null || v === undefined ? undefined : v;
}

/** ≤200KB 的 PNG 内联为 dataUrl；大图只给路径（Client 卡降级为路径文本）。 */
function imagePreview(path, label) {
  try {
    if (!path || !existsSync(path)) return null;
    const size = statSync(path).size;
    const entry = { path, label: label || basename(path), sizeBytes: size, inline: size <= 200 * 1024 };
    if (entry.inline) entry.dataUrl = `data:image/png;base64,${readFileSync(path, "base64")}`;
    return entry;
  } catch {
    return null;
  }
}

/** 领域错误：进入 canonical {ok:false,error} 通道而不是 isError 通道。 */
class CadError extends Error {
  constructor(code, message, hint) {
    super(message);
    this.name = "CadError";
    this.code = code;
    this.hint = hint;
  }
}

function errorOf(e, fallbackCode = "E-INTERNAL") {
  const out = { code: text(e && e.code, fallbackCode), message: text(e && e.message, "cad3 插件内部错误") };
  const hint = text(e && e.hint);
  if (hint) out.hint = hint;
  return out;
}

const errorSchema = {
  type: "object",
  additionalProperties: false,
  properties: {
    code: { type: "string" },
    message: { type: "string" },
    hint: { type: "string" },
  },
  required: ["code", "message"],
};

const freeObject = { type: "object" };

export const name = "cad-studio";
export const inject = ["tools", "subprocess", "skills"];

export default {
  name,
  inject,
  apply(ctx, config) {
    const tools = ctx.tools;
    const subprocess = ctx.get("subprocess");
    const sandbox = ctx.get("sandbox");
    const sandboxPolicy = ctx.get("sandboxPolicy");
    if (!subprocess) {
      ctx.logger?.warn?.("cad-studio: subprocess 服务未就绪，cad3_* 工具将不可用");
    }
    config = config || {};

    // ── 内置 skill：插件自己注册（route A），不依赖第二行 patch 与额外 peer ──
    const SKILL_DIR = join(PACKAGE_DIR, "skills", "freecad-v3");

    function loadBundledSkill() {
      const raw = readFileSync(join(SKILL_DIR, "SKILL.md"), "utf8");
      let name = "freecad-v3";
      let description = "在 DSH 里用 FreeCAD 原生 Python 建模、观察和留档";
      let whenToUse;
      let content = raw;
      const front = raw.match(/^---\n([\s\S]*?)\n---\n?/);
      if (front) {
        content = raw.slice(front[0].length);
        for (const line of front[1].split("\n")) {
          const idx = line.indexOf(":");
          if (idx === -1) continue;
          const key = line.slice(0, idx).trim();
          const value = line.slice(idx + 1).trim().replace(/^["']|["']$/g, "");
          if (key === "name" && value) name = value;
          else if (key === "description" && value) description = value;
          else if (key === "whenToUse" && value) whenToUse = value;
        }
      }
      return { name, description, whenToUse, content };
    }

    const skillService = ctx.get("skills");
    if (skillService && typeof skillService.register === "function") {
      try {
        const skill = loadBundledSkill();
        const register = () =>
          skillService.register({
            name: skill.name,
            description: skill.description,
            ...(skill.whenToUse ? { whenToUse: skill.whenToUse } : {}),
            content: skill.content,
            source: "bundled",
            resourceBase: { kind: "directory", path: SKILL_DIR },
            invocation: { modelInvocable: true, userInvocable: true },
          });
        if (typeof ctx.effect === "function") ctx.effect(register);
        else register();
      } catch (err) {
        ctx.logger?.warn?.(`cad-studio: freecad-v3 skill 注册失败: ${String((err && err.message) || err)}`);
      }
    } else {
      ctx.logger?.warn?.("cad-studio: skills 服务未就绪，freecad-v3 skill 未注册");
    }

    const workspaceOf = (exec) => {
      try {
        const cwd = exec && exec.agent && exec.agent.session && exec.agent.session.header && exec.agent.session.header.cwd;
        if (cwd) return resolve(String(cwd));
      } catch {
        /* fall through */
      }
      try {
        return resolve(process.cwd());
      } catch {
        return resolve(".");
      }
    };
    const sessionOf = (exec) => {
      try {
        return exec && exec.agent ? exec.agent.session : undefined;
      } catch {
        return undefined;
      }
    };
    const resolvePath = (base, p) => (isAbsolute(String(p)) ? resolve(String(p)) : resolve(base, String(p)));

    /** Python 解释器：显式参数 → CAD3_PYTHON → python3/python。宿主代码只依赖标准库。 */
    function resolvePython(explicit) {
      const configured = text(explicit) || text(process.env && process.env.CAD3_PYTHON);
      if (configured) return configured;
      return process.platform === "win32" ? "python" : "python3";
    }

    /** ctx.sandbox 包装 argv；服务缺失时原样通过（subprocess 服务仍是执行边界）。 */
    function confineArgv(argv, session) {
      // sandboxPolicy 在 apply() 时尚未注册（V2/0.1.7 实测），必须按调用惰性解析；
      // 若沿用 apply() 时捕获的 undefined，会静默降级为 unconfined 却毫无提示。
      const sandboxService = sandbox ?? ctx.get("sandbox");
      const policyService = sandboxPolicy ?? ctx.get("sandboxPolicy");
      if (!sandboxService || !policyService) return { argv: [...argv], mode: "unconfined" };
      let policy;
      try {
        policy = session ? policyService.resolve({ session }) : policyService.resolve();
      } catch {
        policy = null;
      }
      if (!policy || policy.mode === "danger-full-access") return { argv: [...argv], mode: policy ? policy.mode : "unconfined" };
      try {
        const wrapped = sandboxService.confine(argv, policy);
        return { argv: wrapped.argv, mode: policy.mode, enforcement: wrapped.enforcement };
      } catch (e) {
        throw new CadError(
          "E-SANDBOX",
          `无法按 ${policy.mode} 策略约束子进程: ${String((e && e.message) || e)}`,
          "当前会话的沙箱后端不可用；请切换沙箱模式或检查 dsh-sandbox 后端",
        );
      }
    }

    /** exec.signal 与显式超时合并。 */
    function combinedSignal(exec, timeoutMs) {
      const caller = exec && exec.signal;
      const bound = timeoutMs > 0 ? timeoutMs : 0;
      if (!caller && !bound) return undefined;
      if (caller && !bound) return caller;
      const controller = new AbortController();
      if (caller) {
        if (caller.aborted) {
          controller.abort();
          return controller.signal;
        }
        caller.addEventListener("abort", () => controller.abort(), { once: true });
      }
      const timer = bound ? setTimeout(() => controller.abort(), bound) : null;
      if (timer) {
        if (typeof timer.unref === "function") timer.unref();
        controller.signal.addEventListener("abort", () => clearTimeout(timer), { once: true });
      }
      return controller.signal;
    }

    /** 执行一个 cad3 子进程，返回 {exitCode, signal, stdout, stderr, mode, ...}。 */
    async function spawnCollect(argv, opts) {
      opts = opts || {};
      if (!subprocess) {
        throw new CadError("E-INFRA", "subprocess 服务不可用", "本插件依赖 DSH Host 的 subprocess capability seam");
      }
      const { argv: finalArgv, mode, enforcement } = confineArgv(argv, opts.session);
      const handle = subprocess.spawn({
        argv: finalArgv,
        cwd: opts.cwd,
        stdio: {
          stdin: "ignore",
          stdout: { maxBytes: opts.stdoutCap ?? STDOUT_CAP, spill: { maxBytes: SPILL_CAP } },
          stderr: { maxBytes: STDERR_CAP, spill: { maxBytes: SPILL_CAP } },
        },
        graceMs: GRACE_MS,
        ...(opts.signal ? { signal: opts.signal } : {}),
      });
      const outcome = await handle.done;
      const so = handle.collected.stdout ? handle.collected.stdout.readFrom(0) : { text: "" };
      const se = handle.collected.stderr ? handle.collected.stderr.readFrom(0) : { text: "" };
      return {
        exitCode: outcome.exitCode,
        signal: outcome.signal,
        stdout: so.text || "",
        stderr: se.text || "",
        mode,
        enforcement: enforcement || null,
      };
    }

    function errorFromCli(data, raw, fallbackMsg) {
      const first = data && Array.isArray(data.errors) ? data.errors[0] : null;
      const out = {
        code: text(first && first.kind, text(data && data.error && data.error.code, raw && raw.exitCode !== 0 ? "E-EXIT" : "E-CAD3")),
        message: text(
          first && first.message,
          text(data && data.error && data.error.message, short(raw && raw.stderr, 600)) || fallbackMsg || "cad3 命令失败",
        ),
      };
      const hint = text(first && first.hint) || text(data && data.error && data.error.hint);
      if (hint) out.hint = hint;
      return out;
    }

    /** 运行内嵌 cad3 CLI；stdout 是单个 JSON（inspect --format code 与 cad3_cli 例外）。 */
    async function runCad3(argv, opts) {
      opts = opts || {};
      const py = resolvePython(opts.python);
      const raw = await spawnCollect([py, PY_ENTRY, ...argv], {
        cwd: opts.cwd,
        session: opts.session,
        signal: opts.signal,
      });
      const stdout = String(raw.stdout || "");
      const trimmed = stdout.trim();
      let data = null;
      if (trimmed.startsWith("{")) {
        try {
          data = JSON.parse(trimmed);
        } catch {
          data = null;
        }
      }
      let error = null;
      if (data && data.ok === false) error = errorFromCli(data, raw, "cad3 命令返回 ok=false");
      else if (!data && raw.exitCode !== 0) {
        error = {
          code: "E-CAD3",
          message: short(text(raw.stderr) || `cad3 退出码 ${raw.exitCode}`, 800),
        };
      }
      const ok = (data ? data.ok === true : raw.exitCode === 0) && !error;
      return { ok, data, textOut: data ? "" : stdout, exitCode: raw.exitCode, stderr: raw.stderr || "", error };
    }

    // ── 工具定义辅助 ────────────────────────────────────────────────────

    const block = (t) => [{ type: "text", text: t }];

    function outSchema(props) {
      return {
        type: "object",
        additionalProperties: true,
        properties: { ok: { type: "boolean" }, error: errorSchema, ...(props || {}) },
      };
    }

    function define(toolName, description, parameters, output, execute) {
      tools.register({
        name: toolName,
        description,
        parameters,
        output,
        execute: async (args, exec) => {
          try {
            return clean(await execute(args || {}, exec));
          } catch (e) {
            if (e instanceof CadError) return clean({ ok: false, error: errorOf(e) });
            throw e;
          }
        },
        presentCall: (args) => ({
          card: "generic",
          title: `${toolName}${summarizeArgs(args)}`,
          kind: "execute",
          rawInput: args || {},
        }),
      });
    }

    function summarizeArgs(args) {
      const bits = [];
      if (args && typeof args === "object") {
        if (text(args.project)) bits.push(text(args.project));
        if (text(args.script)) bits.push(text(args.script));
        if (text(args.revision)) bits.push(text(args.revision));
        if (text(args.action)) bits.push(text(args.action));
        if (text(args.identifier)) bits.push(text(args.identifier));
        if (text(args.query)) bits.push(text(args.query));
      }
      return bits.length ? ` ${bits.join(" ")}` : "";
    }

    const PROJECT = { type: "string", description: "工程目录；缺省为会话工作区（也可传 cad3.json 所在目录）" };
    const PYTHON = { type: "string", description: "Python 3.11+ 解释器路径；缺省 CAD3_PYTHON 或 python3" };
    const FREECAD = { type: "string", description: "freecadcmd 路径；缺省按 CAD3_FREECAD 与常见安装位置探测" };
    const TIMEOUT = (dflt) => ({ type: "number", description: `超时秒数，缺省 ${dflt}` });

    const projectOf = (exec, args) => {
      const ws = workspaceOf(exec);
      const dir = resolvePath(ws, text(args.project) || ".");
      if (!existsSync(dir)) {
        throw new CadError("E-NO-PROJECT", `工程目录不存在: ${dir}`, "先调用 cad3_init 创建工程，或把 project 指向 cad3.json 所在目录");
      }
      return { ws, dir };
    };
    const sessionRun = (exec, args, dflt) => ({
      session: sessionOf(exec),
      signal: combinedSignal(exec, Math.max(1, num(args.timeout, dflt)) * 1000),
      python: text(args.python) || undefined,
    });

    const fail = (error, extra) => ({ ok: false, error, ...(extra || {}) });

    // ── cad3_doctor ────────────────────────────────────────────────────

    define(
      "cad3_doctor",
      "探测本机已安装的 FreeCAD（路径、版本、能力），纯读取，不安装也不修改配置。第一次建模前先调用；缺 FreeCAD 时报告而不是自动安装。",
      {
        type: "object",
        properties: { freecad: FREECAD, python: PYTHON, timeout: TIMEOUT(15) },
        required: [],
      },
      {
        schema: outSchema({
          executable: { type: "string" },
          freecad_version: { type: "array", items: { type: "string" } },
          capabilities: freeObject,
        }),
        render: (_a, v) =>
          block(
            v.ok
              ? `FreeCAD: ${v.executable || "?"}\n版本: ${(v.freecad_version || []).slice(0, 4).join(" ")}`
              : `✗ ${v.error && v.error.message}${v.error && v.error.hint ? `\n提示: ${v.error.hint}` : ""}`,
          ),
        presentationMeta: (_a, v) => ({ kind: "cad-doctor", ok: !!v.ok, error: v.error || null }),
      },
      async (args, exec) => {
        const ws = workspaceOf(exec);
        const argv = ["doctor"];
        if (text(args.freecad)) argv.push("--freecad", text(args.freecad));
        if (num(args.timeout) > 0) argv.push("--timeout", String(num(args.timeout)));
        const r = await runCad3(argv, { cwd: ws, ...sessionRun(exec, args, 15) });
        if (!r.ok) return fail(r.error, { executable: (r.data && r.data.executable) || null });
        return {
          ok: true,
          executable: r.data.executable || null,
          freecad_version: list(r.data.freecad_version),
          capabilities: r.data.capabilities || null,
          warnings: list(r.data.warnings),
        };
      },
    );

    // ── cad3_init ──────────────────────────────────────────────────────

    define(
      "cad3_init",
      "在不存在或为空的目录创建普通 CAD 工程：cad3.json、src/、models/、inputs/、artifacts/、notes/ 与工程自己的 Git 仓库。路径用 project 参数（可相对工作区）。",
      {
        type: "object",
        properties: { path: { type: "string", description: "要创建的工程目录；必须不存在或为空" }, name: { type: "string", description: "工程显示名" } },
        required: ["path"],
      },
      {
        schema: outSchema({ project: { type: "string" }, config: freeObject }),
        render: (_a, v) => block(v.ok ? `✓ 工程已创建: ${v.project}` : `✗ ${v.error && v.error.message}`),
        presentationMeta: (_a, v) => ({ kind: "cad-init", ok: !!v.ok, error: v.error || null }),
      },
      async (args, exec) => {
        const ws = workspaceOf(exec);
        const target = resolvePath(ws, text(args.path));
        const argv = ["init", target];
        if (text(args.name)) argv.push("--name", text(args.name));
        const r = await runCad3(argv, { cwd: ws, session: sessionOf(exec), python: text(args.python) || undefined });
        if (!r.ok) return fail(r.error);
        return { ok: true, project: r.data.project || target, config: r.data.config || null };
      },
    );

    // ── cad3_run ───────────────────────────────────────────────────────

    function summarizeRun(data, r) {
      const warnings = list(data && data.warnings);
      const errors = list(data && data.errors);
      const native = list(data && data.native_issues);
      return {
        ok: !!r.ok,
        run_id: (data && data.run_id) || null,
        run_dir: (data && data.run_dir) || null,
        document: (data && data.document) || null,
        step: (data && data.step) || null,
        model_published: !!(data && data.model_published),
        published: (data && data.published) || {},
        warnings,
        errors,
        native_issues: native,
        elapsed_s: data && data.elapsed_s != null ? data.elapsed_s : null,
        ...(r.error ? { error: r.error } : {}),
      };
    }

    function runText(v) {
      if (!v.ok) return `✗ ${v.error && v.error.code ? `[${v.error.code}] ` : ""}${(v.error && v.error.message) || "运行失败"}`;
      const bits = [`✓ run ${v.run_id || ""}`.trim()];
      if (v.elapsed_s != null) bits.push(`${Number(v.elapsed_s).toFixed(1)}s`);
      if (v.step) bits.push(`step=${v.step}`);
      if (list(v.warnings).length) bits.push(`warnings=${v.warnings.length}`);
      if (list(v.errors).length) bits.push(`errors=${v.errors.length}`);
      if (list(v.native_issues).length) bits.push(`native_issues=${v.native_issues.length}`);
      const lines = [bits.join(" | ")];
      for (const e of list(v.errors).slice(0, 5)) lines.push(`- error: ${e.message || e}`);
      for (const w of list(v.warnings).slice(0, 5)) lines.push(`- warning: ${w}`);
      if (list(v.warnings).length > 5) lines.push(`- ... ${v.warnings.length - 5} more warnings`);
      if (v.run_dir) lines.push(`run_dir: ${v.run_dir}`);
      return lines.join("\n");
    }

    define(
      "cad3_run",
      "在工程里执行原生 FreeCAD Python 脚本（缺省 src/main.py），返回执行协议结果、原生警告/错误与 run 目录；step=true 时同时导出 STEP。ok 只表示执行协议成功，不是设计合格证明。",
      {
        type: "object",
        properties: {
          project: PROJECT,
          script: { type: "string", description: "脚本路径（相对工程或工程内绝对路径）；缺省工程入口 src/main.py" },
          step: { type: "boolean", description: "执行后导出选中对象为 STEP" },
          freecad: FREECAD,
          python: PYTHON,
          timeout: TIMEOUT(120),
        },
        required: [],
      },
      {
        schema: outSchema({
          run_id: { type: "string" },
          run_dir: { type: "string" },
          document: freeObject,
          step: { type: "string" },
          model_published: { type: "boolean" },
          published: freeObject,
          warnings: { type: "array", items: {} },
          errors: { type: "array", items: {} },
          native_issues: { type: "array", items: {} },
          elapsed_s: { type: "number" },
        }),
        render: (_a, v) => block(runText(v)),
        presentationMeta: (_a, v) => ({
          kind: "cad-run",
          ok: !!v.ok,
          warnings: list(v.warnings).length,
          errors: list(v.errors).length,
          native: list(v.native_issues).length,
          step: v.step || null,
          runId: v.run_id || null,
          metrics: firstOutputMetrics(v.document),
          error: v.error || null,
        }),
      },
      async (args, exec) => {
        const { ws, dir } = projectOf(exec, args);
        void ws;
        const argv = ["run", "-p", dir];
        if (text(args.script)) argv.push(text(args.script));
        if (args.step) argv.push("--step");
        if (text(args.freecad)) argv.push("--freecad", text(args.freecad));
        if (num(args.timeout) > 0) argv.push("--timeout", String(num(args.timeout)));
        const r = await runCad3(argv, { cwd: dir, ...sessionRun(exec, args, 120) });
        return summarizeRun(r.data, r);
      },
    );

    // ── cad3_inspect ───────────────────────────────────────────────────

    function firstOutputMetrics(document) {
      // document 没有 metrics；保留函数以便未来扩展（run 不返回几何）。
      void document;
      return null;
    }

    function inspectSummary(data, r, format) {
      const state = data && data.state && typeof data.state === "object" ? data.state : null;
      const objects = state && Array.isArray(state.objects) ? state.objects : [];
      const outputs = objects.filter((o) => o && o.is_output);
      const metricsItems = outputs
        .map((o) => o && o.geometry)
        .filter((g) => g && typeof g === "object")
        .map((g) => ({
          volume_mm3: g.volume_mm3 ?? null,
          bbox_mm: g.bbox_mm ?? null,
          faces: g.faces ?? null,
          edges: g.edges ?? null,
          solids: g.solids ?? null,
          valid: g.valid ?? null,
        }));
      return {
        ok: !!r.ok,
        format: format || "json",
        run_id: (data && data.run_id) || null,
        run_dir: (data && data.run_dir) || null,
        object_count: objects.length,
        output_count: outputs.length,
        metrics: metricsItems[0] || null,
        outputs: outputs.map((o) => ({ name: o.name, type: o.type, geometry: o.geometry || null })),
        objects: objects.map((o) => ({
          name: o.name,
          type: o.type,
          role: o.role,
          is_output: !!o.is_output,
          volume_mm3: o.geometry ? o.geometry.volume_mm3 ?? null : null,
          bbox_mm: o.geometry ? o.geometry.bbox_mm ?? null : null,
        })),
        errors: list(state && state.errors).concat(list(data && data.errors)),
        warnings: list(state && state.warnings).concat(list(data && data.warnings)),
        native_issues: list(data && data.native_issues),
        ...(r.error ? { error: r.error } : {}),
      };
    }

    function inspectText(v) {
      if (!v.ok) return `✗ ${v.error && v.error.message}`;
      const lines = [];
      const dims = v.metrics && v.metrics.bbox_mm ? ` bbox=${v.metrics.bbox_mm.map((x) => Number(x).toFixed(2)).join("×")}mm` : "";
      const vol = v.metrics && v.metrics.volume_mm3 != null ? ` volume=${Number(v.metrics.volume_mm3).toFixed(2)}mm³` : "";
      const shape = v.metrics && v.metrics.faces != null ? ` faces=${v.metrics.faces} edges=${v.metrics.edges} solids=${v.metrics.solids}` : "";
      lines.push(`✓ inspect | ${v.object_count} objects, ${v.output_count} outputs${vol}${dims}${shape}`);
      for (const o of v.outputs.slice(0, 6)) {
        const g = o.geometry || {};
        const extra = g.volume_mm3 != null ? ` ${Number(g.volume_mm3).toFixed(2)}mm³` : "";
        lines.push(`- output ${o.name} (${o.type})${extra}`);
      }
      for (const e of list(v.errors).slice(0, 5)) lines.push(`- error: ${e.message || JSON.stringify(e)}`);
      for (const w of list(v.warnings).slice(0, 5)) lines.push(`- warning: ${w}`);
      if (v.run_dir) lines.push(`run_dir: ${v.run_dir}`);
      lines.push("（需要完整 JSON 或 --detail 时用 cad3_cli 转发 inspect 参数）");
      return lines.join("\n");
    }

    define(
      "cad3_inspect",
      "只读观察当前 FreeCAD 文档：对象/特征、参数与表达式、草图求解诊断、原生错误、几何指标。format=code 返回只读的代码式描述（不是可执行重建脚本）。",
      {
        type: "object",
        properties: {
          project: PROJECT,
          object: { type: "string", description: "只看指定对象名" },
          detail: { type: "boolean", description: "附加参数、表达式与依赖细节" },
          format: { type: "string", enum: ["json", "code"], description: "json（缺省）或只读代码式描述 code" },
          freecad: FREECAD,
          python: PYTHON,
          timeout: TIMEOUT(60),
        },
        required: [],
      },
      {
        schema: outSchema({
          format: { type: "string" },
          run_id: { type: "string" },
          run_dir: { type: "string" },
          object_count: { type: "number" },
          output_count: { type: "number" },
          metrics: freeObject,
          outputs: { type: "array", items: freeObject },
          objects: { type: "array", items: freeObject },
          errors: { type: "array", items: {} },
          warnings: { type: "array", items: {} },
          native_issues: { type: "array", items: {} },
          text: { type: "string" },
        }),
        render: (_a, v) => block(v.format === "code" && v.text ? clip(v.text, 20000) : inspectText(v)),
        presentationMeta: (_a, v) => ({
          kind: "cad-inspect",
          ok: !!v.ok,
          metrics: v.metrics || null,
          format: v.format || "json",
          error: v.error || null,
        }),
      },
      async (args, exec) => {
        const { dir } = projectOf(exec, args);
        const format = text(args.format) === "code" ? "code" : "json";
        const argv = ["inspect", "-p", dir];
        if (text(args.object)) argv.push("--object", text(args.object));
        if (args.detail) argv.push("--detail");
        if (format === "code") argv.push("--format", "code");
        if (text(args.freecad)) argv.push("--freecad", text(args.freecad));
        if (num(args.timeout) > 0) argv.push("--timeout", String(num(args.timeout)));
        const r = await runCad3(argv, { cwd: dir, ...sessionRun(exec, args, 60) });
        if (format === "code") {
          if (!r.ok) return fail(r.error, { format });
          return { ok: true, format: "code", text: clip(r.textOut, 20000), run_id: null, run_dir: null };
        }
        if (!r.ok) return fail(r.error, { format });
        return inspectSummary(r.data, r, format);
      },
    );

    // ── cad3_render ────────────────────────────────────────────────────

    function renderSummary(data, r, exec, args, projectDir) {
      const raw = list(data && data.images);
      const images = raw.map((img) => {
        const runPath = text(img && img.path);
        const publishedRel = text(img && img.published);
        const publishedPath = publishedRel ? resolvePath(projectDir, publishedRel) : "";
        const path = publishedPath && existsSync(publishedPath) ? publishedPath : runPath;
        return {
          view: text(img && img.view, "view"),
          path,
          ok: !!(img && img.ok),
          width: img && img.width != null ? img.width : undefined,
          height: img && img.height != null ? img.height : undefined,
          size_bytes: img && img.size_bytes != null ? img.size_bytes : undefined,
          exists: !!(path && existsSync(path)),
          published: publishedRel || undefined,
        };
      });
      const preview = images
        .filter((img) => img.exists)
        .map((img) => imagePreview(img.path, img.view))
        .filter(Boolean);
      const warnings = list(data && data.warnings);
      const native = list(data && data.native_issues);
      return {
        ok: !!r.ok && images.some((img) => img.exists),
        run_id: (data && data.run_id) || null,
        run_dir: (data && data.run_dir) || null,
        views: text(args.views) || null,
        sections: list(args.section),
        images,
        preview,
        errors: list(data && data.errors),
        warnings,
        native_issues: native,
        ...(r.ok && images.some((img) => img.exists) ? {} : { error: r.error || { code: "E-RENDER", message: "没有生成任何渲染图" } }),
      };
    }

    function renderText(v) {
      if (!v.ok) return `✗ ${v.error && v.error.message}`;
      const lines = [`✓ 渲染完成 | ${v.images.map((i) => i.view).join(", ")}`];
      for (const img of v.images.filter((i) => i.exists)) lines.push(`- ${img.view}: ${img.path}`);
      if (v.sections && v.sections.length) lines.push(`剖面: ${v.sections.join(", ")}`);
      return lines.join("\n");
    }

    define(
      "cad3_render",
      "离屏渲染当前 FreeCAD 文档为 PNG（iso/top/front/right，或轴向剖视 --section）。渲染图写入 run 目录并返回 ≤200KB 的内联预览；不是 GUI 截图。",
      {
        type: "object",
        properties: {
          project: PROJECT,
          views: { type: "string", description: "逗号分隔视图，如 iso,top；缺省四视图（只给剖视时缺省无普通视图）" },
          section: { type: "array", items: { type: "string" }, description: "轴向剖面，如 y=12.5；可重复" },
          width: { type: "number" },
          height: { type: "number" },
          freecad: FREECAD,
          python: PYTHON,
          timeout: TIMEOUT(90),
        },
        required: [],
      },
      {
        schema: outSchema({
          run_id: { type: "string" },
          run_dir: { type: "string" },
          views: { type: "string" },
          sections: { type: "array", items: { type: "string" } },
          images: { type: "array", items: freeObject },
          preview: { type: "array", items: freeObject },
          errors: { type: "array", items: {} },
          warnings: { type: "array", items: {} },
          native_issues: { type: "array", items: {} },
        }),
        render: (_a, v) => block(renderText(v)),
        presentationMeta: (_a, v) => ({
          kind: "cad-render",
          ok: !!v.ok,
          images: v.images || [],
          preview: v.preview || [],
          error: v.error || null,
        }),
      },
      async (args, exec) => {
        const { dir } = projectOf(exec, args);
        const argv = ["render", "-p", dir];
        const views = text(args.views);
        if (views) argv.push("--views", views);
        for (const s of list(args.section)) if (text(s)) argv.push("--section", text(s));
        if (num(args.width) > 0) argv.push("--width", String(Math.trunc(num(args.width))));
        if (num(args.height) > 0) argv.push("--height", String(Math.trunc(num(args.height))));
        if (text(args.freecad)) argv.push("--freecad", text(args.freecad));
        if (num(args.timeout) > 0) argv.push("--timeout", String(num(args.timeout)));
        const r = await runCad3(argv, { cwd: dir, ...sessionRun(exec, args, 90) });
        if (r.data) return renderSummary(r.data, r, exec, args, dir);
        return fail(r.error, { images: [], preview: [] });
      },
    );

    // ── cad3_mates ─────────────────────────────────────────────────────

    function matesSummary(data, r) {
      const overall = (data && data.overall) || null;
      const problems = list(data && data.problems);
      const declarations = (data && data.declarations) || null;
      const geometry = (data && data.geometry) || null;
      const declaredMates = declarations && Array.isArray(declarations.mates) ? declarations.mates : [];
      const findings = declarations && Array.isArray(declarations.findings) ? declarations.findings : [];
      const interfaces = geometry && Array.isArray(geometry.interfaces) ? geometry.interfaces : [];
      return {
        ok: !!r.ok,
        overall,
        run_id: (data && data.run_id) || null,
        run_dir: (data && data.run_dir) || null,
        declared_mates: declaredMates.length,
        findings: findings.length,
        geometry_interfaces: interfaces.length,
        problems: problems.slice(0, 30).map((p) => ({
          verdict: p && p.verdict,
          kind: p && p.kind,
          ref: p && p.ref,
          message: p && (p.message || p.reason || p.detail) || undefined,
        })),
        errors: list(data && data.errors),
        warnings: list(data && data.warnings),
        native_issues: list(data && data.native_issues),
        ...(r.error ? { error: r.error } : {}),
      };
    }

    function matesText(v) {
      if (!v.ok) return `✗ ${v.error && v.error.message}${v.error && v.error.format ? `\n${clip(JSON.stringify(v.error.format), 4000)}` : ""}`;
      const lines = [
        `✓ mates | overall=${v.overall || "?"} | declared_mates=${v.declared_mates} findings=${v.findings} geometry_interfaces=${v.geometry_interfaces} | problems=${v.problems.length}`,
      ];
      for (const p of v.problems.slice(0, 10)) lines.push(`- ${p.verdict} ${p.kind} ${p.ref || ""}${p.message ? `: ${p.message}` : ""}`);
      if (v.run_dir) lines.push(`run_dir: ${v.run_dir}`);
      lines.push("（overall 是设计反馈，不是几何/制造验收）");
      return lines.join("\n");
    }

    define(
      "cad3_mates",
      "读取 notes/interfaces.json，核对声明的零件接口、配合关系与装配连通性；有模型时再在 FreeCAD 里检查圆柱面/平面是否真实存在。返回 PASS/WARN/FAIL/UNKNOWN 设计反馈，顶层 ok 仍只表示运行协议成功。",
      {
        type: "object",
        properties: {
          project: PROJECT,
          file: { type: "string", description: "接口声明路径；缺省 notes/interfaces.json" },
          no_geometry: { type: "boolean", description: "只做声明检查，不启动 FreeCAD" },
          freecad: FREECAD,
          python: PYTHON,
          timeout: TIMEOUT(120),
        },
        required: [],
      },
      {
        schema: outSchema({
          overall: { type: "string" },
          run_id: { type: "string" },
          run_dir: { type: "string" },
          declared_mates: { type: "number" },
          findings: { type: "number" },
          geometry_interfaces: { type: "number" },
          problems: { type: "array", items: freeObject },
          errors: { type: "array", items: {} },
          warnings: { type: "array", items: {} },
          native_issues: { type: "array", items: {} },
        }),
        render: (_a, v) => block(matesText(v)),
        presentationMeta: (_a, v) => ({
          kind: "cad-mates",
          ok: !!v.ok,
          overall: v.overall || null,
          problems: list(v.problems).length,
          error: v.error || null,
        }),
      },
      async (args, exec) => {
        const { dir } = projectOf(exec, args);
        const argv = ["mates", "-p", dir];
        if (text(args.file)) argv.push("--file", text(args.file));
        if (args.no_geometry) argv.push("--no-geometry");
        if (text(args.freecad)) argv.push("--freecad", text(args.freecad));
        if (num(args.timeout) > 0) argv.push("--timeout", String(num(args.timeout)));
        const r = await runCad3(argv, { cwd: dir, ...sessionRun(exec, args, 120) });
        if (!r.data) {
          const err = r.error || { code: "E-MATES", message: "mates 未返回 JSON" };
          return fail(err, { problems: [] });
        }
        return matesSummary(r.data, r);
      },
    );

    // ── cad3_catalog ───────────────────────────────────────────────────

    function catalogRender(v) {
      if (!v.ok) return `✗ ${v.error && v.error.message}`;
      if (v.action === "search") {
        const items = list(v.catalog);
        const lines = [`✓ catalog search | ${items.length} 个候选`];
        for (const item of items.slice(0, 12)) {
          const dims = item && item.dimensions_mm ? Object.entries(item.dimensions_mm).map(([k, x]) => `${k}=${x}`).join(",") : "";
          lines.push(`- ${item.id || item.family}${item.name ? ` (${item.name})` : ""}${dims ? ` ${dims}` : ""}`);
        }
        return lines.join("\n");
      }
      return clip(JSON.stringify(v, null, 2), 12000);
    }

    define(
      "cad3_catalog",
      "查询插件内置的标准件目录：search 按型号/自然语言找候选，describe 看单个契约，compare 比较多个候选，instantiate 返回不建几何的实例参数与接口。目录查询不需要 FreeCAD。",
      {
        type: "object",
        properties: {
          action: { type: "string", enum: ["search", "describe", "compare", "instantiate"], description: "目录操作" },
          query: { type: "string", description: "search 的查询词，如 6204 深沟球轴承" },
          identifier: { type: "string", description: "describe/instantiate 的 ID，如 6204 或 fastener.hex_bolt_metric" },
          identifiers: { type: "array", items: { type: "string" }, description: "compare 的 ID 列表" },
          category: { type: "string", description: "search 的分类过滤，如 bearing" },
          limit: { type: "number", description: "search 候选数，缺省 8" },
          selection: { type: "array", items: { type: "string" }, description: "instantiate 的采购选择 KEY=VALUE，可重复" },
          parameter: { type: "array", items: { type: "string" }, description: "instantiate 的参数覆盖 KEY=VALUE，可重复" },
          python: PYTHON,
        },
        required: ["action"],
      },
      {
        schema: outSchema({
          action: { type: "string" },
          catalog: { type: "array", items: freeObject },
          schema: { type: "string" },
          catalog_id: { type: "string" },
          family: { type: "string" },
          parameters: freeObject,
          dimensions_mm: freeObject,
          ports: { type: "array", items: freeObject },
        }),
        render: (_a, v) => block(catalogRender(v)),
        presentationMeta: (_a, v) => ({ kind: "cad-catalog", ok: !!v.ok, action: v.action || null, error: v.error || null }),
      },
      async (args, exec) => {
        const action = text(args.action);
        if (!["search", "describe", "compare", "instantiate"].includes(action)) {
          return fail({ code: "E-ARGS", message: `action 必须是 search/describe/compare/instantiate，收到 ${action || "(空)"}` });
        }
        const argv = ["catalog", action];
        if (action === "search") {
          if (text(args.query)) argv.push(text(args.query));
          if (text(args.category)) argv.push("--category", text(args.category));
          if (num(args.limit) > 0) argv.push("--limit", String(Math.trunc(num(args.limit))));
        } else if (action === "describe" || action === "instantiate") {
          if (!text(args.identifier)) return fail({ code: "E-ARGS", message: `${action} 需要 identifier` });
          argv.push(text(args.identifier));
          if (action === "instantiate") {
            for (const s of list(args.selection)) if (text(s)) argv.push("--selection", text(s));
            for (const p of list(args.parameter)) if (text(p)) argv.push("--parameter", text(p));
          }
        } else {
          const identifiers = list(args.identifiers).map((x) => text(x)).filter(Boolean);
          if (identifiers.length < 2) return fail({ code: "E-ARGS", message: "compare 至少需要两个 identifiers" });
          argv.push(...identifiers);
        }
        const ws = workspaceOf(exec);
        const r = await runCad3(argv, { cwd: ws, session: sessionOf(exec), python: text(args.python) || undefined });
        if (!r.ok) return fail(r.error, { action });
        return { ...r.data, ok: true, action };
      },
    );

    // ── Git 快照命令 ────────────────────────────────────────────────────

    define(
      "cad3_status",
      "查看工程 Git 状态：HEAD、分支、是否干净、变更文件清单。",
      { type: "object", properties: { project: PROJECT, python: PYTHON }, required: [] },
      {
        schema: outSchema({
          root: { type: "string" },
          head: { type: "string" },
          short_head: { type: "string" },
          branch: { type: "string" },
          clean: { type: "boolean" },
          counts: freeObject,
          changes: { type: "array", items: freeObject },
        }),
        render: (_a, v) => {
          if (!v.ok) return block(`✗ ${v.error && v.error.message}`);
          const c = v.counts || {};
          return block(
            `✓ status | branch=${v.branch || "?"} head=${v.short_head || "(none)"} clean=${v.clean ? "yes" : "no"}\n` +
              `staged=${c.staged ?? 0} modified=${c.modified ?? 0} untracked=${c.untracked ?? 0} conflicted=${c.conflicted ?? 0}` +
              (list(v.changes).length ? `\n变更（前 20）:\n${list(v.changes).slice(0, 20).map((x) => `- ${x.status} ${x.path}`).join("\n")}` : ""),
          );
        },
        presentationMeta: (_a, v) => ({ kind: "cad-status", ok: !!v.ok, clean: !!v.clean, error: v.error || null }),
      },
      async (args, exec) => {
        const { dir } = projectOf(exec, args);
        const r = await runCad3(["status", "-p", dir], { cwd: dir, session: sessionOf(exec), python: text(args.python) || undefined });
        if (!r.ok) return fail(r.error);
        const d = r.data || {};
        return {
          ok: true,
          root: d.root || dir,
          head: d.head || null,
          short_head: d.short_head || null,
          branch: d.branch || null,
          clean: !!d.clean,
          counts: d.counts || null,
          changes: list(d.changes).slice(0, 100),
        };
      },
    );

    define(
      "cad3_log",
      "查看工程已提交的设计快照历史（短哈希、时间、提交信息）。",
      { type: "object", properties: { project: PROJECT, limit: { type: "number", description: "返回条数，缺省 10" }, python: PYTHON }, required: [] },
      {
        schema: outSchema({
          root: { type: "string" },
          head: { type: "string" },
          branch: { type: "string" },
          count: { type: "number" },
          commits: { type: "array", items: freeObject },
        }),
        render: (_a, v) => {
          if (!v.ok) return block(`✗ ${v.error && v.error.message}`);
          const lines = [`✓ log | branch=${v.branch || "?"} head=${v.head ? String(v.head).slice(0, 7) : "(none)"} count=${v.count}`];
          for (const c of list(v.commits).slice(0, 20)) lines.push(`- ${c.short_hash || c.hash} ${c.authored_at || ""} ${c.subject || ""}`);
          return block(lines.join("\n"));
        },
        presentationMeta: (_a, v) => ({ kind: "cad-log", ok: !!v.ok, count: v.count || 0, error: v.error || null }),
      },
      async (args, exec) => {
        const { dir } = projectOf(exec, args);
        const argv = ["log", "-p", dir];
        if (num(args.limit) > 0) argv.push("--limit", String(Math.trunc(num(args.limit))));
        const r = await runCad3(argv, { cwd: dir, session: sessionOf(exec), python: text(args.python) || undefined });
        if (!r.ok) return fail(r.error);
        const d = r.data || {};
        return {
          ok: true,
          root: d.root || dir,
          head: d.head || null,
          branch: d.branch || null,
          count: d.count ?? list(d.commits).length,
          commits: list(d.commits).slice(0, 50),
        };
      },
    );

    define(
      "cad3_diff",
      "查看工程文件差异（Git 文件差异，不是几何语义差异）。可传 revision 查看与某次提交的差异。",
      {
        type: "object",
        properties: { project: PROJECT, revision: { type: "string", description: "可选 revision，如 HEAD~1" }, python: PYTHON },
        required: [],
      },
      {
        schema: outSchema({
          root: { type: "string" },
          revision: { type: "string" },
          target: { type: "string" },
          clean: { type: "boolean" },
          file_count: { type: "number" },
          files: { type: "array", items: freeObject },
          untracked: { type: "array", items: { type: "string" } },
          text: { type: "string" },
          note: { type: "string" },
        }),
        render: (_a, v) => {
          if (!v.ok) return block(`✗ ${v.error && v.error.message}`);
          const lines = [`✓ diff | target=${v.target || "?"} clean=${v.clean ? "yes" : "no"} files=${v.file_count ?? list(v.files).length}`];
          for (const f of list(v.files).slice(0, 30)) lines.push(`- ${f.status || ""} ${f.path || f}`);
          if (list(v.untracked).length) lines.push(`untracked: ${list(v.untracked).length} 个（未进入文本 diff）`);
          if (v.text) lines.push("", clip(v.text, 8000));
          if (v.note) lines.push(v.note);
          return block(lines.join("\n"));
        },
        presentationMeta: (_a, v) => ({ kind: "cad-diff", ok: !!v.ok, files: v.file_count || 0, error: v.error || null }),
      },
      async (args, exec) => {
        const { dir } = projectOf(exec, args);
        const argv = ["diff", "-p", dir];
        if (text(args.revision)) argv.push(text(args.revision));
        const r = await runCad3(argv, { cwd: dir, session: sessionOf(exec), python: text(args.python) || undefined });
        if (!r.ok) return fail(r.error);
        const d = r.data || {};
        return {
          ok: true,
          root: d.root || dir,
          revision: d.revision || null,
          target: d.target || null,
          clean: !!d.clean,
          file_count: d.file_count ?? list(d.files).length,
          files: list(d.files).slice(0, 100),
          untracked: list(d.untracked).slice(0, 100),
          text: clip(d.text || "", 8000),
          note: d.note || null,
        };
      },
    );

    define(
      "cad3_commit",
      "只快照当前工程文件为一次设计提交，不重跑模型；返回提交哈希与文件清单。",
      {
        type: "object",
        properties: { project: PROJECT, message: { type: "string", description: "提交信息" }, python: PYTHON },
        required: ["message"],
      },
      {
        schema: outSchema({
          committed: { type: "boolean" },
          no_changes: { type: "boolean" },
          hash: { type: "string" },
          short_hash: { type: "string" },
          head: { type: "string" },
          previous_head: { type: "string" },
          branch: { type: "string" },
          file_count: { type: "number" },
          files: { type: "array", items: freeObject },
          message: { type: "string" },
        }),
        render: (_a, v) => {
          if (!v.ok) return block(`✗ ${v.error && v.error.message}`);
          if (v.no_changes || !v.committed) return block(`✓ commit | 没有变更需要提交`);
          return block(`✓ commit ${v.short_hash || v.hash} (${v.file_count ?? list(v.files).length} files)\n${v.message || ""}`);
        },
        presentationMeta: (_a, v) => ({ kind: "cad-commit", ok: !!v.ok, hash: v.short_hash || v.hash || null, error: v.error || null }),
      },
      async (args, exec) => {
        if (!text(args.message)) return fail({ code: "E-ARGS", message: "commit 需要 message" });
        const { dir } = projectOf(exec, args);
        const r = await runCad3(["commit", "-p", dir, "-m", text(args.message)], {
          cwd: dir,
          session: sessionOf(exec),
          python: text(args.python) || undefined,
        });
        if (!r.ok) return fail(r.error);
        const d = r.data || {};
        return {
          ok: true,
          committed: !!d.committed,
          no_changes: !!d.no_changes,
          hash: d.hash || null,
          short_hash: d.short_hash || null,
          head: d.head || null,
          previous_head: d.previous_head || null,
          branch: d.branch || null,
          file_count: d.file_count ?? list(d.files).length,
          files: list(d.files).slice(0, 100),
          message: d.message || null,
        };
      },
    );

    define(
      "cad3_restore",
      "把工程恢复到某次提交的文件内容（HEAD 不移动，回退本身可以再次提交）。revision 如 HEAD~1 或短哈希。",
      {
        type: "object",
        properties: { project: PROJECT, revision: { type: "string", description: "要恢复到的 revision" }, python: PYTHON },
        required: ["revision"],
      },
      {
        schema: outSchema({
          revision: { type: "string" },
          target: { type: "string" },
          short_target: { type: "string" },
          head: { type: "string" },
          head_moved: { type: "boolean" },
          branch: { type: "string" },
          restored: { type: "array", items: { type: "string" } },
          verbatim: freeObject,
        }),
        render: (_a, v) => {
          if (!v.ok) return block(`✗ ${v.error && v.error.message}`);
          return block(`✓ restore ${v.short_target || v.target} | ${list(v.restored).length} 个文件恢复 | HEAD 未移动`);
        },
        presentationMeta: (_a, v) => ({ kind: "cad-restore", ok: !!v.ok, target: v.short_target || null, error: v.error || null }),
      },
      async (args, exec) => {
        const revision = text(args.revision);
        if (!revision) return fail({ code: "E-ARGS", message: "restore 需要 revision" });
        const { dir } = projectOf(exec, args);
        const r = await runCad3(["restore", "-p", dir, revision], {
          cwd: dir,
          session: sessionOf(exec),
          python: text(args.python) || undefined,
        });
        if (!r.ok) return fail(r.error);
        const d = r.data || {};
        return {
          ok: true,
          revision: d.revision || revision,
          target: d.target || null,
          short_target: d.short_target || null,
          head: d.head || null,
          head_moved: !!d.head_moved,
          branch: d.branch || null,
          restored: list(d.restored).slice(0, 200),
          verbatim: d,
        };
      },
    );

    // ── cad3_cli（兜底）─────────────────────────────────────────────────

    define(
      "cad3_cli",
      "兜底工具：把任意 cad3 argv 原样转发给内嵌 CLI（例如 inspect --detail 的完整 JSON、catalog 之外的扩展参数）。只在专用工具覆盖不到时使用。",
      {
        type: "object",
        properties: {
          argv: { type: "array", items: { type: "string" }, description: "cad3 子命令与参数，如 [\"inspect\",\"--detail\"]" },
          project: PROJECT,
          timeout: TIMEOUT(120),
          python: PYTHON,
        },
        required: ["argv"],
      },
      {
        schema: outSchema({
          argv: { type: "array", items: { type: "string" } },
          exit_code: { type: "number" },
          json: freeObject,
          text: { type: "string" },
          stderr: { type: "string" },
        }),
        render: (_a, v) => {
          if (!v.ok) return block(`✗ ${v.error && v.error.message}${v.stderr ? `\n${clip(v.stderr, 2000)}` : ""}`);
          if (v.text) return block(clip(v.text, 20000));
          return block(clip(JSON.stringify(v.json, null, 2), 20000));
        },
        presentationMeta: (_a, v) => ({ kind: "cad-cli", ok: !!v.ok, error: v.error || null }),
      },
      async (args, exec) => {
        const argv = list(args.argv).map((x) => text(x)).filter(Boolean);
        if (argv.length === 0) return fail({ code: "E-ARGS", message: "argv 不能为空" });
        const { dir } = projectOf(exec, args);
        const r = await runCad3(argv, {
          cwd: dir,
          session: sessionOf(exec),
          signal: combinedSignal(exec, Math.max(1, num(args.timeout, 120)) * 1000),
          python: text(args.python) || undefined,
        });
        if (r.data) {
          return {
            ok: !!r.ok,
            argv,
            exit_code: r.exitCode,
            json: r.data,
            ...(r.ok ? {} : { error: r.error }),
          };
        }
        return {
          ok: !!r.ok,
          argv,
          exit_code: r.exitCode,
          text: clip(r.textOut || "", 20000),
          stderr: clip(r.stderr || "", 4000),
          ...(r.ok ? {} : { error: r.error || { code: "E-CAD3", message: `cad3 退出码 ${r.exitCode}` } }),
        };
      },
    );
  },
};
