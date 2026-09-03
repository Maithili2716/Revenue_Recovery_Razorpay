import { api } from './client'

export const getLatestEvaluation = () => api('/evaluation/latest')
export const runEvaluation = () => api('/evaluation/run', { method: 'POST' })
