<template>
  <section class="page" data-module="crowd_select">
    <header class="page-head">
      <div>
        <h2>人群圈选管理</h2>
        <p class="page-desc">维护基于标签组合的人群圈选规则与动态刷新管理，为人群圈选提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记圈选条件</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出人群圈选清单</button>
        <button class="btn" data-testid="compare-versions" type="button" :disabled="comparing" @click="compareVersions">
          {{ comparing ? '核对中…' : '核对新旧名单' }}
        </button>
      </div>
    </header>

    <div v-if="compare" class="compare-panel" data-testid="version-compare">
      <h3>新旧名单核对 · {{ compare.condition_id }}</h3>
      <table class="data-table">
        <thead>
          <tr><th>口径</th><th>结果版本</th><th>值类型</th><th>命中人数</th><th>覆盖会员</th><th>任务批次</th></tr>
        </thead>
        <tbody>
          <tr v-for="side in compareRows" :key="side.label" :data-testid="`compare-${side.key}`">
            <td>{{ side.label }}</td>
            <td>v{{ side.row.result_version }}</td>
            <td>{{ side.row.value_type }}</td>
            <td>{{ side.row.matched_count }}</td>
            <td>{{ side.row.member_count }}</td>
            <td>{{ side.batches }}</td>
          </tr>
        </tbody>
      </table>
      <p data-testid="compare-diff">
        仅旧名单：{{ compare.only_in_v1.length }} 人；仅新名单：{{ compare.only_in_v2.length }} 人；
        名单{{ compare.same_users ? '一致' : '不一致' }}。
      </p>
      <p v-if="!compare.version_consistency.v2_all_v2 || !compare.version_consistency.v1_all_v1" class="error-text" data-testid="compare-mixed">
        同一次圈选混用了两个结果版本，请检查批次锁定。
      </p>
      <p
        v-for="check in compare.hit_only_checks"
        :key="check.batch_id"
        :class="check.ok ? '' : 'error-text'"
        :data-testid="`hit-only-${check.batch_id}`"
      >
        批次 {{ check.batch_id }}（{{ check.coverage }}）：未命中 {{ check.miss_count }} 条，
        {{ check.ok ? '没有凭空出现未命中记录' : '出现了不该存在的未命中记录' }}
      </p>
    </div>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无人群圈选数据，可先登记圈选条件</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记圈选条件</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条人群圈选记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/crowd_select'
const columns = ["条件编号", "条件名称", "标签组合", "逻辑表达式", "预计规模", "刷新频率", "输出集合", "条件版本"]
const actions = ["预估规模", "标记生效", "标记过期"]
const statuses = ["草稿", "预估中", "已生效", "已过期"]
const metricNames = ["圈选条件总数", "预估中条件", "已生效条件"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const comparing = ref(false)
const compare = ref<any>(null)
const compareRows = computed(() => {
  if (!compare.value) return []
  return [
    { key: 'v1', label: '旧名单回放', row: compare.value.v1, batches: batchSummary(compare.value.v1) },
    { key: 'v2', label: '新版圈选', row: compare.value.v2, batches: batchSummary(compare.value.v2) },
  ]
})
function batchSummary(row: any) {
  return Object.entries(row.batches ?? {})
    .map(([tag, batch]: [string, any]) => `${tag}:${batch.batch_id ?? `seq-${batch.sequence}`}`)
    .join('，')
}
async function compareVersions() {
  const row = rows.value[0]
  if (!row) return
  comparing.value = true
  errorMessage.value = ''
  try {
    const conditionId = String(row.condition_id ?? row['条件编号'] ?? '')
    const response = await request(`${ENDPOINT}/${conditionId}/compare`)
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '新旧名单核对失败')
    compare.value = payload
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '新旧名单核对失败'
  } finally {
    comparing.value = false
  }
}
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'crowd_select.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('人群圈选动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '人群圈选操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('圈选条件列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '人群圈选列表读取失败'
  }
}

onMounted(reload)
</script>
