#!/usr/bin/env node
// 把仓库里的 Python CLI 与 freecad-v3 skill 同步进插件包。
//
// npm 只能打包包目录内的文件，所以 cad3 的 Python 源码与 skill 以副本形式随
// 插件分发（与 V2 的 packages/sync-vendor.sh 同一思路）。发布前必须运行一次：
//
//   node packages/sync-vendor.mjs          # 同步
//   node packages/sync-vendor.mjs --check  # 只校验是否已同步（CI 用）
//
// 生成物要提交进 Git，这样 `dsh plugin add github:liwuzhan/cad-tool#path:/packages/dsh-cad-studio`
// 这种不跑构建的安装方式也能拿到完整插件。
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const PKG = join(ROOT, "packages", "dsh-cad-studio");
const CHECK = process.argv.includes("--check");

const SKIP_DIRS = new Set(["__pycache__", ".pytest_cache", ".DS_Store", ".git"]);

const SKILL_HEADER = `<!-- 本段由 packages/sync-vendor.mjs 生成；不要手改，改 .agents/skills/freecad-v3/ 后重跑同步。 -->
## DSH 插件里的调用方式

这个 skill 随 \`dsh-cad-studio\` 插件安装。在 DSH 会话里用插件工具做 CAD，不要在 shell 里找
\`cad3\` 可执行文件（插件把 Python CLI 内嵌在自己的包目录里）：

- \`cad3_doctor\`：探测已安装的 FreeCAD（缺 FreeCAD 时先报告，不自动安装）。
- \`cad3_init\` / \`cad3_run\` / \`cad3_inspect\` / \`cad3_render\` / \`cad3_mates\` /
  \`cad3_status\` / \`cad3_log\` / \`cad3_diff\` / \`cad3_commit\` / \`cad3_restore\`：
  对应同名 CLI 子命令，常用参数与 CLI 一致。
- \`cad3_catalog\`：查询随插件内置的标准件目录（search / describe / compare / instantiate）。
- \`cad3_cli\`：兜底工具，把任意 \`cad3\` argv 原样转发，用于工具尚未覆盖的参数。
- 工程目录用 \`project\` 参数传入；不传时默认会话工作区。

下面是从仓库同步来的原 skill 正文。

`;

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (SKIP_DIRS.has(entry.name)) continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (entry.isFile()) yield full;
  }
}

function sha256(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

function relFiles(base) {
  const out = [];
  if (!existsSync(base)) return out;
  if (!statSync(base).isDirectory()) return [[ "", readFileSync(base) ]];
  for (const full of walk(base)) out.push([relative(base, full).split(sep).join("/"), readFileSync(full)]);
  out.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return out;
}

/** 期望的插件内文件：路径 -> Buffer。 */
function expected() {
  const files = new Map();

  // 1) Python 入口
  files.set("python/cad3.py", readFileSync(join(ROOT, "cad3.py")));

  // 2) Python 包（含 data/catalog/*.json）
  const srcBase = join(ROOT, "src", "cad3");
  const srcFiles = [];
  for (const full of walk(srcBase)) srcFiles.push(full);
  srcFiles.sort();
  for (const full of srcFiles) {
    const rel = relative(srcBase, full).split(sep).join("/");
    files.set(`python/src/cad3/${rel}`, readFileSync(full));
  }

  // 3) skill（保留 YAML frontmatter 在第一行，插件说明插在 frontmatter 之后）
  const skillBase = join(ROOT, ".agents", "skills", "freecad-v3");
  const skillFiles = [];
  for (const full of walk(skillBase)) skillFiles.push(full);
  skillFiles.sort();
  for (const full of skillFiles) {
    const rel = relative(skillBase, full).split(sep).join("/");
    let body = readFileSync(full);
    if (rel === "SKILL.md") body = Buffer.from(withHeader(body.toString("utf8")), "utf8");
    files.set(`skills/freecad-v3/${rel}`, body);
  }
  return files;
}

function withHeader(source) {
  const text = source.replace(/^\uFEFF/, "");
  if (text.startsWith("---\n")) {
    const end = text.indexOf("\n---\n", 4);
    if (end !== -1) {
      const front = text.slice(0, end + 5);
      return front + SKILL_HEADER + text.slice(end + 5);
    }
  }
  return SKILL_HEADER + text;
}

function actual() {
  // 只校验生成目录；包内的 lib/、README 等是手写文件，不属于同步范围。
  const files = new Map();
  for (const prefix of ["python", "skills"]) {
    for (const [rel, body] of relFiles(join(PKG, prefix))) {
      files.set(`${prefix}/${rel}`.split(sep).join("/"), body);
    }
  }
  return files;
}

const want = expected();

if (CHECK) {
  const have = actual();
  const problems = [];
  for (const [rel, body] of want) {
    if (!have.has(rel)) problems.push(`missing  ${rel}`);
    else if (sha256(have.get(rel)) !== sha256(body)) problems.push(`stale    ${rel}`);
  }
  for (const rel of have.keys()) if (!want.has(rel)) problems.push(`extra    ${rel}`);
  if (problems.length) {
    console.error("[sync-vendor] 插件 vendor 与仓库源码不一致，运行 node packages/sync-vendor.mjs 后提交：");
    for (const p of problems.slice(0, 40)) console.error("  " + p);
    if (problems.length > 40) console.error(`  ... ${problems.length - 40} more`);
    process.exit(1);
  }
  console.log(`[sync-vendor] OK：插件 vendor 已同步（${want.size} 个文件）`);
  process.exit(0);
}

// 清理旧目录，避免已删除的源文件在插件里残留。
for (const dir of ["python", "skills"]) rmSync(join(PKG, dir), { recursive: true, force: true });
mkdirSync(join(PKG, "lib"), { recursive: true });

for (const [rel, body] of want) {
  const dest = join(PKG, rel);
  mkdirSync(dirname(dest), { recursive: true });
  writeFileSync(dest, body);
}
console.log(`[sync-vendor] 写入 ${want.size} 个文件到 packages/dsh-cad-studio/{python,skills}`);
