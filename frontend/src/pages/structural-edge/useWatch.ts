/**
 * 手册两页共用的活数据 hook。
 *
 * 2026-11-24 手册拆成两页后，两页都要读同一批接口：
 *   机会雷达（/handbook）＝ 扫描 + 账户 + 现金腿 + 反例登记；
 *   策略图书馆（/library）＝ 扫描（广度读数/边界读数）+ 账户（V1 判据）+ 反例（证据 04）。
 * 把 useEffect 取数收进一处，两页不会再各写一份「alive 标记 + setErr」，
   也不会出现一边改了重试策略另一边没跟上的漂移。
 *
 * confirmDays 由调用方持有：图书馆的证据 04 有切换控件，雷达固定用默认 10 日。
 */
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type EtfWatchAccount, type EtfWatchCashLeg, type EtfWatchPayload } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export function useWatchData(opts?: { cashLeg?: boolean; confirmDays?: number }) {
  const confirmDays = opts?.confirmDays ?? 10
  const [payload, setPayload] = useState<EtfWatchPayload | null>(null)
  const [err, setErr] = useState('')
  const [account, setAccount] = useState<EtfWatchAccount | null>(null)
  const [accountErr, setAccountErr] = useState('')
  const [cashLeg, setCashLeg] = useState<EtfWatchCashLeg | null>(null)
  const [cashErr, setCashErr] = useState('')

  useEffect(() => {
    let alive = true
    api.etfWatch()
      .then(d => { if (alive) { setPayload(d); setErr('') } })
      .catch((e: Error) => { if (alive) setErr(e.message) })
    api.etfWatchAccount?.()
      .then(d => { if (alive) { setAccount(d); setAccountErr('') } })
      .catch((e: Error) => { if (alive) setAccountErr(e.message) })
    // 现金腿与账户指令同源（同一次 analysis 运行产出），任何一边失败都不影响另一边渲染。
    if (opts?.cashLeg) {
      const legReq = api.etfWatchCashLeg
      if (legReq) {
        legReq()
          .then(d => { if (alive) { setCashLeg(d); setCashErr('') } })
          .catch((e: Error) => { if (alive) setCashErr(e.message) })
      }
    }
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const art = useQuery({
    queryKey: QK.positionArt(confirmDays),
    queryFn: () => api.positionArt(confirmDays),
    staleTime: 5 * 60 * 1000,
    retry: false,
  })

  return { payload, err, account, accountErr, cashLeg, cashErr, art }
}
