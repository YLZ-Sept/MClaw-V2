import { readFileSync } from "node:fs";
import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

const buildTarget = process.env.VITE_BUILD_TARGET || "tauri";
const isWebBuild = buildTarget === "web";
const isCapBuild = buildTarget === "capacitor";
const isRemoteBuild = isWebBuild || isCapBuild;

// P-RC-2 commit P2.8: stable build identifier embedded into the
// bundle. CI sets VITE_BUILD_ID to a short SHA / timestamp.
//
// Local fallback (no VITE_BUILD_ID):
// - `vite build` sets NODE_ENV=production: read the version from
//   package.json so the bundle id matches the backend `mclaw`
//   package version. A `dev-...` id is always reported as outdated
//   by the backend's `is_frontend_bundle_outdated` rule, which would
//   trip `/api/health` `frontend_bundle.outdated` after every
//   `npm run build:web`.
// - `vite` (serve) sets NODE_ENV=development: keep the `dev-<ts>`
//   sentinel so HMR reloads show a stable id within a session and
//   StaleBundleBanner short-circuits on the `dev-` prefix.
function readPackageVersion(): string {
  try {
    const pkg = JSON.parse(
      readFileSync(path.resolve(__dirname, "package.json"), "utf-8"),
    ) as { version?: string };
    return pkg.version || "";
  } catch {
    return "";
  }
}

const buildId =
  process.env.VITE_BUILD_ID ||
  (process.env.NODE_ENV === "production" ? readPackageVersion() : "") ||
  `dev-${Date.now().toString(36)}`;

function tauriStubPlugin(): Plugin {
  const prefix = "@tauri-apps/";
  return {
    name: "tauri-stub",
    enforce: "pre",
    resolveId(id) {
      if (id.startsWith(prefix)) return `\0tauri-stub:${id}`;
    },
    load(id) {
      if (!id.startsWith("\0tauri-stub:")) return;
      const noop = "() => Promise.resolve(undefined)";
      return [
        `const _noop = ${noop};`,
        `export default _noop;`,
        // re-export every name any source file might import
        ...[
          "invoke", "listen", "emit", "getVersion", "getName", "getTauriVersion",
          "getCurrentWebview", "getCurrentWindow", "WebviewWindow",
          "confirm", "open", "save", "message", "ask",
          "check", "relaunch", "exit",
          "fetch", "readFile", "writeFile", "readTextFile", "writeTextFile",
          "readDir", "createDir", "removeDir", "removeFile", "renameFile", "copyFile", "exists",
        ].map((n) => `export const ${n} = _noop;`),
      ].join("\n");
    },
  };
}

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss(), ...(isRemoteBuild ? [tauriStubPlugin()] : [])],
  define: {
    __BUILD_TARGET__: JSON.stringify(buildTarget),
    __BUILD_ID__: JSON.stringify(buildId),
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      react: path.resolve(__dirname, "./node_modules/react"),
      "react-dom": path.resolve(__dirname, "./node_modules/react-dom"),
      "react-i18next": path.resolve(__dirname, "./node_modules/react-i18next"),
      "@shared/providers.json": path.resolve(
        __dirname,
        "../../src/mclaw/llm/registries/providers.json",
      ),
    },
    // Force a single instance of React + react-dom across the dep graph.
    // Without this, lazy-loaded views (e.g. PluginManagerView / OrgEditorView) can end up
    // calling react-i18next's useTranslation() against a different React copy
    // than the host renderer, causing "Cannot read properties of null
    // (reading 'useContext')" at hook-dispatch time.
    dedupe: ["react", "react-dom", "react-i18next", "@xyflow/react", "zustand", "radix-ui", "three",
      "i18next", "i18next-browser-languagedetector", "class-variance-authority", "clsx", "tailwind-merge",
      "lucide-react", "sonner", "react-force-graph-3d", "3d-force-graph", "three-forcegraph",
      "three-render-objects"],
  },
  optimizeDeps: {
    include: [
      // Pre-bundle React + the i18n chain together at server start so they
      // share a single optimized chunk hash. Otherwise Vite may discover
      // react-i18next on first plugin-page navigation and generate a
      // mismatched React reference.
      "react",
      "react-dom",
      "react-dom/client",
      "react/jsx-runtime",
      "react/jsx-dev-runtime",
      "react-i18next",
      "i18next",
      "i18next-browser-languagedetector",
      "@xyflow/react",
      "zustand",
      "zustand/traditional",
      "radix-ui",
      "react-force-graph-3d",
      "3d-force-graph",
      "three-forcegraph",
      "three-render-objects",
      "three",
      // All shared UI deps that lazy views might import for the first time
      // — pre-bundle them so Vite never re-optimizes mid-session and
      // creates a duplicate React instance.
      "class-variance-authority",
      "clsx",
      "tailwind-merge",
      "lucide-react",
      "sonner",
      "@radix-ui/react-slider",
      "react-markdown",
      "react-virtuoso",
      "rehype-highlight",
      "rehype-raw",
      "rehype-sanitize",
      "remark-gfm",
      "remark-math",
      "katex",
      "highlight.js",
      "html-to-image",
      "next-themes",
      "qrcode.react",
    ],
  },
  base: isWebBuild ? "/web/" : isCapBuild ? "./" : undefined,
  build: isRemoteBuild
    ? { outDir: "dist-web" }
    : undefined,
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    ...(isWebBuild
      ? {
          proxy: {
            "/api": {
              target: "http://127.0.0.1:18900",
              changeOrigin: true,
            },
            "/ws": {
              target: "ws://127.0.0.1:18900",
              ws: true,
            },
          },
        }
      : {}),
  },
  clearScreen: false,
});

