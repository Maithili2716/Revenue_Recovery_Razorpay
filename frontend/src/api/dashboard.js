import { api } from './client'

export const getSummary = () => api('/dashboard/summary')
export const getCases = () => api('/recovery/cases')
export const getLatestEvaluation = () => api('/evaluation/latest')
