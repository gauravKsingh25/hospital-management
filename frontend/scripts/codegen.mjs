#!/usr/bin/env node
/**
 * Regenerate the API types from the backend, or verify they are current.
 *
 *   node scripts/codegen.mjs          # write them
 *   node scripts/codegen.mjs --check  # fail if what is committed is stale
 *
 * Three artefacts come out of the backend, all machine-written:
 *
 *   openapi.json                 the schema, straight from FastAPI
 *   src/types/openapi.d.ts       every request and response shape
 *   src/types/rbac.generated.ts  permission and role name unions
 *
 * `--check` is the point of the exercise. Generated types that nobody
 * verifies are hand-written types with extra steps: they drift the moment
 * someone changes a Pydantic model and forgets to rerun this. Wiring the
 * check into CI makes "the frontend types match the backend" a fact rather
 * than a habit — the same role `alembic check` plays for the database.
 *
 * Written in Node rather than as a shell one-liner in package.json purely so
 * it works on a Windows workstation and in a Linux container without two
 * versions to keep in step.
 */

import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(frontendDir, "..");
const backendDir = join(repoRoot, "backend");

const check = process.argv.includes("--check");

/**
 * Find the interpreter that has the backend's dependencies installed.
 *
 * The repository's virtualenv first, because that is where FastAPI actually
 * lives on a development machine; a bare `python` last, which is what a
 * container image has after `pip install`.
 */
function findPython() {
  const candidates = [
    join(repoRoot, ".venv", "Scripts", "python.exe"), // Windows
    join(repoRoot, ".venv", "bin", "python"), // macOS / Linux
    join(backendDir, ".venv", "Scripts", "python.exe"),
    join(backendDir, ".venv", "bin", "python"),
  ];

  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  return process.platform === "win32" ? "python" : "python3";
}

const python = findPython();

function run(command, args, cwd) {
  execFileSync(command, args, { cwd, stdio: "inherit" });
}

function generate(targets) {
  run(python, [join("scripts", "export_openapi.py"), "--output", targets.openapiJson], backendDir);
  run(
    process.execPath,
    [
      join(frontendDir, "node_modules", "openapi-typescript", "bin", "cli.js"),
      targets.openapiJson,
      "-o",
      targets.openapiTypes,
    ],
    frontendDir,
  );
  run(python, [join("scripts", "export_rbac.py"), "--output", targets.rbacTypes], backendDir);
}

const committed = {
  openapiJson: join(frontendDir, "openapi.json"),
  openapiTypes: join(frontendDir, "src", "types", "openapi.d.ts"),
  rbacTypes: join(frontendDir, "src", "types", "rbac.generated.ts"),
};

if (!check) {
  generate(committed);
  console.log("\nAPI types regenerated.");
  process.exit(0);
}

// --check: generate into a scratch directory and compare, so a stale tree is
// reported rather than silently fixed. Fixing it here would make CI pass on a
// commit whose contents are wrong.
const scratch = mkdtempSync(join(tmpdir(), "hms-codegen-"));
try {
  const fresh = {
    openapiJson: join(scratch, "openapi.json"),
    openapiTypes: join(scratch, "openapi.d.ts"),
    rbacTypes: join(scratch, "rbac.generated.ts"),
  };
  generate(fresh);

  const stale = Object.keys(committed).filter((key) => {
    if (!existsSync(committed[key])) return true;
    return readFileSync(committed[key], "utf8") !== readFileSync(fresh[key], "utf8");
  });

  if (stale.length > 0) {
    console.error(
      `\nThese generated files are out of date:\n` +
        stale.map((key) => `  - ${committed[key]}`).join("\n") +
        `\n\nThe backend's API has changed. Run \`npm run codegen\` and commit the result.\n`,
    );
    process.exit(1);
  }

  console.log("\nGenerated API types are up to date.");
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
