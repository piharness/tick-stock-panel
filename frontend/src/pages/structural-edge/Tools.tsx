/**
 * 「工具」章 —— 回放台 + 维护四节。
 *
 * 为什么回放放在工具章而不是证据章：
 *   证据是「结论与验收」，回放是「把某一天放回去看」的工具。工具可以反复用，
 *   结论只在重跑实验时变 —— 两者的生命周期不同，混在一起会让「读到旧数」这件事
 *   分不清是结论过期还是工具没更新。
 *
 * 旧锚点 #replay 仍然可用：下面留了一个空锚点，指向回放台第一块。
 */
import { MaintSections } from './Maintenance'
import { ReplayPanel } from './Replay'
import { TabLayout } from './Shell'

export function ToolsPanel({ active }: { active: boolean }) {
  return (
    <TabLayout
      tab="tools"
      items={[
        { id: 'replay-day', no: '01', label: '单日回放' },
        { id: 'replay-track', no: '02', label: '信号轨迹' },
        { id: 'replay-outcome', no: '03', label: '买点效果' },
        { id: 'maint', no: '04', label: '更新节奏' },
        { id: 'flow', no: '05', label: '数据链路' },
        { id: 'docs', no: '06', label: '文档地图' },
        { id: 'discipline', no: '07', label: '改数规则' },
      ]}
    >
      <span id="replay" className="block scroll-mt-28" />
      <ReplayPanel active={active} />
      <MaintSections />
    </TabLayout>
  )
}
