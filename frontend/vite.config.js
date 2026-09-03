import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  server: {
    proxy: {
      '/demo': 'http://localhost:8000',
      '/dashboard': 'http://localhost:8000',
      '/recovery/cases': 'http://localhost:8000',
      '/evaluation': 'http://localhost:8000',
    },
  },
  plugins: [
    react(),
    {
      name: 'demo-return-post-redirect',
      configureServer(server) {
        server.middlewares.use('/recovery/demo-return', (request, response, next) => {
          if (request.method !== 'POST') {
            next()
            return
          }
          response.statusCode = 303
          response.setHeader('Location', '/recovery/demo-return')
          response.end()
        })
      },
    },
  ],
})
