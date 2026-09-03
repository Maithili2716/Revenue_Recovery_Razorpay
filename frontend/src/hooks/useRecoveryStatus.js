import { useEffect, useState } from 'react'
import { getDemo } from '../api/demo'

export function useRecoveryStatus(demoId, onError) {
  const [snapshot, setSnapshot] = useState(null)

  useEffect(() => {
    if (!demoId) {
      setSnapshot(null)
      return undefined
    }
    let active = true
    const refresh = async () => {
      try {
        const next = await getDemo(demoId)
        if (active) setSnapshot(next)
      } catch (error) {
        if (active) onError?.(error.message)
      }
    }
    refresh()
    const id = window.setInterval(refresh, 1000)
    return () => { active = false; window.clearInterval(id) }
  }, [demoId, onError])

  return snapshot
}
