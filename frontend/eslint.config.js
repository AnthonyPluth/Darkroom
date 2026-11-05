import js from "@eslint/js";
import { defineConfig } from "eslint/config";
import svelte from "eslint-plugin-svelte";
import globals from "globals";
import ts from "typescript-eslint";

const FRONTEND = "frontend/**/*.{js,ts,svelte}";

const RESTRICTED = {
  errorCast: {
    selector: "MemberExpression[property.name='message'][object.type='TSAsExpression'][object.typeAnnotation.typeName.name='Error']",
    message: "Don't cast to Error to read .message: errMsg(e) from lib/act.ts says what was thrown, whatever it is.",
  },
  fetch: {
    selector: "CallExpression[callee.name='fetch'], CallExpression[callee.object.name=/^(window|globalThis|self)$/][callee.property.name='fetch']",
    message: "Call the server through api() in lib/api.ts, or apiCall() in lib/contract.ts (it sends the cookies and turns a failure into an Error); fetch() belongs only in lib/api.ts.",
  },
};
const restrict = (...names) => ["error", ...names.map((n) => RESTRICTED[n])];

const darkroom = {
  rules: {
    "no-silent-catch": {
      meta: { type: "suggestion", schema: [], messages: { silent: "`.catch(() => {})` swallows the failure: pass `ignoreFailure` from lib/act.ts, which says so by name." } },
      create(context) {
        return {
          CallExpression(node) {
            const { callee, arguments: args } = node;
            const handler = args[0];
            if (callee.type !== "MemberExpression" || callee.property.name !== "catch" || !handler) return;
            if (handler.type !== "ArrowFunctionExpression" && handler.type !== "FunctionExpression") return;
            const body = handler.body;
            if (handler.params.length === 0 && body.type === "BlockStatement" && body.body.length === 0) {
              context.report({ node: handler, messageId: "silent" });
            }
          },
        };
      },
    },
    "no-comments": {
      meta: { type: "problem", schema: [], messages: { comment: "No comments: name things so the code says it, and put the why in the commit message or the docs. Tool directives (eslint-, @ts-, svelte-ignore, @vitest-environment) are the exception." } },
      create(context) {
        const directive = /^\/?\s*(eslint-|@ts-|svelte-ignore|@vitest|<reference|istanbul|c8 |v8 ignore|prettier-ignore|global )/;
        return {
          Program() {
            for (const c of context.sourceCode.getAllComments()) {
              if (c.type === "Shebang" || directive.test(c.value)) continue;
              context.report({ loc: c.loc, messageId: "comment" });
            }
          },
        };
      },
    },
  },
};

export default defineConfig(
  { ignores: ["frontend/coverage/**", "**/node_modules/**"] },
  js.configs.recommended,
  ts.configs.recommended,
  ...svelte.configs.recommended.map((c) => (c.files ? c : { ...c, files: [FRONTEND] })),
  {
    files: [FRONTEND],
    languageOptions: { globals: { ...globals.browser } },
    rules: {
      "no-restricted-syntax": restrict("errorCast", "fetch"),
      "darkroom/no-silent-catch": "error",
      "no-console": ["error", { allow: ["error", "warn"] }],
    },
    plugins: { darkroom },
  },
  { files: ["frontend/src/lib/api.ts"], rules: { "no-restricted-syntax": restrict("errorCast") } },
  {
    rules: {
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" }],
    },
  },
  {
    files: ["frontend/*.{js,ts}", "frontend/verify/*.mjs"],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    files: ["frontend/verify/a11y.mjs"],
    languageOptions: { globals: { ...globals.node, ...globals.browser } },
  },
  {
    files: ["frontend/**/*.svelte", "frontend/**/*.svelte.ts"],
    languageOptions: { parserOptions: { parser: ts.parser } },
  },
  {
    files: ["frontend/**/*.{js,mjs,ts,svelte}"],
    plugins: { darkroom },
    rules: { "darkroom/no-comments": "error" },
  },
);
