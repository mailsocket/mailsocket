// Post-process the CommonJS tsc output: rename .js/.d.ts -> .cjs/.d.cts,
// rewrite internal require()/import() specifiers to match, and drop a
// {"type":"commonjs"} package.json into dist/cjs so require('mailsocket-sdk')
// resolves these files as CommonJS regardless of the package's own
// "type": "module".
import { readdirSync, readFileSync, writeFileSync, unlinkSync, statSync } from "node:fs";
import { join } from "node:path";

const CJS_DIR = new URL("../dist/cjs", import.meta.url).pathname;

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      out.push(...walk(full));
    } else {
      out.push(full);
    }
  }
  return out;
}

// The CJS build ships without source maps (see below), so drop the
// `//# sourceMappingURL=x.js.map` trailer tsc appends — otherwise every .cjs /
// .d.cts would advertise a map that isn't in the package.
function stripSourceMapComment(content) {
  return content.replace(/^\/\/# sourceMappingURL=.*(?:\r?\n)?/gm, "");
}

function rewriteSpecifiers(content) {
  return stripSourceMapComment(content)
    .replace(/(require\(["']\.[^"']*?)\.js(["']\))/g, "$1.cjs$2")
    .replace(/(from\s+["']\.[^"']*?)\.js(["'])/g, "$1.cjs$2")
    .replace(/(import\(["']\.[^"']*?)\.js(["']\))/g, "$1.cjs$2");
}

const files = walk(CJS_DIR);
const toDelete = [];

for (const file of files) {
  if (file.endsWith(".d.ts")) {
    const content = rewriteSpecifiers(readFileSync(file, "utf8"));
    writeFileSync(file.slice(0, -".d.ts".length) + ".d.cts", content);
    toDelete.push(file);
  } else if (file.endsWith(".js")) {
    const content = rewriteSpecifiers(readFileSync(file, "utf8"));
    writeFileSync(file.slice(0, -".js".length) + ".cjs", content);
    toDelete.push(file);
  } else if (file.endsWith(".js.map") || file.endsWith(".d.ts.map")) {
    // Maps reference pre-rename filenames; the CJS build ships without them
    // (the ESM build already carries source maps for debugging).
    toDelete.push(file);
  }
}

for (const file of toDelete) unlinkSync(file);

writeFileSync(join(CJS_DIR, "package.json"), JSON.stringify({ type: "commonjs" }, null, 2) + "\n");
