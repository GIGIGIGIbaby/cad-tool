// 插件注册与真实 CLI 冒烟测试（CI 与本地都能跑，不需要 FreeCAD）。
//
//   node packages/dsh-cad-studio/test/register.test.mjs
//
// 覆盖：
//   1. 插件模块能 apply，注册 13 个 cad3_* 工具，每个都有 parameters/output/execute；
//   2. 用真实 node:child_process 冒充 Host 的 subprocess 服务；
//   3. cad3_catalog 真的调用内嵌 Python CLI 并返回目录结果；
//   4. cad3_init + cad3_status 在临时目录里跑通（不依赖 FreeCAD 与 git 身份）。
import { spawn as nodeSpawn } from "node:child_process";
import { mkdtempSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PACKAGE_DIR = resolve(HERE, "..");
const PY_ENTRY = join(PACKAGE_DIR, "python", "cad3.py");

const assert = (cond, message) => {
  if (!cond) throw new Error(message);
};

/** 用 node:child_process 实现 Host subprocess service 的最小形状。 */
function fakeSubprocess() {
  return {
    spawn(opts) {
      const [cmd, ...args] = opts.argv;
      const child = nodeSpawn(cmd, args, { cwd: opts.cwd, env: process.env });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (d) => {
        stdout += String(d);
      });
      child.stderr.on("data", (d) => {
        stderr += String(d);
      });
      const done = new Promise((resolveDone) => {
        child.on("close", (exitCode, signal) => resolveDone({ exitCode, signal }));
      });
      const collected = {
        stdout: { readFrom: () => ({ text: stdout, truncated: false }) },
        stderr: { readFrom: () => ({ text: stderr, truncated: false }) },
      };
      return { done, collected };
    },
  };
}

const registered = [];
const registeredSkills = [];
const services = {
  subprocess: fakeSubprocess(),
  skills: { register: (def) => { registeredSkills.push(def); return () => {}; } },
};
const ctx = {
  tools: { register: (def) => registered.push(def) },
  get: (name) => services[name],
  logger: { warn: () => {} },
  effect: (fn) => { fn(); },
};

const plugin = (await import("../lib/index.js")).default;
assert(typeof plugin.apply === "function", "插件 default.apply 不是函数");
assert(Array.isArray(plugin.inject) && plugin.inject.includes("subprocess"), "inject 必须包含 subprocess");
plugin.apply(ctx, {});

const expected = [
  "cad3_doctor", "cad3_init", "cad3_run", "cad3_inspect", "cad3_render",
  "cad3_mates", "cad3_catalog", "cad3_status", "cad3_log", "cad3_diff",
  "cad3_commit", "cad3_restore", "cad3_cli",
];
const names = registered.map((d) => d.name);
for (const want of expected) assert(names.includes(want), `缺少工具 ${want}`);
assert(registered.length === expected.length, `工具数应为 ${expected.length}，实际 ${registered.length}`);

for (const def of registered) {
  assert(typeof def.description === "string" && def.description.length > 0, `${def.name} 缺 description`);
  assert(def.parameters && def.parameters.type === "object", `${def.name} 缺 parameters schema`);
  assert(def.output && def.output.schema, `${def.name} 缺 output.schema`);
  assert(typeof def.output.render === "function", `${def.name} 缺 output.render`);
  assert(typeof def.execute === "function", `${def.name} 缺 execute`);
}

const byName = new Map(registered.map((d) => [d.name, d]));
assert(registeredSkills.length === 1, `应注册 1 个 skill，实际 ${registeredSkills.length}`);
const skill = registeredSkills[0];
assert(skill.name === "freecad-v3", `skill name 错误: ${skill.name}`);
assert(/FreeCAD/.test(skill.description), "skill description 异常");
assert(skill.content.includes("DSH 插件里的调用方式"), "skill 正文缺插件调用说明");
assert(skill.resourceBase && skill.resourceBase.kind === "directory", "skill resourceBase 异常");

const fakeExec = { agent: { session: { header: { cwd: PACKAGE_DIR } } } };

// 1) catalog 使用真实内嵌 CLI 查询（不需要 FreeCAD）。
const catalog = await byName.get("cad3_catalog").execute({ action: "search", query: "6204", limit: 2 }, fakeExec);
assert(catalog.ok === true, `catalog search 失败: ${JSON.stringify(catalog.error || catalog)}`);
assert(Array.isArray(catalog.catalog) && catalog.catalog.length > 0, "catalog search 没有返回候选");
const catalogText = byName.get("cad3_catalog").output.render({}, catalog);
assert(Array.isArray(catalogText) && catalogText[0].text.includes("catalog search"), "catalog render 文本异常");

// 2) init + status 在临时目录跑通（FreeCAD 不参与）。
const tmp = mkdtempSync(join(tmpdir(), "cad3-plugin-test-"));
try {
  const projectDir = join(tmp, "demo");
  const init = await byName.get("cad3_init").execute({ path: projectDir, name: "插件测试" }, fakeExec);
  assert(init.ok === true, `init 失败: ${JSON.stringify(init.error || init)}`);
  assert(existsSync(join(projectDir, "cad3.json")), "init 没生成 cad3.json");
  const status = await byName.get("cad3_status").execute({ project: projectDir }, fakeExec);
  assert(status.ok === true, `status 失败: ${JSON.stringify(status.error || status)}`);
  assert(typeof status.branch === "string", "status 没返回 branch");
} finally {
  rmSync(tmp, { recursive: true, force: true });
}

// 3) 渲染文本与 presentationMeta 形状。
const meta = byName.get("cad3_catalog").output.presentationMeta({}, catalog);
assert(meta && meta.kind === "cad-catalog" && meta.ok === true, "presentationMeta 形状异常");

console.log(`[register.test] PASS：${registered.length} 个工具注册成功；catalog 与 init/status 冒烟通过`);
