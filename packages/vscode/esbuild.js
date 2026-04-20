/**
 * esbuild bundler for the CrabCode VS Code extension.
 *
 * Production:  node esbuild.js --production
 * Watch mode:  node esbuild.js
 */

const esbuild = require("esbuild");

const isProd = process.argv.includes("--production");

const buildOpts = {
  entryPoints: ["src/extension.ts"],
  bundle: true,
  outfile: "dist/extension.js",
  external: ["vscode"],
  format: "cjs",
  platform: "node",
  target: "ES2022",
  sourcemap: !isProd,
  minify: isProd,
  define: {
    "process.env.NODE_ENV": isProd ? '"production"' : '"development"',
  },
};

if (!isProd) {
  // Watch mode
  esbuild
    .context(buildOpts)
    .then((ctx) => ctx.watch())
    .catch(() => process.exit(1));
} else {
  esbuild.build(buildOpts).catch(() => process.exit(1));
}
