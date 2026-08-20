import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// https://vite.dev/config/
export default defineConfig(() => {
  // 静态 Demo（GitHub Pages）构建：
  //   VITE_DEMO=1 VITE_BASE=/quant/ VITE_OUT_DIR=dist-demo npm run build
  // 常规构建（随 FastAPI 一起提供）保持输出到项目根目录的 static/
  const base = process.env.VITE_BASE || '/'
  const outDir = process.env.VITE_OUT_DIR
    ? path.resolve(__dirname, process.env.VITE_OUT_DIR)
    : path.resolve(__dirname, '../../static')

  return {
    base,
    // 显式注入，避免依赖 .env 文件即可用 VITE_DEMO=1 构建
    define: {
      'import.meta.env.VITE_DEMO': JSON.stringify(process.env.VITE_DEMO ?? ''),
    },
    plugins: [
      react({
        babel: {
          plugins: [['babel-plugin-react-compiler']],
        },
      }),
    ],
    server: {
      host: '0.0.0.0',  // 允许公网访问
      port: 5173,       // 默认端口
    },
    build: {
      outDir,
      emptyOutDir: true,
    },
  }
})
