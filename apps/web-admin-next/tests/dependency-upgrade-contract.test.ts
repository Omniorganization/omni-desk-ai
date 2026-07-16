import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const packageJson = JSON.parse(
  readFileSync(new URL("../package.json", import.meta.url), "utf8"),
) as {
  scripts: Record<string, string>;
  devDependencies: Record<string, string>;
};
const evidence = JSON.parse(
  readFileSync(
    new URL("../evidence/typescript-6-upgrade.json", import.meta.url),
    "utf8",
  ),
) as {
  schema: string;
  tests: string[];
  risk_notes: string[];
  rollback_steps: string[];
};

test("web admin dependency upgrade remains runtime-aligned and evidenced", () => {
  assert.match(packageJson.devDependencies["@types/node"], /^(\^|~)?22\./);
  assert.equal(packageJson.scripts.typecheck, "next typegen && tsc --noEmit");
  assert.equal(evidence.schema, "omnidesk-dependency-upgrade-evidence/v1");
  assert.ok(evidence.tests.includes("npm run build"));
  assert.ok(evidence.risk_notes.length >= 2);
  assert.ok(evidence.rollback_steps.length >= 2);
});
