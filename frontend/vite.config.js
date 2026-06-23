
import { defineConfig } from 'vite';

import react from '@vitejs/plugin-react';
import { readFileSync } from 'node:fs';


const packageJson = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf-8'));



export default defineConfig({

  plugins: [react()],
  define: {
    'import.meta.env.VITE_DPMS_VERSION': JSON.stringify(process.env.VITE_DPMS_VERSION || packageJson.version),
  },

  server: {

    port: 3000,

    proxy: {

      '/api': 'http://localhost:80'

    }

  },

  build: {

    outDir: '../dashboard/dist',

    emptyOutDir: true

  }

});
