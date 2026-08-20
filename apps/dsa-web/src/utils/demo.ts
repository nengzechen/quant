// 静态 Demo 模式：VITE_DEMO=1 构建时启用。
// 此模式下前端不访问后端 API，改为读取构建时生成的静态 JSON 快照
// （由 scripts/build_demo_data.py 从 data/seed_pool_*.json 生成），
// 用于部署到 GitHub Pages 这类纯静态托管。
export const IS_DEMO = import.meta.env.VITE_DEMO === '1';

const DEMO_DATA_BASE = `${import.meta.env.BASE_URL}demo-data/`;

export async function fetchDemoJson<T>(file: string): Promise<T> {
  const res = await fetch(`${DEMO_DATA_BASE}${file}`, { cache: 'no-cache' });
  if (!res.ok) {
    throw new Error(`demo data ${file}: HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}
