const apiBase = import.meta.env.VITE_API_BASE_URL ?? ''

export async function api(path, options) {
  const response = await fetch(`${apiBase}${path}`, options)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const error = new Error(body.detail || `Request failed (${response.status})`)
    error.status = response.status
    throw error
  }
  return response.json()
}
