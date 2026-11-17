import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconStockFocus } from '@/components/BrandIcons'

export const Citizenship = lazy(() => import('./Page').then(m => ({ default: m.Citizenship })))

const extension: FrontendExtension = {
  id: 'official.citizenship',
  apiVersion: 1,
  routes: [
    { id: 'citizenship', path: '/citizenship', component: Citizenship },
  ],
  navigation: [
    { id: 'citizenship', routeId: 'citizenship', label: '市场公民', icon: IconStockFocus },
  ],
}

export default extension
