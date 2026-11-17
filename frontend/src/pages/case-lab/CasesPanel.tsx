/**
 * 案例库 —— 把「人写的外部信息」和「机器算的痕迹」放在同一条时间轴上对账。
 *
 * 这里唯一不可替代的功能是 `reconcile`：
 *   每条证据（处罚文书 / 爆料 / 媒体报道）都去找它日期附近有没有对应痕迹。
 *   traced   = 那天前后确实出现了结构、上榜或大幅波动；
 *   untraced = 本地数据看不到。**看不到不等于叙事为假** —— 公告、大宗交易、
 *              股东名册都不在这个仓库里，缺数据是边界，不是判决。
 *
 * 所以文案里不许写「与事实不符」，只能写「本地痕迹未支持」。
 */
import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, CircleDashed, Loader2, Pencil, Plus, Trash2, X } from 'lucide-react'
import { api, type CaseDoc, type CaseEvidence, type ReconcileItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { Modal } from '@/components/Modal'
import {
  ACTOR_META, KIND_META, RELIABILITY_META, STATUS_META,
  type ActorType, type CaseStatus, type EvidenceKind, type Reliability,
} from './calibers'

const EMPTY: Partial<CaseDoc> = {
  symbol: '', name: '', title: '', actor_type: 'unknown', status: 'trace_only',
  start: '', end: '', thesis: '', evidence: [], phases: [], playbook: [], lessons: '',
}

const emptyEvidence = (): CaseEvidence => ({
  date: '', kind: 'media', source: '', text: '', reliability: 'media', url: '',
})

export function CasesPanel({
  draft, onDraftConsumed,
}: {
  draft: Partial<CaseDoc> | null
  onDraftConsumed: () => void
}) {
  const qc = useQueryClient()
  const list = useQuery({
    queryKey: QK.caseLabCases(),
    queryFn: () => api.caseLabCases(),
    staleTime: 30_000,
  })
  // 选中的案例进 URL：刷新不丢、可直接把「某份档案」发给别人
  const [params, setParams] = useSearchParams()
  const selected = params.get('case')
  const setSelected = (id: string | null) => {
    const next = new URLSearchParams(params)
    if (id) next.set('case', id)
    else next.delete('case')
    setParams(next, { replace: true })
  }
  const [editing, setEditing] = useState<Partial<CaseDoc> | null>(null)

  // 从「解剖」页带过来的草稿：直接进编辑态，用户补完叙事再保存
  useEffect(() => {
    if (draft) {
      setEditing({ ...EMPTY, ...draft })
      onDraftConsumed()
    }
  }, [draft, onDraftConsumed])

  const items = list.data?.items ?? []
  const detail = useQuery({
    queryKey: QK.caseLabCase(selected ?? ''),
    queryFn: () => api.caseLabCase(selected as string),
    enabled: !!selected,
  })
  const recon = useQuery({
    queryKey: QK.caseLabReconcile(selected ?? '', 3),
    queryFn: () => api.caseLabReconcile(selected as string, 3),
    enabled: !!selected && !!detail.data,
    staleTime: 5 * 60_000,
  })

  const save = useMutation({
    mutationFn: (payload: Partial<CaseDoc>) => api.caseLabSaveCase(payload),
    onSuccess: doc => {
      setEditing(null)
      setSelected(doc.id)
      void qc.invalidateQueries({ queryKey: QK.caseLabCases() })
      void qc.invalidateQueries({ queryKey: QK.caseLabCase(doc.id) })
    },
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.caseLabDeleteCase(id),
    onSuccess: () => {
      setSelected(null)
      void qc.invalidateQueries({ queryKey: QK.caseLabCases() })
    },
  })

  return (
    <div className="flex min-h-[60vh] gap-3 px-5 py-3">
      <aside className="w-72 shrink-0">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[11px] text-muted">共 {items.length} 个案例</span>
          <button
            onClick={() => setEditing({ ...EMPTY })}
            className="flex items-center gap-1 rounded-btn border border-accent/40 bg-accent/10 px-2 py-1 text-[11px] text-accent hover:bg-accent/15"
          >
            <Plus className="h-3 w-3" /> 新建
          </button>
        </div>
        <div className="max-h-[70vh] space-y-1 overflow-y-auto">
          {list.isLoading && <div className="py-6 text-[11px] text-muted">读取中…</div>}
          {!list.isLoading && items.length === 0 && (
            <div className="rounded-card border border-dashed border-border p-3 text-[11px] leading-relaxed text-muted">
              还没有案例。最快的一条路：在「结构异常」里点一只票 → 解剖 → 存为案例，
              然后补上处罚文书或爆料的日期与出处。
            </div>
          )}
          {items.map(c => {
            const st = STATUS_META[c.status as CaseStatus] ?? STATUS_META.trace_only
            return (
              <button
                key={c.id}
                onClick={() => setSelected(c.id)}
                className={cn(
                  'w-full rounded-card border px-2.5 py-2 text-left transition-colors',
                  selected === c.id
                    ? 'border-accent/40 bg-accent/5'
                    : 'border-border bg-surface hover:bg-elevated/60',
                )}
              >
                <div className="flex items-center justify-between gap-1">
                  <span className="truncate text-[11px] text-foreground">{c.title || c.symbol}</span>
                  <span className={cn('shrink-0 rounded border px-1 text-[9px]', st.cls)}>{st.label}</span>
                </div>
                <div className="mt-0.5 flex items-center gap-2 font-mono text-[9px] text-muted">
                  <span>{c.symbol}</span>
                  <span>{c.start ?? '—'} → {c.end ?? '—'}</span>
                  <span className="ml-auto">{c.evidence_count} 条证据</span>
                </div>
              </button>
            )
          })}
        </div>
      </aside>

      <section className="min-w-0 flex-1">
        {!selected && (
          <div className="rounded-card border border-border bg-surface p-4 text-[11px] leading-relaxed text-muted">
            选一个案例看时间轴与对账。案例的写法没有标准答案，但有一条底线：
            <b className="text-secondary">外部信息必须带日期与出处</b>，否则它无法与痕迹对齐，
            也就无法被检验。
          </div>
        )}
        {selected && detail.isLoading && (
          <div className="flex items-center gap-2 py-10 text-xs text-muted">
            <Loader2 className="h-4 w-4 animate-spin" /> 读取案例…
          </div>
        )}
        {detail.data && (
          <CaseDetail
            doc={detail.data}
            recon={recon.data?.items ?? []}
            reconNote={recon.data?.note}
            reconLoading={recon.isLoading}
            onEdit={() => setEditing({ ...detail.data })}
            onDelete={() => {
              if (confirm(`删除案例「${detail.data?.title || detail.data?.symbol}」？`)) {
                remove.mutate(detail.data!.id)
              }
            }}
          />
        )}
      </section>

      {editing && (
        <CaseEditor
          value={editing}
          saving={save.isPending}
          onChange={setEditing}
          onCancel={() => setEditing(null)}
          onSave={() => save.mutate(editing)}
          error={save.isError ? (save.error as Error).message : null}
        />
      )}
    </div>
  )
}

// ===== 详情 =====

function CaseDetail({
  doc, recon, reconNote, reconLoading, onEdit, onDelete,
}: {
  doc: CaseDoc
  recon: ReconcileItem[]
  reconNote?: string
  reconLoading: boolean
  onEdit: () => void
  onDelete: () => void
}) {
  const st = STATUS_META[doc.status as CaseStatus] ?? STATUS_META.trace_only
  const actor = ACTOR_META[doc.actor_type as ActorType] ?? ACTOR_META.unknown
  const reconById = useMemo(() => new Map(recon.map(r => [r.evidence.id ?? r.evidence.date ?? '', r])), [recon])

  return (
    <div className="space-y-3">
      <div className="rounded-card border border-border bg-surface p-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-[14px] font-medium text-foreground">{doc.title || doc.symbol}</h2>
              <span className={cn('rounded border px-1.5 py-0.5 text-[9px]', st.cls)}>{st.label}</span>
              <span className="rounded border border-border bg-elevated px-1.5 py-0.5 text-[9px] text-secondary">
                {actor.label}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 font-mono text-[10px] text-muted">
              <span>{doc.symbol}</span>
              <span>{doc.name ?? ''}</span>
              <span>{doc.start ?? '—'} → {doc.end ?? '—'}</span>
              {doc.updated_at && <span>更新 {doc.updated_at.replace('T', ' ')}</span>}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <button onClick={onEdit} className="rounded-btn border border-border bg-surface p-1.5 text-secondary hover:bg-elevated hover:text-foreground">
              <Pencil className="h-3.5 w-3.5" />
            </button>
            <button onClick={onDelete} className="rounded-btn border border-border bg-surface p-1.5 text-secondary hover:bg-danger/10 hover:text-danger">
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
        {doc.thesis && <p className="mt-2 text-[11px] leading-relaxed text-secondary">{doc.thesis}</p>}
        {doc.playbook.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1">
            {doc.playbook.map(p => (
              <span key={p} className="rounded border border-border bg-elevated px-1.5 py-0.5 font-mono text-[9px] text-secondary">
                {p}
              </span>
            ))}
          </div>
        )}
      </div>

      {doc.phases.length > 0 && (
        <div className="rounded-card border border-border bg-surface p-3">
          <h3 className="mb-1.5 text-[11px] font-medium text-foreground">阶段（人工判断）</h3>
          <div className="space-y-1">
            {doc.phases.map((p, i) => (
              <div key={i} className="flex items-start gap-2 text-[10px]">
                <span className="w-16 shrink-0 font-medium text-accent">{p.label}</span>
                <span className="w-40 shrink-0 font-mono text-muted">{p.start ?? '—'} → {p.end ?? '—'}</span>
                <span className="text-secondary">{p.note}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="rounded-card border border-border bg-surface p-3">
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-[11px] font-medium text-foreground">
            证据与痕迹对账
            <span className="ml-2 font-mono text-[9px] text-muted">
              {recon.length > 0 && `${recon.filter(r => r.support === 'traced').length}/${recon.length} 有本地痕迹`}
            </span>
          </h3>
          {reconLoading && <Loader2 className="h-3 w-3 animate-spin text-muted" />}
        </div>
        {doc.evidence.length === 0 && (
          <div className="text-[10px] text-muted">还没有证据。编辑案例，把处罚文书、爆料或报道的日期与原文粘进来。</div>
        )}
        <div className="space-y-2">
          {doc.evidence.map((ev, i) => {
            const key = ev.id ?? ev.date ?? `idx-${i}`
            const r = reconById.get(key) ?? reconById.get(ev.date ?? '')
            return <EvidenceRow key={key} ev={ev} item={r} />
          })}
        </div>
        {reconNote && (
          <p className="mt-2 border-t border-border/60 pt-2 text-[9px] leading-relaxed text-muted">{reconNote}</p>
        )}
      </div>

      {doc.lessons && (
        <div className="rounded-card border border-border bg-surface p-3">
          <h3 className="mb-1 text-[11px] font-medium text-foreground">可迁移的判断</h3>
          <p className="whitespace-pre-wrap text-[11px] leading-relaxed text-secondary">{doc.lessons}</p>
        </div>
      )}
    </div>
  )
}

function EvidenceRow({ ev, item }: { ev: CaseEvidence; item?: ReconcileItem }) {
  const kind = KIND_META[ev.kind as EvidenceKind] ?? KIND_META.other
  const rel = RELIABILITY_META[ev.reliability as Reliability] ?? RELIABILITY_META.media
  const traced = item?.support === 'traced'
  return (
    <div className="rounded border border-border/70 bg-elevated/30 p-2">
      <div className="flex flex-wrap items-center gap-2 text-[9px]">
        <span className="font-mono text-secondary">{ev.date ?? '无日期'}</span>
        <span className="rounded border border-border bg-surface px-1 py-0.5 text-secondary">{kind.label}</span>
        <span className={cn('rounded border px-1 py-0.5', rel.cls)} title={rel.hint}>{rel.label}</span>
        <span className="truncate text-muted">{ev.source}</span>
        <span className="ml-auto flex items-center gap-1">
          {item?.support === 'undated' && <span className="text-muted">未对账</span>}
          {item && item.support !== 'undated' && (
            <>
              {traced
                ? <CheckCircle2 className="h-3 w-3 text-accent" />
                : <CircleDashed className="h-3 w-3 text-muted" />}
              <span className={traced ? 'text-accent' : 'text-muted'}>
                {traced ? '本地有痕迹' : '本地未见痕迹'}
              </span>
            </>
          )}
        </span>
      </div>
      {ev.text && <p className="mt-1 whitespace-pre-wrap text-[10px] leading-relaxed text-secondary">{ev.text}</p>}
      {item?.traces && item.traces.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1">
          {item.traces.map((t, i) => (
            <span key={i} className="rounded border border-accent/25 bg-accent/5 px-1 py-0.5 font-mono text-[9px] text-accent">
              {t.date} · {t.labels.join(' / ')}
            </span>
          ))}
        </div>
      )}
      {ev.url && (
        <a href={ev.url} target="_blank" rel="noreferrer" className="mt-1 inline-block truncate text-[9px] text-accent hover:underline">
          {ev.url}
        </a>
      )}
    </div>
  )
}

// ===== 编辑器 =====

function CaseEditor({
  value, onChange, onCancel, onSave, saving, error,
}: {
  value: Partial<CaseDoc>
  onChange: (v: Partial<CaseDoc>) => void
  onCancel: () => void
  onSave: () => void
  saving: boolean
  error: string | null
}) {
  const fps = useQuery({
    queryKey: QK.caseLabFingerprints(),
    queryFn: () => api.caseLabFingerprints(),
    staleTime: Infinity,
  })
  const set = <K extends keyof CaseDoc>(k: K, v: CaseDoc[K]) => onChange({ ...value, [k]: v })
  const evidence = value.evidence ?? []

  const updateEv = (i: number, patch: Partial<CaseEvidence>) => {
    const next = evidence.map((e, j) => (j === i ? { ...e, ...patch } : e))
    set('evidence', next)
  }

  return (
    <Modal
      onClose={onCancel}
      labelledBy="case-editor-title"
      panelClassName="w-[94vw] max-w-4xl max-h-[92vh] overflow-y-auto bg-surface border border-border rounded-card shadow-xl"
    >
      <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-surface px-4 py-2">
        <h2 id="case-editor-title" className="text-sm font-medium text-foreground">
          {value.id ? '编辑案例' : '新建案例'}
        </h2>
        <div className="flex items-center gap-2">
          <button onClick={onCancel} className="rounded-btn border border-border px-2.5 py-1 text-[11px] text-secondary hover:bg-elevated">
            取消
          </button>
          <button
            onClick={onSave}
            disabled={saving || !value.symbol}
            className="rounded-btn border border-accent/40 bg-accent/10 px-2.5 py-1 text-[11px] text-accent hover:bg-accent/15 disabled:opacity-40"
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>

      <div className="space-y-3 px-4 py-3">
        {error && <div className="rounded border border-danger/40 bg-danger/10 px-2 py-1 text-[11px] text-danger">{error}</div>}

        <div className="grid gap-2 sm:grid-cols-2">
          <Field label="标的代码（必填）">
            <input className={INPUT} value={value.symbol ?? ''} onChange={e => set('symbol', e.target.value)} placeholder="002086.SZ" />
          </Field>
          <Field label="名称">
            <input className={INPUT} value={value.name ?? ''} onChange={e => set('name', e.target.value)} />
          </Field>
          <Field label="标题">
            <input className={INPUT} value={value.title ?? ''} onChange={e => set('title', e.target.value)} placeholder="一句话说清这段介入是什么" />
          </Field>
          <Field label="区间">
            <div className="flex items-center gap-1">
              <input type="date" className={INPUT} value={value.start ?? ''} onChange={e => set('start', e.target.value)} />
              <span className="text-muted">→</span>
              <input type="date" className={INPUT} value={value.end ?? ''} onChange={e => set('end', e.target.value)} />
            </div>
          </Field>
          <Field label="主体类型">
            <select className={INPUT} value={value.actor_type ?? 'unknown'} onChange={e => set('actor_type', e.target.value)}>
              {(Object.keys(ACTOR_META) as ActorType[]).map(k => (
                <option key={k} value={k}>{ACTOR_META[k].label}</option>
              ))}
            </select>
          </Field>
          <Field label="结论状态">
            <select className={INPUT} value={value.status ?? 'trace_only'} onChange={e => set('status', e.target.value)}>
              {(Object.keys(STATUS_META) as CaseStatus[]).map(k => (
                <option key={k} value={k}>{STATUS_META[k].label}</option>
              ))}
            </select>
          </Field>
        </div>

        <Field label="一句话论点（你认为这段发生了什么）">
          <textarea className={cn(INPUT, 'h-16 resize-y')} value={value.thesis ?? ''} onChange={e => set('thesis', e.target.value)} />
        </Field>

        <div>
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[10px] text-muted">证据（外部信息，必须带日期与出处）</span>
            <button
              onClick={() => set('evidence', [...evidence, emptyEvidence()])}
              className="flex items-center gap-0.5 rounded border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:bg-elevated"
            >
              <Plus className="h-3 w-3" /> 加一条
            </button>
          </div>
          <div className="space-y-2">
            {evidence.map((ev, i) => (
              <div key={i} className="rounded border border-border bg-elevated/20 p-2">
                <div className="mb-1 grid gap-1 sm:grid-cols-4">
                  <input type="date" className={INPUT} value={ev.date ?? ''} onChange={e => updateEv(i, { date: e.target.value })} />
                  <select className={INPUT} value={ev.kind} onChange={e => updateEv(i, { kind: e.target.value })}>
                    {(Object.keys(KIND_META) as EvidenceKind[]).map(k => (
                      <option key={k} value={k}>{KIND_META[k].label}</option>
                    ))}
                  </select>
                  <select className={INPUT} value={ev.reliability} onChange={e => updateEv(i, { reliability: e.target.value })}>
                    {(Object.keys(RELIABILITY_META) as Reliability[]).map(k => (
                      <option key={k} value={k}>{RELIABILITY_META[k].label}</option>
                    ))}
                  </select>
                  <button
                    onClick={() => set('evidence', evidence.filter((_, j) => j !== i))}
                    className="flex items-center justify-center gap-1 rounded border border-border text-[10px] text-secondary hover:bg-danger/10 hover:text-danger"
                  >
                    <X className="h-3 w-3" /> 删除
                  </button>
                </div>
                <input className={cn(INPUT, 'mb-1 w-full')} placeholder="出处：决定书文号 / 媒体名 / 链接" value={ev.source} onChange={e => updateEv(i, { source: e.target.value })} />
                <textarea className={cn(INPUT, 'h-14 w-full resize-y')} placeholder="原文摘录或要点。不要写摘要式的「据说」——保留可核对的细节。" value={ev.text} onChange={e => updateEv(i, { text: e.target.value })} />
                <input className={cn(INPUT, 'mt-1 w-full')} placeholder="链接（可选）" value={ev.url ?? ''} onChange={e => updateEv(i, { url: e.target.value })} />
              </div>
            ))}
          </div>
        </div>

        <Field label="手法标签（来自判据库，用于和判据统计对齐）">
          <div className="flex flex-wrap gap-1">
            {(fps.data?.items ?? []).map(fp => {
              const on = (value.playbook ?? []).includes(fp.id)
              return (
                <button
                  key={fp.id}
                  onClick={() => {
                    const cur = value.playbook ?? []
                    set('playbook', on ? cur.filter(x => x !== fp.id) : [...cur, fp.id])
                  }}
                  className={cn(
                    'rounded border px-1.5 py-0.5 text-[10px]',
                    on ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border bg-surface text-secondary hover:bg-elevated',
                  )}
                >
                  {fp.label}
                </button>
              )
            })}
          </div>
        </Field>

        <Field label="可迁移的判断（下次看到什么，应该警惕什么）">
          <textarea className={cn(INPUT, 'h-20 resize-y')} value={value.lessons ?? ''} onChange={e => set('lessons', e.target.value)} />
        </Field>
      </div>
    </Modal>
  )
}

const INPUT = 'rounded border border-border bg-base px-2 py-1 font-mono text-[11px] text-foreground outline-none focus:border-accent/50'

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-0.5">
      <span className="text-[10px] text-muted">{label}</span>
      {children}
    </label>
  )
}
