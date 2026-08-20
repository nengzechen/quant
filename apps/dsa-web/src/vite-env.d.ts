/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL?: string;
  /** 静态 Demo 模式（GitHub Pages 构建时置为 '1'） */
  readonly VITE_DEMO?: string;
}
