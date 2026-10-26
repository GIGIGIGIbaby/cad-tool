// 验证 npm pack 出来的 tarball 在干净项目里可安装、可解析、可运行内嵌 CLI。
//
//   node packages/dsh-cad-studio/test/tarball.test.mjs dist/dsh-cad-studio.tgz
//
// 覆盖：exports 解析（主入口 / ./client / package.json）、dsh.bundle 清单、
// 13 个工具注册、内嵌 python/cad3.py 的 catalog 查询（不需要 FreeCAD）。
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const tarball = process.argv[2];
if (!tarball) throw new Error("用法: node tarball.test.mjs <dsh-cad-studio.tgz>");
const here = dirname(fileURLToPath(import.meta.url));

const assert = (cond, message) => {
  if (!cond) throw new Error(message);
};

const work = mkdtempSync(join(tmpdir(), "dsh-cad-studio-tarball-"));
try {
  writeFileSync(join(work, "package.json"), JSON.stringify({ name: "tarball-smoke", private: true, type: "module" }, null, 2));
  execFileSync("npm", ["install", "--no-save", "--ignore-scripts", "--no-audit", "--no-fund", resolve(tarball)], {
    cwd: work,
    stdio: ["ignore", "inherit", "inherit"],
    env: { ...process.env, npm_config_loglevel: "error" },
  });

  const require = createRequire(join(work, "package.json"));
  const packageJsonPath = require.resolve("dsh-cad-studio/package.json");
  const packageDir = dirname(packageJsonPath);
  const pkg = JSON.parse(readFileSync(packageJsonPath, "utf8"));
  assert(pkg.name === "dsh-cad-studio", "package name 错误");
  assert(pkg.dsh?.bundle?.patch === "./cordis.patch.yml", "缺 dsh.bundle.patch");
  assert(pkg.dsh?.client?.platform === "web", "缺 dsh.client.platform");
  assert(Array.isArray(pkg.dsh?.client?.inject) && pkg.dsh.client.inject.length >= 1, "缺 dsh.client.inject");
  assert(existsSync(join(packageDir, "cordis.patch.yml")), "tarball 缺 cordis.patch.yml");
  assert(existsSync(join(packageDir, "python", "cad3.py")), "tarball 缺内嵌 python/cad3.py");
  assert(existsSync(join(packageDir, "skills", "freecad-v3", "SKILL.md")), "tarball 缺 freecad-v3 skill");

  const clientUrl = require.resolve("dsh-cad-studio/client");
  assert(readFileSync(clientUrl, "utf8").includes('id: "dsh-cad-studio"'), "client 模块 id 不对");

  const plugin = (await import(pathToFileURL(require.resolve("dsh-cad-studio")).href)).default;
  assert(typeof plugin.apply === "function", "插件没有 apply");

  // 用 node:child_process 冒充 Host subprocess 服务，跑内嵌 CLI。
  const { spawn } = await import("node:child_process");
  const subprocess = {
    spawn(opts) {
      const [cmd, ...args] = opts.argv;
      const child = spawn(cmd, args, { cwd: opts.cwd, env: process.env });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (d) => (stdout += String(d)));
      child.stderr.on("data", (d) => (stderr += String(d)));
      const done = new Promise((res) => child.on("close", (exitCode, signal) => res({ exitCode, signal })));
      return {
        done,
        collected: {
          stdout: { readFrom: () => ({ text: stdout, truncated: false }) },
          stderr: { readFrom: () => ({ text: stderr, truncated: false }) },
        },
      };
    },
  };
  const defs = [];
  plugin.apply({ tools: { register: (d) => defs.push(d) }, get: (n) => (n === "subprocess" ? subprocess : undefined), logger: { warn: () => {} } }, {});
  assert(defs.length === 13, `工具数应为 13，实际 ${defs.length}`);

  const catalogTool = defs.find((d) => d.name === "cad3_catalog");
  const exec = { agent: { session: { header: { cwd: work } } } };
  const result = await catalogTool.execute({ action: "search", query: "6204", limit: 1 }, exec);
  assert(result.ok === true, `tarball 内嵌 CLI 的 catalog 查询失败: ${JSON.stringify(result.error || result)}`);
  assert(Array.isArray(result.catalog) && result.catalog.length === 1, "catalog search 返回数量异常");

  console.log(`[tarball.test] PASS：tarball 可安装、exports 可解析、13 个工具注册、内嵌 CLI 可查询目录（${packageDir}）`);
} finally {
  rmSync(work, { recursive: true, force: true });
}
