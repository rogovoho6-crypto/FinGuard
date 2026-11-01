import { defineConfig } from 'vite';
export default defineConfig({server: {proxy: {'/api': {target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8765', changeOrigin: false}}, watch: {usePolling: process.env.VITE_USE_POLLING === '1'}}, build: {target: 'es2022'}});
