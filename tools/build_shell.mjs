import { build } from "esbuild";
import { mkdirSync } from "node:fs";
mkdirSync("ankiweb/shell/static", { recursive: true });
await build({
  entryPoints: {
    bootstrap: "shell_src/bootstrap.ts",
    security: "shell_src/security.ts",
  },
  bundle: true,
  format: "iife",
  target: "es2020",
  outdir: "ankiweb/shell/static",
});
console.log("built ankiweb/shell/static/bootstrap.js + security.js");
