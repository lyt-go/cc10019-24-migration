<template>
  <section class="page" data-module="tag_compute">
    <header class="page-head">
      <div><h2>标签计算</h2><p class="page-desc">维护标签批量计算的任务编排、分区依赖与执行追踪，为标签计算提供可验证的数据工程流程。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记计算任务</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出作业清单</button>
        <button class="btn" type="button" data-testid="migrate" :disabled="migrating" @click="runMigration">
          {{ migrating ? '迁移中…' : '兼容迁移旧结果' }}
        </button>
      </div>
    </header>
    <div v-if="migration" class="migration-panel" data-testid="migration-panel">
      <strong>第 2 版结果</strong>
      <span data-testid="migration-state">迁移状态：{{ migration.state }}</span>
      <span>已完成批次：{{ migration.completed_batches.length }}</span>
      <span data-testid="migration-pending">待迁移批次：{{ migration.pending_batches.length }}</span>
      <span>迁移问题：{{ migration.issues.length }}</span>
      <div v-for="batch in v2Batches" :key="batch.batch_id" class="migration-batch" :data-testid="`v2-${batch.batch_id}`">
        {{ batch.batch_id }} · 值类型 {{ batch.value_type }} · 覆盖
        {{ batch.coverage === 'explicit' ? '全体会员' : batch.coverage === 'hit_only' ? '仅命中者' : '未知' }} ·
        命中 {{ batch.hit_count }} / 未命中 {{ batch.miss_count }}
        <span v-if="!batch.miss_covered">（无未命中记录）</span>
      </div>
    </div>
    <div class="filter-bar">
      <label><span>开始日期</span><input type="date" v-model="dateFrom" /></label>
      <label><span>结束日期</span><input type="date" v-model="dateTo" /></label>
      <button class="btn" type="button" @click="reload">查询</button>
    </div>
    <div class="timeline-wrap">
      <div v-for="item in timelineItems" :key="item.id" class="timeline-row">
        <span class="timeline-label">{{ item.name || item.label }}</span>
        <div class="timeline-track">
          <div class="timeline-bar" :class="`status-${item.status}`" :style="{ left: barLeft(item) + '%', width: barWidth(item) + '%' }" :data-testid="`bar-${item.id}`" :title="`${item.start} ~ ${item.end}`" @click="selectItem(item)">&nbsp;</div>
        </div>
        <span style="font-size:12px;color:var(--muted);margin-left:8px;">{{ item.start }} ~ {{ item.end }}</span>
      </div>
      <p v-if="!timelineItems.length" style="text-align:center;color:var(--muted)">暂无作业记录</p>
    </div>
    <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
      <strong>{{ selectedItem.name || selectedItem.label }}</strong> — 状态：{{ selectedItem.status }}
      <div style="margin-top:4px;">{{ selectedItem.logs || '暂无日志' }}</div>
    </div>
    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记计算任务</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/tag_compute'
const MIGRATION_ENDPOINT = '/api/tag-compute/migration'
const V2_ENDPOINT = '/api/tag-compute/results/v2'
const columns = ["任务编号", "关联标签", "计算范围", "计算引擎", "输入分区", "输出分区", "并行度", "执行状态"]
const actions = ["提交执行", "停止计算", "重算任务"]
const timelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref(''); const dateTo = ref('')
const errorMessage = ref(''); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const allItems = ref<any[]>([])
const migration = ref<any>(null)
const v2Batches = ref<any[]>([])
const migrating = ref(false)
async function loadResults() {
  try {
    const [migrationRes, batchRes] = await Promise.all([request(MIGRATION_ENDPOINT), request(V2_ENDPOINT)])
    migration.value = await migrationRes.json()
    v2Batches.value = (await batchRes.json()).items || []
  } catch(e) { /* 结果面板失败不影响作业清单 */ }
}
async function runMigration() {
  migrating.value = true
  try { await request(`${MIGRATION_ENDPOINT}/run`, { method: 'POST' }) } catch(e) { errorMessage.value = '兼容迁移失败' }
  migrating.value = false
  await loadResults()
}
function barLeft(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 0; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return ((new Date(item.start).getTime() - min) / range) * 100 }
function barWidth(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 20; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100) }
function selectItem(item: any) { selectedItem.value = selectedItem.value?.id === item.id ? null : item }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); allItems.value = data.items || []; applyFilter() } catch(e) { errorMessage.value = '加载失败' }
}
function applyFilter() {
  let items = allItems.value
  if(dateFrom.value) items = items.filter((i: any) => i.start >= dateFrom.value)
  if(dateTo.value) items = items.filter((i: any) => i.end <= dateTo.value)
  timelineItems.value = items; selectedItem.value = null
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
onMounted(() => { reload(); loadResults() })
</script>
