import { api } from './client'

export const createTestPayment = (amount_minor, currency) => api('/demo/test-payment', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ amount_minor, currency }),
})

export const getDemo = (demoId) => api(`/demo/${demoId}`)
