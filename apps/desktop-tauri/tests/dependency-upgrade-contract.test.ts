import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const readJson = (path: string) =>
  JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8")) as Record<
    string,
    unknown
  >;

test("desktop upgrade separates browser and Node type environments", () => {
  const packageJson = readJson("../package.json") as {
    scripts: Record<string, string>;
    devDependencies: Record<string, string>;
  };
  const productionConfig = readJson("../tsconfig.json") as {
    compilerOptions: { types: string[] };
    include: string[];
  };
  const testConfig = readJson("../tsconfig.test.json") as {
    compilerOptions: { types: string[] };
    include: string[];
  };
  const evidence = readJson("../evidence/typescript-6-upgrade.json") as {
    schema: string;
    tests: string[];
    risk_notes: string[];
    rollback_steps: string[];
  };

  assert.deepEqual(productionConfig.compilerOptions.types, []);
  assert.deepEqual(productionConfig.include, ["src"]);
  assert.deepEqual(testConfig.compilerOptions.types, ["node"]);
  assert.deepEqual(testConfig.include, ["tests/**/*.ts"]);
  assert.match(packageJson.devDependencies["@types/node"], /^(\^|~)?22\./);
  assert.match(packageJson.scripts.typecheck, /tsconfig\.test\.json/);
  assert.equal(evidence.schema, "omnidesk-dependency-upgrade-evidence/v1");
  assert.ok(evidence.tests.includes("npm run build"));
  assert.ok(evidence.risk_notes.length >= 2);
  assert.ok(evidence.rollback_steps.length >= 2);
});
