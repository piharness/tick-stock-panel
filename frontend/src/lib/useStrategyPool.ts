import { useState, useCallback, useEffect, useRef } from 'react'
import { storage } from '@/lib/storage'
import { api } from '@/lib/api'

// 旧版按周期隔离的双池 ('strategy-pool' 日线 + 'strategy-pool-1m' 分钟) 已合并为
// 统一池 ('strategy-pool'): 策略自带周期声明, 执行时按各自 timeframes 路由,
// 池不再按周期隔离。此处做一次性迁移: 日线在前、分钟在后、按 ID 去重,
// 完成后移除旧分钟 key 保证幂等 (StrictMode 双调用 / HMR 重放均安全)。
function loadUnifiedPool(): string[] {
  const minute = storage.strategyPoolMinute.get([])
  const daily = storage.strategyPool.get([])
  if (minute.length === 0) {
    storage.strategyPoolMinute.remove()
    return daily
  }
  const merged = [...daily]
  for (const id of minute) {
    if (!merged.includes(id)) merged.push(id)
  }
  storage.strategyPool.set(merged)
  storage.strategyPoolMinute.remove()
  return merged
}

/**
 * 策略池 — 日线与分钟策略共用的统一池。
 *
 * 后端持久化 (data/user_data/strategy_pool.json) 为准, localStorage 作离线兜底:
 * - 首屏用 localStorage 同步初始化 (快, 无空白), 随后异步 GET 后端覆盖 (后端为准)。
 * - 每次变更同步更新本地 state + 异步 PUT 后端; PUT 失败静默 (下次 GET 或重启自愈)。
 * - 后端不可用时回退 localStorage, 功能不中断。
 */
export function useStrategyPool() {
  const [pool, setPool] = useState<string[]>(loadUnifiedPool)
  // 后端加载是否已完成 (首屏后端覆盖前为 false; 用于调用方区分「还没同步」与「就是空」)
  const [synced, setSynced] = useState(false)
  // 防止 StrictMode 双调用 / 卸载后写回
  const mounted = useRef(true)

  // 启动时从后端拉取 (后端为准), 失败保留 localStorage 值
  useEffect(() => {
    mounted.current = true
    api.strategyPoolGet()
      .then(res => {
        if (!mounted.current) return
        const ids = Array.isArray(res.ids) ? res.ids.filter((x): x is string => typeof x === 'string') : []
        setPool(ids)
        storage.strategyPool.set(ids)  // 同步回 localStorage, 保持离线兜底最新
        setSynced(true)
      })
      .catch(() => { /* 后端不可用: 保留 localStorage 值, 功能不中断 */ })
    return () => { mounted.current = false }
  }, [])

  // 每次变更: 同步 localStorage (离线兜底) + 异步 PUT 后端 (后端为准)
  const persist = useCallback((next: string[]) => {
    storage.strategyPool.set(next)
    void api.strategyPoolPut(next).catch(() => { /* 静默, 下次 GET 自愈 */ })
  }, [])

  const addToPool = useCallback((id: string) => {
    setPool(prev => {
      if (prev.includes(id)) return prev
      const next = [...prev, id]
      persist(next)
      return next
    })
  }, [persist])

  const removeFromPool = useCallback((id: string) => {
    setPool(prev => {
      const next = prev.filter(x => x !== id)
      if (next.length === prev.length) return prev
      persist(next)
      return next
    })
  }, [persist])

  const reorderPool = useCallback((newOrder: string[]) => {
    setPool(newOrder)
    persist(newOrder)
  }, [persist])

  // 清除池中不存在于 validIds 的失效策略(如本地开发残留的自定义策略)。
  // 调用方传入"全周期合并的策略列表" ID, 池内日线/分钟策略一并校验。
  // 仅当确实有失效项时才更新,避免无谓重渲染。
  const prune = useCallback((validIds: Iterable<string>) => {
    const validSet = validIds instanceof Set ? validIds : new Set(validIds)
    setPool(prev => {
      if (prev.length === 0) return prev
      const next = prev.filter(id => validSet.has(id))
      if (next.length === prev.length) return prev
      persist(next)
      return next
    })
  }, [persist])

  const isInPool = useCallback((id: string) => pool.includes(id), [pool])

  return { pool, addToPool, removeFromPool, reorderPool, prune, isInPool, synced }
}
