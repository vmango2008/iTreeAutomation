/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the FastAPI backend, e.g. https://itree-canopy-api.fly.dev.
   * Unset in local dev, where Vite's own /api proxy (vite.config.ts) is used
   * instead; set at build time for the GitHub Pages deploy, which has no
   * dev-server proxy to rely on. */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
