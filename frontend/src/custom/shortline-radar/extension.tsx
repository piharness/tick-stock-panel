import { lazy } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import type { FrontendExtension } from '@/extensions/types'
import { IconStockFocus } from '@/components/BrandIcons'
import { SHORTLINE_RADAR_PAGE_TITLE } from './meta'

export const ShortlineRadar = lazy(() => import('./Page').then(m => ({ default: m.ShortlineRadar })))

/**
 * 旧路径 `/citizenship`（页面曾叫「市场公民」）→ `/shortline-radar`。
 * 保留 query 与 hash：`?as_of=`、后续要加的 `?tag=` 与页内锚点都不丢；replace 不新增历史。
 */
function LegacyCitizenshipRedirect() {
  const { search, hash } = useLocation()
  return <Navigate to={{ pathname: '/shortline-radar', search, hash }} replace />
}

const extension: FrontendExtension = {
  id: 'official.shortline-radar',
  apiVersion: 1,
  routes: [
    { id: 'shortline-radar', path: '/shortline-radar', component: ShortlineRadar },
    { id: 'shortline-radar-legacy', path: '/citizenship', component: LegacyCitizenshipRedirect },
  ],
  navigation: [
    { id: 'shortline-radar', routeId: 'shortline-radar', label: SHORTLINE_RADAR_PAGE_TITLE, icon: IconStockFocus },
  ],
}

export default extension
