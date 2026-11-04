import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Vite 8 is powered by Rolldown: its native codeSplitting groups are the
// supported equivalent of Rollup's deprecated output.manualChunks callback.
// Keep PDF.js out of the initial route bundle and isolate stable vendors.
export default defineConfig({
  plugins: [react()],
  build: {
    chunkSizeWarningLimit: 600,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: 'pdfjs', test: /node_modules[\\/]pdfjs-dist[\\/]/, priority: 100 },
            { name: 'pdf-viewer', test: /node_modules[\\/]react-pdf[\\/]/, priority: 95 },
            { name: 'react-dom', test: /node_modules[\\/]react-dom[\\/]/, priority: 90 },
            { name: 'react', test: /node_modules[\\/]react[\\/]/, priority: 85 },
            { name: 'state', test: /node_modules[\\/]zustand[\\/]/, priority: 80 },
            { name: 'vendor', test: /node_modules[\\/]/, priority: 10 },
          ],
        },
      },
    },
  },
})
